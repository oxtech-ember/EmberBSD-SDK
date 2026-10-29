# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Check where release producers import the common base C++ runtime."""

import unittest

from test_core import SDK_SOURCE
from emberbsd_sdk.os_release_builder import producer_commands


class ReleaseRuntimeSequenceTests(unittest.TestCase):
    def test_optional_rcs_source_reaches_release_and_set_checks(self):
        paths = {name: '/sdk/' + name for name in (
            'compiler', 'runtime-prefix', 'llvm-source', 'llvm-unwind', 'llvm-resource',
            'libedit-source', 'rcs-source', 'objects', 'tools', 'destination', 'os')}
        selection = {'format': 'emberbsd-host-prefix', 'version': 2,
                     'tools': {'cc': '/host/clang', 'cxx': '/host/clang++', 'gmake': '/host/gmake'},
                     'compiler': {'family': 'clang', 'triplet': 'aarch64-apple-darwin27.0.0'},
                     'abi_flags': {name: [] for name in ('cppflags', 'cflags', 'cxxflags', 'ldflags')}}
        flags, commands = producer_commands('aarch64', paths, {}, 'a' * 40, 1, '2',
                            '/host/sh', compiler_family='clang', base_cxx=True,
                            install_base_cxx=True, native_selection=selection)
        for flag in ('EMBER_RCS_SOURCE=/sdk/rcs-source', 'HOST_GMAKE=/host/gmake',
                     'EMBER_HOST_GNU_TRIPLE=aarch64-apple-darwin27.0.0'):
            self.assertIn(flag, flags)
            for command in commands:
                self.assertIn(flag, command)

    def test_optional_ekermit_source_reaches_release_and_set_checks(self):
        paths = {name: '/sdk/' + name for name in (
            'compiler', 'runtime-prefix', 'llvm-source', 'llvm-unwind', 'llvm-resource',
            'libedit-source', 'ekermit-source', 'objects', 'tools', 'destination', 'os')}
        selection = {'format': 'emberbsd-host-prefix', 'version': 2,
                     'tools': {'cc': '/host/clang', 'cxx': '/host/clang++'},
                     'compiler': {'family': 'clang', 'triplet': 'aarch64-apple-darwin27.0.0'},
                     'abi_flags': {name: [] for name in ('cppflags', 'cflags', 'cxxflags', 'ldflags')}}
        flags, commands = producer_commands('aarch64', paths, {}, 'a' * 40, 1, '2',
                            '/host/sh', compiler_family='clang', base_cxx=True,
                            install_base_cxx=True, native_selection=selection)
        self.assertIn('EMBER_EKERMIT_SOURCE=/sdk/ekermit-source', flags)
        for command in commands:
            self.assertIn('EMBER_EKERMIT_SOURCE=/sdk/ekermit-source', command)

    def test_optional_nvi_source_reaches_release_and_set_checks(self):
        paths = {name: '/sdk/' + name for name in (
            'compiler', 'runtime-prefix', 'llvm-source', 'llvm-unwind', 'llvm-resource',
            'libedit-source', 'nvi-source', 'objects', 'tools', 'destination', 'os')}
        selection = {'format': 'emberbsd-host-prefix', 'version': 2,
                     'tools': {'cc': '/host/clang', 'cxx': '/host/clang++'},
                     'compiler': {'family': 'clang', 'triplet': 'aarch64-apple-darwin27.0.0'},
                     'abi_flags': {name: [] for name in ('cppflags', 'cflags', 'cxxflags', 'ldflags')}}
        flags, commands = producer_commands('aarch64', paths, {}, 'a' * 40, 1, '2',
                            '/host/sh', compiler_family='clang', base_cxx=True,
                            install_base_cxx=True, native_selection=selection)
        self.assertIn('EMBER_NVI_SOURCE=/sdk/nvi-source', flags)
        for command in commands:
            self.assertIn('EMBER_NVI_SOURCE=/sdk/nvi-source', command)

    def test_optional_top_source_reaches_release_and_set_checks(self):
        paths = {name: '/sdk/' + name for name in (
            'compiler', 'runtime-prefix', 'llvm-source', 'llvm-unwind', 'llvm-resource',
            'libedit-source', 'top-source', 'objects', 'tools', 'destination', 'os')}
        selection = {'format': 'emberbsd-host-prefix', 'version': 2,
                     'tools': {'cc': '/host/clang', 'cxx': '/host/clang++'},
                     'compiler': {'family': 'clang', 'triplet': 'aarch64-apple-darwin27.0.0'},
                     'abi_flags': {name: [] for name in ('cppflags', 'cflags', 'cxxflags', 'ldflags')}}
        flags, commands = producer_commands('aarch64', paths, {}, 'a' * 40, 1, '2',
                            '/host/sh', compiler_family='clang', base_cxx=True,
                            install_base_cxx=True, native_selection=selection)
        self.assertIn('EMBER_TOP_SOURCE=/sdk/top-source', flags)
        for command in commands:
            self.assertIn('EMBER_TOP_SOURCE=/sdk/top-source', command)

    def test_ntp_sources_reach_release_and_set_checks(self):
        paths = {name: '/sdk/' + name for name in (
            'compiler', 'runtime-prefix', 'llvm-source', 'llvm-unwind', 'llvm-resource',
            'libedit-source', 'iscsi-source', 'ntp-source', 'libopts-source', 'objects', 'tools', 'destination', 'os')}
        selection = {'format': 'emberbsd-host-prefix', 'version': 2,
                     'tools': {'cc': '/host/clang', 'cxx': '/host/clang++'},
                     'compiler': {'family': 'clang', 'triplet': 'aarch64-apple-darwin27.0.0'},
                     'abi_flags': {name: [] for name in ('cppflags', 'cflags', 'cxxflags', 'ldflags')}}
        flags, commands = producer_commands('aarch64', paths, {}, 'a' * 40, 1, '2',
                            '/host/sh', compiler_family='clang', base_cxx=True,
                            install_base_cxx=True, native_selection=selection)
        self.assertIn('EMBER_ISCSI_SOURCE=/sdk/iscsi-source', flags)
        for command in commands:
            self.assertIn('EMBER_ISCSI_SOURCE=/sdk/iscsi-source', command)
        for flag in ('EMBER_NTP_SOURCE=/sdk/ntp-source', 'EMBER_LIBOPTS_SOURCE=/sdk/libopts-source'):
            self.assertIn(flag, flags)
            for command in commands:
                self.assertIn(flag, command)

    def test_optional_libpcap_source_reaches_release_and_set_checks(self):
        paths = {name: '/sdk/' + name for name in (
            'compiler', 'runtime-prefix', 'llvm-source', 'llvm-unwind', 'llvm-resource',
            'libedit-source', 'libpcap-source', 'objects', 'tools', 'destination', 'os')}
        selection = {'format': 'emberbsd-host-prefix', 'version': 2,
                     'tools': {'cc': '/host/clang', 'cxx': '/host/clang++'},
                     'compiler': {'family': 'clang', 'triplet': 'aarch64-apple-darwin27.0.0'},
                     'abi_flags': {name: [] for name in ('cppflags', 'cflags', 'cxxflags', 'ldflags')}}
        flags, commands = producer_commands('aarch64', paths, {}, 'a' * 40, 1, '2',
                            '/host/sh', compiler_family='clang', base_cxx=True,
                            install_base_cxx=True, native_selection=selection)
        self.assertIn('EMBER_LIBPCAP_SOURCE=/sdk/libpcap-source', flags)
        for command in commands:
            self.assertIn('EMBER_LIBPCAP_SOURCE=/sdk/libpcap-source', command)

    def test_optional_ppp_source_reaches_release_and_set_checks(self):
        paths = {name: '/sdk/' + name for name in (
            'compiler', 'runtime-prefix', 'llvm-source', 'llvm-unwind', 'llvm-resource',
            'libedit-source', 'ppp-source', 'objects', 'tools', 'destination', 'os')}
        selection = {'format': 'emberbsd-host-prefix', 'version': 2,
                     'tools': {'cc': '/host/clang', 'cxx': '/host/clang++'},
                     'compiler': {'family': 'clang', 'triplet': 'aarch64-apple-darwin27.0.0'},
                     'abi_flags': {name: [] for name in ('cppflags', 'cflags', 'cxxflags', 'ldflags')}}
        flags, commands = producer_commands('aarch64', paths, {}, 'a' * 40, 1, '2',
                            '/host/sh', compiler_family='clang', base_cxx=True,
                            install_base_cxx=True, native_selection=selection)
        self.assertIn('EMBER_PPP_SOURCE=/sdk/ppp-source', flags)
        for command in commands:
            self.assertIn('EMBER_PPP_SOURCE=/sdk/ppp-source', command)

    def test_optional_diffutils_source_reaches_release_and_set_checks(self):
        paths = {name: '/sdk/' + name for name in (
            'compiler', 'runtime-prefix', 'llvm-source', 'llvm-unwind', 'llvm-resource',
            'libedit-source', 'diffutils-source', 'objects', 'tools', 'destination', 'os')}
        selection = {'format': 'emberbsd-host-prefix', 'version': 2,
                     'tools': {'cc': '/host/clang', 'cxx': '/host/clang++', 'gmake': '/host/gmake'},
                     'compiler': {'family': 'clang', 'triplet': 'aarch64-apple-darwin27.0.0'},
                     'abi_flags': {name: [] for name in ('cppflags', 'cflags', 'cxxflags', 'ldflags')}}
        flags, commands = producer_commands('aarch64', paths, {}, 'a' * 40, 1, '2',
                            '/host/sh', compiler_family='clang', base_cxx=True,
                            install_base_cxx=True, native_selection=selection)
        for flag in ('EMBER_DIFFUTILS_SOURCE=/sdk/diffutils-source', 'HOST_GMAKE=/host/gmake',
                     'EMBER_HOST_GNU_TRIPLE=aarch64-apple-darwin27.0.0'):
            self.assertIn(flag, flags)
            for command in commands:
                self.assertIn(flag, command)

    def test_optional_tdb_source_reaches_release_and_set_checks(self):
        paths = {name: '/sdk/' + name for name in (
            'compiler', 'runtime-prefix', 'llvm-source', 'llvm-unwind', 'llvm-resource',
            'libedit-source', 'tdb-source', 'nvi-source', 'objects', 'tools', 'destination', 'os')}
        selection = {'format': 'emberbsd-host-prefix', 'version': 2,
                     'tools': {'cc': '/host/clang', 'cxx': '/host/clang++', 'python': '/host/python'},
                     'compiler': {'family': 'clang', 'triplet': 'aarch64-apple-darwin27.0.0'},
                     'abi_flags': {name: [] for name in ('cppflags', 'cflags', 'cxxflags', 'ldflags')}}
        flags, commands = producer_commands('aarch64', paths, {}, 'a' * 40, 1, '2',
                            '/host/sh', compiler_family='clang', base_cxx=True,
                            install_base_cxx=True, native_selection=selection)
        for flag in ('EMBER_TDB_SOURCE=/sdk/tdb-source', 'EMBER_NVI_SOURCE=/sdk/nvi-source',
                     'HOST_PYTHON=/host/python'):
            self.assertIn(flag, flags)
            for command in commands:
                self.assertIn(flag, command)

    def test_gettext_libtool_uses_final_foundation_and_tool_paths(self):
        from emberbsd_sdk.os_release_builder import gettext_libtool_command
        paths = {name: '/sdk/' + name for name in (
            'python', 'ports', 'sdk-source', 'host-prefix', 'compiler', 'tools',
            'destination', 'objects', 'llvm-runtime-stage')}
        command = gettext_libtool_command(paths, '/usr/bin/git', 'aarch64', '2', 'clang', 'clang')
        self.assertEqual(command[0], '/sdk/python')
        self.assertIn('/sdk/ports/profiles/common-build-tools/cross/prepare-libtool.py', command)
        self.assertEqual(command[command.index('--sysroot') + 1], '/sdk/destination/dest')
        self.assertEqual(command[command.index('--output') + 1], '/sdk/tools/gettext-libtool')
        self.assertEqual(command[command.index('--lock-directory') + 1], '/sdk')
        self.assertEqual(command[command.index('--runtime-stage') + 1], '/sdk/llvm-runtime-stage')

    def test_gettext_release_builds_foundation_before_distribution(self):
        paths = {name: '/sdk/' + name for name in (
            'compiler', 'runtime-prefix', 'llvm-source', 'llvm-unwind', 'llvm-resource',
            'libedit-source', 'gettext-source', 'libxml2-source', 'objects', 'tools', 'destination', 'os',
            'python', 'ports', 'sdk-source', 'host-prefix', 'git', 'llvm-runtime-stage')}
        roles = ('cc', 'cxx', 'gmake', 'pkg-config', 'od', 'msgfmt', 'msgmerge',
                 'xgettext', 'msginit', 'msgconv', 'msgfilter', 'sed')
        selection = {'format': 'emberbsd-host-prefix', 'version': 2,
                     'tools': {role: '/host/' + role for role in roles},
                     'compiler': {'family': 'clang', 'triplet': 'host-triplet'},
                     'abi_flags': {name: [] for name in ('cppflags', 'cflags', 'cxxflags', 'ldflags')}}
        _, commands = producer_commands('aarch64', paths, {}, 'a' * 40, 1, '2',
                            '/host/sh', compiler_family='clang', base_cxx=True,
                            install_base_cxx=True, native_selection=selection)
        self.assertEqual(commands[0][-2:], ['tools', 'kernel=EMBER64'])
        self.assertEqual(commands[1][-1], 'libs')
        self.assertIn('profiles/common-build-tools/cross/prepare-libtool.py', commands[2][2])
        self.assertEqual(commands[3][-1], 'distribution')

    def test_gettext_and_xml_reach_release_and_set_checks(self):
        paths = {name: '/sdk/' + name for name in (
            'compiler', 'runtime-prefix', 'llvm-source', 'llvm-unwind', 'llvm-resource',
            'libedit-source', 'gettext-source', 'libxml2-source', 'objects', 'tools', 'destination', 'os',
            'python', 'ports', 'sdk-source', 'host-prefix', 'git', 'llvm-runtime-stage')}
        roles = ('cc', 'cxx', 'gmake', 'pkg-config', 'od', 'msgfmt', 'msgmerge',
                 'xgettext', 'msginit', 'msgconv', 'msgfilter', 'sed')
        selection = {'format': 'emberbsd-host-prefix', 'version': 2,
                     'tools': {role: '/host/' + role for role in roles},
                     'compiler': {'family': 'clang', 'triplet': 'host-triplet'},
                     'abi_flags': {name: [] for name in ('cppflags', 'cflags', 'cxxflags', 'ldflags')}}
        flags, commands = producer_commands('aarch64', paths, {}, 'a' * 40, 1, '2',
                            '/host/sh', compiler_family='clang', base_cxx=True,
                            install_base_cxx=True, native_selection=selection)
        for flag in ('EMBER_GETTEXT_SOURCE=/sdk/gettext-source',
                     'EMBER_LIBXML2_SOURCE=/sdk/libxml2-source',
                     'HOST_OD=/host/od', 'HOST_PKG_CONFIG=/host/pkg-config',
                     'HOST_MSGINIT=/host/msginit',
                     'GNU_TARGET_LIBTOOL=/sdk/tools/bin/target-libtool',
                     'EMBER_TARGET_TOOL_DIR=/sdk/tools/gettext-libtool/current/target-tools'):
            self.assertIn(flag, flags)
            for command in commands:
                if command[0] != paths['python']:
                    self.assertIn(flag, command)

    def test_optional_grep_source_reaches_release_and_set_checks(self):
        paths = {name: '/sdk/' + name for name in (
            'compiler', 'runtime-prefix', 'llvm-source', 'llvm-unwind', 'llvm-resource',
            'libedit-source', 'grep-source', 'objects', 'tools', 'destination', 'os')}
        selection = {'format': 'emberbsd-host-prefix', 'version': 2,
                     'tools': {'cc': '/host/clang', 'cxx': '/host/clang++', 'gmake': '/host/gmake'},
                     'compiler': {'family': 'clang', 'triplet': 'aarch64-apple-darwin27.0.0'},
                     'abi_flags': {name: [] for name in ('cppflags', 'cflags', 'cxxflags', 'ldflags')}}
        flags, commands = producer_commands('aarch64', paths, {}, 'a' * 40, 1, '2',
                            '/host/sh', compiler_family='clang', base_cxx=True,
                            install_base_cxx=True, native_selection=selection)
        for flag in ('EMBER_GREP_SOURCE=/sdk/grep-source', 'HOST_GMAKE=/host/gmake',
                     'EMBER_HOST_GNU_TRIPLE=aarch64-apple-darwin27.0.0'):
            self.assertIn(flag, flags)
            for command in commands:
                self.assertIn(flag, command)

    def test_optional_texinfo_source_reaches_release_commands(self):
        paths = {name: '/sdk/' + name for name in (
            'compiler', 'runtime-prefix', 'llvm-source', 'llvm-unwind', 'llvm-resource',
            'libedit-source', 'texinfo-source', 'objects', 'tools', 'destination', 'os')}
        host = {'cc': {'path': '/sdk/host/bin/clang'}, 'c++': {'path': '/sdk/host/bin/clang++'}}
        _, commands = producer_commands('aarch64', paths, host, 'a' * 40, 1, '2',
                                        '/sdk/host/bin/sh', compiler_family='clang', base_cxx=True)
        for command in commands:
            self.assertIn('EMBER_TEXINFO_SOURCE=/sdk/texinfo-source', command)

    def test_texinfo_gnu_host_selection_reaches_release_and_set_checks(self):
        paths = {name: '/sdk/' + name for name in (
            'compiler', 'runtime-prefix', 'llvm-source', 'llvm-unwind', 'llvm-resource',
            'libedit-source', 'texinfo-source', 'objects', 'tools', 'destination', 'os')}
        selection = {'format': 'emberbsd-host-prefix', 'version': 2,
                     'tools': {'cc': '/host/clang', 'cxx': '/host/clang++', 'gmake': '/host/gmake'},
                     'compiler': {'family': 'clang', 'triplet': 'aarch64-apple-darwin27.0.0'},
                     'abi_flags': {name: [] for name in ('cppflags', 'cflags', 'cxxflags', 'ldflags')}}
        flags, commands = producer_commands('aarch64', paths, {}, 'a' * 40, 1, '2',
                            '/host/sh', compiler_family='clang', base_cxx=True,
                            install_base_cxx=True, native_selection=selection)
        for flag in ('HOST_GMAKE=/host/gmake', 'EMBER_HOST_GNU_TRIPLE=aarch64-apple-darwin27.0.0'):
            self.assertIn(flag, flags)
            for command in commands:
                self.assertIn(flag, command)

    def test_optional_byacc_source_reaches_release_commands(self):
        paths = {name: '/sdk/' + name for name in (
            'compiler', 'runtime-prefix', 'llvm-source', 'llvm-unwind', 'llvm-resource',
            'libedit-source', 'byacc-source', 'objects', 'tools', 'destination', 'os')}
        host = {'cc': {'path': '/sdk/host/bin/clang'}, 'c++': {'path': '/sdk/host/bin/clang++'}}
        _, commands = producer_commands('aarch64', paths, host, 'a' * 40, 1, '2',
                                        '/sdk/host/bin/sh', compiler_family='clang', base_cxx=True)
        for command in commands:
            self.assertIn('EMBER_BYACC_SOURCE=/sdk/byacc-source', command)

    def x11_flags(self, target='aarch64', family='clang', x11=True):
        paths = {name: '/sdk/' + name for name in (
            'compiler', 'runtime-prefix', 'llvm-source', 'llvm-unwind', 'llvm-resource',
            'libedit-source', 'objects', 'tools', 'destination', 'os')}
        if x11:
            paths['x11-source'] = '/sdk/xsrc'
        host = {'cc': {'path': '/sdk/host/bin/clang'}, 'c++': {'path': '/sdk/host/bin/clang++'}}
        return producer_commands(target, paths, host, 'a' * 40, 1, '2', '/sdk/host/bin/sh',
                                 compiler_family=family, base_cxx=family == 'clang')

    def test_x11_source_selects_x11_profile_and_sets(self):
        flags, commands = self.x11_flags()
        self.assertIn('MKX11=yes', flags)
        self.assertNotIn('MKX11=no', flags)
        self.assertIn('X11SRCDIR=/sdk/xsrc', flags)
        self.assertIn('MAKETARSETS=base comp xbase xcomp xetc xfont xserver', flags)
        for command in commands:
            if command[0] == '/sdk/host/bin/sh':
                self.assertIn('MKX11=yes', command)
                self.assertIn('X11SRCDIR=/sdk/xsrc', command)

    def test_default_profile_keeps_x11_disabled(self):
        flags, _ = self.x11_flags(x11=False)
        self.assertIn('MKX11=no', flags)
        self.assertNotIn('MKX11=yes', flags)
        self.assertFalse(any(flag.startswith('X11SRCDIR=') for flag in flags))
        self.assertIn('MAKETARSETS=base comp', flags)

    def test_x11_profile_requires_clang_aarch64(self):
        from emberbsd_sdk.errors import SDKError
        for target, family in (('amd64', 'clang'), ('aarch64', 'gcc')):
            with self.subTest(target=target, family=family), \
                    self.assertRaisesRegex(SDKError, 'X11 sets require'):
                self.x11_flags(target, family)

    def test_x11_source_must_match_os_pin(self):
        import json, tempfile
        from pathlib import Path
        from emberbsd_sdk.errors import SDKError
        from emberbsd_sdk.os_release_builder import check_x11_pin
        url = 'https://cdn.netbsd.org/pub/NetBSD/NetBSD-11.0/source/sets/xsrc.tgz'
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / 'ember').mkdir()
            (Path(root) / 'ember/source.json').write_text(json.dumps(
                {'x11SourceUrl': url, 'x11SourceSha512': 'a' * 128}))
            check_x11_pin(root, {'identity': {'url': url, 'sha512': 'a' * 128}})
            for identity in ({'url': url, 'sha512': 'b' * 128},
                             {'url': url + '.old', 'sha512': 'a' * 128}, {}):
                with self.subTest(identity=identity), self.assertRaisesRegex(SDKError, 'x11Source pin'):
                    check_x11_pin(root, {'identity': identity})

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

    def test_release_uses_bootstrap_libc_unwind_and_full_runtime(self):
        paths = {name: '/sdk/' + name for name in (
            'compiler', 'runtime-prefix', 'llvm-source', 'llvm-unwind', 'llvm-libc-unwind',
            'llvm-resource', 'libedit-source', 'objects', 'tools', 'destination', 'os')}
        host = {'cc': {'path': '/sdk/host/bin/clang'}, 'c++': {'path': '/sdk/host/bin/clang++'}}
        _, commands = producer_commands('aarch64', paths, host, 'a' * 40, 1, '2',
                                        '/sdk/host/bin/sh', compiler_family='clang', base_cxx=True)
        self.assertIn('EMBER_LIBUNWIND_ARCHIVE=/sdk/llvm-libc-unwind', commands[0])
        self.assertIn('EMBER_EXTERNAL_LLVM_RUNTIME=/sdk/runtime-prefix', commands[0])

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
