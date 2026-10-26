# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
from types import SimpleNamespace

from test_core import TemporaryCase
from emberbsd_sdk.native_runtime_install import NativeRuntimeInstaller
from emberbsd_sdk.errors import SDKError


class NativeRuntimeInstallTests(TemporaryCase):
    def setUp(self):
        super().setUp()
        self.destination = self.root / 'dest'; self.destination.mkdir()
        self.write('dest/METALOG', './ type=dir mode=0755 uid=0 gid=0\n')
        self.source = self.root / 'os'; (self.source / 'etc').mkdir(parents=True)
        self.executable = self.write('tools/target-install', 'fixture')
        self.executable.chmod(0o755)
        self.commands = []
        self.installer = NativeRuntimeInstaller(self.destination,
            {'install': str(self.executable)}, self.source, self.commands.append)

    def test_native_metadata_options_and_literal_targets(self):
        self.installer.directory(self.destination / 'new dir', 0o755)
        source = self.write('input', 'bytes')
        self.installer.file(source, self.destination / 'file', 0o640)
        self.installer.link('../pkg/llvm/lib/a.so.1', self.destination / 'alias', 0o755)
        for command in self.commands:
            self.assertEqual(command[:12], (str(self.executable), '-U', '-M',
                str(self.destination / 'METALOG'), '-D', str(self.destination),
                '-N', str(self.source / 'etc'), '-o', 'root', '-g', 'wheel'))
        self.assertEqual(self.commands[0][12:], ('-d', '-m', '0755', '--',
                                                str(self.destination / 'new dir')))
        self.assertEqual(self.commands[1][12:], ('-p', '-h', 'sha256', '-m', '0640',
                                                '--', str(source), str(self.destination / 'file')))
        self.assertEqual(self.commands[2][12:], ('-l', 's', '-m', '0755', '--',
            '../pkg/llvm/lib/a.so.1', str(self.destination / 'alias')))

    def test_existing_directories_are_not_relogged_and_conflicts_refuse(self):
        self.installer.directory(self.destination, 0o755)
        self.assertEqual(self.commands, [])
        path = self.write('dest/existing', 'preserve')
        for operation in (lambda: self.installer.directory(path, 0o755),
                          lambda: self.installer.file(path, path, 0o644),
                          lambda: self.installer.link('elsewhere', path, 0o755)):
            with self.assertRaises(SDKError): operation()
        self.assertEqual(path.read_text(), 'preserve')
        self.assertEqual(self.commands, [])

    def test_escape_and_symlink_ancestor_are_refused(self):
        outside = self.root / 'outside'; outside.mkdir()
        (self.destination / 'escape').symlink_to(outside)
        for path in (outside / 'file', self.destination / 'escape/file'):
            with self.assertRaises(SDKError): self.installer.link('target', path, 0o755)
        self.assertEqual(self.commands, [])

    def test_existing_distribution_metadata_and_failures_are_required(self):
        (self.destination / 'METALOG').unlink()
        with self.assertRaises(SDKError):
            NativeRuntimeInstaller(self.destination, {'install': str(self.executable)},
                                   self.source, self.commands.append)
        self.write('dest/METALOG', './ type=dir\n')
        installer = NativeRuntimeInstaller(self.destination, {'install': str(self.executable)},
            self.source, lambda command: SimpleNamespace(returncode=9))
        with self.assertRaises(SDKError): installer.directory(self.destination / 'new', 0o755)
