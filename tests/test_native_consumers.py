# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Pure metadata and refusal cases. No fixture is a qualified native provider."""

import copy
import contextlib
import io
import os
from pathlib import Path
import runpy
import sys
from unittest.mock import patch

from test_core import TemporaryCase, SDK_SOURCE
from test_native_toolset import Fixture
from emberbsd_sdk import jsonio, native_toolset
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.os_release_builder import (OSReleaseBuilder, check_native_command_identity,
                                            producer_commands)
from emberbsd_sdk.sysroot_builder import SysrootBuilder, native_host_flags


class NativeConsumerTests(TemporaryCase):
    def setUp(self):
        super().setUp()
        self.fixture = Fixture(self.root, bundle_name='native-bundle')
        for method, value in (('system', 'Linux'), ('machine', 'x86_64')):
            selected = patch('emberbsd_sdk.native_toolset.platform.' + method, return_value=value)
            selected.start(); self.addCleanup(selected.stop)

    def options(self):
        f = self.fixture
        return {'native-toolset': f.descriptor_ref['path'],
                'native-toolset-sha256': f.descriptor_ref['sha256'],
                'native-producer-receipt': f.receipt_ref['path'],
                'native-producer-receipt-sha256': f.receipt_ref['sha256'],
                'host-cc': f.records['cc']['path'], 'host-cxx': f.records['cxx']['path'],
                'python': f.records['python']['path']}

    def select(self, **changes):
        builder = SysrootBuilder(self.options() | changes)
        with patch.object(builder, 'capture', side_effect=AssertionError('compiler query')):
            builder.select_native_toolset()
        return builder

    def test_required_four_pins_before_any_query(self):
        for name in ('native-toolset', 'native-toolset-sha256',
                     'native-producer-receipt', 'native-producer-receipt-sha256'):
            options = self.options(); del options[name]
            builder = SysrootBuilder(options)
            with self.subTest(name=name), patch.object(builder, 'capture') as capture:
                with self.assertRaisesRegex(SDKError, '--' + name + ' is required'):
                    builder.select_native_toolset()
                capture.assert_not_called()

    def test_failed_qualification_never_queries_a_driver(self):
        with self.assertRaisesRegex(SDKError, 'SHA256 mismatch'):
            self.select(**{'native-toolset-sha256': '0' * 64})
        self.fixture.receipt['scope'] = 'bootstrap-seed'
        self.fixture.accept_receipt()
        with self.assertRaises(SDKError):
            self.select()

    def test_single_load_requires_direct_shell_and_python_roles(self):
        loader = native_toolset.load_native_toolset
        with patch.object(native_toolset, 'load_native_toolset', wraps=loader) as called:
            builder = self.select()
        self.assertEqual(called.call_count, 1)
        self.assertEqual(called.call_args.kwargs['required_roles'], ('cc', 'cxx', 'sh', 'python'))
        self.assertEqual(builder.shell, self.fixture.records['sh']['path'])
        self.assertEqual(builder.host_tools['cc']['origin'], 'accepted-native-toolset')
        changed = builder.native.as_dict(); changed['tools']['cc'] = '/changed'
        self.assertEqual(builder.native_selection['tools']['cc'], self.fixture.records['cc']['path'])

    def test_aliases_cannot_select_another_compiler_or_interpreter(self):
        other = self.root / 'other'; other.write_bytes(self.fixture.image); other.chmod(0o755)
        for option in ('host-cc', 'host-cxx', 'python'):
            with self.subTest(option=option), self.assertRaisesRegex(SDKError, 'differs from the qualified'):
                self.select(**{option: str(other)})

    def test_qualified_symlink_keeps_driver_spelling_and_offline_identity(self):
        selected = Path(self.fixture.records['cc']['path'])
        real = selected.with_name('cc-real')
        selected.rename(real); selected.symlink_to(real.name)
        self.fixture.seal()
        builder = self.select(**{'host-cc': str(real)})
        self.assertEqual(builder.host_tools['cc']['path'], str(real))
        self.assertEqual(builder.host_tools['cc']['selected'], str(selected))
        signature = dict(native_toolset=builder.native_selection, host_tools=builder.host_tools,
                         python={'path': builder.native.tool('python')})
        with patch('emberbsd_sdk.paths.canonical_existing', side_effect=AssertionError('offline filesystem read')):
            check_native_command_identity(signature, builder.shell)
        paths = {name: '/fixture/' + name for name in
                 ('os', 'compiler', 'runtime-prefix', 'libedit-source', 'objects', 'tools', 'destination')}
        flags, commands = producer_commands('aarch64', paths, builder.host_tools, 'a' * 40,
                                            1, '1', builder.shell, builder.native_selection)
        self.assertIn('HOST_CC=' + str(selected), flags)
        self.assertNotIn('HOST_CC=' + str(real), flags)
        self.assertEqual(commands[0][0], builder.native.tool('sh'))
        changed = copy.deepcopy(signature); changed['host_tools']['cc']['selected'] = str(real)
        with self.assertRaisesRegex(SDKError, 'Mixed native selection'):
            check_native_command_identity(changed, builder.shell)

    def test_abi_flags_are_native_only(self):
        selection = self.fixture.load().as_dict()
        selection['abi_flags'] = {'cppflags': ['-I', '/explicit/native headers'],
                                  'cflags': ['-O2'], 'cxxflags': ['-std=c++20'],
                                  'ldflags': ['-L/explicit/native/lib']}
        flags = native_host_flags(selection)
        self.assertIn("HOST_CPPFLAGS=-I '/explicit/native headers'", flags)
        self.assertEqual({word.split('=', 1)[0] for word in flags},
                         {'HOST_CPPFLAGS', 'HOST_CFLAGS', 'HOST_CXXFLAGS', 'HOST_LDFLAGS', 'HOST_SH'})
        self.assertFalse(any(word.startswith(('CFLAGS=', 'CPPFLAGS=', 'CXXFLAGS=', 'LDFLAGS=')) for word in flags))

    def test_accepted_version_suffix_preserves_complete_recorded_identity(self):
        version = '16.2.0+sdk.1'
        self.fixture.compiler['version'] = version
        self.fixture.records['cpp']['version'] = version
        for role in ('cc', 'cxx'):
            self.fixture.records[role]['version'] = version
            identity = self.fixture.receipt['checks']['identity'][role]
            identity['-dumpfullversion'] = self.fixture.command(
                [self.fixture.records[role]['path'], '-dumpfullversion'], (version + '\n').encode())
        self.fixture.seal()
        builder = self.select()
        signature = dict(native_toolset=builder.native_selection, host_tools=builder.host_tools,
                         python={'path': builder.native.tool('python')})
        check_native_command_identity(signature, builder.shell)
        self.assertEqual(builder.host_tools['cc']['gcc_version'], version)
        for mutation in ('family', 'version', 'machine'):
            changed = copy.deepcopy(signature)
            if mutation == 'family':
                changed['native_toolset']['compiler']['family'] = 'clang'
            elif mutation == 'version':
                changed['host_tools']['c++']['gcc_version'] = '16.2.0'
            else:
                changed['host_tools']['cc']['machine'] = 'aarch64--netbsd'
            with self.subTest(mutation=mutation), self.assertRaises(SDKError):
                check_native_command_identity(changed, builder.shell)

    def test_cache_refuses_changed_selection_and_old_missing_identity(self):
        root = self.root / 'objects'; root.mkdir()
        selected = self.fixture.load().as_dict()
        signature = {'native_toolset': selected}
        jsonio.write_json(root / SysrootBuilder.CONTEXT,
                          dict(format='emberbsd-sysroot-object-context', version=1,
                               signature=signature, sources={}))
        for field in ('descriptor', 'producer', 'abi', 'legacy'):
            builder = SysrootBuilder({}); builder.signature = copy.deepcopy(signature)
            if field == 'descriptor':
                builder.signature['native_toolset']['descriptor_sha256'] = 'f' * 64
            elif field == 'producer':
                builder.signature['native_toolset']['producer_receipt']['sha256'] = 'f' * 64
            elif field == 'abi':
                builder.signature['native_toolset']['abi_flags']['cflags'] = ['-O3']
            else:
                builder.signature = {}
            with self.subTest(field=field), self.assertRaisesRegex(SDKError, 'incompatible object context'):
                builder.inspect_context(str(root))

    def test_preflight_uses_only_declared_directories_before_inspection(self):
        builder = SysrootBuilder(self.options() | {'sdk': str(self.root), 'target': 'aarch64', 'jobs': '1'})
        with patch('emberbsd_sdk.workspace.inspect_layout', return_value={}), \
                patch('emberbsd_sdk.workspace.require_target'), patch('emberbsd_sdk.process.require_posix'), \
                patch.object(builder, 'find_command', return_value='/control/git') as find, \
                patch('emberbsd_sdk.process.SessionInspector', side_effect=SDKError('inspection boundary')):
            with self.assertRaisesRegex(SDKError, 'inspection boundary'):
                builder.preflight()
        find.assert_called_once_with('git')
        self.assertEqual(builder.environment['PATH'], str(self.fixture.bundle))

    def test_actual_sysroot_and_release_refuse_before_probe_or_producer(self):
        for cls in (SysrootBuilder, OSReleaseBuilder):
            builder = cls({'target': 'aarch64'})
            with self.subTest(builder=cls.__name__), \
                    patch.object(builder, 'preflight') as preflight, \
                    patch.object(builder, 'dispatch') as dispatch:
                with self.assertRaisesRegex(SDKError, 'nested native-tool routing is not qualified'):
                    builder.run()
                preflight.assert_not_called(); dispatch.assert_not_called()
        builder = SysrootBuilder({'plan': True})
        builder.receipt = {'qualification': 'not-built'}
        with patch.object(builder, 'preflight') as preflight:
            self.assertEqual(builder.run(), builder.receipt)
        preflight.assert_called_once_with()

    def test_revalidation_reuses_pins_and_refuses_mutation_before_dispatch(self):
        for changed_input in ('descriptor', 'receipt', 'tool', 'abi'):
            builder = self.select()
            f = self.fixture
            path = Path({'descriptor': f.descriptor_ref['path'], 'receipt': f.receipt_ref['path'],
                         'tool': f.records['cc']['path'],
                         'abi': str(Path(f.descriptor['host_abi']['headers'][0]['root']) / 'fixture-input')}[changed_input])
            original = path.read_bytes(); path.write_bytes(original + b'changed')
            loader = native_toolset.load_native_toolset
            with self.subTest(input=changed_input), \
                    patch.object(native_toolset, 'load_native_toolset', wraps=loader) as called, \
                    patch('emberbsd_sdk.process.OwnedProcessGroup', side_effect=AssertionError('producer started')):
                with self.assertRaises(SDKError): builder.dispatch(['never-run'])
                self.assertEqual(called.call_args.args, (self.options()['native-toolset'],))
                self.assertEqual(called.call_args.kwargs['descriptor_sha256'], self.options()['native-toolset-sha256'])
                self.assertEqual(called.call_args.kwargs['accepted_producer_receipt'], f.receipt_ref)
            path.write_bytes(original)

    def test_detected_drift_quarantines_contexts_even_after_restoring_bytes(self):
        builder = self.select()
        builder.paths = {name: str(self.root / name) for name in ('objects', 'tools')}
        for path in builder.paths.values(): Path(path).mkdir()
        builder.contexts_prepared = True
        tool = Path(self.fixture.records['cc']['path']); original = tool.read_bytes()
        tool.write_bytes(original + b'changed')
        with self.assertRaises(SDKError): builder.verify_native_toolset()
        tool.write_bytes(original)
        for path in builder.paths.values():
            self.assertTrue(Path(path, builder.QUARANTINE).is_file())
            with self.assertRaisesRegex(SDKError, 'quarantined'): SysrootBuilder({}).inspect_context(path)

    def test_native_path_separator_refused_before_queries(self):
        directory = self.root / 'colon-fixture'; directory.mkdir()
        self.fixture = Fixture(directory, bundle_name='native:tools')
        builder = SysrootBuilder(self.options() | {'sdk': str(self.root), 'target': 'aarch64', 'jobs': '1'})
        with patch('emberbsd_sdk.workspace.inspect_layout', return_value={}), \
                patch('emberbsd_sdk.workspace.require_target'), patch('emberbsd_sdk.process.require_posix'), \
                patch.object(builder, 'find_command', return_value='/control/git'), \
                patch.object(builder, 'capture', side_effect=AssertionError('compiler query')), \
                patch('emberbsd_sdk.process.SessionInspector', side_effect=AssertionError('late inspection')):
            with self.assertRaisesRegex(SDKError, 'contains the PATH separator'):
                builder.preflight()


