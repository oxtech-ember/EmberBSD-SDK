# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
import os
import shutil
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

from test_core import TemporaryCase, git_environment
from emberbsd_sdk import provenance, verification_receipts
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.llvm_sysroot import LLVMCoordinator
from emberbsd_sdk.sysroot_builder import SysrootBuilder
from emberbsd_sdk.verification_receipts import RunReceipts, fingerprint

OWNER = r'''
import hashlib, json, os, sys
with open(os.environ['CALLS'], 'a') as log:
    log.write('verify\n')
source = sys.argv[sys.argv.index('--source') + 1]
digest = hashlib.sha256()
for name in sorted(os.listdir(source)):
    with open(os.path.join(source, name), 'rb') as handle:
        digest.update(name.encode() + b'\0' + handle.read())
print(json.dumps({'format': 'emberbsd-prepared-source', 'version': 1,
                  'package': 'devel/byacc', 'tree_sha256': digest.hexdigest()}))
'''


class VerificationReceiptTests(TemporaryCase):
    def builder(self, receipts=None):
        builder = SysrootBuilder({}, receipts=receipts)
        builder.paths = {'python': sys.executable, 'ports': str(self.root / 'ports'),
                         'sdk-source': str(self.root / 'sdk'), 'byacc-source': str(self.source)}
        builder.git_command = '/host/git'
        builder.environment = {'CALLS': str(self.calls), 'PATH': '/usr/bin:/bin'}
        return builder

    def setUp(self):
        super().setUp()
        self.write('ports/scripts/source-inputs/byacc/source.py', OWNER)
        self.source = self.root / 'prepared/byacc'
        self.write('prepared/byacc/main.c', 'int main(void) { return 0; }\n')
        self.write('prepared/byacc/defs.h', '#define YES 1\n')
        self.calls = self.root / 'calls.log'

    def content_checks(self):
        return len(self.calls.read_text().splitlines()) if self.calls.exists() else 0

    def test_missing_host_tool_fails_before_any_owner_verifier(self):
        builder = self.builder()
        self.write('archives/rcs.tar.lz', 'archive')
        builder.options = {'rcs-archive': str(self.root / 'archives/rcs.tar.lz')}
        builder.paths['rcs-source'] = str(self.source)
        (self.root / 'host-prefix/bin').mkdir(parents=True)
        builder.native = SimpleNamespace(bundle_root=str(self.root / 'host-prefix'))
        with self.assertRaisesRegex(SDKError, 'missing native tool in host prefix: lzip'):
            builder.verify_common_sources()
        # byacc precedes rcs, but no content verifier may run first.
        self.assertEqual(self.content_checks(), 0)

    def test_unchanged_inputs_are_verified_by_content_once_per_run(self):
        builder = self.builder()
        # Preflight, before the build and after the build of one phase ...
        records = builder.verify_common_sources()
        for _ in range(2):
            self.assertEqual(builder.verify_common_sources(), records)
        # ... and every later phase builder of the same run.
        later = self.builder(receipts=builder.verification)
        self.assertEqual(later.verify_common_sources(), records)
        self.assertEqual(self.content_checks(), 1)

    def test_changed_input_after_plan_is_verified_again_and_fails_the_run(self):
        builder = self.builder()
        planned = builder.verify_common_sources()
        self.assertEqual(builder.verify_common_sources(), planned)
        # Same size and restored times: only the change time shows the write.
        path = self.source / 'defs.h'
        before = path.stat()
        path.write_text('#define YES 0\n')
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(path.stat().st_size, before.st_size)
        # The builder compares this with its plan and stops the run.
        self.assertNotEqual(builder.verify_common_sources(), planned)
        self.assertEqual(self.content_checks(), 2)
        # A metadata-only change is verified by content and still accepted.
        changed = builder.verify_common_sources()
        os.utime(path)
        self.assertEqual(builder.verify_common_sources(), changed)
        self.assertEqual(self.content_checks(), 3)

    def test_added_or_removed_input_file_is_verified_again(self):
        builder = self.builder()
        builder.verify_common_sources()
        self.write('prepared/byacc/extra.c', '')
        builder.verify_common_sources()
        (self.source / 'extra.c').unlink()
        builder.verify_common_sources()
        self.assertEqual(self.content_checks(), 3)

    def test_new_run_ignores_receipts_of_an_earlier_run(self):
        listing = sorted(os.listdir(self.root))
        self.builder().verify_common_sources()
        self.builder().verify_common_sources()
        self.assertEqual(self.content_checks(), 2)
        # Receipts stay in memory; nothing is written next to the inputs.
        self.assertEqual(sorted(os.listdir(self.root)), sorted(listing + ['calls.log']))
        self.assertEqual(sorted(os.listdir(self.source)), ['defs.h', 'main.c'])
        # Each coordinated run starts with its own receipts.
        options = {'sdk': str(self.root / 'sdk'), 'runtime-work': str(self.root / 'work'), 'target': 'aarch64'}
        self.assertIsNot(LLVMCoordinator(options, SysrootBuilder).receipts,
                         LLVMCoordinator(options, SysrootBuilder).receipts)

    def test_failed_verification_leaves_no_receipt(self):
        receipts = RunReceipts()
        def fail():
            raise RuntimeError('content differs')
        with self.assertRaises(RuntimeError):
            receipts.verify('key', [str(self.source)], fail)
        self.assertEqual(receipts.verify('key', [str(self.source)], lambda: 'ok'), 'ok')
        self.assertEqual(receipts.content_checks['key'], 2)

    def test_unsafe_inputs_are_always_verified_by_content(self):
        outside = self.write('outside.h', '')
        os.symlink(outside, self.source / 'link.h')
        self.assertIsNone(fingerprint([str(self.source)]))
        self.assertIsNone(fingerprint([str(self.root / 'missing')]))
        self.assertIsNone(fingerprint(['relative/path']))
        (self.source / 'link.h').unlink()
        os.symlink('main.c', self.source / 'link.h')
        self.assertIsNotNone(fingerprint([str(self.source)]))
        receipts = RunReceipts()
        for _ in range(2):
            receipts.verify('key', [str(self.root / 'missing')], lambda: 'ok')
        self.assertEqual(receipts.content_checks['key'], 2)

    def test_receipts_are_never_persisted(self):
        with patch('builtins.open', side_effect=AssertionError('receipt written')):
            receipts = RunReceipts()
            receipts.verify('key', [str(self.source)], lambda: {'record': 1})
            self.assertEqual(receipts.verify('key', [str(self.source)], lambda: {'record': 2}), {'record': 1})
        self.assertFalse(hasattr(verification_receipts, 'load'))


