# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Use an installed native prefix in sysroot and release consumers."""

import copy
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from test_core import TemporaryCase, SDK_SOURCE
from test_host_prefix import PrefixFixture
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.os_release_builder import OSReleaseBuilder, producer_commands
from emberbsd_sdk.sysroot_builder import (SysrootBuilder, native_host_flags,
                                          native_path, validate_native_binding)

CONTRACT = ('EmberBSD-native-tools 1\ncc cc HOST_CC\nc++ cxx HOST_CXX\n'
            'cmp cmp -\nmake make MAKE\nreadlink readlink -\nsh sh HOST_SH\n')


class NativeConsumerTests(TemporaryCase):
    def setUp(self):
        super().setUp()
        self.fixture = PrefixFixture(self.root)
        self.os = self.root / 'os'
        contract = self.os / 'tools/native-tools/contract'
        contract.parent.mkdir(parents=True)
        contract.write_text(CONTRACT)
        for name, value in (('system', 'Linux'), ('machine', 'x86_64')):
            selected = patch('emberbsd_sdk.host_prefix.platform.' + name, return_value=value)
            selected.start(); self.addCleanup(selected.stop)
        selected = patch('emberbsd_sdk.host_prefix.process.capture', side_effect=self.fixture.capture)
        selected.start(); self.addCleanup(selected.stop)

    def options(self):
        return {'host-prefix': str(self.fixture.root), 'os': str(self.os)}

    def select(self, cls=SysrootBuilder, **changes):
        builder = cls(self.options() | changes)
        builder.select_native_toolset()
        return builder

    def test_only_prefix_is_required_for_native_selection(self):
        builder = self.select()
        self.assertEqual(builder.host_tools['cc']['origin'], 'host-prefix')
        self.assertEqual(set(builder.native_inputs), {'host-prefix', 'host-cc', 'host-cxx', 'python'})
        self.assertEqual(builder.options['python'], builder.native.tool('python'))
        self.assertIn('make', builder.native.tools)
        self.assertFalse(any('receipt' in key or 'sha256' in key for key in builder.native_inputs))

    def test_missing_prefix_fails_before_a_query(self):
        with self.assertRaisesRegex(SDKError, '--host-prefix is required'):
            SysrootBuilder({'os': str(self.os)}).select_native_toolset()
        self.assertEqual(self.fixture.queries, [])

    def test_alias_cannot_override_selected_role(self):
        for option in ('host-cc', 'host-cxx', 'python'):
            with self.subTest(option=option), self.assertRaisesRegex(SDKError, 'differs from the native'):
                self.select(**{option: str(self.fixture.root / 'bin/ar')})

    def test_producer_loads_all_os_roles(self):
        with (self.os / 'tools/native-tools/contract').open('a') as stream:
            stream.write('yacc yacc HOST_YACC\n')
        with self.assertRaisesRegex(SDKError, 'missing native tool.*yacc'):
            self.select()

    def test_sysroot_and_release_do_not_have_unconditional_gate(self):
        for cls in (SysrootBuilder, OSReleaseBuilder):
            builder = cls(self.options() | {'target': 'aarch64'})
            with self.subTest(cls=cls), patch.object(builder, 'preflight', side_effect=SDKError('reached preflight')):
                with self.assertRaisesRegex(SDKError, '^reached preflight$'):
                    builder.run()

    def test_routing_publishes_real_selection_without_receipts(self):
        builder = self.select()
        tools = self.root / 'tools'; tools.mkdir()
        builder.paths = {'native-routing': str(tools / 'native-routing')}
        builder.prepare_native_routing()
        self.assertEqual(os.readlink(tools / 'native-routing/cc'), builder.native.tool('cc'))
        builder.prepare_native_routing()
        (tools / 'native-routing/cc').unlink()
        (tools / 'native-routing/cc').symlink_to(builder.native.tool('cxx'))
        with self.assertRaisesRegex(SDKError, 'saved native routing differs'):
            builder.prepare_native_routing()

    def test_release_commands_use_os_mapping_without_conflicting_overrides(self):
        builder = self.select()
        paths = {name: '/fixture/' + name for name in
                 ('os', 'compiler', 'runtime-prefix', 'libedit-source', 'objects', 'tools', 'destination')}
        flags, commands = producer_commands('aarch64', paths, builder.host_tools, 'a' * 40,
                                            1, '1', builder.shell, builder.native_selection)
        self.assertEqual(commands[0][0], builder.native.tool('sh'))
        self.assertFalse(any(word.startswith(('HOST_CC=', 'HOST_CXX=', 'HOST_SH=')) for word in flags))
        self.assertTrue(all(word.startswith('HOST_') for word in native_host_flags(builder.native_selection)))

    def test_saved_native_binding_uses_prefix_and_os_route(self):
        builder = self.select()
        paths = {name: '/fixture/' + name for name in
                 ('os', 'compiler', 'runtime-prefix', 'libedit-source', 'objects', 'tools', 'destination', 'native-routing')}
        paths.update({name: builder.native.tool(role) for name, role in
                      (('host-cc', 'cc'), ('host-cxx', 'cxx'), ('python', 'python'))})
        paths['host-prefix'] = builder.native.bundle_root
        flags, _ = producer_commands('aarch64', paths, builder.host_tools, 'a' * 40,
                                     1, '1', builder.shell, builder.native_selection)
        signature = dict(native_toolset=builder.native_selection, host_tools=builder.host_tools,
                         native_inputs=builder.native_inputs, python={'path': builder.native.tool('python')},
                         flags=flags, environment={'PATH': native_path(builder.native_selection),
                                                   'EMBER_NATIVE_TOOLS': paths['native-routing']})
        validate_native_binding(signature, paths)
        changed = copy.deepcopy(signature); changed['environment']['PATH'] += ':/usr/bin'
        with self.assertRaisesRegex(SDKError, 'PATH differs'):
            validate_native_binding(changed, paths)

    def test_cli_documents_prefix_without_qualification_options(self):
        for script in ('build-sysroot.py', 'build-os-release.py'):
            result = subprocess.run([sys.executable, '-B', str(SDK_SOURCE / 'scripts' / script), '--help'], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(b'--host-prefix', result.stdout)
            self.assertNotIn(b'--native-toolset', result.stdout)
            self.assertNotIn(b'--native-producer-receipt', result.stdout)
