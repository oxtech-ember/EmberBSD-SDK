# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Native check environments cannot inherit target compiler selection."""

import json
import importlib.util
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

from test_core import TemporaryCase, SDK_SOURCE
from test_host_clang import ClangFixture
from emberbsd_sdk import native_checks
from emberbsd_sdk.errors import SDKError, CommandError


class NativeCheckTests(TemporaryCase):
    def setUp(self):
        super().setUp()
        self.fixture = ClangFixture(self.root)
        self.native = self.fixture.load(('cc', 'cxx', 'cpp', 'sh', 'python', 'ar'))
        self.bindings = self.root / 'bindings'

    def test_environment_removes_ambient_target_and_build_search_inputs(self):
        ambient = dict(PATH='/target/bin:/usr/bin', CC='/target/gcc', CXX='/target/g++',
            CFLAGS='--sysroot=/target', CPPFLAGS='-I/target/include', LDFLAGS='-L/target/lib',
            SDKROOT='/foreign/sdk', CPATH='/foreign/include', LIBRARY_PATH='/foreign/lib',
            PYTHONPATH='/foreign/modules', HOME=str(self.root), USER='fixture')
        env = native_checks.check_environment(self.native, bindings=self.bindings, environment=ambient,
            extra_flags=dict(cflags=['-O1'], cppflags=['-DHOST_TEST=1']))
        for variable, role in (('CC','cc'), ('CXX','cxx'), ('CPP','cpp')):
            self.assertEqual(env[variable], str(self.bindings / role))
        self.assertEqual(env['AR'], self.native.tool('ar'))
        self.assertEqual(env['PYTHON'], self.native.tool('python'))
        self.assertEqual(env['SHELL'], self.native.tool('sh'))
        self.assertEqual(env['PATH'], str(self.bindings) + os.pathsep + self.native.path)
        self.assertEqual(os.readlink(self.bindings/'sh'), self.native.tool('sh'))
        self.assertEqual(os.readlink(self.bindings/'python3'), self.native.tool('python'))
        recorded = json.loads((self.bindings/'commands.json').read_text())
        self.assertEqual(recorded['flags']['cflags'], ['-O1'])
        self.assertEqual(recorded['flags']['cppflags'], ['-DHOST_TEST=1'])
        self.assertEqual(env['CFLAGS'], '')
        self.assertEqual(env['LDFLAGS'], '')
        for name in ('SDKROOT', 'CPATH', 'LIBRARY_PATH', 'PYTHONPATH'):
            self.assertNotIn(name, env)

    def test_selected_abi_flags_precede_explicit_extra_flags(self):
        native = SimpleNamespace(tools=self.native.tools, command=self.native.command,
            path=self.native.path, abi_flags=dict(self.native.abi_flags,
                cflags=('-isysroot', '/selected/sdk'), ldflags=('-Wl,-rpath,/selected/lib',)))
        env = native_checks.check_environment(native, bindings=self.bindings, extra_flags={'cflags':['-O2']})
        recorded = json.loads((self.bindings/'commands.json').read_text())
        self.assertEqual(recorded['flags']['cflags'], ['-isysroot', '/selected/sdk', '-O2'])
        self.assertEqual(recorded['flags']['ldflags'], ['-Wl,-rpath,/selected/lib'])
        self.assertEqual(env['LDFLAGS'], '')

    def test_non_flags_and_implicit_command_strings_are_rejected(self):
        for flags in ({'CC':['/foreign/cc']}, {'cflags':'-O2'}, {'cflags':['x\0y']}):
            with self.subTest(flags=flags), self.assertRaises(SDKError):
                native_checks.check_environment(self.native, bindings=self.bindings, extra_flags=flags)
        native = SimpleNamespace(tools=self.native.tools, command=lambda role: ('/cc','--extra'),
            path=self.native.path, abi_flags=self.native.abi_flags)
        with self.assertRaisesRegex(SDKError, 'single executable'):
            native_checks.check_environment(native, bindings=self.bindings)

    def test_runner_uses_selected_role_and_existing_process_cleanup(self):
        log = self.root / 'check.log'
        with patch('emberbsd_sdk.native_checks.process.run_logged') as run:
            native_checks.run_check(self.native, 'python', ['-B', '/owner/check.py'],
                cwd=str(self.root), log=str(log), bindings=self.bindings, environment={'CC':'/target/cc'})
        self.assertEqual(run.call_args.args, ([self.native.tool('python'), '-B', '/owner/check.py'], str(log)))
        self.assertEqual(run.call_args.kwargs['env']['CC'], str(self.bindings/'cc'))
        with self.assertRaises(SDKError):
            native_checks.run_check(self.native, 'missing', [], cwd=str(self.root), log=str(log), bindings=self.bindings)

    def test_actual_child_receives_clean_environment_and_failure_is_preserved(self):
        tools = dict(self.native.tools, python=sys.executable)
        native = SimpleNamespace(tools=tools, command=lambda role: (tools[role],),
            path=self.native.path, abi_flags=self.native.abi_flags)
        log = self.root / 'actual.log'
        script = "import os; print(os.environ['CC']); print(os.environ['CFLAGS']); raise SystemExit(7)"
        with self.assertRaises(CommandError) as error:
            native_checks.run_check(native, 'python', ['-c', script], cwd=str(self.root),
                log=str(log), bindings=self.bindings, environment={'HOME':str(self.root),'CC':'/target/cc'},
                extra_flags={'cflags':['-DHOST_ONLY']})
        self.assertEqual(error.exception.returncode, 7)
        self.assertEqual(log.read_text().splitlines(), [str(self.bindings/'cc'), ''])

    def test_nested_consumer_ignoring_standard_flags_still_gets_exact_abi_arguments(self):
        compiler = self.root / 'record-compiler'
        compiler.write_text('#!' + sys.executable + '\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n')
        compiler.chmod(0o755)
        tools = dict(self.native.tools, python=sys.executable, cc=str(compiler))
        native = SimpleNamespace(tools=tools, command=lambda role:(tools[role],),
            path=self.native.path, abi_flags=dict(self.native.abi_flags,
                cppflags=('-I','/selected include'), cflags=('-DNAME=two words',)))
        log = self.root / 'nested.log'
        # Models existing source contracts that call CC and never read CFLAGS.
        script = "import os,subprocess; subprocess.run([os.environ['CC'], '-c', 'input with space.c'], check=True)"
        native_checks.run_check(native, 'python', ['-c',script], cwd=str(self.root),
            log=str(log), bindings=self.bindings, environment={'CFLAGS':'--sysroot=/target'})
        self.assertEqual(json.loads(log.read_text()),
            ['-I','/selected include','-DNAME=two words','-c','input with space.c'])

    def test_direct_compiler_entry_uses_the_same_bound_abi_arguments(self):
        compiler = self.root / 'record-direct-compiler'
        compiler.write_text('#!' + sys.executable + '\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n')
        compiler.chmod(0o755)
        tools = dict(self.native.tools, python=sys.executable,
                     cc=str(compiler), cxx=str(compiler), cpp=str(compiler))
        native = SimpleNamespace(tools=tools, command=lambda role:(tools[role],),
            path=self.native.path, abi_flags=dict(self.native.abi_flags, cppflags=('-DHOST_ABI',)))
        for role in ('cc','cxx','cpp'):
            log = self.root / (role + '.log')
            native_checks.run_check(native, role, ['-c','x.c'], cwd=str(self.root),
                log=str(log), bindings=self.bindings)
            self.assertEqual(json.loads(log.read_text()), ['-DHOST_ABI','-c','x.c'])

    def test_optional_analyzers_are_selected_and_cli_forwards_family_and_roles(self):
        self.fixture.write('cppcheck', self.fixture.root.joinpath('bin/clang').read_bytes())
        native = self.fixture.load((*native_checks.REQUIRED_ROLES,'clang','cppcheck'))
        env = native_checks.check_environment(native, bindings=self.bindings)
        self.assertEqual(env['CSA'], str(self.bindings/'csa'))
        self.assertEqual(env['CPPCHECK'], native.tool('cppcheck'))
        spec = importlib.util.spec_from_file_location('native_check_cli', SDK_SOURCE/'scripts/run-native-check.py')
        cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)
        with patch.object(cli, 'load_host_prefix', return_value=native) as load, \
             patch.object(cli.native_checks, 'run_check', side_effect=CommandError(7, ['selected'])) as run:
            status = cli.main(['--host-prefix',str(self.fixture.root),'--compiler-family','gcc',
                '--required-role','cppcheck','--tool','python','--cwd',str(self.root),
                '--log',str(self.root/'cli.log'),'--bindings',str(self.bindings),
                '--cflag=-O1','--','-B','/owner/test.py'])
        self.assertEqual(status, 7)
        self.assertEqual(load.call_args.kwargs['compiler_family'], 'gcc')
        self.assertIn('cppcheck', load.call_args.kwargs['required_roles'])
        self.assertEqual(run.call_args.args[1:], ('python',['-B','/owner/test.py']))
        self.assertEqual(run.call_args.kwargs['extra_flags']['cflags'], ['-O1'])