class SourceReceiptStoreTests(TemporaryCase):
    """Prepared-source records kept between runs in a selected directory."""
    content_checks = VerificationReceiptTests.content_checks

    def setUp(self):
        super().setUp()
        self.write('ports/scripts/source-inputs/byacc/source.py', OWNER)
        self.source = self.root / 'prepared/byacc'
        self.write('prepared/byacc/main.c', 'int main(void) { return 0; }\n')
        self.write('prepared/byacc/defs.h', '#define YES 1\n')
        self.calls = self.root / 'calls.log'

    def builder(self, receipts=None, ports='a' * 40):
        builder = VerificationReceiptTests.builder(self, receipts)
        builder.options['source-receipts'] = str(self.root / 'receipts')
        builder.sources = {'ports': {'path': str(self.root / 'ports'), 'commit': ports, 'external_gitlinks': []},
                           'sdk': {'path': str(self.root / 'sdk'), 'commit': 'b' * 40, 'external_gitlinks': []}}
        return builder

    def test_selected_store_reuses_an_unchanged_record_in_a_new_run(self):
        record = self.builder().verify_common_sources()
        self.assertEqual(self.builder().verify_common_sources(), record)
        self.assertEqual(self.content_checks(), 1)
        # Nothing is written next to the inputs; the record lies in the store.
        self.assertEqual(sorted(os.listdir(self.source)), ['defs.h', 'main.c'])
        self.assertEqual(len(os.listdir(self.root / 'receipts')), 1)

    def test_changed_input_is_verified_again_in_a_new_run(self):
        planned = self.builder().verify_common_sources()
        path = self.source / 'defs.h'
        before = path.stat()
        path.write_text('#define YES 0\n')
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        changed = self.builder().verify_common_sources()
        self.assertNotEqual(changed, planned)
        self.assertEqual(self.content_checks(), 2)
        # The replaced entry is reused by the next run.
        self.assertEqual(self.builder().verify_common_sources(), changed)
        self.assertEqual(self.content_checks(), 2)

    def test_changed_checkout_or_command_is_verified_again(self):
        self.builder().verify_common_sources()
        self.builder(ports='c' * 40).verify_common_sources()
        self.assertEqual(self.content_checks(), 2)
        other = self.builder()
        other.environment['EXTRA'] = '1'
        other.verify_common_sources()
        self.assertEqual(self.content_checks(), 3)
        # TMPDIR names only throwaway scratch space.
        scratch = self.builder()
        scratch.environment['TMPDIR'] = str(self.root / 'tmp')
        scratch.verify_common_sources()
        self.assertEqual(self.content_checks(), 3)

    def test_damaged_or_foreign_entry_is_verified_by_content(self):
        self.builder().verify_common_sources()
        store = self.root / 'receipts'
        entry = store / os.listdir(store)[0]
        entry.write_text('{"format": "emberbsd-source-receipt", "version": 1}')
        self.builder().verify_common_sources()
        self.assertEqual(self.content_checks(), 2)
        entry.write_text('not json')
        self.builder().verify_common_sources()
        self.assertEqual(self.content_checks(), 3)

    def test_store_needs_checked_checkouts_and_an_absolute_directory(self):
        builder = self.builder()
        del builder.sources['sdk']
        with self.assertRaisesRegex(SDKError, 'checked Ports and SDK'):
            builder.verify_common_sources()
        builder = self.builder()
        builder.options['source-receipts'] = 'receipts'
        with self.assertRaisesRegex(SDKError, 'absolute'):
            builder.verify_common_sources()
        self.assertEqual(self.content_checks(), 0)

    def test_hit_makes_no_shared_export(self):
        tool = self.write('host/bin/gpatch', '#!/bin/sh\n', 0o755)
        self.write('ports/' + SysrootBuilder.SHARED_EXPORT_SCRIPT, 'exporter')
        exported = []
        def export(patch):
            exported.append(patch)
            directory = self.root / f'export-{len(exported)}'
            directory.mkdir()
            return str(directory)
        def run():
            builder = self.builder()
            builder.options['source-patch'] = str(tool)
            builder.native = SimpleNamespace(bundle_root=str(self.root / 'host'))
            with patch('emberbsd_sdk.sysroot_builder.native_toolset.load_host_prefix',
                       return_value=SimpleNamespace(tool=lambda role: str(tool))), \
                 patch.object(builder, 'shared_pkgsrc_export', side_effect=export):
                return builder.verify_common_sources()
        record = run()
        self.assertEqual((len(exported), self.content_checks()), (1, 1))
        self.assertEqual(run(), record)
        self.assertEqual((len(exported), self.content_checks()), (1, 1))
        os.utime(self.source / 'main.c')
        self.assertEqual(run(), record)
        self.assertEqual((len(exported), self.content_checks()), (2, 2))


