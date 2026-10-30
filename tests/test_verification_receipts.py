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
from emberbsd_sdk.verification_receipts import RunReceipts


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

    def test_final_recheck_finds_a_commit_or_change_during_the_run(self):
        builder = self.builder()
        record = builder.source(str(self.repository), 'os')
        # A commit changes only the Git directory, not the working files.
        self.commit('Empty', '--allow-empty')
        # Phases reuse the start-of-run record; the final check asks Git.
        self.assertEqual(builder.source(str(self.repository), 'os'), record)
        moved = builder.source(str(self.repository), 'os', recheck=True)
        self.assertNotEqual(moved['commit'], record['commit'])
        self.write('os/sys/kern/main.c', 'int main(void) { return 1; }\n')
        with self.assertRaisesRegex(SDKError, 'clean Git root'):
            builder.source(str(self.repository), 'os', recheck=True)
        self.run_git('checkout', '-q', '--', '.')
        self.write('os/untracked.c', 'int x;\n')
        with self.assertRaisesRegex(SDKError, 'clean Git root'):
            builder.source(str(self.repository), 'os', recheck=True)
        # A failed check leaves no record to reuse.
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
            self.assertEqual(phases[-1].source(str(linked), 'os', recheck=True), records[0])
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(record == records[0] for record in records))
        with open(linked / 'Makefile', 'a') as handle:
            handle.write('# edited\n')
        with self.assertRaisesRegex(SDKError, 'clean Git root'):
            phases[0].source(str(linked), 'os', recheck=True)

    def test_only_the_final_build_checks_sources_with_git_after_building(self):
        receipts = RunReceipts()
        start = self.builder(receipts=receipts)
        start.sources = {'os': start.source(str(self.repository), 'os')}
        self.write('os/untracked.c', 'int x;\n')
        calls, counted = self.counted()
        with counted:
            for phase in ('headers', 'base'):
                stage = self.builder(receipts=receipts)
                stage.sources, stage.llvm_phase = start.sources, phase
                stage.require_unchanged_sources()
            self.assertEqual(calls, [])
            final = self.builder(receipts=receipts)
            final.sources, final.llvm_phase = start.sources, 'final'
            with self.assertRaisesRegex(SDKError, 'clean Git root'):
                final.require_unchanged_sources()
        self.assertEqual(len(calls), 1)

    def test_run_once_receipt_reruns_only_on_recheck_or_failure(self):
        receipts = RunReceipts()
        results = iter([{'n': 1}, {'n': 2}])
        first = receipts.once('key', lambda: next(results))
        first['n'] = 99
        self.assertEqual(receipts.once('key', lambda: self.fail('reused')), {'n': 1})
        self.assertEqual(receipts.once('key', lambda: next(results), recheck=True), {'n': 2})
        def broken():
            raise SDKError('broken')
        with self.assertRaisesRegex(SDKError, 'broken'):
            receipts.once('key', broken, recheck=True)
        self.assertEqual(receipts.once('key', lambda: {'n': 3}), {'n': 3})
        self.assertEqual(receipts.content_checks['key'], 4)
