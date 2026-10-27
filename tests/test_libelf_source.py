# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Verify the Ports libelf preparation contract and its refusal paths."""

import copy
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tarfile
from unittest.mock import patch

from test_core import TemporaryCase, git_environment, hostile_git_environment
from libelf_fixture import RECIPE, recipe_fixture
from emberbsd_sdk import jsonio
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.libelf import (MANIFEST, RECEIPT, SOURCE_HEADER_OVERRIDES,
                                 prepare_source, validate_identity, validate_source)


class LibelfSourceTests(TemporaryCase):
    def setUp(self):
        super().setUp()
        self.ports = self.root / 'ports'; self.ports.mkdir()
        self.archive = self.root / 'elftoolchain.tar.bz2'
        self.output = self.root / 'prepared'
        recipe_fixture(self.ports, self.archive)
        subprocess.run(['git', '-C', str(self.ports), 'init', '-q'], check=True, env=git_environment())
        self.commit()

    def commit(self):
        subprocess.run(['git', '-C', str(self.ports), 'add', '-f', '.'], check=True, env=git_environment())
        subprocess.run(['git', '-C', str(self.ports), '-c', 'user.name=Fixture',
                        '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'Fixture'],
                       check=True, env=git_environment())

    def prepare(self):
        return prepare_source(self.ports, self.archive, self.output)

    def test_prepare_exact_sibling_snapshot_and_validate_offline(self):
        record = self.prepare()
        self.assertEqual(record, validate_source(self.output))
        self.assertEqual(record, validate_source(self.output, ports=self.ports))
        self.assertEqual({path.name for path in self.output.iterdir()},
                         {'common', 'libelf', 'ports.distinfo', RECEIPT})
        self.assertEqual((self.output / 'common/Makefile').read_bytes(), b'common patched\n')
        self.assertEqual((self.output / 'common/sys/generated.m4').read_bytes(),
                         b'new generated source\n')
        self.assertEqual((self.output / 'common/sys/elfconstants.m4').read_bytes(),
                         b"define(`STN_UNDEF', 0)\n")
        self.assertEqual((self.output / 'common/sys/elfdefinitions.m4').read_bytes(),
                         b'elf definitions patched\n')
        self.assertFalse((self.output / 'common/native-elf-format').exists())
        self.assertFalse((self.output / 'common/os.Linux.mk').exists())
        self.assertEqual((self.output / 'libelf/Makefile').read_bytes(), b'libelf ABI2 Makefile\n')
        self.assertEqual((self.output / 'libelf/Version.map').read_bytes(),
                         b'ELF_1.0 { global: elf_getversion; };\n')
        self.assertEqual((self.output / 'ports.distinfo').read_bytes(),
                         (self.ports / RECIPE / 'distinfo').read_bytes())
        self.assertEqual((self.output / RECEIPT).stat().st_mode & 0o777, 0o444)
        with self.assertRaisesRegex(SDKError, 'already exists'):
            self.prepare()

    def selected_patch(self):
        directory = self.root / 'selected tools'; directory.mkdir()
        selected = directory / 'nbpatch'
        # Keep the selected pathname (including spaces) without copying a
        # platform-signed executable, which Darwin may refuse to execute.
        selected.write_text('#!/bin/sh\nexec ' + shlex.quote(shutil.which('patch')) + ' "$@"\n')
        selected.chmod(0o755)
        return selected

    def test_explicit_patch_tool_does_not_search_path_or_change_receipt(self):
        selected = self.selected_patch()
        with patch('emberbsd_sdk.libelf.shutil.which',
                   side_effect=AssertionError('explicit selection searched PATH')):
            record = prepare_source(self.ports, self.archive, self.output,
                                    patch_tool=selected)
        self.assertEqual(record, validate_source(self.output, ports=self.ports))
        self.assertEqual(set(record),
                         {'format', 'version', 'recipe', 'archive', 'files', 'tree_sha256'})
        self.assertNotIn(str(self.root), json.dumps(record))

    def test_explicit_patch_tool_refuses_invalid_paths_without_fallback(self):
        nonexecutable = self.write('not-executable', 'not a tool\n', mode=0o644)
        for selected, error in (
                ('nbpatch', 'absolute executable'),
                (self.root / 'missing-nbpatch', 'invalid libelf patch tool'),
                (nonexecutable, 'not executable'),
                (self.root, 'regular file')):
            with self.subTest(selected=str(selected)):
                with patch('emberbsd_sdk.libelf.shutil.which',
                           side_effect=AssertionError('invalid selection fell back to PATH')):
                    with self.assertRaisesRegex(SDKError, error):
                        prepare_source(self.ports, self.archive, self.output,
                                       patch_tool=selected)
                self.assertFalse(self.output.exists())
                self.assertEqual(list(self.root.glob('.libelf-*')), [])

    def test_cli_explicit_patch_tool_and_refusals(self):
        selected = self.selected_patch()
        script = Path(__file__).resolve().parents[1] / 'scripts/prepare-libelf.py'
        command = [sys.executable, '-B', '-I', '-S', str(script),
                   '--ports', str(self.ports), '--archive', str(self.archive)]
        good = subprocess.run(command + ['--output', str(self.output),
                                         '--patch-tool', str(selected)], capture_output=True)
        self.assertEqual(good.returncode, 0, good.stderr)
        self.assertEqual(json.loads(good.stdout), validate_source(self.output))
        for name, tool, error in (
                ('relative', 'nbpatch', b'absolute executable'),
                ('missing', str(self.root / 'missing-nbpatch'), b'invalid libelf patch tool')):
            output = self.root / name
            result = subprocess.run(command + ['--output', str(output),
                                               '--patch-tool', tool], capture_output=True)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn(error, result.stderr)
            self.assertFalse(output.exists())
        validate = subprocess.run([sys.executable, '-B', '-I', '-S', str(script),
                                   '--validate', str(self.output),
                                   '--patch-tool', str(selected)], capture_output=True)
        self.assertEqual(validate.returncode, 2)
        self.assertIn(b'select --validate', validate.stderr)

    def test_git_archive_and_checkout_preserve_valid_source_receipt(self):
        record = self.prepare()
        repository = self.root / 'bundled-source'
        relative = Path('external/bsd/elftoolchain/libelf-source')
        committed = repository / relative
        committed.parent.mkdir(parents=True)
        shutil.copytree(self.output, committed)
        subprocess.run(['git', 'init', '-q', str(repository)], check=True, env=git_environment())
        subprocess.run(['git', '-C', str(repository), 'add', str(relative)], check=True, env=git_environment())
        subprocess.run(['git', '-C', str(repository), '-c', 'user.name=Fixture',
                        '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'Fixture'],
                       check=True, env=git_environment())
        tree_entry = subprocess.run(
            ['git', '-C', str(repository), 'ls-tree', 'HEAD',
             (relative / RECEIPT).as_posix()], check=True, capture_output=True, env=git_environment()).stdout
        self.assertTrue(tree_entry.startswith(b'100644 blob '))

        archive = self.root / 'bundled-source.tar'
        subprocess.run(['git', '-C', str(repository), 'archive', '--format=tar',
                        '--output=' + str(archive), 'HEAD', relative.as_posix()], check=True, env=git_environment())
        archived = self.root / 'archive-extract'
        archived.mkdir()
        checkout = self.root / 'git-checkout'
        previous_umask = os.umask(0o022)
        try:
            with tarfile.open(archive) as packed:
                packed.extractall(archived, filter='data')
            subprocess.run(['git', 'clone', '-q', '--no-hardlinks', str(repository),
                            str(checkout)], check=True, env=git_environment())
        finally:
            os.umask(previous_umask)

        for root in (archived / relative, checkout / relative):
            self.assertEqual((root / RECEIPT).stat().st_mode & 0o777, 0o644)
            self.assertEqual(validate_source(root), record)
            self.assertEqual(validate_source(root, ports=self.ports), record)

    def test_machine_git_config_cannot_drop_bundled_makefiles(self):
        # A developer's global ignore of Makefile once made git add drop
        # common/Makefile and libelf/Makefile from the bundled fixture.
        with patch.dict(os.environ, hostile_git_environment(self.root / 'machine')):
            self.test_git_archive_and_checkout_preserve_valid_source_receipt()

    def test_receipt_rejects_unexpected_permissions(self):
        self.prepare()
        receipt = self.output / RECEIPT
        receipt.chmod(0o600)
        with self.assertRaisesRegex(SDKError, 'receipt mode'):
            validate_source(self.output)

    def test_tampered_snapshot_and_receipt_are_rejected(self):
        self.prepare()
        (self.output / 'libelf/libelf.c').write_text('changed\n')
        with self.assertRaisesRegex(SDKError, 'content mismatch|tree changed'):
            validate_source(self.output)
        (self.output / 'libelf/libelf.c').write_text('libelf source patched\n')
        (self.output / RECEIPT).chmod(0o644)
        record = jsonio.read_json(self.output / RECEIPT)
        record['recipe']['ports_commit'] = '0' * 40
        jsonio.write_json(self.output / RECEIPT, record, exclusive=False)
        (self.output / RECEIPT).chmod(0o444)
        with self.assertRaisesRegex(SDKError, 'does not match selected clean Ports'):
            validate_source(self.output, ports=self.ports)

    def test_malformed_receipts_raise_sdkerror(self):
        record = self.prepare()
        for change in ('version', 'files', 'patches', 'archive'):
            with self.subTest(change=change):
                broken = copy.deepcopy(record)
                if change == 'version':
                    broken['version'] = True
                elif change == 'files':
                    broken['files']['../escape'] = {'mode': 0o644, 'size': 0, 'sha256': '0' * 64}
                elif change == 'patches':
                    broken['recipe']['patches'].append(copy.deepcopy(broken['recipe']['patches'][0]))
                else:
                    broken['archive']['sha256'] = '0' * 64
                with self.assertRaises(SDKError):
                    validate_identity(broken)

    def test_archive_corruption_is_refused_before_publication(self):
        self.archive.write_bytes(self.archive.read_bytes() + b'x')
        with self.assertRaisesRegex(SDKError, 'archive checksum'):
            self.prepare()
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob('.libelf-*')), [])

    def test_dirty_ports_and_untracked_patch_are_refused(self):
        (self.ports / 'untracked').write_text('dirty\n')
        with self.assertRaisesRegex(SDKError, 'clean Git'):
            self.prepare()
        (self.ports / 'untracked').unlink()
        (self.ports / RECIPE / 'patches/patch-extra').write_text('not a patch\n')
        with self.assertRaisesRegex(SDKError, 'clean Git'):
            self.prepare()

    def test_unknown_source_hooks_and_transforms_are_refused(self):
        makefile = self.ports / RECIPE / 'Makefile'
        original = makefile.read_text()
        for extra in ('DISTFILES=alternate.tar.bz2\n', 'PATCH_ARGS+=-R\n',
                      'SUBST_CLASSES+=rewrite\n', '.include "custom.mk"\n',
                      'post-extract:\n\t@echo changed\n',
                      'pre-configure:\n\t@echo changed\n'):
            with self.subTest(extra=extra):
                makefile.write_text(original + extra)
                self.commit()
                with self.assertRaisesRegex(SDKError, 'unsupported elftoolchain'):
                    self.prepare()
                self.assertFalse(self.output.exists())
        makefile.write_text(original)
        self.commit()

    def test_manifest_override_must_bind_base_and_supported_paths(self):
        manifest_path = self.ports / MANIFEST
        original = json.loads(manifest_path.read_text())
        for name in ('libelf/Makefile', *sorted(SOURCE_HEADER_OVERRIDES)):
            with self.subTest(bad_base=name):
                manifest = copy.deepcopy(original)
                manifest['packaging_overrides'][name]['base_sha256'] = '0' * 64
                manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
                self.commit()
                with self.assertRaisesRegex(SDKError, 'packaging override'):
                    self.prepare()
        manifest_path.write_text(json.dumps(original, indent=2) + '\n')
        self.commit()
        for name in sorted(SOURCE_HEADER_OVERRIDES):
            with self.subTest(partial=name):
                manifest = copy.deepcopy(original)
                del manifest['packaging_overrides'][name]
                manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
                self.commit()
                with self.assertRaisesRegex(SDKError, 'override set'):
                    self.prepare()
        manifest = copy.deepcopy(original)
        manifest['packaging_overrides']['libelf/unreviewed.c'] = dict(
            mode=0o644, size=0, sha256='0' * 64, base_sha256='0' * 64,
            reason='unreviewed')
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
        self.commit()
        with self.assertRaisesRegex(SDKError, 'override set'):
            self.prepare()

    def test_legacy_two_path_override_manifest_remains_supported(self):
        manifest_path = self.ports / MANIFEST
        manifest = json.loads(manifest_path.read_text())
        for name in SOURCE_HEADER_OVERRIDES:
            row = manifest['packaging_overrides'].pop(name)
            manifest['files'][name] = {
                key: row[key] for key in ('size', 'mode', 'sha256')
            }
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
        self.commit()
        record = self.prepare()
        self.assertEqual(record, validate_source(self.output, ports=self.ports))
        self.assertEqual((self.output / 'common/sys/elfconstants.m4').read_bytes(),
                         b"define(`STN_UNDEF', 0)\n")

    def test_unsafe_archive_members_are_refused(self):
        import shutil
        for name, member, kind, error in (
                ('traversal', 'elftoolchain-0.7.1/../escape', None, 'unsafe elftoolchain archive path'),
                ('symlink', 'elftoolchain-0.7.1/link', tarfile.SYMTYPE, 'link or special'),
                ('hardlink', 'elftoolchain-0.7.1/link', tarfile.LNKTYPE, 'link or special'),
                ('special', 'elftoolchain-0.7.1/fifo', tarfile.FIFOTYPE, 'link or special'),
                ('duplicate', 'elftoolchain-0.7.1/libelf/libelf.c', None, 'duplicate elftoolchain archive member')):
            with self.subTest(name=name):
                shutil.rmtree(self.ports / RECIPE)
                recipe_fixture(self.ports, self.archive, bad_member=member, bad_type=kind)
                self.commit()
                with self.assertRaisesRegex(SDKError, error):
                    self.prepare()
                self.assertFalse(self.output.exists())
                self.assertFalse((self.root / 'escape').exists())

    def test_output_directory_is_not_replaced(self):
        self.output.mkdir()
        marker = self.output / 'marker'; marker.write_text('keep\n')
        with self.assertRaisesRegex(SDKError, 'already exists'):
            self.prepare()
        self.assertEqual(marker.read_text(), 'keep\n')

    def test_unsafe_patch_paths_fail_closed(self):
        patch_path = self.ports / RECIPE / 'patches/patch-libelf_source'
        patch_path.write_bytes(b'--- ../../escape\n+++ ../../escape\n@@ -1 +1 @@\n-x\n+y\n')
        distinfo = self.ports / RECIPE / 'distinfo'
        lines = [line for line in distinfo.read_text().splitlines()
                 if not line.startswith('SHA1 (patch-libelf_source)')]
        import hashlib
        filtered = b''.join(line for line in patch_path.read_bytes().splitlines(keepends=True)
                            if b'$NetBSD' not in line)
        lines.append(f'SHA1 (patch-libelf_source) = {hashlib.sha1(filtered).hexdigest()}')
        distinfo.write_text('\n'.join(lines) + '\n')
        self.commit()
        with self.assertRaisesRegex(SDKError, 'unsafe elftoolchain patch path'):
            self.prepare()
        self.assertFalse(self.output.exists())

    def test_patch_backup_artifacts_are_outside_prepared_root(self):
        self.prepare()
        self.assertFalse(any(path.name.endswith(('.orig', '.rej'))
                             for path in self.output.rglob('*')))
        self.assertEqual(list(self.root.glob('.libelf-*')), [])
