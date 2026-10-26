# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Exercise extraction and real patch application, including refusal paths."""
import hashlib
from pathlib import Path
import shutil
import subprocess
from unittest.mock import patch

from test_core import TemporaryCase, SDK_SOURCE
from libedit_fixture import recipe_fixture
from emberbsd_sdk import jsonio
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.libedit import prepare_source, validate_source, validate_identity, RECIPE, RECEIPT


class LibeditTests(TemporaryCase):
    def setUp(self):
        super().setUp()
        self.ports = self.root / 'ports'; self.ports.mkdir()
        self.archive = self.root / 'libedit.tar.gz'
        self.output = self.root / 'prepared'
        recipe_fixture(self.ports, self.archive)
        subprocess.run(['git', '-C', str(self.ports), 'init', '-q'], check=True)
        self.commit()

    def commit(self):
        subprocess.run(['git', '-C', str(self.ports), 'add', '-f', '.'], check=True)
        subprocess.run(['git', '-C', str(self.ports), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'Fixture'], check=True)

    def prepare(self):
        return prepare_source(self.ports, self.archive, self.output)

    def test_prepare_validate_and_all_patches(self):
        record = self.prepare()
        self.assertEqual(record, validate_source(self.output, ports=self.ports))
        self.assertEqual(len(record['recipe']['patches']), 7)
        for n in range(7):
            self.assertEqual((self.output / 'src' / ('input' + str(n))).read_text(), 'patched\n')
        self.assertEqual((self.output / 'ports.distinfo').read_bytes(), (self.ports / RECIPE / 'distinfo').read_bytes())
        self.assertEqual((self.output / RECEIPT).stat().st_mode & 0o777, 0o444)
        with self.assertRaisesRegex(SDKError, 'already exists'):
            self.prepare()

    def test_patch_offset_does_not_leave_backup(self):
        patchfile = self.ports / RECIPE / 'patches/patch-input0'
        before = patchfile.read_bytes()
        after = before.replace(b'@@ -1 +1 @@', b'@@ -2 +2 @@')
        patchfile.write_bytes(after)
        distinfo = self.ports / RECIPE / 'distinfo'
        def checksum(data):
            return hashlib.sha1(b''.join(line for line in data.splitlines(keepends=True)
                                        if b'$NetBSD' not in line)).hexdigest()
        distinfo.write_text(distinfo.read_text().replace(checksum(before), checksum(after)))
        self.commit()
        self.prepare()
        self.assertEqual((self.output / 'src/input0').read_text(), 'patched\n')
        expected = {'src/input' + str(n) for n in range(7)}
        expected.update(('doc/editline.3', 'ports.distinfo', RECEIPT))
        self.assertEqual({str(p.relative_to(self.output)) for p in self.output.rglob('*')
                          if p.is_file()}, expected)

    def test_unrelated_ports_commit_preserves_prepared_source_and_origin(self):
        record = self.prepare()
        receipt = (self.output / RECEIPT).read_bytes()
        (self.ports / 'unrelated').write_text('another package changed\n')
        self.commit()
        current = subprocess.check_output(['git', '-C', str(self.ports), 'rev-parse', 'HEAD'], text=True).strip()
        self.assertNotEqual(current, record['recipe']['ports_commit'])
        self.assertEqual(validate_source(self.output, ports=self.ports), record)
        self.assertEqual((self.output / RECEIPT).read_bytes(), receipt)

    def test_changed_recipe_still_refuses_prepared_source(self):
        self.prepare()
        path = self.ports / RECIPE / 'Makefile'
        path.write_text(path.read_text() + '# Changed recipe\n')
        self.commit()
        with self.assertRaisesRegex(SDKError, 'does not match selected clean Ports recipe'):
            validate_source(self.output, ports=self.ports)

    def test_changed_patch_with_updated_distinfo_still_refuses_prepared_source(self):
        self.prepare()
        path = self.ports / RECIPE / 'patches/patch-input0'
        before = path.read_bytes()
        after = before.replace(b'+patched', b'+different')
        path.write_bytes(after)
        def checksum(data):
            return hashlib.sha1(b''.join(line for line in data.splitlines(keepends=True)
                                        if b'$NetBSD' not in line)).hexdigest()
        distinfo = self.ports / RECIPE / 'distinfo'
        distinfo.write_text(distinfo.read_text().replace(checksum(before), checksum(after)))
        self.commit()
        with self.assertRaisesRegex(SDKError, 'does not match selected clean Ports recipe'):
            validate_source(self.output, ports=self.ports)

    def test_shared_patch_helper_preserves_libedit_invocation(self):
        from emberbsd_sdk import libedit
        original = libedit.process.capture
        calls = []
        def record(argv, **kwargs):
            if Path(argv[0]).name == 'patch':
                calls.append(list(argv))
            return original(argv, **kwargs)
        with patch.object(libedit.process, 'capture', side_effect=record):
            self.prepare()
        expected = [shutil.which('patch'), '-p0', '-F0', '--posix', '--batch',
                    '--forward', '--no-backup-if-mismatch', '-i']
        self.assertEqual(len(calls), 7)
        for argv in calls:
            self.assertEqual(argv[:8], expected)

    def test_patch_backup_and_reject_files_are_still_refused(self):
        from emberbsd_sdk import libedit
        original = libedit.process.capture
        for suffix in ('.orig', '.rej'):
            with self.subTest(suffix=suffix):
                def leave_file(argv, **kwargs):
                    result = original(argv, **kwargs)
                    if '-F0' in argv:
                        Path(kwargs['cwd'], 'leftover' + suffix).write_text('unexpected')
                    return result
                with patch.object(libedit.process, 'capture', side_effect=leave_file):
                    with self.assertRaisesRegex(SDKError, 'backup or reject'):
                        self.prepare()
                self.assertFalse(self.output.exists())
                self.assertEqual(list(self.root.glob('.libedit-*')), [])

    def test_malformed_receipt_types_raise_sdkerror(self):
        import copy
        import sys
        record = self.prepare()
        for field, value in (('distname', None), ('distname', 1), ('patches', None), ('patches', [None]),
                             ('patches', [dict(name=None, sha1='a' * 40, sha256='b' * 64)])):
            with self.subTest(field=field, value=value):
                changed = copy.deepcopy(record)
                changed['recipe'][field] = value
                with self.assertRaises(SDKError):
                    validate_identity(changed)
        changed = copy.deepcopy(record); changed['recipe']['distname'] = None
        (self.output / RECEIPT).chmod(0o644)
        jsonio.write_json(self.output / RECEIPT, changed, exclusive=False)
        result = subprocess.run([sys.executable, '-B', str(SDK_SOURCE / 'scripts/prepare-libedit.py'),
                                 '--ports', str(self.ports), '--validate', str(self.output)], capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(b'Traceback', result.stderr)

    def test_archive_receipt_must_match_distinfo(self):
        record = self.prepare()
        record['archive']['blake2s'] = '0' * 64
        (self.output / RECEIPT).chmod(0o644)
        jsonio.write_json(self.output / RECEIPT, record, exclusive=False)
        with self.assertRaisesRegex(SDKError, 'archive receipt'):
            validate_source(self.output, ports=self.ports)

    def test_archive_corruption(self):
        self.archive.write_bytes(self.archive.read_bytes() + b'x')
        with self.assertRaisesRegex(SDKError, 'archive checksum'):
            self.prepare()
        self.assertFalse(self.output.exists())

    def test_patch_corruption_even_when_committed(self):
        path = self.ports / RECIPE / 'patches/patch-input6'
        path.write_bytes(path.read_bytes() + b'changed\n'); self.commit()
        with self.assertRaisesRegex(SDKError, 'patch checksum'):
            self.prepare()

    def test_dirty_ports(self):
        (self.ports / 'untracked').write_text('x')
        with self.assertRaisesRegex(SDKError, 'clean Git'):
            self.prepare()

    def test_dynamic_recipe_refused(self):
        path = self.ports / RECIPE / 'Makefile'
        path.write_text(path.read_text().replace('libedit-20260512-3.1', '${DISTNAME}')); self.commit()
        with self.assertRaisesRegex(SDKError, 'literal DISTNAME'):
            self.prepare()

    def test_source_affecting_overrides_refused(self):
        path = self.ports / RECIPE / 'Makefile'
        original = path.read_text()
        for extra in ('do-patch:\n\t@:\n', 'post-extract:\n\t@:\n', 'DISTFILES=other.tar.gz\n',
                      'PATCHDIR=../other\n', 'EXTRACT_SUFX=.tar.xz\n', 'PATCH_ARGS+=-R\n',
                      'SUBST_CLASSES+=rewrite\n', 'SUBST_STAGE.rewrite=pre-configure\n',
                      '.include \"custom-patches.mk\"\n', 'pre-configure:\n\tsed -i s/original/changed/ src/input0\n'):
            with self.subTest(extra=extra):
                path.write_text(original + extra); self.commit()
                with self.assertRaisesRegex(SDKError, 'unsupported libedit source'):
                    self.prepare()
                self.assertFalse(self.output.exists())

    def test_added_unrecorded_patch(self):
        (self.ports / RECIPE / 'patches/patch-extra').write_text('x'); self.commit()
        with self.assertRaisesRegex(SDKError, 'every patch'):
            self.prepare()

    def test_tampered_source_and_receipt(self):
        self.prepare()
        (self.output / 'src/input0').write_text('changed')
        with self.assertRaisesRegex(SDKError, 'tree changed'):
            validate_source(self.output)
        (self.output / RECEIPT).chmod(0o644)
        record = jsonio.read_json(self.output / RECEIPT); record['recipe']['ports_commit'] = '0' * 40
        jsonio.write_json(self.output / RECEIPT, record, exclusive=False)
        with self.assertRaises(SDKError):
            validate_source(self.output, ports=self.ports)

    def test_path_escape_archive_refused(self):
        import shutil
        shutil.rmtree(self.ports / RECIPE)
        recipe_fixture(self.ports, self.archive, bad_member='libedit-20260512-3.1/../escape')
        self.commit()
        with self.assertRaisesRegex(SDKError, 'unsafe'):
            self.prepare()
        self.assertFalse(self.output.exists())
        self.assertFalse((self.root / 'escape').exists())

    def test_archive_links_and_special_files_refused(self):
        import shutil
        import tarfile
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE):
            with self.subTest(kind=kind):
                shutil.rmtree(self.ports / RECIPE)
                recipe_fixture(self.ports, self.archive, bad_member='libedit-20260512-3.1/link', bad_type=kind)
                self.commit()
                with self.assertRaisesRegex(SDKError, 'link or special'):
                    self.prepare()
                self.assertFalse(self.output.exists())

    def test_metadata_must_be_tracked(self):
        subprocess.run(['git', '-C', str(self.ports), 'rm', '--cached', RECIPE + '/Makefile'], check=True, stdout=subprocess.DEVNULL)
        (self.ports / '.gitignore').write_text('/' + RECIPE + '/Makefile\n')
        subprocess.run(['git', '-C', str(self.ports), 'add', '.gitignore'], check=True)
        subprocess.run(['git', '-C', str(self.ports), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'Ignore metadata'], check=True)
        with self.assertRaises(SDKError):
            self.prepare()
        self.assertFalse(self.output.exists())

    def test_patch_failure_does_not_publish(self):
        from emberbsd_sdk import libedit
        original = libedit.process.capture
        def fail(argv, **kwargs):
            if '-F0' in argv:
                raise SDKError('injected patch failure')
            return original(argv, **kwargs)
        with patch.object(libedit.process, 'capture', side_effect=fail):
            with self.assertRaisesRegex(SDKError, 'injected patch'):
                self.prepare()
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob('.libedit-*')), [])
