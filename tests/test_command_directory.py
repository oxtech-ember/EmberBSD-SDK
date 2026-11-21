# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Command references on hosts without exclusive directory rename."""
import concurrent.futures
import errno
import os
from pathlib import Path
import threading
from unittest.mock import patch

from test_core import TemporaryCase
from emberbsd_sdk import command_directory as commands
from emberbsd_sdk.errors import SDKError


class CommandDirectoryTests(TemporaryCase):
    def setUp(self):
        super().setUp()
        platform = patch.object(commands.sys, 'platform', 'netbsd11')
        platform.start()
        self.addCleanup(platform.stop)
        self.output = self.root / 'bindings'

    def stage(self, name, content):
        directory = self.root / name
        directory.mkdir()
        (directory / 'cmd').write_bytes(content)
        (directory / 'cmd').chmod(0o555)
        (directory / 'cc').symlink_to('cmd')
        return str(directory)

    def test_simultaneous_publishers_only_expose_complete_trees(self):
        barrier = threading.Barrier(4)
        def attempt(i):
            source = self.stage(str(i), b'complete')
            barrier.wait(10)
            try:
                commands.publish(source, self.output)
                return True
            except FileExistsError:
                self.assertEqual((self.output / 'cc').read_bytes(), b'complete')
                return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(sum(pool.map(attempt, range(4))), 1)
        tree = commands.managed_tree(self.output)
        self.assertEqual(commands.binding_path(self.output), str(self.output))
        self.output.unlink()
        commands.publish(self.stage('repeat', b'complete'), self.output)
        self.assertEqual(commands.managed_tree(self.output), tree)
        self.assertEqual(list(tree.parent.parent.glob('g-*')), [tree.parent])

    def test_foreign_destination_wins_even_at_last_syscall(self):
        symlink = os.symlink
        def competing_writer(target, destination):
            if Path(destination) == self.output:
                self.output.mkdir()
            return symlink(target, destination)
        stage = self.stage('stage', b'new')
        with patch.object(os, 'symlink', side_effect=competing_writer):
            with self.assertRaises(FileExistsError):
                commands.publish(stage, self.output)
        self.assertFalse(self.output.is_symlink())
        self.assertEqual(list(self.output.iterdir()), [])

    def test_refuses_foreign_link_and_corrupt_cached_generation(self):
        self.output.symlink_to(self.root / 'absent')
        with self.assertRaises(SDKError):
            commands.binding_path(self.output)
        self.output.unlink()
        commands.publish(self.stage('first', b'expected'), self.output)
        cmd = commands.managed_tree(self.output) / 'cmd'
        self.output.unlink()
        cmd.chmod(0o755)
        cmd.write_bytes(b'changed')
        with self.assertRaisesRegex(SDKError, 'differs'):
            commands.publish(self.stage('second', b'expected'), self.output)
        self.assertFalse(os.path.lexists(self.output))

    def test_cross_device_stage_is_not_copied(self):
        stage = self.stage('stage', b'original')
        with patch.object(os, 'rename', side_effect=OSError(errno.EXDEV, 'cross device')):
            with self.assertRaises(OSError) as failure:
                commands.publish(stage, self.output)
        self.assertEqual(failure.exception.errno, errno.EXDEV)
        self.assertEqual((Path(stage) / 'cc').read_bytes(), b'original')
        self.assertFalse(os.path.lexists(self.output))
