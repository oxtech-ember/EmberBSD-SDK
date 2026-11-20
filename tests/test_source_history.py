# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Real Git history admission of kept objects; no target build or ABI qualification."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from emberbsd_sdk.errors import CommandError, SDKError
from emberbsd_sdk.sysroot_builder import SysrootBuilder


class SourceHistoryTests(unittest.TestCase):
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
        self.builder.validate_source_history(self.record, self.root)

    def test_identical_pin(self):
        self.check(self.old)

    def test_later_earlier_and_other_history_commits_are_accepted(self):
        Path(self.root, 'input').write_text('new\n')
        self.git('commit', '-qam', 'next')
        later = self.git('rev-parse', 'HEAD')
        self.check(later)
        # Objects of the later commit and an earlier current commit.
        self.record['sources']['os']['commit'] = later
        self.check(self.old)
        # A rewritten commit with other files.
        Path(self.root, 'input').write_text('changed\n')
        self.git('commit', '-qam', 'rewritten inputs', '--amend')
        self.check(self.git('rev-parse', 'HEAD'))
        # A commit of other history.
        self.git('checkout', '-q', '--orphan', 'other')
        Path(self.root, 'input').write_text('other history\n')
        self.git('commit', '-qam', 'other root')
        self.check(self.git('rev-parse', 'HEAD'))

    def test_an_earlier_commit_that_removes_a_file_of_the_objects_is_refused(self):
        # Make does not see a removed file: an object of x.c would stay.
        Path(self.root, 'x.c').write_text('int x;\n')
        Path(self.root, 'notes').write_text('notes\n')
        self.git('add', '-A')
        self.git('commit', '-qm', 'add x.c')
        self.record['sources']['os']['commit'] = self.git('rev-parse', 'HEAD')
        with self.assertRaisesRegex(SDKError, 'removes files of the kept objects .*x.c.*select new objects'):
            self.check(self.old)
        # A later commit that removes the file still uses the objects, as before.
        self.git('rm', '-q', 'x.c')
        self.git('commit', '-qm', 'remove x.c')
        self.check(self.git('rev-parse', 'HEAD'))

    def test_an_earlier_commit_without_a_document_of_the_objects_is_accepted(self):
        Path(self.root, 'notes.md').write_text('notes\n')
        Path(self.root, 'input').write_text('later\n')
        self.git('add', '-A')
        self.git('commit', '-qm', 'notes')
        self.record['sources']['os']['commit'] = self.git('rev-parse', 'HEAD')
        self.check(self.old)

    def test_unknown_old_commit_refused(self):
        self.record['sources']['ports']['commit'] = 'f' * 40
        with self.assertRaisesRegex(SDKError, 'recorded ports commit f+ .* is not in .*fetch it'):
            self.check(self.old)

    def test_malformed_old_pin_refused(self):
        self.record['sources']['sdk']['commit'] = 'HEAD'
        with self.assertRaises(SDKError):
            self.check(self.old)

    def test_git_error_is_a_failure(self):
        with patch.object(self.builder, 'git', side_effect=CommandError(128, ['git'])):
            with self.assertRaises(SDKError):
                self.check(self.old)


if __name__ == '__main__':
    unittest.main()
