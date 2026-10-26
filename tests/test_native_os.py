# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Native OS routing must remain separate from target compiler arguments."""

import importlib.util
import io
import os
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from test_core import TemporaryCase, SDK_SOURCE
from test_host_clang import ClangFixture
from emberbsd_sdk import native_os, native_os_routing
from emberbsd_sdk.errors import SDKError, CommandError

CONTRACT = (b'EmberBSD-native-tools 1\ncc cc HOST_CC\nc++ cxx HOST_CXX\n'
            b'cmp cmp -\nmake make MAKE\nreadlink readlink -\nsh sh HOST_SH\n')


class NativeOSRunnerTests(TemporaryCase):
    def setUp(self):
        super().setUp()
        self.fixture = ClangFixture(self.root)
        self.native = self.fixture.load(('cc', 'cxx', 'cpp', 'sh', 'python'))
        self.source = self.root/'os'
        (self.source/'tools/native-tools').mkdir(parents=True)
        self.contract = self.source/'tools/native-tools/contract'
        # Mapping-only tests reuse the selected executable fixture for roles
        # that are never executed. Host qualification is covered elsewhere.
        self.contract.write_bytes(CONTRACT.replace(b'cmp cmp', b'cmp cpp')
            .replace(b'make make', b'make cc').replace(b'readlink readlink', b'readlink cpp'))
        (self.source/'build.sh').write_text('exit 0\n')
        self.routing = self.root/'routing'

    def run_os(self, *arguments, **kwargs):
        return native_os.run_native_os(self.native, source=self.source,
            routing=self.routing, cwd=self.root, arguments=arguments, **kwargs)

    def test_environment_clears_target_flags_and_preserves_native_host_binding(self):
        with patch('emberbsd_sdk.native_os.process.run_logged') as run:
            self.run_os('-m', 'evbarm', '-V', 'EMBER_COMPILER_FAMILY=gcc', 'tools',
                build_sh=True, log=self.root/'build.log', append=True,
                environment={'CC':'/ambient/target', 'CXX':'/ambient/cxx', 'CFLAGS':'-target',
                             'HOST_CC':'/foreign/host', 'PATH':'/usr/bin', 'HOME':str(self.root)})
        argv = run.call_args.args[0]
        self.assertEqual(argv[:2], [self.native.tool('sh'), str(self.source/'build.sh')])
        self.assertIn('EMBER_COMPILER_FAMILY=gcc', argv)
        self.assertIn('EMBER_HOST_COMPILER_FAMILY=clang', argv)
        for flag in native_os_routing.native_host_flags(self.native.as_dict()):
            self.assertIn(flag, argv)
        env = run.call_args.kwargs['env']
        self.assertEqual(env['EMBER_NATIVE_TOOLS'], str(self.routing))
        self.assertEqual(env['EMBER_HOST_COMPILER_FAMILY'], 'clang')
        self.assertEqual(env['PATH'], str(self.routing)+os.pathsep+self.native.path)
        for name in ('CC','CXX','CPP','CFLAGS','CXXFLAGS','CPPFLAGS','LDFLAGS','HOST_CC'):
            self.assertNotIn(name, env)
        self.assertTrue(run.call_args.kwargs['append'])

    def test_capture_and_shell_use_existing_process_backend(self):
        script = self.root/'nbmake'; script.write_text('exit 0\n')
        with patch('emberbsd_sdk.native_os.process.capture') as capture:
            self.run_os(str(script), '-V', 'HOST_CC', shell=True, capture=True)
        self.assertEqual(capture.call_args.args[0], [self.native.tool('sh'), str(script), '-V', 'HOST_CC'])
        self.assertNotIn('CC', capture.call_args.kwargs['env'])
        with patch('emberbsd_sdk.native_os.process.capture') as capture:
            self.run_os(self.native.tool('cpp'), '-E', capture=True)
        self.assertEqual(capture.call_args.args[0], [self.native.tool('cpp'), '-E'])

    def test_exact_existing_routing_is_reused_but_changed_links_are_rejected(self):
        with patch('emberbsd_sdk.native_os.process.capture'):
            self.run_os(self.native.tool('cpp'), capture=True)
            inode = os.stat(self.routing).st_ino
            self.run_os(self.native.tool('cpp'), capture=True)
        self.assertEqual(os.stat(self.routing).st_ino, inode)
        (self.routing/'cc').unlink(); (self.routing/'cc').symlink_to(self.native.tool('cxx'))
        with self.assertRaisesRegex(SDKError, 'routing'):
            self.run_os(self.native.tool('cpp'), capture=True)

    def test_build_sh_cannot_override_selected_host_variables(self):
        for args in (('-V','HOST_CC=/foreign'), ('-VHOST_CFLAGS=-bad',),
                     ('-V','EMBER_HOST_COMPILER_FAMILY=gcc'), ('-V','PATH=/usr/bin')):
            with self.subTest(args=args), self.assertRaisesRegex(SDKError, 'selected native'):
                self.run_os(*args, 'tools', build_sh=True, log=self.root/'build.log')

    def test_build_sh_requires_logged_process_drainage(self):
        with self.assertRaisesRegex(SDKError, 'build.sh producers require a log'):
            self.run_os('-m', 'amd64', 'tools', build_sh=True, capture=True)

    def test_loader_uses_all_contract_roles_and_explicit_family(self):
        with patch('emberbsd_sdk.native_os.load_host_prefix', return_value=self.native) as load:
            native_os.select_native_os_tools('/selected', self.source, compiler_family='gcc')
        self.assertEqual(load.call_args.kwargs['compiler_family'], 'gcc')
        self.assertEqual(set(load.call_args.kwargs['required_roles']), {'cc','cxx','cpp','sh','python'})

    def test_cli_forwards_modes_and_preserves_capture_bytes(self):
        spec = importlib.util.spec_from_file_location('run_native_os', SDK_SOURCE/'scripts/run-native-os.py')
        cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)
        with patch.object(cli.native_os, 'select_native_os_tools', return_value=self.native), \
                patch.object(cli.native_os, 'run_native_os') as run:
            result = cli.main(['--host-prefix','/selected','--compiler-family','gcc',
                '--source',str(self.source),'--routing',str(self.routing),'--cwd',str(self.root),
                '--log',str(self.root/'build.log'),'--append-log','--build-sh','--','-m','amd64','tools'])
        self.assertEqual(result, 0)
        self.assertTrue(run.call_args.kwargs['build_sh'])
        self.assertTrue(run.call_args.kwargs['append'])
        self.assertEqual(run.call_args.kwargs['arguments'], ['-m','amd64','tools'])

    def test_cli_capture_preserves_stdout_stderr_and_exit_status(self):
        spec = importlib.util.spec_from_file_location('run_native_os_capture', SDK_SOURCE/'scripts/run-native-os.py')
        cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)
        args = ['--host-prefix','/selected','--source',str(self.source),
            '--routing',str(self.routing),'--cwd',str(self.root),'--capture','--',self.native.tool('cpp')]
        for failure in (False, True):
            with self.subTest(failure=failure):
                output, errors = io.BytesIO(), io.BytesIO()
                stdout = io.TextIOWrapper(output, encoding='utf-8', write_through=True)
                stderr = io.TextIOWrapper(errors, encoding='utf-8', write_through=True)
                result = SimpleNamespace(stdout=b'value\x00\n', stderr=b'diagnostic\n')
                effect = CommandError(7, ['/selected'], result.stdout, result.stderr) if failure else None
                with patch.object(cli.native_os, 'select_native_os_tools', return_value=self.native), \
                        patch.object(cli.native_os, 'run_native_os', return_value=result, side_effect=effect), \
                        patch.object(cli.sys, 'stdout', stdout), patch.object(cli.sys, 'stderr', stderr):
                    status = cli.main(args)
                self.assertEqual(status, 7 if failure else 0)
                self.assertEqual(output.getvalue(), result.stdout)
                self.assertTrue(errors.getvalue().startswith(result.stderr))
