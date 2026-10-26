# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Run source patch backup checks with an explicitly selected patch executable."""
import os
from pathlib import Path
import unittest

from test_core import TemporaryCase
from emberbsd_sdk import sourceprep


@unittest.skipUnless(os.environ.get('EMBERBSD_TEST_PATCH'),
                     'select the real patch tool with EMBERBSD_TEST_PATCH')
class SourcePatchTests(TemporaryCase):
    def check_level(self, level):
        tool = Path(os.environ['EMBERBSD_TEST_PATCH'])
        self.assertTrue(tool.is_absolute() and tool.is_file() and os.access(tool, os.X_OK))
        source = self.root / 'source'; source.mkdir()
        (source / 'nested').mkdir()
        existing = source / 'nested/input.c'
        existing.write_text('before\n')
        prefix = 'prefix/' * level
        delta = ('--- ' + prefix + 'nested/input.c\n+++ ' + prefix + 'nested/input.c\n'
                 '@@ -1 +1 @@\n-before\n+after\n'
                 '--- /dev/null\n+++ ' + prefix + 'nested/created.c\n'
                 '@@ -0,0 +1 @@\n+new file\n').encode()
        patch = self.root / 'change.patch'; patch.write_bytes(delta)
        backups = self.root / 'backups'
        sourceprep.apply_patch(str(tool), patch, delta, source, 'fixture',
            patch_args=('-p' + str(level), '-f', '-F0', '-V', 'none'),
            backup_directory=backups, allow_dev_null=True)
        # BSD patch can return zero when backup creation prevented installation.
        # Check actual bytes and source cleanliness, not just its exit status.
        self.assertEqual(existing.read_text(), 'after\n')
        self.assertEqual((source / 'nested/created.c').read_text(), 'new file\n')
        self.assertFalse(list(source.rglob('*.orig')))
        self.assertFalse(list(source.rglob('*.rej')))
        self.assertTrue(any(p.is_file() and p.read_text() == 'before\n'
                            for p in backups.rglob('*')))

    def test_unstripped_source_backup(self):
        self.check_level(0)

    def test_one_stripped_component_backup(self):
        self.check_level(1)

    def test_two_stripped_components_backup(self):
        self.check_level(2)
