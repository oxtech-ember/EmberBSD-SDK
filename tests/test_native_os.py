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

CONTRACT = (b'EmberBSD-native-tools 2\ncc cc HOST_CC\nc++ cxx HOST_CXX\n'
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

    def test_native_abi_flags_use_separate_make_variables(self):
        flags = native_os_routing.native_host_flags(self.native.as_dict())
        names = {flag.split('=', 1)[0] for flag in flags}
        self.assertTrue(names <= {
            'EMBER_HOST_ABI_CPPFLAGS', 'EMBER_HOST_ABI_CFLAGS',
            'EMBER_HOST_ABI_CXXFLAGS', 'EMBER_HOST_ABI_LDFLAGS',
            'EMBER_HOST_COMPILER_FAMILY', 'HOST_SH'})
        self.assertTrue({
            'EMBER_HOST_ABI_CPPFLAGS', 'EMBER_HOST_ABI_CFLAGS',
            'EMBER_HOST_ABI_CXXFLAGS', 'EMBER_HOST_ABI_LDFLAGS'} <= names)
        env = native_os.native_os_environment(self.native, self.routing)
        self.assertTrue(all(flag.split('=', 1)[0] in env for flag in flags))
        self.assertFalse(any(name in env for name in (
            'HOST_CPPFLAGS', 'HOST_CFLAGS', 'HOST_CXXFLAGS', 'HOST_LDFLAGS')))

    def test_old_contract_cannot_silently_drop_native_abi_flags(self):
        self.contract.write_bytes(CONTRACT.replace(b'tools 2', b'tools 1'))
        with self.assertRaisesRegex(SDKError, 'unsupported OS native-tool contract'):
            self.run_os(self.native.tool('cpp'), capture=True)

    def test_environment_clears_target_flags_and_preserves_native_host_binding(self):
        with patch('emberbsd_sdk.native_os.process.run_logged') as run:
            self.run_os('-m', 'evbarm', '-V', 'EMBER_COMPILER_FAMILY=gcc', 'tools',
                build_sh=True, log=self.root/'build.log', append=True,
                environment={'CC':'/ambient/target', 'CXX':'/ambient/cxx', 'CFLAGS':'-target',
                             'HOST_CC':'/foreign/host', 'HOST_SH':'/foreign/sh',
                             'PATH':'/usr/bin', 'HOME':str(self.root)})
        argv = run.call_args.args[0]
        self.assertEqual(argv[:2], [self.native.tool('sh'), str(self.source/'build.sh')])
        self.assertIn('EMBER_COMPILER_FAMILY=gcc', argv)
        self.assertIn('EMBER_HOST_COMPILER_FAMILY=clang', argv)
        for flag in native_os_routing.native_host_flags(self.native.as_dict()):
            self.assertIn(flag, argv)
        env = run.call_args.kwargs['env']
        self.assertEqual(env['EMBER_NATIVE_TOOLS'], str(self.routing))
        self.assertEqual(env['EMBER_HOST_COMPILER_FAMILY'], 'clang')
        self.assertEqual(env.get('HOST_SH'), self.native.tool('sh'))
        self.assertEqual(env['PATH'], str(self.routing)+os.pathsep+self.native.path)
        for name in ('CC','CXX','CPP','CFLAGS','CXXFLAGS','CPPFLAGS','LDFLAGS','HOST_CC'):
            self.assertNotIn(name, env)
        self.assertTrue(run.call_args.kwargs['append'])

    def test_gnu_distribution_selects_host_generators_and_owned_target_tools(self):
        original = self.native
        extra = {name: str(self.fixture.write(name.replace('pkg-config', 'pkgconf'), b'tool'))
                 for name in ('gmake', 'pkg-config', 'od', 'msgfmt', 'msgmerge',
                              'xgettext', 'msginit', 'msgconv', 'msgfilter', 'sed')}
        selection = original.as_dict()
        selection['tools'] = dict(selection['tools'], **extra)
        self.native = SimpleNamespace(path=original.path, as_dict=lambda: selection,
            tool=lambda role: extra[role] if role in extra else original.tool(role))
        tooldir = self.root/'os-tools'
        target = tooldir/'gettext-libtool/context/target-tools'
        (target/'bin').mkdir(parents=True)
        libtool = tooldir/'gettext-libtool/context/bin/target-libtool'
        libtool.parent.mkdir(parents=True)
        libtool.write_text('tool'); libtool.chmod(0o755)
        (target/'bin/target-libtool').symlink_to(libtool)
        with patch('emberbsd_sdk.native_os.native_os_routing.ensure_native_os_routing'), \
             patch('emberbsd_sdk.native_os.process.run_logged') as run:
            self.run_os('-T', str(tooldir), 'distribution', build_sh=True, log=self.root/'build.log',
                        target_tools=str(target), target_libtool=str(libtool))
        argv = run.call_args.args[0]
        for flag in native_os_routing.gnu_target_host_flags(selection) + native_os_routing.gettext_target_host_flags(selection):
            self.assertIn(flag, argv)
        self.assertIn('GNU_TARGET_LIBTOOL=' + str(libtool), argv)
        self.assertIn('EMBER_TARGET_TOOL_DIR=' + str(target), argv)
        self.assertIn('EMBER_HOST_GNU_TRIPLE=' + selection['compiler']['triplet'], argv)
        unsafe = tooldir / "target'unsafe"
        (unsafe/'bin').mkdir(parents=True)
        (unsafe/'bin/target-libtool').symlink_to(libtool)
        with patch('emberbsd_sdk.native_os.native_os_routing.ensure_native_os_routing'), \
             self.assertRaisesRegex(SDKError, 'target tools'):
            self.run_os('-T', str(tooldir), 'distribution', build_sh=True, log=self.root/'build.log',
                        target_tools=str(unsafe), target_libtool=str(libtool))
        with patch('emberbsd_sdk.native_os.native_os_routing.ensure_native_os_routing'), \
             patch('emberbsd_sdk.native_os.process.run_logged') as clustered:
            self.run_os('-uT' + str(tooldir), 'distribution', build_sh=True, log=self.root/'build.log',
                        target_tools=str(target), target_libtool=str(libtool))
        self.assertIn('EMBER_TARGET_TOOL_DIR=' + str(target), clustered.call_args.args[0])
        for late in (('--', '-T', str(tooldir), 'distribution'),
                     ('distribution', '-T', str(tooldir))):
            with self.subTest(late=late), \
                 patch('emberbsd_sdk.native_os.native_os_routing.ensure_native_os_routing'), \
                 self.assertRaisesRegex(SDKError, 'TOOLDIR'):
                self.run_os(*late, build_sh=True, log=self.root/'build.log',
                            target_tools=str(target), target_libtool=str(libtool))
        old_msgfmt = selection['tools']['msgfmt']
        selection['tools']['msgfmt'] = str(self.root / "host'unsafe/msgfmt")
        with patch('emberbsd_sdk.native_os.native_os_routing.ensure_native_os_routing'), \
             self.assertRaisesRegex(SDKError, 'safe selected host paths'):
            self.run_os('-T', str(tooldir), 'distribution', build_sh=True, log=self.root/'build.log',
                        target_tools=str(target), target_libtool=str(libtool))
        selection['tools']['msgfmt'] = old_msgfmt
        shell_meta = tooldir / 'target&unsafe'
        (shell_meta/'bin').mkdir(parents=True)
        (shell_meta/'bin/target-libtool').symlink_to(libtool)
        with patch('emberbsd_sdk.native_os.native_os_routing.ensure_native_os_routing'), \
             self.assertRaisesRegex(SDKError, 'safe absolute paths'):
            self.run_os('-T', str(tooldir), 'distribution', build_sh=True, log=self.root/'build.log',
                        target_tools=str(shell_meta), target_libtool=str(libtool))
        foreign = self.root/'foreign'
        foreign.mkdir()
        with patch('emberbsd_sdk.native_os.native_os_routing.ensure_native_os_routing'), \
             self.assertRaisesRegex(SDKError, 'target tools'):
            self.run_os('-T', str(tooldir), 'distribution', build_sh=True, log=self.root/'build.log',
                        target_tools=str(foreign), target_libtool=str(libtool))
        with patch('emberbsd_sdk.native_os.native_os_routing.ensure_native_os_routing'), \
             self.assertRaisesRegex(SDKError, 'selected native'):
            self.run_os('-T', str(tooldir), '-V', 'HOST_MSGFMT=/foreign', 'distribution',
                        build_sh=True, log=self.root/'build.log',
                        target_tools=str(target), target_libtool=str(libtool))

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
        for args in (('-V','HOST_CC=/foreign'), ('-VHOST_CFLAGS=-bad',), ('-ZHOST_MSGFMT',),
                     ('-V', 'HOST_SH=/foreign/sh'),
                     ('-V','EMBER_HOST_ABI_CPPFLAGS=-bad'),
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

    def test_gnu_loader_requires_all_selected_generators(self):
        with patch('emberbsd_sdk.native_os.load_host_prefix', return_value=self.native) as load:
            native_os.select_native_os_tools('/selected', self.source, gnu_target=True)
        expected = {'gmake'} | {role for _, role in native_os_routing.GETTEXT_TARGET_ROLES}
        self.assertTrue(expected <= set(load.call_args.kwargs['required_roles']))

    def test_cli_forwards_explicit_gnu_target_selection(self):
        spec = importlib.util.spec_from_file_location('run_native_os_gnu', SDK_SOURCE/'scripts/run-native-os.py')
        cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)
        with patch.object(cli.native_os, 'select_native_os_tools', return_value=self.native) as select, \
             patch.object(cli.native_os, 'run_native_os') as run:
            result = cli.main(['--host-prefix','/selected','--source',str(self.source),
                '--routing',str(self.routing),'--cwd',str(self.root),'--log',str(self.root/'build.log'),
                '--target-tools','/os-tools/target','--target-libtool','/os-tools/libtool',
                '--build-sh','--','-T','/os-tools','distribution'])
        self.assertEqual(result, 0)
        self.assertTrue(select.call_args.kwargs['gnu_target'])
        self.assertEqual(run.call_args.kwargs['target_tools'], '/os-tools/target')
        self.assertEqual(run.call_args.kwargs['target_libtool'], '/os-tools/libtool')

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