class GitSourceReceiptTests(TemporaryCase):
    """Clean-checkout checks of the OS, Ports and SDK roots within one run."""

    def setUp(self):
        super().setUp()
        self.git_tool = os.path.realpath(shutil.which('git'))
        self.repository = self.root / 'os'
        self.write('os/sys/kern/main.c', 'int main(void) { return 0; }\n')
        self.write('os/Makefile', 'all:\n')
        self.run_git('init', '-q')
        self.run_git('add', '.')
        self.commit('Fixture')

    def run_git(self, *args):
        subprocess.run([self.git_tool, '-C', str(self.repository), *args], check=True,
                       capture_output=True, env=git_environment())

    def commit(self, message, *extra):
        self.run_git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                     'commit', '-q', *extra, '-m', message)

    def builder(self, receipts=None):
        builder = SysrootBuilder({}, receipts=receipts)
        builder.git_command = self.git_tool
        builder.environment = {'PATH': os.environ['PATH']}
        return builder

    def counted(self):
        calls = []
        real = provenance.git_source
        def check(*args, **kwargs):
            calls.append(args[0])
            return real(*args, **kwargs)
        return calls, patch.object(provenance, 'git_source', side_effect=check)

    def test_unchanged_checkout_is_checked_with_git_once_per_run(self):
        builder = self.builder()
        calls, counted = self.counted()
        with counted:
            record = builder.source(str(self.repository), 'os')
            self.assertEqual(builder.source(str(self.repository), 'os'), record)
            later = self.builder(receipts=builder.verification)
            self.assertEqual(later.source(str(self.repository), 'os'), record)
            # A new run starts with Git again.
            self.assertEqual(self.builder().source(str(self.repository), 'os'), record)
        self.assertEqual(len(calls), 2)

    def test_changed_file_or_commit_is_checked_with_git_again(self):
        builder = self.builder()
        record = builder.source(str(self.repository), 'os')
        # A commit changes only the Git directory, not the working files.
        self.commit('Empty', '--allow-empty')
        moved = builder.source(str(self.repository), 'os')
        self.assertNotEqual(moved['commit'], record['commit'])
        self.write('os/sys/kern/main.c', 'int main(void) { return 1; }\n')
        with self.assertRaisesRegex(SDKError, 'clean Git root'):
            builder.source(str(self.repository), 'os')
        self.run_git('checkout', '-q', '--', '.')
        self.write('os/untracked.c', 'int x;\n')
        with self.assertRaisesRegex(SDKError, 'clean Git root'):
            builder.source(str(self.repository), 'os')

    def test_git_state_outside_the_root_is_checked_with_git_every_time(self):
        builder = self.builder()
        root = str(self.repository)
        self.assertEqual(builder.git_source_inputs(root), [os.path.realpath(root), self.git_tool])
        # A submodule whose Git directory is inside the root's Git directory.
        self.write('os/.gitmodules', '[submodule "sub"]\n\tpath = upstream/sub\n\turl = ../sub\n')
        self.write('os/.git/modules/sub/HEAD', 'ref: refs/heads/main\n')
        self.write('os/upstream/sub/.git', 'gitdir: ../../.git/modules/sub\n')
        self.assertIsNotNone(builder.git_source_inputs(root))
        # The same submodule with its Git directory elsewhere.
        self.write('elsewhere/HEAD', 'ref: refs/heads/main\n')
        self.write('os/upstream/sub/.git', f'gitdir: {self.root / "elsewhere"}\n')
        self.assertIsNone(builder.git_source_inputs(root))
        os.unlink(self.repository / 'upstream/sub/.git')
        os.symlink(self.root / 'elsewhere', self.repository / 'upstream/sub/.git')
        self.assertIsNone(builder.git_source_inputs(root))
        os.unlink(self.repository / 'upstream/sub/.git')
        self.write('os/upstream/sub/.git', 'gitdir: ../../.git/modules/sub\n')
        self.assertIsNotNone(builder.git_source_inputs(root))
        # A Git tool named only through PATH can change without a changed file.
        builder.git_command = 'git'
        self.assertIsNone(builder.git_source_inputs(root))
        # A linked worktree keeps its Git directory outside the checkout.
        linked = self.root / 'linked'
        self.run_git('worktree', 'add', '-q', '--detach', str(linked))
        builder.git_command = self.git_tool
        self.assertIsNone(builder.git_source_inputs(str(linked)))
        calls, counted = self.counted()
        with counted:
            for _ in range(2):
                builder.source(str(linked), 'os')
        self.assertEqual(len(calls), 2)
