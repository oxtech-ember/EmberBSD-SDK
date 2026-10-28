# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""LLVM first-base dependency ordering; synthetic outputs are not ABI evidence."""

from pathlib import Path
import json
import copy
import contextlib
import io
import os
import runpy
import shutil
import sys
from unittest.mock import patch

from test_core import TemporaryCase, SDK_SOURCE
from emberbsd_sdk import inventory, jsonio
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.sysroot_builder import SysrootBuilder
from emberbsd_sdk.os_release_builder import OSReleaseBuilder
from emberbsd_sdk.llvm_sysroot import runtime_paths, llvm_flags, LLVMCoordinator, LLVMPhaseBuilder
from emberbsd_sdk.libedit import prepare_source
from emberbsd_sdk.target_toolchain import CLANG_BINARIES
from emberbsd_sdk import sysroot_transfer
from test_builder import B, fixture, write, git, commit
import test_llvm_runtime as runtime_tests


class ClangBuilder(B):
    """Dispatch only: synthetic driver identity is not compiler qualification."""
    def compiler(self):
        prefix = self.paths['compiler']
        names = {'cc': 'clang', 'cxx': 'clang++', 'cpp': 'clang', 'as': 'clang', 'ld': 'ld.lld'}
        binaries, commands = {}, {}
        for role in CLANG_BINARIES:
            path = prefix + '/bin/' + names.get(role, 'llvm-' + role)
            binaries[role] = dict(path=path, selected_path=path, sha256='f' * 64)
            commands[role] = [path]
        for role in ('cc', 'cxx'):
            commands[role] += ['--target=aarch64--netbsd', '--ld-path=' + binaries['ld']['path']]
        commands['cpp'] += ['-E', '--target=aarch64--netbsd']
        commands['as'] += ['--target=aarch64--netbsd', '-c', '-x', 'assembler']
        return dict(family='clang', version='23.1.4', triplet='aarch64--netbsd',
                    reported_triplet='aarch64-unknown-netbsd', resource_dir=prefix + '/lib/clang/23',
                    binaries=binaries, commands=commands)