class NativeConsumerCLITests(TemporaryCase):
    def test_each_cli_requires_each_native_pin(self):
        for name in ('build-sysroot.py', 'build-os-release.py'):
            entry = runpy.run_path(str(SDK_SOURCE / 'scripts' / name))['main']
            required = ['sdk', 'os' if name == 'build-sysroot.py' else 'os-source', 'ports',
                        'compiler', 'runtime-work', 'gcc-archive', 'pkgsrc', 'jobs', 'python',
                        'sdk-source', 'libedit-source', 'host-cc', 'host-cxx']
            required += ['target'] if name == 'build-sysroot.py' else ['profile', 'destination']
            pins = ['native-toolset', 'native-toolset-sha256',
                    'native-producer-receipt', 'native-producer-receipt-sha256']
            for missing in pins:
                argv = [name]
                for key in required + [key for key in pins if key != missing]:
                    argv += ['--' + key, 'aarch64-sysroot' if key == 'profile' else 'fixture']
                with self.subTest(cli=name, missing=missing), patch.object(sys, 'argv', argv), \
                        contextlib.redirect_stderr(io.StringIO()) as error:
                    with self.assertRaises(SystemExit) as failed:
                        entry()
                    self.assertEqual(failed.exception.code, 2)
                    self.assertIn('--' + missing, error.getvalue())
