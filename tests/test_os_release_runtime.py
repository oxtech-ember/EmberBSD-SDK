# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Check where release producers import the common base C++ runtime."""

import unittest

from test_core import SDK_SOURCE
from emberbsd_sdk.os_release_builder import producer_commands


class ReleaseRuntimeSequenceTests(unittest.TestCase):
    def test_common_sources_reach_release_and_direct_make_commands(self):
        paths = {name: '/sdk/' + name for name in (
            'compiler', 'runtime-prefix', 'llvm-source', 'llvm-unwind', 'llvm-resource',
            'libedit-source', 'libarchive-source', 'libevent-source', 'objects', 'tools', 'destination', 'os')}
        host = {'cc': {'path': '/sdk/host/bin/clang'}, 'c++': {'path': '/sdk/host/bin/clang++'}}
        _, commands = producer_commands('aarch64', paths, host, 'a' * 40, 1, '2',
                                        '/sdk/host/bin/sh', compiler_family='clang', base_cxx=True)
        for command in commands:
            self.assertIn('EMBER_LIBARCHIVE_SOURCE=/sdk/libarchive-source', command)
            self.assertIn('EMBER_LIBEVENT_SOURCE=/sdk/libevent-source', command)

    def commands(self, target, base_cxx, **selection):
        paths = {name: '/sdk/' + name for name in (
            'compiler', 'runtime-prefix', 'llvm-source', 'llvm-unwind',
            'llvm-resource', 'libedit-source', 'objects', 'tools', 'destination', 'os')}
        host = {'cc': {'path': '/sdk/host/bin/clang'},
                'c++': {'path': '/sdk/host/bin/clang++'}}
        return producer_commands(target, paths, host, 'a' * 40, 1, '2',
                                 '/sdk/host/bin/sh', compiler_family='clang',
                                 base_cxx=base_cxx, **selection)[1]

    def test_aarch64_separates_distribution_from_runtime_sets(self):
        commands = self.commands('aarch64', True)
        self.assertEqual([command[-1] for command in commands],
                         ['kernel=EMBER64', 'distribution', 'checkflist', 'sets'])
        self.assertFalse(any('EMBER_SETS_SOURCE=' in word
                             for command in commands[:2] for word in command))
        for command in commands[2:]:
            self.assertIn('EMBER_SETS_SOURCE=/sdk/destination/llvm-sets', command)
        self.assertIn('/sdk/tools/bin/nbmake-evbarm', commands[2])
        self.assertIn('/sdk/os/distrib/sets', commands[2])

    def test_amd64_keeps_release_media_after_import(self):
        commands = self.commands('amd64', True)
        self.assertEqual([command[-1] for command in commands],
                         ['distribution', 'checkflist', 'release', 'install_image', 'iso-image'])
        self.assertEqual(commands[0][-2:], ['tools', 'distribution'])
        self.assertEqual(commands[2][:3],
                         ['/sdk/tools/bin/nbmake-amd64', '-C', '/sdk/os/etc'])
        self.assertFalse(any('EMBER_SETS_SOURCE=' in word for word in commands[0]))
        for command in commands[1:3]:
            self.assertIn('EMBER_SETS_SOURCE=/sdk/destination/llvm-sets', command)

    def test_existing_profiles_keep_their_combined_operations(self):
        aarch64 = self.commands('aarch64', False)
        self.assertEqual(aarch64[1][-2:], ['distribution', 'sets'])
        amd64 = self.commands('amd64', False)
        self.assertEqual(amd64[0][-2:], ['tools', 'release'])
        self.assertFalse(any('EMBER_SETS_SOURCE=' in word
                             for command in aarch64 + amd64 for word in command))

    def test_historical_schema4_selection_keeps_its_old_commands(self):
        for target, expected in (('aarch64', ['distribution', 'sets']),
                                 ('amd64', ['tools', 'release'])):
            commands = self.commands(target, True, install_base_cxx=False)
            self.assertEqual(commands[1 if target == 'aarch64' else 0][-2:], expected)
            self.assertFalse(any('EMBER_SETS_SOURCE=' in word
                                 for command in commands for word in command))
