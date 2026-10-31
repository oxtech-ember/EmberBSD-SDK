# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
import os
import shutil
import subprocess
from unittest.mock import patch

from test_core import TemporaryCase, git_environment
from emberbsd_sdk import provenance
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.sysroot_builder import SysrootBuilder
from emberbsd_sdk.verification_receipts import RunReceipts, fingerprint


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

    def test_a_failed_check_leaves_no_record_to_reuse(self):
        self.write('os/untracked.c', 'int x;\n')
        builder = self.builder()
        with self.assertRaisesRegex(SDKError, 'clean Git root'):
            builder.source(str(self.repository), 'os')
        with self.assertRaisesRegex(SDKError, 'clean Git root'):
            builder.source(str(self.repository), 'os')

    def test_linked_worktree_is_checked_with_git_once_per_run(self):
        # Release checkouts are linked worktrees, whose Git directory lies
        # outside the checkout; each phase used to run git status again.
        linked = self.root / 'linked'
        self.run_git('worktree', 'add', '-q', '--detach', str(linked))
        receipts = RunReceipts()
        calls, counted = self.counted()
        with counted:
            phases = [self.builder(receipts=receipts) for _ in range(5)]
            records = [phase.source(str(linked), 'os') for phase in phases]
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(record == records[0] for record in records))
        with open(linked / 'Makefile', 'a') as handle:
            handle.write('# edited\n')
        with self.assertRaisesRegex(SDKError, 'clean Git root'):
            self.builder().source(str(linked), 'os')

    def test_no_phase_checks_sources_with_git_after_building(self):
        # A build used to run git status of every checkout again at its end
        # (about four minutes for the OS tree); the start record is final.
        self.assertFalse(hasattr(SysrootBuilder, 'require_unchanged_sources'))
        receipts = RunReceipts()
        start = self.builder(receipts=receipts)
        record = start.source(str(self.repository), 'os')
        self.write('os/untracked.c', 'int x;\n')
        calls, counted = self.counted()
        with counted:
            self.assertEqual(self.builder(receipts=receipts).source(str(self.repository), 'os'), record)
        self.assertEqual(calls, [])

    def test_run_once_receipt_reruns_only_after_a_failure(self):
        receipts = RunReceipts()
        first = receipts.once('key', lambda: {'n': 1})
        first['n'] = 99
        self.assertEqual(receipts.once('key', lambda: self.fail('reused')), {'n': 1})
        receipts = RunReceipts()
        def broken():
            raise SDKError('broken')
        with self.assertRaisesRegex(SDKError, 'broken'):
            receipts.once('key', broken)
        self.assertEqual(receipts.once('key', lambda: {'n': 3}), {'n': 3})
        self.assertEqual(receipts.content_checks['key'], 2)


class ContentReceiptTests(TemporaryCase):
    """A receipt is keyed by input-key files; a stage tree is never walked."""

    def test_stage_tree_is_not_walked_and_a_changed_manifest_verifies_again(self):
        manifest = self.write('stage/runtime.json', '{"key": 1}\n')
        for index in range(20):
            self.write(f'stage/lib/file{index}', 'x')
        receipts, runs = RunReceipts(), []
        def verifier():
            runs.append(True)
            return {'run': len(runs)}
        with patch.object(os, 'scandir', side_effect=AssertionError('stage tree walked')):
            self.assertEqual(receipts.verify('stage', [str(manifest)], verifier), {'run': 1})
            self.assertEqual(receipts.verify('stage', [str(manifest)], verifier), {'run': 1})
            # A stage file is not read again; the manifest is the key.
            (self.root / 'stage/lib/file0').write_text('changed')
            self.assertEqual(receipts.verify('stage', [str(manifest)], verifier), {'run': 1})
        os.utime(manifest, ns=(1, 1))
        manifest.write_text('{"key": 2}\n')
        self.assertEqual(receipts.verify('stage', [str(manifest)], verifier), {'run': 2})

    def test_directory_input_has_no_fingerprint(self):
        self.write('stage/runtime.json', '{}\n')
        with patch.object(os, 'scandir', side_effect=AssertionError('walked')):
            self.assertIsNone(fingerprint([str(self.root / 'stage')]))
