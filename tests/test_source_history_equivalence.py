# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Real Git history admission tests; no target build or ABI qualification."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from emberbsd_sdk.errors import CommandError, SDKError
from emberbsd_sdk.sysroot_builder import SysrootBuilder


class SourceHistoryEquivalenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.env = dict(os.environ, GIT_AUTHOR_NAME='Test', GIT_AUTHOR_EMAIL='test@example.invalid',
                        GIT_COMMITTER_NAME='Test', GIT_COMMITTER_EMAIL='test@example.invalid')
        self.git('init', '-q')
        Path(self.root, 'input').write_text('unchanged\n')
        self.git('add', 'input')
        self.git('commit', '-qm', 'initial')
        self.old = self.git('rev-parse', 'HEAD')
        self.builder = object.__new__(SysrootBuilder)
        self.builder.git_command = shutil.which('git')
        self.builder.environment = self.env
        self.record = {'sources': {name: {'commit': self.old} for name in ('os', 'ports', 'sdk')}}

    def git(self, *args):
        return subprocess.check_output(['git', '-C', self.root, *args], env=self.env).decode().strip()

    def check(self, current):
        self.builder.sources = {name: {'commit': current, 'path': self.root}
                                for name in ('os', 'ports', 'sdk')}
        self.builder.validate_source_ancestry(self.record, self.root)

    def test_identical_pin(self):
        self.check(self.old)

    def test_descendant_still_allowed(self):
        Path(self.root, 'input').write_text('new\n')
        self.git('commit', '-qam', 'next')
        self.check(self.git('rev-parse', 'HEAD'))

    def test_rewritten_history_equal_tree(self):
        self.git('commit', '--amend', '-qm', 'rewritten provenance')
        current = self.git('rev-parse', 'HEAD')
        self.assertNotEqual(current, self.old)
        self.check(current)

    def test_rewritten_history_changed_tree_refused(self):
        Path(self.root, 'input').write_text('changed\n')
        self.git('commit', '-qam', 'rewritten inputs', '--amend')
        with self.assertRaises(CommandError):
            self.check(self.git('rev-parse', 'HEAD'))

    def test_unknown_old_commit_refused(self):
        self.record['sources']['ports']['commit'] = 'f' * 40
        with self.assertRaises(CommandError):
            self.check(self.old)

    def test_malformed_old_pin_refused(self):
        self.record['sources']['sdk']['commit'] = 'HEAD'
        with self.assertRaises(SDKError):
            self.check(self.old)

    def test_git_error_never_becomes_tree_fallback(self):
        with patch.object(self.builder, 'git', side_effect=CommandError(128, ['git'])) as call:
            with self.assertRaises(CommandError):
                self.check(self.old)
        self.assertEqual(call.call_count, 1)