class LLVMBuilderTests(TemporaryCase):
    def test_runtime_work_cli_plan_with_and_without_selected_stage(self):
        options = fixture(self.root)
        write(Path(options['os']) / 'share/mk/bsd.own.mk', 'EMBER_VERSION?= 1.0\n')
        commit(options['os'])
        builder, _ = self.builder(options)
        original = dict(builder.options)
        original.pop('runtime-work')
        original.pop('gcc-archive', None)
        original.pop('pkgsrc', None)
        original.pop('base-cxx', None)

        class FixtureRelease(OSReleaseBuilder, ClangBuilder):
            pass

        for script_name, module, fixture_class in (
                ('build-sysroot.py', 'emberbsd_sdk.sysroot_builder.SysrootBuilder', ClangBuilder),
                ('build-os-release.py', 'emberbsd_sdk.os_release_builder.OSReleaseBuilder', FixtureRelease)):
            for family, stage, expected in (('clang', True, 0), ('clang', False, 1), ('gcc', True, 1)):
                with self.subTest(script=script_name, family=family, stage=stage):
                    values = dict(original, **{'compiler-family': family})
                    if not stage:
                        values.pop('llvm-runtime-stage')
                        values.pop('llvm-bootstrap-stage')
                    if script_name == 'build-os-release.py':
                        values['os-source'] = values.pop('os')
                        values.pop('target')
                        values['profile'] = 'aarch64-sysroot'
                    argv = [str(SDK_SOURCE / 'scripts' / script_name)]
                    for key, value in values.items():
                        argv.extend(['--' + key, str(value)])
                    argv.append('--plan')
                    stdout, stderr = io.StringIO(), io.StringIO()
                    with patch(module, fixture_class), patch.object(sys, 'argv', argv), \
                            contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
                            self.assertRaises(SystemExit) as result:
                        runpy.run_path(argv[0], run_name='__main__')
                    self.assertEqual(result.exception.code, expected, stderr.getvalue())
                    if expected:
                        self.assertIn('--runtime-work', stderr.getvalue())
                    else:
                        self.assertEqual(json.loads(stdout.getvalue())['qualification'], 'not-built')

    def test_byacc_source_requires_supported_os_producer(self):
        builder = SysrootBuilder({})
        builder.paths = {'os': str(self.root / 'os'), 'byacc-source': '/prepared/byacc',
                         'tools': '/build/tools'}
        builder.target = {'machine': 'evbarm', 'arch': 'aarch64'}
        builder.environment = {}
        from types import SimpleNamespace
        builder.native = SimpleNamespace(tool=lambda name: '/host/bmake')
        answer = lambda value: SimpleNamespace(stdout=value.encode())
        with patch('emberbsd_sdk.sysroot_builder.process.capture',
                   side_effect=[answer('/prepared/byacc'), answer('/old/dist')]):
            with self.assertRaisesRegex(SDKError, 'does not support EMBER_BYACC_SOURCE'):
                builder.require_byacc_source_support()
        with patch('emberbsd_sdk.sysroot_builder.process.capture',
                   side_effect=[answer('/prepared/byacc'), answer('/prepared/byacc')]) as run:
            builder.require_byacc_source_support()
        self.assertEqual(run.call_count, 2)
        self.assertIn('/os/external/bsd/byacc/bin', run.call_args_list[0].args[0][5])
        self.assertIn('/os/tools/yacc', run.call_args_list[1].args[0][5])
        del builder.paths['byacc-source']
        with patch('emberbsd_sdk.sysroot_builder.process.capture') as run:
            builder.require_byacc_source_support()
            run.assert_not_called()

    def test_ekermit_source_requires_real_os_producer(self):
        from types import SimpleNamespace
        builder = SysrootBuilder({})
        builder.paths = {'os': str(self.root / 'os'), 'ekermit-source': '/prepared/ekermit', 'tools': '/build/tools'}
        builder.target = {'machine': 'evbarm', 'arch': 'aarch64'}
        builder.environment = {}
        builder.native = SimpleNamespace(tool=lambda name: '/host/' + name)
        with patch('emberbsd_sdk.sysroot_builder.process.capture', return_value=SimpleNamespace(stdout=b'/old/dist\n')):
            with self.assertRaisesRegex(SDKError, 'does not support EMBER_EKERMIT_SOURCE'):
                builder.require_ekermit_source_support()
        with patch('emberbsd_sdk.sysroot_builder.process.capture', return_value=SimpleNamespace(stdout=b'/prepared/ekermit\n')) as run:
            builder.require_ekermit_source_support()
        self.assertIn('/os/external/bsd/ekermit/bin/ekermit', run.call_args.args[0][5])
        del builder.paths['ekermit-source']
        with patch('emberbsd_sdk.sysroot_builder.process.capture') as run:
            builder.require_ekermit_source_support()
            run.assert_not_called()

    def test_optional_ekermit_source_verified_by_owner(self):
        from types import SimpleNamespace
        builder = SysrootBuilder({})
        builder.paths = {'python': '/host/python', 'ports': '/ports', 'sdk-source': '/sdk', 'ekermit-source': '/prepared/ekermit'}
        builder.git_command = '/host/git'
        builder.environment = {}
        record = {'format': 'emberbsd-prepared-source', 'version': 1, 'package': 'comms/ekermit'}
        with patch('emberbsd_sdk.sysroot_builder.process.capture', return_value=SimpleNamespace(stdout=json.dumps(record))) as run:
            self.assertEqual(builder.verify_common_sources(), {'ekermit': record})
        self.assertIn('/ports/scripts/source-inputs/ekermit/source.py', run.call_args.args[0])

    def test_rcs_source_requires_real_os_producer(self):
        from types import SimpleNamespace
        builder = SysrootBuilder({})
        builder.paths = {'os': str(self.root / 'os'), 'rcs-source': '/prepared/rcs', 'tools': '/build/tools'}
        builder.target = {'machine': 'evbarm', 'arch': 'aarch64'}
        builder.environment = {}
        builder.native = SimpleNamespace(tool=lambda name: '/host/' + name,
                                         compiler={'triplet': 'aarch64-apple-darwin27.0.0'})
        with patch('emberbsd_sdk.sysroot_builder.process.capture',
                   return_value=SimpleNamespace(stdout=b'/old/dist\n')):
            with self.assertRaisesRegex(SDKError, 'does not support EMBER_RCS_SOURCE'):
                builder.require_rcs_source_support()
        with patch('emberbsd_sdk.sysroot_builder.process.capture',
                   return_value=SimpleNamespace(stdout=b'/prepared/rcs\n')) as run:
            builder.require_rcs_source_support()
        self.assertIn('/os/external/gpl2/rcs/gnu', run.call_args.args[0][5])
        self.assertIn('HOST_GMAKE=/host/gmake', run.call_args.args[0])
        del builder.paths['rcs-source']
        with patch('emberbsd_sdk.sysroot_builder.process.capture') as run:
            builder.require_rcs_source_support()
            run.assert_not_called()

    def test_optional_rcs_source_verified_by_owner(self):
        from types import SimpleNamespace
        builder = SysrootBuilder({})
        builder.paths = {'python': '/host/python', 'ports': '/ports', 'sdk-source': '/sdk', 'rcs-source': '/prepared/rcs'}
        builder.git_command = '/host/git'
        builder.environment = {}
        record = {'format': 'emberbsd-prepared-source', 'version': 1, 'package': 'devel/rcs'}
        with patch('emberbsd_sdk.sysroot_builder.process.capture',
                   return_value=SimpleNamespace(stdout=json.dumps(record))) as run:
            self.assertEqual(builder.verify_common_sources(), {'rcs': record})
        self.assertIn('/ports/scripts/source-inputs/rcs/source.py', run.call_args.args[0])

    def test_diffutils_source_requires_real_os_producer(self):
        from types import SimpleNamespace
        builder = SysrootBuilder({})
        builder.paths = {'os': str(self.root / 'os'), 'diffutils-source': '/prepared/diffutils', 'tools': '/build/tools'}
        builder.target = {'machine': 'evbarm', 'arch': 'aarch64'}
        builder.environment = {}
        builder.native = SimpleNamespace(tool=lambda name: '/host/' + name,
                                         compiler={'triplet': 'aarch64-apple-darwin27.0.0'})
        with patch('emberbsd_sdk.sysroot_builder.process.capture',
                   return_value=SimpleNamespace(stdout=b'/old/dist\n')):
            with self.assertRaisesRegex(SDKError, 'does not support EMBER_DIFFUTILS_SOURCE'):
                builder.require_diffutils_source_support()
        with patch('emberbsd_sdk.sysroot_builder.process.capture',
                   return_value=SimpleNamespace(stdout=b'/prepared/diffutils\n')) as run:
            builder.require_diffutils_source_support()
        self.assertIn('/os/external/gpl2/diffutils/gnu', run.call_args.args[0][5])
        self.assertIn('HOST_GMAKE=/host/gmake', run.call_args.args[0])
        del builder.paths['diffutils-source']
        with patch('emberbsd_sdk.sysroot_builder.process.capture') as run:
            builder.require_diffutils_source_support()
            run.assert_not_called()

    def test_optional_diffutils_source_verified_by_owner(self):
        from types import SimpleNamespace
        builder = SysrootBuilder({})
        builder.paths = {'python': '/host/python', 'ports': '/ports', 'sdk-source': '/sdk', 'diffutils-source': '/prepared/diffutils'}
        builder.git_command = '/host/git'
        builder.environment = {}
        record = {'format': 'emberbsd-prepared-source', 'version': 1, 'package': 'devel/diffutils'}
        with patch('emberbsd_sdk.sysroot_builder.process.capture',
                   return_value=SimpleNamespace(stdout=json.dumps(record))) as run:
            self.assertEqual(builder.verify_common_sources(), {'diffutils': record})
        self.assertIn('/ports/scripts/source-inputs/diffutils/source.py', run.call_args.args[0])

    def test_texinfo_source_requires_real_c_only_os_producer(self):
        builder = SysrootBuilder({})
        builder.paths = {'os': str(self.root / 'os'), 'texinfo-source': '/prepared/texinfo',
                         'tools': '/build/tools'}
        builder.target = {'machine': 'evbarm', 'arch': 'aarch64'}
        builder.environment = {}
        from types import SimpleNamespace
        builder.native = SimpleNamespace(tool=lambda name: '/host/' + name,
                                         compiler={'triplet': 'aarch64-apple-darwin27.0.0'})
        answer = lambda value: SimpleNamespace(stdout=value.encode())
        with patch('emberbsd_sdk.sysroot_builder.process.capture',
                   return_value=answer('/old/dist\n--disable-perl-tools --enable-native-c-only-tools\ntools/gnulib/lib tools/info\nmakedoc\ntools/gnulib/lib tools/info\n')):
            with self.assertRaisesRegex(SDKError, 'does not support EMBER_TEXINFO_SOURCE'):
                builder.require_texinfo_source_support()
        with patch('emberbsd_sdk.sysroot_builder.process.capture',
                   return_value=answer('/prepared/texinfo\n--enable-perl-tools\ntools/gnulib/lib tools/info\nmakedoc\ntools/gnulib/lib tools/info\n')):
            with self.assertRaisesRegex(SDKError, 'does not support EMBER_TEXINFO_SOURCE'):
                builder.require_texinfo_source_support()
        with patch('emberbsd_sdk.sysroot_builder.process.capture',
                   return_value=answer('/prepared/texinfo\n--disable-perl-tools\ntools/gnulib/lib tools/info\nmakedoc\ntools/gnulib/lib tools/info\n')):
            with self.assertRaisesRegex(SDKError, 'does not support EMBER_TEXINFO_SOURCE'):
                builder.require_texinfo_source_support()
        with patch('emberbsd_sdk.sysroot_builder.process.capture',
                   return_value=answer('/prepared/texinfo\n--disable-perl-tools --enable-native-c-only-tools\ntools/gnulib/lib tools/info\nmakedoc\n\n')):
            with self.assertRaisesRegex(SDKError, 'does not support EMBER_TEXINFO_SOURCE'):
                builder.require_texinfo_source_support()
        with patch('emberbsd_sdk.sysroot_builder.process.capture',
                   return_value=answer('/prepared/texinfo\n--disable-perl-tools --enable-native-c-only-tools\ntools/gnulib/lib tools/info\nmakedoc\ntools/gnulib/lib tools/info\n')) as run:
            builder.require_texinfo_source_support()
        self.assertIn('/os/external/gpl2/texinfo/bin', run.call_args.args[0][5])
        self.assertIn('HOST_GMAKE=/host/gmake', run.call_args.args[0])
        self.assertIn('HOST_SH=/host/sh', run.call_args.args[0])
        self.assertIn('EMBER_HOST_GNU_TRIPLE=aarch64-apple-darwin27.0.0', run.call_args.args[0])
        del builder.paths['texinfo-source']
        with patch('emberbsd_sdk.sysroot_builder.process.capture') as run:
            builder.require_texinfo_source_support()
            run.assert_not_called()

    def test_texinfo_selects_gmake_only_when_requested(self):
        root = self.root / 'os'
        contract = root / 'tools/native-tools/contract'
        contract.parent.mkdir(parents=True)
        contract.write_text('EmberBSD-native-tools 1\ncmp cmp -\nmake make MAKE\nreadlink readlink -\nsh sh HOST_SH\n')
        builder = SysrootBuilder({'os': str(root), 'host-prefix': '/host'})
        with patch('emberbsd_sdk.sysroot_builder.native_toolset.load_host_prefix') as load:
            builder.load_native_toolset()
            self.assertNotIn('gmake', load.call_args.kwargs['required_roles'])
            builder.options['texinfo-source'] = '/source/texinfo'
            builder.load_native_toolset()
            self.assertIn('gmake', load.call_args.kwargs['required_roles'])
            del builder.options['texinfo-source']
            builder.options['rcs-source'] = '/source/rcs'
            builder.load_native_toolset()
            self.assertIn('gmake', load.call_args.kwargs['required_roles'])
            del builder.options['rcs-source']
            builder.options['diffutils-source'] = '/source/diffutils'
            builder.load_native_toolset()
            self.assertIn('gmake', load.call_args.kwargs['required_roles'])

    def test_optional_texinfo_source_is_verified_by_its_owner(self):
        builder = SysrootBuilder({})
        builder.paths = {'python': '/host/python', 'ports': '/ports',
                         'sdk-source': '/sdk', 'texinfo-source': '/prepared/texinfo'}
        builder.git_command = '/host/git'
        builder.environment = {}
        record = {'format': 'emberbsd-prepared-source', 'version': 1,
                  'package': 'devel/gtexinfo'}
        from types import SimpleNamespace
        with patch('emberbsd_sdk.sysroot_builder.process.capture',
                   return_value=SimpleNamespace(stdout=json.dumps(record))) as run:
            self.assertEqual(builder.verify_common_sources(), {'texinfo': record})
        self.assertEqual(run.call_args.args[0], [
            '/host/python', '-B', '/ports/scripts/source-inputs/texinfo/source.py',
            '--sdk-source', '/sdk', '--git', '/host/git', 'verify-source',
            '--source', '/prepared/texinfo'])

    def test_optional_byacc_source_is_verified_by_its_owner(self):
        builder = SysrootBuilder({})
        builder.paths = {'python': '/host/python', 'ports': '/ports',
                         'sdk-source': '/sdk', 'byacc-source': '/prepared/byacc'}
        builder.git_command = '/host/git'
        builder.environment = {}
        record = {'format': 'emberbsd-prepared-source', 'version': 1,
                  'package': 'devel/byacc'}
        from types import SimpleNamespace
        with patch('emberbsd_sdk.sysroot_builder.process.capture',
                   return_value=SimpleNamespace(stdout=json.dumps(record))) as run:
            self.assertEqual(builder.verify_common_sources(), {'byacc': record})
        self.assertEqual(run.call_args.args[0], [
            '/host/python', '-B', '/ports/scripts/source-inputs/byacc/source.py',
            '--sdk-source', '/sdk', '--git', '/host/git', 'verify-source',
            '--source', '/prepared/byacc'])

    def test_source_comparison_uses_selected_prefix_patch_and_archive(self):
        from types import SimpleNamespace
        prefix = self.root / 'host'
        prefix.mkdir()
        patch_tool = prefix / 'patch'
        patch_tool.write_text('fixture')
        patch_tool.chmod(0o755)
        archive = self.root / 'byacc.tgz'
        archive.write_text('archive')
        builder = SysrootBuilder({'source-patch': str(patch_tool), 'byacc-archive': str(archive)})
        builder.native = SimpleNamespace(bundle_root=str(prefix), tool=lambda name: str(patch_tool))
        builder.paths = {'python': '/host/python', 'ports': '/ports',
                         'sdk-source': '/sdk', 'byacc-source': '/prepared/byacc'}
        builder.git_command = '/host/git'
        builder.environment = {}
        record = {'format': 'emberbsd-prepared-source', 'version': 1, 'package': 'devel/byacc'}
        with patch('emberbsd_sdk.sysroot_builder.native_toolset.load_host_prefix',
                   return_value=SimpleNamespace(tool=lambda name: str(patch_tool))) as load, \
                patch('emberbsd_sdk.sysroot_builder.process.capture',
                      return_value=SimpleNamespace(stdout=json.dumps(record))) as run:
            builder.verify_common_sources()
        self.assertEqual(load.call_args.kwargs['required_roles'], {'gpatch'})
        self.assertEqual(run.call_args.args[0][-4:], ['--patch', str(patch_tool), '--archive', str(archive)])
        builder.options['source-patch'] = str(archive)
        archive.chmod(0o755)
        with patch('emberbsd_sdk.sysroot_builder.native_toolset.load_host_prefix',
                   return_value=SimpleNamespace(tool=lambda name: str(patch_tool))):
            with self.assertRaisesRegex(SDKError, 'differs from the selected native gpatch'):
                builder.verify_common_sources()

    def test_optional_x11_source_is_verified_by_its_owner(self):
        builder = SysrootBuilder({})
        builder.paths = {'python': '/host/python', 'ports': '/ports',
                         'sdk-source': '/sdk', 'x11-source': '/prepared/xsrc'}
        builder.git_command = '/host/git'
        builder.environment = {}
        record = {'format': 'emberbsd-prepared-source', 'version': 1, 'package': 'netbsd/xsrc'}
        from types import SimpleNamespace
        with patch('emberbsd_sdk.sysroot_builder.process.capture',
                   return_value=SimpleNamespace(stdout=json.dumps(record))) as run:
            self.assertEqual(builder.verify_common_sources(), {'x11': record})
        self.assertEqual(run.call_args.args[0], [
            '/host/python', '-B', '/ports/scripts/source-inputs/xsrc/source.py',
            '--sdk-source', '/sdk', 'verify-source', '--source', '/prepared/xsrc'])
        record['package'] = 'devel/other'
        with patch('emberbsd_sdk.sysroot_builder.process.capture',
                   return_value=SimpleNamespace(stdout=json.dumps(record))), \
                self.assertRaisesRegex(SDKError, 'invalid x11 prepared source'):
            builder.verify_common_sources()

    def test_base_cxx_builder_default_tracks_family_and_explicit_value(self):
        self.assertIs(SysrootBuilder({'compiler-family': 'clang'}).options['base-cxx'], True)
        self.assertIs(SysrootBuilder({'compiler-family': 'gcc'}).options['base-cxx'], False)
        self.assertIs(SysrootBuilder({'compiler-family': 'clang', 'base-cxx': False}).options['base-cxx'], False)
        self.assertIs(SysrootBuilder({'compiler-family': 'gcc', 'base-cxx': True}).options['base-cxx'], True)
        with self.assertRaisesRegex(SDKError, 'base C\\+\\+ aliases require'):
            SysrootBuilder({'compiler-family': 'gcc', 'base-cxx': True}).preflight()
        with self.assertRaisesRegex(SDKError, 'unsupported target compiler family'):
            SysrootBuilder({'compiler-family': 'rust'}).preflight()

    def test_base_cxx_cli_defaults_and_boolean_opt_out(self):
        base = dict(sdk='/unused/sdk', ports='/unused/ports', compiler='/unused/compiler',
                    runtime_work='/unused/runtime', jobs='2',
                    sdk_source=str(SDK_SOURCE), libedit_source='/unused/libedit',
                    libarchive_source='/unused/libarchive', libevent_source='/unused/libevent',
                    host_prefix='/unused/host', host_binutils='/unused/host-binutils',
                    llvm_source='/unused/llvm')

        def invoke(script_name, builder_type, profile_args, extra_args=()):
            captured = {}
            def fake_run(builder):
                captured.update(builder.options)
                return {'captured': True}
            script = SDK_SOURCE / 'scripts' / script_name
            arguments = []
            for key, value in base.items():
                arguments.extend(['--' + key.replace('_', '-'), value])
            arguments.extend(profile_args)
            arguments.extend(extra_args)
            with patch.object(builder_type, 'run', fake_run), \
                 patch.object(sys, 'argv', [str(script), *arguments]), \
                 contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as exit_info:
                runpy.run_path(str(script), run_name='__main__')
            self.assertEqual(exit_info.exception.code, 0)
            return captured

        sysroot = lambda *args: invoke('build-sysroot.py', SysrootBuilder,
            ['--os', '/unused/os', '--target', 'aarch64',
             '--destination', '/unused/destination'], args)
        release = lambda *args: invoke('build-os-release.py', OSReleaseBuilder,
            ['--os-source', '/unused/os', '--profile', 'aarch64-sysroot',
             '--destination', '/unused/destination'], args)
        for cli in (sysroot, release):
            with self.subTest(cli=cli.__name__):
                self.assertIs(cli()['base-cxx'], True)
                self.assertIs(cli('--no-base-cxx')['base-cxx'], False)
                self.assertIs(cli('--base-cxx')['base-cxx'], True)
                self.assertIs(cli('--compiler-family', 'gcc')['base-cxx'], False)

    def test_bootstrap_phases_keep_base_aliases_disabled(self):
        options = {'sdk': '/sdk', 'runtime-work': '/sdk/obj/runtime',
                   'target': 'aarch64'}
        coordinator = LLVMCoordinator(options, SysrootBuilder)
        self.assertTrue(SysrootBuilder(coordinator.stage_options('final')).options['base-cxx'])
        for phase in ('headers', 'bootstrap', 'base'):
            selected = coordinator.stage_options(phase)
            self.assertIs(selected['base-cxx'], False)
        selected_paths = {'compiler': '/compiler', 'llvm-source': '/source',
                          'runtime-prefix': '/stage/usr/pkg/llvm',
                          'llvm-unwind': '/stage/usr/pkg/llvm/lib/libunwind.a',
                          'llvm-resource': '/stage/usr/pkg/llvm/lib/clang/23'}
        for phase in ('headers', 'base'):
            self.assertIn('MKLIBCXX=no', llvm_flags(
                selected_paths, phase=phase, base_cxx=True))

    def test_coordinator_uses_selected_stage_and_object_directories(self):
        options = {'sdk': '/sdk', 'runtime-work': '/sdk/obj/runtime', 'target': 'aarch64',
                   'llvm-objects': '/sdk/obj/alternate', 'llvm-stages': '/sdk/stages/alternate'}
        coordinator = LLVMCoordinator(options, SysrootBuilder)
        headers = coordinator.stage_options('headers')
        self.assertEqual(headers['objects'], '/sdk/obj/alternate/headers')
        self.assertEqual(headers['destination'], '/sdk/stages/alternate/headers')
        self.assertEqual(headers['tools'], '/sdk/obj/alternate/tools')

    def test_coordinator_roots_stay_inside_shared_workspace(self):
        sdk = self.root / 'sdk'
        (sdk / 'obj').mkdir(parents=True)
        (sdk / 'stages').mkdir()
        options = {'sdk': str(sdk), 'runtime-work': str(sdk / 'obj/runtime'),
                   'target': 'aarch64', 'llvm-objects': str(sdk / 'obj/alternate'),
                   'llvm-stages': str(sdk / 'stages/alternate')}
        class Header:
            output_path = SysrootBuilder.output_path
            def __init__(self, selected, phase):
                self.logical_sdk = str(sdk)
                self.paths = {'sdk': str(sdk)}
            def preflight(self):
                pass
        def prepare(selected):
            with patch('emberbsd_sdk.llvm_sysroot.workspace.inspect_layout', return_value={}), \
                    patch('emberbsd_sdk.llvm_sysroot.workspace.require_target'), \
                    patch('emberbsd_sdk.llvm_sysroot.LLVMPhaseBuilder', Header):
                LLVMCoordinator(selected, SysrootBuilder).prepare()
        prepare(options)
        for name in ('llvm-objects', 'llvm-stages'):
            with self.subTest(name=name), self.assertRaisesRegex(SDKError, 'output must be inside'):
                prepare({**options, name: str(self.root / 'outside')})

    def test_base_cxx_option_type_is_checked_before_compiler_selection(self):
        with self.assertRaisesRegex(SDKError, 'must be boolean'):
            SysrootBuilder({'compiler-family': 'gcc', 'base-cxx': 'false'}).preflight()

    def builder(self, options=None, builder_type=ClangBuilder, retarget=None):
        options = fixture(self.root) if options is None else options
        source = self.root / 'llvm-selected-source'; source.mkdir()
        runtime = runtime_tests.LLVMRuntimeTests()
        runtime.setUp(); self.addCleanup(runtime.doCleanups)
        runtime.full()
        source_record = dict(format='emberbsd-llvm-source', version=1, llvm_version='23.1.4',
                             source=runtime.record['source'])
        write(source / 'llvm-source.json', json.dumps(source_record))
        script = 'pkgsrc/lang/compiler-rt/files/runtime.py'
        write(Path(options['ports']) / script, '''import json,sys
from pathlib import Path
a=sys.argv[1:]
key='--source' if 'verify-source' in a else '--stage'
print((Path(a[a.index(key)+1])/('llvm-source.json' if key=='--source' else 'runtime.json')).read_text())
''')
        commit(options['ports'])
        options['libedit-source'] = str(self.root / 'libedit-current')
        prepare_source(options['ports'], self.root / 'libedit.tar.gz', options['libedit-source'])
        runtime.record['recipe'] = dict(ports_revision=git(options['ports'], 'rev-parse', 'HEAD'),
            files={script: inventory.sha256_file(Path(options['ports']) / script)})
        runtime.record['base']['os_revision'] = git(options['os'], 'rev-parse', 'HEAD')
        bootstrap = runtime_tests.LLVMRuntimeTests()
        bootstrap.setUp(); self.addCleanup(bootstrap.doCleanups)
        bootstrap.static_unwind()
        bootstrap.record['recipe'] = copy.deepcopy(runtime.record['recipe'])
        bootstrap.record['base']['os_revision'] = runtime.record['base']['os_revision']
        bootstrap.save()
        if retarget:  # before pinning, so the full runtime records the retargeted bootstrap
            retarget(runtime); retarget(bootstrap)
        names = (bootstrap.record['artifacts']['builtins'], bootstrap.record['artifacts']['libunwind']['static'])
        runtime.record['bootstrap'] = dict(manifest_sha256=inventory.sha256_file(bootstrap.stage / 'runtime.json'),
            files={name: bootstrap.record['files'][name] for name in names})
        runtime.save()
        options.update({'llvm-bootstrap-stage': str(bootstrap.stage), 'compiler-family': 'clang', 'llvm-source': str(source),
                        'llvm-runtime-stage': str(runtime.stage),
                        'destination': str(Path(options['sdk']) / 'stages/clang')})
        return builder_type(options), runtime

    def manifest(self):
        return {'prefix': 'usr/pkg/llvm', 'llvm_version': '23.1.4', 'artifacts': {
            'builtins': 'usr/pkg/llvm/lib/clang/23/lib/netbsd/libclang_rt.builtins-aarch64.a',
            'libunwind': {'static': 'usr/pkg/llvm/lib/libunwind.a'}}}

    def test_direct_full_runtime_requires_bootstrap_for_both_builders(self):
        original, _ = self.builder()
        options = dict(original.options)
        del options['llvm-bootstrap-stage']
        for builder_type in (ClangBuilder, OSReleaseBuilder):
            with self.assertRaisesRegex(SDKError, '--llvm-bootstrap-stage is required'):
                builder_type(options).preflight()

    def test_resource_location_comes_from_measured_builtins(self):
        stage = str(self.root / 'stage')
        selected = runtime_paths(stage, self.manifest())
        self.assertEqual(selected['llvm-resource'], stage + '/usr/pkg/llvm/lib/clang/23')
        self.assertEqual(selected['llvm-unwind'], stage + '/usr/pkg/llvm/lib/libunwind.a')
        bad = self.manifest()
        bad['artifacts']['builtins'] = 'usr/pkg/llvm/lib/libbuiltins.a'
        with self.assertRaisesRegex(SDKError, 'resource directory'):
            runtime_paths(stage, bad)

    def test_base_and_final_have_distinct_runtime_policies(self):
        selected = {'compiler': '/compiler', 'llvm-source': '/source',
                    **runtime_paths('/stage', self.manifest())}
        selected['llvm-libc-unwind'] = '/bootstrap/usr/pkg/llvm/lib/libunwind.a'
        base = llvm_flags(selected, phase='base')
        self.assertIn('MKCXX=no', base)
        self.assertIn('EMBER_LIBUNWIND_ARCHIVE=/stage/usr/pkg/llvm/lib/libunwind.a', base)
        self.assertIn('EMBER_EXTERNAL_LLVM_RUNTIME=/stage/usr/pkg/llvm', base)
        self.assertIn('EMBER_LLVM_RUNTIME_PHASE=bootstrap', base)
        self.assertIn('EMBER_LLVM_RESOURCE_DIR=/stage/usr/pkg/llvm/lib/clang/23', base)
        final = llvm_flags(selected, phase='final')
        self.assertIn('MKCXX=yes', final)
        self.assertIn('MKLIBCXX=no', final)
        self.assertIn('EMBER_EXTERNAL_LLVM_RUNTIME=/stage/usr/pkg/llvm', final)
        self.assertIn('EMBER_LIBUNWIND_ARCHIVE=/bootstrap/usr/pkg/llvm/lib/libunwind.a', final)
        self.assertIn('EMBER_LLVM_RUNTIME_PHASE=full', final)
        self.assertFalse(any('GCC' in v or 'STDCXX' in v for v in final))
        opted_in = llvm_flags(selected, phase='final', base_cxx=True)
        self.assertIn('MKLIBCXX=yes', opted_in)
        self.assertIn('MKLIBCXX=no', llvm_flags(selected, phase='final', base_cxx=False))

    def test_bootstrap_phases_do_not_require_a_final_runtime(self):
        options = {'sdk': str(self.root / 'sdk'), 'runtime-work': str(self.root / 'sdk/obj/runtime'),
                   'target': 'aarch64', 'destination': str(self.root / 'sdk/stages/final'),
                   'jobs': '2', 'host-prefix': '/host', 'compiler': '/compiler',
                   'llvm-source': '/source', 'sdk-source': '/sdk-source', 'os': '/os', 'ports': '/ports'}
        coordinator = LLVMCoordinator(options, SysrootBuilder)
        events = []
        class Stage:
            def __init__(self, selected, phase):
                self.options, self.phase = selected, phase
            def run(self):
                events.append(self.phase)
                return {'phase': self.phase}
        class Final:
            def __init__(self, selected):
                self.options = selected
            def run(self):
                events.append('final')
                self_stage = self.options['llvm-runtime-stage']
                return {'runtime': self_stage, 'bootstrap': self.options['llvm-bootstrap-stage']}
        coordinator.builder_type = Final
        with patch.object(coordinator, 'prepare', return_value=None), \
             patch.object(coordinator, 'lock'), \
             patch('emberbsd_sdk.llvm_sysroot.LLVMPhaseBuilder', Stage), \
             patch.object(coordinator, 'runtime', side_effect=lambda phase, base: events.append(phase)):
            result = coordinator.run()
        self.assertEqual(events, ['headers', 'bootstrap', 'base', 'full', 'final'])
        self.assertEqual(result['runtime'], options['runtime-work'] + '/full-stage')
        self.assertEqual(result['bootstrap'], options['runtime-work'] + '/bootstrap-stage')

    def test_headers_use_the_limited_os_operation_without_runtime_headers(self):
        class Headers(LLVMPhaseBuilder, ClangBuilder):
            pass
        builder, _ = self.builder(builder_type=lambda options:Headers(options, 'headers'))
        builder.options.pop('libarchive-source')
        builder.options.pop('libevent-source')
        receipt = builder.preflight()
        self.assertEqual(len(receipt['plan']), 1)
        self.assertEqual(receipt['plan'][0][-2:], ['tools', 'bootstrap-headers'])
        self.assertFalse(any('EMBER_LIBARCHIVE_SOURCE=' in arg or 'EMBER_LIBEVENT_SOURCE=' in arg
                             for arg in receipt['plan'][0]))
        self.assertIn('EMBER_HOST_BFD_CONFIG=' + builder.options['host-binutils'] + '/host-bfd.mk', receipt['plan'][0])
        self.assertIn('MKREPRO=yes', receipt['plan'][0])
        self.assertIn('MKREPRO_TIMESTAMP=' + str(builder.source_timestamp()), receipt['plan'][0])
        for name in ('stdio.h','sys/types.h','sys/param.h','stdint.h','limits.h','math.h','pthread.h'):
            write(Path(builder.paths['destination'])/'usr/include'/name, '/* base header */\n')
        builder.inspect_payload()
        self.assertFalse((Path(builder.paths['destination'])/'usr/include/unwind.h').exists())

    def completed_headers(self):
        class Headers(LLVMPhaseBuilder, ClangBuilder):
            def __init__(self, options):
                super().__init__(options, 'headers')

            def build_payload(self):
                for name in ('stdio.h', 'sys/types.h', 'sys/param.h',
                             'stdint.h', 'limits.h', 'math.h', 'pthread.h'):
                    write(Path(self.paths['destination']) / 'usr/include' / name,
                          '/* source header fixture */\n')

        builder, _ = self.builder(builder_type=Headers)
        return builder, builder.run()

    def test_completed_headers_reuse_after_ports_and_controller_updates(self):
        builder, original = self.completed_headers()
        for source in ('ports', 'sdk-source'):
            write(Path(builder.options[source]) / 'unrelated-update', 'new revision')
            commit(builder.options[source])
        previous = type(builder).python_identity
        def changed_modules(current):
            result = previous(current)
            result['modules']['controller_fixture.py'] = '1' * 64
            return result
        with patch.object(type(builder), 'python_identity', changed_modules), \
             patch.object(type(builder), 'build_payload', side_effect=AssertionError('must reuse')):
            result = type(builder)(builder.options).run()
        self.assertEqual(result, original)
        self.assertEqual(jsonio.read_json(Path(builder.paths['destination']) / builder.RECEIPT), original)

    def test_completed_headers_accept_same_os_tree_and_reject_changed_tree(self):
        builder, original = self.completed_headers()
        source = builder.options['os']
        git(source, '-c', 'user.name=SDK fixture', '-c', 'user.email=fixture@example.invalid',
            'commit', '--allow-empty', '-qm', 'Same OS tree')
        self.assertEqual(type(builder)(builder.options).run(), original)
        write(Path(source) / 'include/new-header.h', '/* new OS input */\n')
        commit(source)
        with self.assertRaisesRegex(SDKError, 'OS source tree changed'):
            type(builder)(builder.options).run()

    def test_completed_headers_reject_changed_output_without_dispatch(self):
        builder, _ = self.completed_headers()
        write(Path(builder.paths['destination']) / 'usr/include/stdio.h', 'tampered')
        with patch.object(type(builder), 'build_payload', side_effect=AssertionError('must refuse')):
            with self.assertRaisesRegex(SDKError, 'inputs or outputs changed'):
                type(builder)(builder.options).run()

    def test_completed_headers_reject_compiler_and_phase_changes(self):
        builder, _ = self.completed_headers()
        previous = type(builder).compiler
        def changed_compiler(current):
            result = previous(current)
            result['commands']['cc'].append('-DCHANGED_ABI')
            return result
        with patch.object(type(builder), 'compiler', changed_compiler):
            with self.assertRaisesRegex(SDKError, 'incompatible object context'):
                type(builder)(builder.options).run()
        receipt_path = Path(builder.paths['destination']) / builder.RECEIPT
        receipt = jsonio.read_json(receipt_path)
        receipt['phase'] = 'base'
        write(receipt_path, json.dumps(receipt))
        with self.assertRaisesRegex(SDKError, 'incomplete or unrelated LLVM OS stage'):
            type(builder)(builder.options).run()

    def test_completed_headers_do_not_use_target_linker_bytes(self):
        builder, original = self.completed_headers()
        previous = type(builder).compiler
        def updated_linker(current):
            result = previous(current)
            result['binaries']['ld']['sha256'] = 'a' * 64
            return result
        with patch.object(type(builder), 'compiler', updated_linker), \
             patch.object(type(builder), 'build_payload', side_effect=AssertionError('must reuse')):
            self.assertEqual(type(builder)(builder.options).run(), original)

    def test_completed_headers_do_not_consume_common_runtime_source(self):
        builder, original = self.completed_headers()
        source = self.root / 'llvm-profile-source-update'
        source.mkdir()
        record = jsonio.read_json(Path(builder.options['llvm-source']) / 'llvm-source.json')
        record['tree_sha256'] = 'a' * 64
        record['recipe'] = {'files': {'profile-patch': 'b' * 64}}
        write(source / 'llvm-source.json', json.dumps(record))
        options = builder.options | {'llvm-source': str(source)}
        with patch.object(type(builder), 'build_payload', side_effect=AssertionError('must reuse')):
            self.assertEqual(type(builder)(options).run(), original)
        # Selection still verifies the new prepared source against the compiler.
        record['llvm_version'] = '99.0.0'
        write(source / 'llvm-source.json', json.dumps(record))
        with self.assertRaisesRegex(SDKError, 'common LLVM source and selected compiler differ'):
            type(builder)(options).run()

    def test_headers_source_exception_keeps_other_flags_strict(self):
        builder, _ = self.completed_headers()
        record = {'signature': copy.deepcopy(builder.signature), 'sources': builder.sources}
        builder.signature['llvm_source']['tree_sha256'] = 'a' * 64
        builder.signature['flags'].append('MKFORTIFY=no')
        self.assertFalse(builder.compatible_context(record, builder.paths['objects']))

    def test_common_runtime_source_changes_still_invalidate_base_and_final(self):
        builder, _ = self.builder()
        builder.preflight()
        for phase in ('base', 'final'):
            with self.subTest(phase=phase):
                builder.llvm_phase = phase
                original = copy.deepcopy(builder.signature)
                record = {'signature': copy.deepcopy(original), 'sources': builder.sources}
                builder.signature['llvm_source']['tree_sha256'] = 'a' * 64
                self.assertFalse(builder.compatible_context(record, builder.paths['objects']))
                builder.signature = copy.deepcopy(original)
                builder.signature['flags'] = [
                    'EMBER_LLVM_SOURCE=/updated/source' if flag.startswith('EMBER_LLVM_SOURCE=') else flag
                    for flag in original['flags']]
                self.assertFalse(builder.compatible_context(record, builder.paths['objects']))
                builder.signature = original

    def test_target_linker_bytes_only_affect_contexts_that_link_target_code(self):
        builder, _ = self.builder()
        builder.preflight()
        original = copy.deepcopy(builder.signature)
        records = {name: {'signature': copy.deepcopy(builder.context_signature(builder.paths[name])),
                          'sources': builder.sources} for name in ('tools', 'objects')}
        builder.signature['compiler']['binaries']['ld']['sha256'] = 'a' * 64
        self.assertTrue(builder.compatible_context(records['tools'], builder.paths['tools']))
        for phase in ('base', 'final'):
            builder.llvm_phase = phase
            self.assertFalse(builder.compatible_context(records['objects'], builder.paths['objects']))
        for phase in ('headers', 'final'):
            builder.llvm_phase = phase
            for change in (
                    lambda s: s['compiler']['binaries']['ld'].update(path='/other/ld.lld'),
                    lambda s: s['compiler']['binaries']['ld'].update(selected_path='/other/ld.lld'),
                    lambda s: s['compiler']['commands']['cc'].append('-DCHANGED_ABI'),
                    lambda s: s['compiler']['binaries']['cc'].update(sha256='b' * 64),
                    lambda s: s['native_toolset']['abi_flags'].update(cflags=['-m32'])):
                with self.subTest(phase=phase, change=change):
                    builder.signature = copy.deepcopy(original)
                    builder.signature['compiler']['binaries']['ld']['sha256'] = 'a' * 64
                    change(builder.signature)
                    self.assertFalse(builder.compatible_context(records['tools'], builder.paths['tools']))
                    self.assertFalse(builder.compatible_context(records['objects'], builder.paths['objects']))

    def bootstrap_base(self):
        header = '/* measured bootstrap header */\n'
        class Base(LLVMPhaseBuilder, ClangBuilder):
            installed_header = header

            def __init__(self, options):
                super().__init__(options, 'base')

            def build_payload(self):
                super().build_payload()
                write(Path(self.paths['destination']) / 'usr/include/stdint.h', self.installed_header)

        builder, runtime = self.builder(builder_type=Base)
        builder.options['base-cxx'] = False
        base = self.root / 'measured-headers'
        write(base / 'usr/include/stdint.h', header)
        runtime.record['base']['files'] = {
            'usr/include/stdint.h': inventory.sha256_file(base / 'usr/include/stdint.h')}
        runtime.save()
        builder.options['llvm-runtime-base'] = str(base)
        return builder, runtime, base

    def test_bootstrap_runtime_reuses_headers_across_os_fix(self):
        builder, runtime, _ = self.bootstrap_base()
        write(Path(builder.options['os']) / 'build-rule-fix', 'changed rule, same headers')
        commit(builder.options['os'])
        result = builder.run()
        self.assertEqual(result['qualification'], 'built-and-inspected')
        self.assertNotEqual(result['sources']['os']['commit'], runtime.record['base']['os_revision'])
        self.assertEqual(result['signature']['runtime']['manifest'], runtime.record)

    def test_bootstrap_runtime_rejects_changed_input_headers_before_dispatch(self):
        builder, _, base = self.bootstrap_base()
        write(base / 'usr/include/stdint.h', 'changed input')
        with patch.object(type(builder), 'build_payload', side_effect=AssertionError('must refuse')):
            with self.assertRaisesRegex(SDKError, 'base inputs changed'):
                builder.run()
        self.assertFalse(Path(builder.options['destination']).exists())

    def test_bootstrap_runtime_rejects_new_base_headers_before_qualification(self):
        builder, _, _ = self.bootstrap_base()
        builder.installed_header = 'changed installed header'
        with self.assertRaisesRegex(SDKError, 'base inputs changed'):
            builder.run()
        destination = Path(builder.options['destination'])
        self.assertTrue((destination / builder.MARKER).exists())
        self.assertEqual(jsonio.read_json(destination / builder.RECEIPT)['qualification'], 'incomplete')

    def test_bootstrap_runtime_needs_header_inputs_to_accept_an_ancestor(self):
        builder, _, _ = self.bootstrap_base()
        del builder.options['llvm-runtime-base']
        write(Path(builder.options['os']) / 'build-rule-fix', 'changed rule')
        commit(builder.options['os'])
        with self.assertRaisesRegex(SDKError, 'base OS differs'):
            builder.preflight()

    def test_bootstrap_runtime_rejects_unrelated_os_even_with_matching_headers(self):
        builder, runtime, _ = self.bootstrap_base()
        source = builder.options['os']
        unrelated = git(source, '-c', 'user.name=SDK fixture',
            '-c', 'user.email=fixture@example.invalid', 'commit-tree',
            git(source, 'rev-parse', 'HEAD^{tree}'), '-m', 'Unrelated OS history')
        runtime.record['base']['os_revision'] = unrelated
        runtime.save()
        with self.assertRaises(SDKError):
            builder.preflight()
        self.assertFalse(Path(builder.options['destination']).exists())

    def test_selected_final_runtime_retains_its_original_os_revision(self):
        builder, runtime = self.builder()
        old = runtime.record['base']['os_revision']
        write(Path(builder.options['os']) / 'changed', 'new source')
        commit(builder.options['os'])
        receipt = builder.run()
        self.assertNotEqual(receipt['sources']['os']['commit'], old)
        self.assertEqual(receipt['signature']['runtime']['manifest']['base']['os_revision'], old)

    def test_final_dispatch_installs_default_base_aliases(self):
        builder, runtime = self.builder()
        result = builder.run()
        self.assertEqual(result['version'], 3)
        self.assertEqual(result['qualification'], 'built-and-inspected')
        self.assertEqual(result['signature']['runtime']['manifest'], runtime.record)
        output = Path(builder.paths['destination'])
        self.assertTrue((output / 'usr/pkg/llvm/lib/libc++.so').is_symlink())
        self.assertTrue((output / 'usr/include/c++/vector').is_symlink())
        self.assertFalse((output / 'usr/pkg/gcc16').exists())
        self.assertIn('EMBER_COMPILER_FAMILY=clang', result['signature']['flags'])
        self.assertIn('EMBER_HOST_COMPILER_FAMILY=gcc', result['signature']['flags'])
        self.assertIn('EMBER_HOST_BFD_CONFIG=' + builder.options['host-binutils'] + '/host-bfd.mk', result['signature']['flags'])

    def test_default_base_cxx_aliases_export_import_and_relocate(self):
        from emberbsd_sdk import llvm_runtime_install
        builder, runtime = self.builder()
        receipt = builder.run()
        root = Path(builder.paths['destination'])
        plan = llvm_runtime_install.base_alias_plan(runtime.record)
        self.assertIn('MKLIBCXX=yes', receipt['signature']['flags'])
        self.assertIn('usr/include/c++/vector', plan)
        self.assertTrue((root / 'usr/include/c++/vector').is_symlink())
        self.assertEqual(receipt['outputs'], inventory.sysroot(root, 183,
            exclude=(builder.RECEIPT, builder.LOG, builder.MARKER)))
        bundle = self.root / 'base-cxx-bundle'
        exported = sysroot_transfer.export_sysroot(root, bundle, target='aarch64')
        manifest = jsonio.read_json(bundle / 'manifest.json')
        self.assertEqual(manifest['provenance']['base_cxx'], 'libcxx')
        alias = 'usr/include/c++/vector'
        missing = copy.deepcopy(manifest)
        del missing['inventory'][alias]
        with self.assertRaises(SDKError):
            sysroot_transfer.validate_manifest(missing, target='aarch64')
        changed = copy.deepcopy(manifest)
        changed['inventory'][alias]['target'] = '../../pkg/llvm/include/c++/v1'
        with self.assertRaisesRegex(SDKError, 'base alias'):
            sysroot_transfer.validate_manifest(changed, target='aarch64')
        extra = copy.deepcopy(manifest)
        extra['inventory']['usr/include/c++/legacy'] = {'kind': 'directory', 'mode': 0o755}
        extra['inventory']['usr/include/c++/legacy/old.h'] = {'kind': 'file', 'mode': 0o644,
            'size': 0, 'sha256': '0' * 64}
        with self.assertRaisesRegex(SDKError, r'base C\+\+ namespace'):
            sysroot_transfer.validate_manifest(extra, target='aarch64')
        forged = copy.deepcopy(manifest)
        forged['provenance']['base_cxx'] = 'other'
        with self.assertRaisesRegex(SDKError, 'provenance'):
            sysroot_transfer.validate_manifest(forged, target='aarch64')
        restored = self.root / 'base-cxx-restored'
        sysroot_transfer.import_sysroot(bundle, restored, target='aarch64',
            manifest_sha256=exported['manifest_sha256'])
        llvm_runtime_install.validate_base_aliases(restored, runtime.record)

    def test_base_alias_conflict_preflights_before_runtime_copy(self):
        builder, runtime = self.builder()
        builder.preflight()
        conflict = Path(builder.paths['destination']) / 'usr/include/c++/vector'
        conflict.parent.mkdir(parents=True)
        conflict.write_text('conflict')
        with self.assertRaisesRegex(SDKError, 'alias conflict'):
            builder.copy_runtime()
        self.assertFalse((Path(builder.paths['destination']) / 'usr/pkg/llvm').exists())

    def test_runtime_aliases_follow_configured_flags_not_raw_option(self):
        builder, _ = self.builder()
        builder.options['base-cxx'] = False
        builder.preflight()
        Path(builder.paths['destination']).mkdir(parents=True)
        builder.flags = [flag for flag in builder.flags if not flag.startswith('MKLIBCXX=')]
        builder.flags.append('MKLIBCXX=yes')
        builder.copy_runtime()
        self.assertTrue((Path(builder.paths['destination']) / 'usr/include/c++/vector').is_symlink())

    def test_copy_runtime_rejects_base_cxx_flags_outside_final_clang_before_io(self):
        builder, _ = self.builder()
        builder.preflight()
        builder.flags = [flag for flag in builder.flags if not flag.startswith(('MKCXX=', 'MKLIBCXX='))]
        builder.flags.extend(('MKCXX=yes', 'MKLIBCXX=yes'))
        builder.options['base-cxx'] = False
        with patch.object(builder, 'verify_runtime', side_effect=AssertionError('must reject before runtime access')):
            builder.options['compiler-family'] = 'gcc'
            with self.assertRaisesRegex(SDKError, 'final Clang'):
                builder.copy_runtime()
            builder.options['compiler-family'] = 'clang'
            builder.llvm_phase = 'base'
            with self.assertRaisesRegex(SDKError, 'final Clang'):
                builder.copy_runtime()

    def test_release_destination_uses_one_installer_for_payload_and_aliases(self):
        builder, runtime = self.builder()
        builder.options['base-cxx'] = True
        builder.preflight()
        output = Path(builder.paths['destination']); output.mkdir(parents=True)
        destination = output / 'dest'; destination.mkdir()
        events = []

        class Installer:
            root = str(destination)
            def directory(self, path, mode):
                events.append(('directory', path))
                Path(path).mkdir(exist_ok=True)
            def file(self, source, path, mode):
                events.append(('file', path))
                shutil.copyfile(source, path)
                os.chmod(path, mode)
            def link(self, target, path, mode):
                events.append(('link', path))
                os.symlink(target, path)

        builder.copy_runtime(destination=destination, installer=Installer())
        self.assertFalse((output / 'usr').exists())
        self.assertTrue((destination / 'usr/include/c++/vector').is_symlink())
        self.assertIn(('link', str(destination / 'usr/lib/libc++.so')), events)
        self.assertIn(('file', str(destination / 'usr/pkg/llvm/lib/libc++.so.1')), events)
        self.assertEqual(inventory.snapshot_tree(destination / 'usr/pkg/llvm', machine=183),
                         builder.runtime_record['inventory'])

    def test_runtime_destination_cannot_escape_the_owned_output(self):
        builder, _ = self.builder()
        builder.preflight()
        output = Path(builder.paths['destination']); output.mkdir(parents=True)
        outside = self.root / 'outside'; outside.mkdir()
        with self.assertRaisesRegex(SDKError, 'owned producer output'):
            builder.copy_runtime(destination=outside)
        self.assertEqual(list(outside.iterdir()), [])

    def test_runtime_of_another_llvm_version_fails_before_outputs(self):
        builder, runtime = self.builder()
        runtime.record['llvm_version'] = '22.1.0'; runtime.save()
        with self.assertRaisesRegex(SDKError, 'runtime compiler|bootstrap and full runtime inputs differ'):
            builder.run()
        self.assertFalse(Path(builder.options['destination']).exists())

    def test_compatible_runtime_survives_unrelated_ports_commit(self):
        builder, runtime = self.builder()
        builder.preflight()
        builder.sources['ports']['commit'] = '1' * 40
        self.assertEqual(builder.verify_runtime()['manifest'], runtime.record)
        recipe = Path(builder.paths['ports']) / next(iter(runtime.record['recipe']['files']))
        recipe.write_text(recipe.read_text() + '\n# changed recipe input\n')
        with self.assertRaisesRegex(SDKError, 'recipe input changed'):
            builder.verify_runtime()

    def test_runtime_original_source_is_forwarded_to_owner(self):
        builder, runtime = self.builder()
        builder.options['llvm-runtime-original-source'] = builder.options['llvm-source']
        builder.preflight()
        self.assertEqual(builder.runtime_command[builder.runtime_command.index('--original-source') + 1],
                         builder.paths['llvm-runtime-original-source'])
        self.assertEqual(builder.verify_runtime()['manifest'], runtime.record)
        artifact = Path(builder.paths['llvm-runtime-stage']) / runtime.record['artifacts']['builtins']
        artifact.write_bytes(artifact.read_bytes() + b'changed')
        with self.assertRaises(SDKError):
            builder.verify_runtime()

    def test_clang_transfer_is_explicit_and_preserves_historical_format(self):
        builder, runtime = self.builder()
        builder.options['base-cxx'] = False
        write(Path(builder.options['os']) / 'changed', 'new source')
        commit(builder.options['os'])
        receipt = builder.run()
        bundle = self.root / 'bundle'
        exported = sysroot_transfer.export_sysroot(builder.paths['destination'], bundle, target='aarch64')
        manifest = jsonio.read_json(bundle / 'manifest.json')
        self.assertEqual(manifest['version'], 2)
        self.assertEqual(manifest['provenance']['compiler']['family'], 'clang')
        sysroot_transfer.import_sysroot(bundle, self.root / 'restored', target='aarch64',
            manifest_sha256=exported['manifest_sha256'])
        advanced = copy.deepcopy(receipt)
        advanced['sources']['ports']['commit'] = '1' * 40
        sysroot_transfer._provenance(advanced, 'aarch64')
        mixed = copy.deepcopy(receipt)
        mixed['signature']['llvm_source']['source']['sha512'] = 'b' * 128
        with self.assertRaisesRegex(SDKError, 'runtime differs'):
            sysroot_transfer._provenance(mixed, 'aarch64')
        receipt['version'] = 2
        with self.assertRaisesRegex(SDKError, 'GCC16'):
            sysroot_transfer._provenance(receipt, 'aarch64')

    def test_target_runtime_changes_do_not_rebuild_native_tool_context(self):
        builder, runtime = self.builder()
        builder.preflight()
        before = builder.context_signature(builder.paths['tools'])
        builder.signature['runtime'] = {'changed': True}
        builder.signature['llvm_phase'] = 'base'
        self.assertEqual(builder.context_signature(builder.paths['tools']), before)
        self.assertNotEqual(builder.context_signature(builder.paths['objects']), before)
        bfd = builder.paths['host-binutils']
        builder.paths['host-binutils'] = bfd + '-changed'
        self.assertNotEqual(builder.context_signature(builder.paths['tools']), before)
        builder.paths['host-binutils'] = bfd
        builder.signature['native_toolset'] = {'different': True}
        self.assertNotEqual(builder.context_signature(builder.paths['tools']), before)

    def test_byacc_source_changes_invalidate_native_tools(self):
        builder, _ = self.builder()
        builder.preflight()
        builder.llvm_phase = 'headers'
        tool_path = builder.paths['tools']
        original = builder.context_signature(tool_path)
        record = {'signature': copy.deepcopy(original)}
        builder.paths['byacc-source'] = '/prepared/byacc-one'
        builder.signature['common_sources']['byacc'] = {'tree_sha256': 'a' * 64}
        self.assertFalse(builder.compatible_context(record, tool_path))
        selected = builder.context_signature(tool_path)
        record['signature'] = copy.deepcopy(selected)
        builder.signature['common_sources']['byacc']['tree_sha256'] = 'b' * 64
        self.assertFalse(builder.compatible_context(record, tool_path))
        builder.signature['common_sources']['byacc']['tree_sha256'] = 'a' * 64
        builder.paths['byacc-source'] = '/prepared/byacc-two'
        self.assertFalse(builder.compatible_context(record, tool_path))

    def test_texinfo_target_source_does_not_change_native_tools_or_headers(self):
        builder, _ = self.builder()
        builder.preflight()
        builder.llvm_phase = 'headers'
        tool_path = builder.paths['tools']
        object_path = builder.paths['objects']
        builder.llvm_phase = 'base'
        base_record = {'signature': copy.deepcopy(builder.context_signature(object_path))}
        builder.llvm_phase = 'headers'
        tool_record = {'signature': copy.deepcopy(builder.context_signature(tool_path))}
        header_record = {'signature': copy.deepcopy(builder.context_signature(object_path))}
        builder.paths['texinfo-source'] = '/prepared/texinfo'
        builder.signature['common_sources']['texinfo'] = {'tree_sha256': 'a' * 64}
        builder.signature['flags'].append('EMBER_TEXINFO_SOURCE=/prepared/texinfo')
        self.assertTrue(builder.compatible_context(tool_record, tool_path))
        self.assertTrue(builder.compatible_context(header_record, object_path))
        builder.llvm_phase = 'base'
        self.assertFalse(builder.compatible_context(base_record, object_path))

    def test_rcs_selection_reuses_headers_and_native_tools_with_new_gmake(self):
        from emberbsd_sdk.native_os_routing import native_path
        builder, _ = self.builder()
        builder.preflight()
        builder.llvm_phase = 'headers'
        tools, objects = builder.paths['tools'], builder.paths['objects']
        records = {path: {'signature': copy.deepcopy(builder.context_signature(path)),
                          'sources': copy.deepcopy(builder.sources)} for path in (tools, objects)}
        builder.paths['rcs-source'] = '/prepared/rcs'
        builder.signature['common_sources']['rcs'] = {'tree_sha256': 'a' * 64}
        builder.signature['flags'].append('EMBER_RCS_SOURCE=/prepared/rcs')
        builder.signature['native_toolset']['tools']['gmake'] = str(Path(builder.signature['native_toolset']['tools']['cc']).parent / 'gmake')
        builder.signature['environment']['PATH'] = native_path(builder.signature['native_toolset'])
        for path in (tools, objects):
            self.assertTrue(builder.compatible_context(records[path], path))
        builder.llvm_phase = 'base'
        self.assertFalse(builder.compatible_context(records[objects], objects))

    def test_diffutils_selection_reuses_headers_and_native_tools_with_new_gmake(self):
        from emberbsd_sdk.native_os_routing import native_path
        builder, _ = self.builder()
        builder.preflight()
        builder.llvm_phase = 'headers'
        tools, objects = builder.paths['tools'], builder.paths['objects']
        records = {path: {'signature': copy.deepcopy(builder.context_signature(path)),
                          'sources': copy.deepcopy(builder.sources)} for path in (tools, objects)}
        builder.paths['diffutils-source'] = '/prepared/diffutils'
        builder.signature['common_sources']['diffutils'] = {'tree_sha256': 'a' * 64}
        builder.signature['flags'].append('EMBER_DIFFUTILS_SOURCE=/prepared/diffutils')
        builder.signature['native_toolset']['tools']['gmake'] = str(Path(builder.signature['native_toolset']['tools']['cc']).parent / 'gmake')
        builder.signature['environment']['PATH'] = native_path(builder.signature['native_toolset'])
        for path in (tools, objects):
            self.assertTrue(builder.compatible_context(records[path], path))
        builder.llvm_phase = 'base'
        self.assertFalse(builder.compatible_context(records[objects], objects))

    def test_ekermit_selection_reuses_headers_and_native_tools(self):
        builder, _ = self.builder()
        builder.preflight()
        builder.llvm_phase = 'headers'
        tools, objects = builder.paths['tools'], builder.paths['objects']
        records = {path: {'signature': copy.deepcopy(builder.context_signature(path)),
                          'sources': copy.deepcopy(builder.sources)} for path in (tools, objects)}
        builder.paths['ekermit-source'] = '/prepared/ekermit'
        builder.signature['common_sources']['ekermit'] = {'tree_sha256': 'a' * 64}
        builder.signature['flags'].append('EMBER_EKERMIT_SOURCE=/prepared/ekermit')
        self.assertTrue(builder.compatible_context(records[tools], tools))
        self.assertTrue(builder.compatible_context(records[objects], objects))
        builder.llvm_phase = 'base'
        self.assertFalse(builder.compatible_context(records[objects], objects))

    def test_header_objects_do_not_depend_on_byacc_source(self):
        builder, _ = self.builder()
        builder.preflight()
        builder.llvm_phase = 'headers'
        object_path = builder.paths['objects']
        record = {'signature': copy.deepcopy(builder.context_signature(object_path))}
        builder.paths['byacc-source'] = '/prepared/byacc'
        builder.signature['common_sources']['byacc'] = {'tree_sha256': 'a' * 64}
        builder.signature['flags'].append('EMBER_BYACC_SOURCE=/prepared/byacc')
        self.assertTrue(builder.compatible_context(record, object_path))

    def test_x11_source_changes_invalidate_native_tools(self):
        builder, _ = self.builder()
        builder.preflight()
        builder.llvm_phase = 'headers'
        tool_path = builder.paths['tools']
        record = {'signature': copy.deepcopy(builder.context_signature(tool_path))}
        builder.paths['x11-source'] = '/prepared/xsrc'
        builder.signature['common_sources']['x11'] = {'tree_sha256': 'a' * 64}
        self.assertFalse(builder.compatible_context(record, tool_path))
        record['signature'] = copy.deepcopy(builder.context_signature(tool_path))
        self.assertTrue(builder.compatible_context(record, tool_path))
        builder.signature['common_sources']['x11']['tree_sha256'] = 'b' * 64
        self.assertFalse(builder.compatible_context(record, tool_path))
