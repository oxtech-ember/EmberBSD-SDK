# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Initial Darwin make configuration. No tool or compiler execution."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import struct
from unittest.mock import patch

from test_core import TemporaryCase, SDK_SOURCE
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.darwin_makeconf import write_makeconf, render_variables


FACTS = dict(os='Darwin', cpu='aarch64', kernel_version='25.0.0',
             product_version='26.0', pkgsrc_opsys_version='260000')
EARLY = 'awk basename cat cut date dirname env expr grep head id pwd sed sort tail tr uname uniq wc'
ROLES = ('cc cxx cpp make gmake sh as ld dsymutil ar ranlib nm strip '
         'pkg_info pkg_admin pkg_add pkg_create').split() + EARLY.split()


class DarwinMakeconfTests(TemporaryCase):
    def setUp(self):
        super().setUp()
        native = bytearray(32)
        struct.pack_into('<4I', native, 0, 0xfeedfacf, 0x100000c, 0, 2)
        self.tools = {name: str(self.write('initial/bin/' + name, native, 0o755))
                      for name in set(ROLES)}
        self.write('pkgsrc/EMBERBSD-DARWIN-BOOTSTRAP-MK.CONF',
                   '_EMBERBSD_DARWIN_EARLY_TOOLS= ' + EARLY + '\n'
                   '.for _ember_tool in gzip gunzip gzcat\n.endfor\n')
        self.write('pkgsrc/EMBERBSD-DEVELOPMENT-MK.CONF', '# selected common policy\n')
        self.write('mk/sys.mk', '.SHELL: name=sh path=' + self.tools['sh'] + '\n')
        self.write('abi/SDKSettings.plist', plistlib.dumps({'Version': '26.0'}))
        self.write('abi/usr/lib/libiconv.tbd', 'fixture ABI\n')
        (self.root / 'work').mkdir()
        self.options = dict(pkgsrc=str(self.root / 'pkgsrc'), output=str(self.root / 'work/mk.conf'),
            prefix=str(self.root / 'final-prefix'), work_root=str(self.root / 'work'),
            distfiles=str(self.root / 'distfiles'), packages=str(self.root / 'packages'),
            pkgdb=str(self.root / 'final-prefix/pkgdb'), utility_bin=str(self.root / 'work/tools'),
            makefiles=str(self.root / 'mk'), macos_sdk=str(self.root / 'abi'), deployment='15.0',
            tools=self.tools, compiler_args={name: ('-B/explicit/driver/', '-specs=/explicit/seed.specs',
                '-isysroot', str(self.root / 'abi')) for name in ('cc', 'cxx', 'cpp')},
            tool_roots=(str(self.root / 'initial'),), jobs=1)

    def render(self, **changes):
        with patch('emberbsd_sdk.darwin_makeconf._current_host_facts', return_value=FACTS), \
             patch('emberbsd_sdk.process.capture', side_effect=AssertionError('tool query during rendering')):
            return write_makeconf(**(self.options | changes))

    def test_writes_direct_configuration_and_full_initial_make_command(self):
        result = self.render()
        data = Path(self.options['output']).read_text()
        self.assertIn('EMBERBSD_DARWIN_SEED_CC= ' + self.tools['cc'] +
                      ' -B/explicit/driver/ -specs=/explicit/seed.specs -isysroot ' + self.options['macos_sdk'], data)
        self.assertIn('.include "' + self.options['pkgsrc'] + '/EMBERBSD-DARWIN-BOOTSTRAP-MK.CONF"', data)
        self.assertIn('LOCALBASE= ' + self.options['prefix'], data)
        self.assertIn('EMBERBSD_DARWIN_SOURCE_TOOLS= gunzip gzcat gzip', data)
        self.assertEqual(result['initial_command'], [self.tools['make'], '-m', self.options['makefiles'],
            'SHELL=' + self.tools['sh'], 'MAKECONF=' + self.options['output'],
            'EMBERBSD_DARWIN_BOOTSTRAP=yes', '-C', self.options['pkgsrc'] + '/devel/bmake', 'build', 'install'])
        self.assertEqual(os.readlink(Path(self.options['utility_bin']) / 'awk'), self.tools['awk'])
        self.assertFalse((Path(self.options['utility_bin']) / 'cc').exists())
        self.assertFalse(any('receipt' in key or 'sha256' in key for key in result))
        self.assertEqual(self.render(), result)

    def test_default_family_selects_clang_without_gcc_policy_inputs(self):
        result = self.render()
        data = Path(self.options['output']).read_text()
        self.assertIn('EMBERBSD_COMPILER_FAMILY= clang\n', data)
        self.assertIn('EMBERBSD_DARWIN_LD64_LLD= ' + self.tools['ld'], data)
        self.assertIn('EMBERBSD_DARWIN_SEED_AS= ' + self.tools['cc'], data)
        self.assertIn(' -c -x assembler\n', data)
        self.assertNotIn('EMBERBSD_GCC16_', data)
        self.assertEqual(result['compiler_family'], 'clang')

    def test_gcc_family_is_an_explicit_option(self):
        result = self.render(compiler_family='gcc')
        self.assertIn('EMBERBSD_COMPILER_FAMILY= gcc\n', Path(self.options['output']).read_text())
        self.assertEqual(result['compiler_family'], 'gcc')

    def test_parallel_configuration_persists_budget_and_shared_launcher(self):
        for family in ('clang', 'gcc'):
            self.render(jobs=18, cpu_budget=18, compiler_family=family,
                        link_python=self.tools['cc'], lock_root=self.root / 'work',
                        heavy_link_context=self.root / 'work/heavy-link')
            data = Path(self.options['output']).read_text()
            self.assertIn('EMBERBSD_DARWIN_JOBS= 18\n', data)
            self.assertIn('EMBERBSD_DARWIN_CPU_BUDGET= 18\n', data)
            words = [self.tools['cc'], str(SDK_SOURCE / 'scripts/link-launcher.py'),
                     '--lock-root', str(self.root / 'work'), '--context', str(self.root / 'work/heavy-link'), '--']
            self.assertIn('EMBERBSD_DARWIN_CMAKE_LINKER_LAUNCHER= ' + ';'.join(words) + '\n', data)
            self.assertEqual('EMBERBSD_GCC16_LINKER_LAUNCHER=' in data, family == 'gcc')

    def test_invalid_parallel_inputs_preserve_no_outputs(self):
        for changes in ({'jobs': 0}, {'jobs': True}, {'jobs': -2}, {'jobs': 18, 'cpu_budget': 8},
                        {'cpu_budget': 0}, {'jobs': 18}, {'link_python': self.tools['cc']}):
            with self.subTest(changes=changes), self.assertRaises(SDKError):
                self.render(**changes)
            self.assertFalse(Path(self.options['output']).exists())
            self.assertFalse(Path(self.options['utility_bin']).exists())

    def test_unknown_family_fails_without_outputs(self):
        with self.assertRaisesRegex(SDKError, 'compiler family'):
            self.render(compiler_family='automatic')
        self.assertFalse(Path(self.options['output']).exists())

    def test_missing_role_fails_without_path_fallback_or_outputs(self):
        tools = dict(self.tools); del tools['awk']
        with self.assertRaisesRegex(SDKError, 'missing.*awk'):
            self.render(tools=tools)
        self.assertFalse(Path(self.options['output']).exists())
        self.assertFalse(Path(self.options['utility_bin']).exists())

    def test_returned_environment_drops_inherited_build_overrides(self):
        with patch.dict(os.environ, {'MAKEFLAGS': '-m /outside', 'COMPILER_PATH': '/outside',
                                     'CC': '/outside/cc', 'CONFIG_SITE': '/outside/site'}):
            result = self.render()
        env = result['environment']
        self.assertEqual(env['PATH'], self.options['utility_bin'])
        self.assertEqual(env['LC_ALL'], 'C')
        self.assertEqual(env['PATH_SEPARATOR'], os.pathsep)
        self.assertFalse({'MAKEFLAGS', 'COMPILER_PATH', 'CC', 'CONFIG_SITE'} & set(env))

    def test_rejects_external_tool_and_wrong_cpu(self):
        tools = dict(self.tools); tools['awk'] = self.tools['awk'].replace('/initial/bin/awk', '/outside')
        self.write('outside', Path(self.tools['awk']).read_bytes(), 0o755)
        with self.assertRaisesRegex(SDKError, 'escapes'):
            self.render(tools=tools)
        wrong = bytearray(Path(self.tools['awk']).read_bytes())
        struct.pack_into('<I', wrong, 4, 0x1000007)
        Path(self.tools['awk']).write_bytes(wrong)
        with self.assertRaisesRegex(SDKError, 'native executable'):
            self.render()

    def test_preserves_internal_tool_alias_and_explicit_second_root(self):
        original = Path(self.tools['awk'])
        real = original.with_name('nawk'); original.rename(real); original.symlink_to('nawk')
        shell = self.write('second/bin/sh', Path(self.tools['sh']).read_bytes(), 0o755)
        script = self.write('initial/bin/script', ('#!' + str(shell) + '\n').encode(), 0o755)
        result = self.render(tools=self.tools | {'script': str(script)},
                             tool_roots=(*self.options['tool_roots'], str(self.root / 'second')))
        self.assertEqual(os.readlink(Path(result['utility_bin']) / 'awk'), str(original))

    def test_relative_compiler_executable_cannot_use_ambient_path(self):
        with self.assertRaises(SDKError):
            self.render(tools=self.tools | {'cc': 'gcc'})

    def test_rejects_make_injection_and_whitespace(self):
        for bad in ('two words', '$(shell)', 'x\ny', 'x#y', 'x;false'):
            with self.subTest(value=bad), self.assertRaises(SDKError):
                self.render(compiler_args={'cc': (bad,), 'cxx': (), 'cpp': ()})
        with self.assertRaises(SDKError):
            self.render(prefix=str(self.root / 'space prefix'))

    def test_output_must_stay_in_explicit_work_root(self):
        with self.assertRaisesRegex(SDKError, 'work root'):
            self.render(output=str(self.root / 'outside.conf'))
        with self.assertRaisesRegex(SDKError, 'work root'):
            self.render(utility_bin=str(self.root / 'outside-tools'))

    def test_rejects_foreign_existing_utility_mapping(self):
        directory = Path(self.options['utility_bin']); directory.mkdir()
        (directory / 'awk').symlink_to(self.tools['sh'])
        with self.assertRaisesRegex(SDKError, 'utility mapping'):
            self.render()
        self.assertEqual(os.readlink(directory / 'awk'), self.tools['sh'])
        self.assertFalse(Path(self.options['output']).exists())

    def test_preserves_old_config_and_removes_own_mapping_after_write_failure(self):
        output = Path(self.options['output']); output.write_text('old\n')
        with patch('emberbsd_sdk.darwin_makeconf.os.replace', side_effect=OSError('fixture write failure')):
            with self.assertRaises(OSError):
                self.render()
        self.assertEqual(output.read_text(), 'old\n')
        self.assertFalse(Path(self.options['utility_bin']).exists())

    def test_rejects_unbound_make_shell_and_missing_sdk_metadata(self):
        Path(self.options['makefiles'], 'sys.mk').write_text('# no selected shell\n')
        with self.assertRaisesRegex(SDKError, 'make shell'):
            self.render()
        Path(self.options['makefiles'], 'sys.mk').write_text('.SHELL: name=sh path=' + self.tools['sh'] + '\n')
        Path(self.options['macos_sdk'], 'SDKSettings.plist').unlink()
        with self.assertRaises((SDKError, OSError)):
            self.render()

    def test_accepts_internal_abi_alias_without_changing_link_flags(self):
        alias = Path(self.options['macos_sdk'], 'usr/lib/libiconv.tbd')
        self.render(ldflags=(str(alias),))
        original = Path(self.options['output']).read_bytes()
        alias.rename(alias.with_name('libiconv.2.tbd'))
        alias.symlink_to('libiconv.2.tbd')
        self.render(ldflags=(str(alias),))
        self.assertEqual(Path(self.options['output']).read_bytes(), original)
        self.assertEqual(os.readlink(alias), 'libiconv.2.tbd')

    def test_rejects_broken_abi_alias_without_outputs(self):
        alias = Path(self.options['macos_sdk'], 'usr/lib/libiconv.tbd')
        alias.unlink(); alias.symlink_to('missing.tbd')
        with self.assertRaises(SDKError):
            self.render()
        self.assertFalse(Path(self.options['output']).exists())
        self.assertFalse(Path(self.options['utility_bin']).exists())

    def test_rejects_external_abi_alias_without_outputs(self):
        alias = Path(self.options['macos_sdk'], 'usr/lib/libiconv.tbd')
        outside = self.write('external.tbd', 'outside ABI\n')
        alias.unlink(); alias.symlink_to(outside)
        with self.assertRaises(SDKError):
            self.render()
        self.assertFalse(Path(self.options['output']).exists())
        self.assertFalse(Path(self.options['utility_bin']).exists())

    def test_shared_format_preserves_historical_nested_flags(self):
        self.assertEqual(render_variables({'Z': 'last', 'LDFLAGS': '-L/abi'}, '/profile',
                                         nested_ldflags=True, metadata_make='/make'),
            b'# SPDX-License-Identifier: BSD-2-Clause\n'
            b'# Source-owned SDK metadata inputs. This file is not qualification.\n'
            b'.if defined(BSD_PKG_MK)\nLDFLAGS= -L/abi\n.endif\nZ= last\n'
            b'.include "/profile"\n_MAKE= /make\nMAKE= /make\n')

    def cli(self, extra=()):
        spec = importlib.util.spec_from_file_location('darwin_cli', SDK_SOURCE / 'scripts/prepare-darwin-bootstrap.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        argv = ['--sdk-source', str(SDK_SOURCE)]
        for name, value in self.options.items():
            if name in ('tools', 'compiler_args', 'tool_roots'):
                continue
            argv += ['--' + name.replace('_', '-'), str(value)]
        for root in self.options['tool_roots']:
            argv += ['--tool-root', root]
        for name, path in self.tools.items():
            argv += ['--tool', name + '=' + path]
        for role, words in self.options['compiler_args'].items():
            argv += ['--' + role + '-arg=' + word for word in words]
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch('emberbsd_sdk.darwin_makeconf._current_host_facts', return_value=FACTS), \
             patch('emberbsd_sdk.process.capture', side_effect=AssertionError('compiler query')), \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = module.main([*argv, *extra])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_cli_writes_configuration_without_prior_acceptance(self):
        code, stdout, stderr = self.cli()
        self.assertEqual((code, stderr), (0, ''))
        self.assertEqual(json.loads(stdout)['makeconf'], self.options['output'])
        self.assertIn('-B/explicit/driver/ -specs=/explicit/seed.specs', Path(self.options['output']).read_text())

    def test_cli_persists_parallel_inputs(self):
        self.options.update(jobs=18, cpu_budget=18, link_python=self.tools['cc'],
                            lock_root=self.root / 'work', heavy_link_context=self.root / 'work/heavy-link')
        code, stdout, stderr = self.cli()
        self.assertEqual((code, stderr), (0, ''))
        data = Path(json.loads(stdout)['makeconf']).read_text()
        self.assertIn('EMBERBSD_DARWIN_JOBS= 18\n', data)
        self.assertIn('EMBERBSD_DARWIN_CPU_BUDGET= 18\n', data)
        self.assertIn('EMBERBSD_DARWIN_CMAKE_LINKER_LAUNCHER=', data)

    def test_cli_reports_duplicate_tool_without_traceback_or_output(self):
        code, stdout, stderr = self.cli(('--tool', 'awk=' + self.tools['awk']))
        self.assertEqual((code, stdout), (1, ''))
        self.assertIn('supply each tool once', stderr)
        self.assertNotIn('Traceback', stderr)
        self.assertFalse(Path(self.options['output']).exists())

    def test_cli_reports_make_injection_without_output(self):
        code, stdout, stderr = self.cli(('--cc-arg=$(false)',))
        self.assertEqual((code, stdout), (1, ''))
        self.assertIn('unsupported make or shell syntax', stderr)
        self.assertFalse(Path(self.options['output']).exists())
