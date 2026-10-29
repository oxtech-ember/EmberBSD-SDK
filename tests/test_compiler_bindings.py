# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Compiler adapters preserve exact native ABI and caller argument words."""

import json
import os
import shlex
from pathlib import Path
import subprocess
import sys
import unittest

from test_core import TemporaryCase
from emberbsd_sdk import compiler_bindings
from emberbsd_sdk.errors import SDKError


class CompilerBindingTests(TemporaryCase):
    def setUp(self):
        super().setUp()
        self.compiler = self.root / 'compiler'
        self.compiler.write_text('#!' + sys.executable + '\nimport json,sys\n'
            'print(json.dumps(sys.argv[1:]))\n'
            'sys.exit(7 if "--fail" in sys.argv else 0)\n')
        self.compiler.chmod(0o755)
        self.commands = {role:[str(self.compiler), '--selected-' + role] for role in ('cc','cxx','cpp','csa')}
        self.flags = dict(cppflags=['-I', '/include path'], cflags=['-DNAME=a b'],
            cxxflags=['-std=c++20'], ldflags=['-L', '/library path', '-lnative'])
        self.destination = self.root / 'binding with space'

    def invoke(self, role, arguments, *, destination=None):
        tools = compiler_bindings.bind_compilers(destination or self.destination,
            commands=self.commands, flags=self.flags, python=sys.executable)
        result = subprocess.run([tools[role], *arguments], cwd=self.root, capture_output=True, text=True)
        return result, json.loads(result.stdout)

    def test_c_link_preserves_quoting_and_places_libraries_after_inputs(self):
        result, args = self.invoke('cc', ['source with space.c', '-o', 'output name', '-Werror'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((self.destination / 'commands.json').read_text())['version'], 1)
        self.assertEqual(args, ['--selected-cc','-I','/include path','-DNAME=a b',
            'source with space.c','-o','output name','-Werror','-L','/library path','-lnative'])

    def test_cxx_compile_and_preprocessor_do_not_receive_linker_flags(self):
        _, args = self.invoke('cxx', ['-c', 'c++.cc'])
        self.assertEqual(args, ['--selected-cxx','-I','/include path','-std=c++20','-c','c++.cc'])
        _, args = self.invoke('cpp', ['input.c'])
        self.assertEqual(args, ['--selected-cpp','-I','/include path','input.c'])

    def test_static_analyzer_keeps_c_arguments_without_link_flags(self):
        _, args = self.invoke('csa', ['--analyze', 'input.c'])
        self.assertEqual(args, ['--selected-csa','-I','/include path','-DNAME=a b','--analyze','input.c'])

    def test_response_file_compile_mode_keeps_original_argument(self):
        response = self.root / 'arguments with space.rsp'
        response.write_text('-c "input with space.c" -Werror')
        _, args = self.invoke('cc', ['@' + str(response)])
        self.assertEqual(args, ['--selected-cc','-I','/include path','-DNAME=a b','@' + str(response)])

    def test_query_does_not_receive_build_flags_and_exit_status_is_preserved(self):
        _, args = self.invoke('cc', ['-print-resource-dir'])
        self.assertEqual(args, ['--selected-cc','-print-resource-dir'])
        result, _ = self.invoke('cc', ['--fail'])
        self.assertEqual(result.returncode, 7)

    def test_assembler_and_linker_preserve_only_their_selected_arguments(self):
        self.commands['as'] = [str(self.compiler), '--target=aarch64--netbsd', '-c', '-x', 'assembler']
        self.commands['ld'] = [str(self.compiler), '--sysroot=/selected target']
        for role in ('as', 'ld'):
            with self.subTest(role=role):
                # These response files belong to the selected tool. The compiler
                # binding must not parse them or attach C/C++ ABI/link flags.
                arguments = ['@tool-specific.rsp', '--', 'input with space.o']
                result, args = self.invoke(role, arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(args, self.commands[role][1:] + arguments)

    def test_existing_binding_must_match_and_is_not_overwritten(self):
        self.invoke('cc', ['-c', 'x.c'])
        self.flags['cflags'] = ['-Dchanged']
        with self.assertRaisesRegex(SDKError, 'binding differs'):
            self.invoke('cc', ['-c', 'x.c'])

    def test_response_cycle_is_refused_without_running_compiler(self):
        response = self.root / 'cycle'; response.write_text('@' + str(response))
        tools = compiler_bindings.bind_compilers(self.destination,
            commands=self.commands, flags=self.flags, python=sys.executable)
        result = subprocess.run([tools['cc'], '@' + str(response)], cwd=self.root, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')
        self.assertIn('response', result.stderr)

    def test_link_option_terminator_cannot_turn_selected_flags_into_filenames(self):
        tools = compiler_bindings.bind_compilers(self.destination,
            commands=self.commands, flags=self.flags, python=sys.executable)
        response = self.root / 'terminated.rsp'; response.write_text('-- input.c')
        for arguments in (['--','input.c'], ['@' + str(response)]):
            with self.subTest(arguments=arguments):
                result = subprocess.run([tools['cc'],*arguments], cwd=self.root, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, '')
                self.assertIn('option terminator', result.stderr)

    def test_language_link_flags_are_not_compilation_flags_or_c_defaults(self):
        tools = compiler_bindings.bind_compilers(self.destination,
            commands=self.commands, flags=self.flags, python=sys.executable,
            link_flags={'cxx':['-stdlib=libc++']})
        for role, operation in (('cc', []), ('cxx', []), ('cxx', ['-c']), ('cpp', [])):
            with self.subTest(role=role, operation=operation):
                result = subprocess.run([tools[role], *operation, 'input.cc'],
                    cwd=self.root, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                args = json.loads(result.stdout)
                self.assertEqual('-stdlib=libc++' in args, role == 'cxx' and not operation)
        with self.assertRaisesRegex(SDKError, 'link.*role'):
            compiler_bindings.bind_compilers(self.destination, commands=self.commands,
                flags=self.flags, python=sys.executable, link_flags={'cpp':['-stdlib=libc++']})

    def runtime_linker(self):
        """A driver that logs its argv and writes an ELF64 with DT_NEEDED from --needs=NAME."""
        log = self.root / 'links.log'
        linker = self.root / 'linking compiler'
        linker.write_text('#!' + sys.executable + '\nimport json,struct,sys\n'
            'args = sys.argv[1:]\n'
            f'open({str(log)!r}, "a").write(json.dumps(args) + "\\n")\n'
            'if "--fail" in args: sys.exit(7)\n'
            'if "-c" in args or "-o" not in args: sys.exit(0)\n'
            'names = [word[8:].encode() for word in args if word.startswith("--needs=")]\n'
            'strings, offsets = b"\\0", []\n'
            'for name in names: offsets.append(len(strings)); strings += name + b"\\0"\n'
            'tags = [(5, 0x1000 + 400), (10, len(strings))] + [(1, at) for at in offsets] + [(0, 0)]\n'
            'dynamic = b"".join(struct.pack("<qQ", tag, value) for tag, value in tags)\n'
            'data = bytearray(512)\n'
            'data[:7] = b"\\x7fELF\\x02\\x01\\x01"\n'
            'struct.pack_into("<HHIQQ", data, 16, 3, 183, 1, 0, 64)\n'
            'struct.pack_into("<HHH", data, 52, 64, 56, 2)\n'
            'struct.pack_into("<IIQQQQQQ", data, 64, 1, 4, 0, 0x1000, 0, 512, 512, 4096)\n'
            'struct.pack_into("<IIQQQQQQ", data, 120, 2, 4, 256, 0x1100, 0, len(dynamic), len(dynamic), 8)\n'
            'data[256:256 + len(dynamic)] = dynamic\n'
            'data[400:400 + len(strings)] = strings\n'
            'open(args[args.index("-o") + 1], "wb").write(data)\n')
        linker.chmod(0o755)
        commands = {role:[str(linker), '--selected-' + role] for role in ('cc','cxx','cpp')}
        tools = compiler_bindings.bind_compilers(self.root / 'runtime binding', commands=commands,
            flags=self.flags, python=sys.executable, link_flags={'cxx':['-stdlib=libc++']},
            runtime_rpath={'flags':['-Wl,-rpath,/runtime lib'], 'libraries':['libc++', 'libunwind']})
        return tools, log

    def test_runtime_search_path_only_in_outputs_that_need_a_runtime_library(self):
        tools, log = self.runtime_linker()
        cases = (('cc', ['input.o'], [False]),
                 ('cc', ['input.o', '--needs=libunwind.so.1'], [False, True]),
                 ('cc', ['input.o', '--needs=libunwind-extra.so.1', '--needs=libc.so.12'], [False]),
                 ('cxx', ['input.o', '--needs=libc++.so.1'], [True]),
                 ('cxx', ['input.o', '--needs=libc.so.12'], [True, False]),
                 ('cxx', ['-nostdlib++', 'input.o'], [False]),
                 ('cxx', ['-c', 'input.cc'], [False]),
                 ('cc', ['-r', 'input.o', '--needs=libunwind.so.1'], [False]))
        for role, arguments, runs in cases:
            with self.subTest(role=role, arguments=arguments):
                log.unlink(missing_ok=True)
                output = self.root / 'linked output'
                output.unlink(missing_ok=True)
                result = subprocess.run([tools[role], *arguments, '-o', str(output)],
                    cwd=self.root, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                calls = [json.loads(line) for line in log.read_text().splitlines()]
                self.assertEqual(['-Wl,-rpath,/runtime lib' in call for call in calls], runs)
                # A repeated link runs the same caller argv.
                self.assertEqual({tuple(call[:call.index('-o') + 2]) for call in calls}, {tuple(calls[0][:calls[0].index('-o') + 2])})

    def test_runtime_search_path_link_failure_is_not_retried(self):
        tools, log = self.runtime_linker()
        result = subprocess.run([tools['cxx'], 'input.o', '--fail', '-o', 'output'],
            cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(len(log.read_text().splitlines()), 1)
        for runtime_rpath in ({'flags':[], 'libraries':['libc++']}, {'flags':['-Wl,-rpath,/x'], 'libraries':['c++']},
                              {'flags':['-Wl,-rpath,/x']}):
            with self.subTest(runtime_rpath=runtime_rpath), self.assertRaisesRegex(SDKError, 'runtime search path'):
                compiler_bindings.bind_compilers(self.root / 'refused', commands=self.commands,
                    flags=self.flags, python=sys.executable, runtime_rpath=runtime_rpath)

    @unittest.skipUnless(os.environ.get('EMBER_TEST_CLANG'), 'requires explicitly selected Clang driver')
    def test_real_target_assembler_and_relocatable_linker(self):
        clang = os.environ['EMBER_TEST_CLANG']
        linker = str(Path(clang).parent / 'ld.lld')
        source = self.root / 'input.s'; source.write_text('.text\n.globl probe\nprobe:\nret\n')
        for target, machine in (('aarch64--netbsd', 183), ('x86_64--netbsd', 62)):
            with self.subTest(target=target):
                commands = {role:[clang, '--target='+target] for role in ('cc','cxx','cpp')}
                commands['as'] = [clang, '--target='+target, '-c', '-x', 'assembler']
                commands['ld'] = [linker, '--sysroot='+str(self.root)]
                tools = compiler_bindings.bind_compilers(self.root/target,
                    commands=commands, flags=self.flags, python=sys.executable)
                obj, output = self.root/(target+'.o'), self.root/(target+'-linked.o')
                for argv in ([tools['as'], '-Werror', str(source), '-o', str(obj)],
                             [tools['ld'], '-r', str(obj), '-o', str(output)]):
                    result = subprocess.run(argv, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                header = output.read_bytes()[:20]
                self.assertEqual(header[:6], b'\x7fELF\x02\x01')
                self.assertEqual(int.from_bytes(header[16:18], 'little'), 1)
                self.assertEqual(int.from_bytes(header[18:20], 'little'), machine)

    @unittest.skipUnless(os.environ.get('EMBER_TEST_CLANG'), 'requires explicitly selected Clang driver')
    def test_real_clang_compile_diagnostics_and_cxx_link_selection(self):
        clang = os.environ['EMBER_TEST_CLANG']
        source = self.root/'input.cc'; source.write_text('int probe(void) { return 0; }\n')
        for target in ('aarch64--netbsd', 'x86_64--netbsd'):
            commands = dict(cc=[clang, '--target='+target],
                cxx=[clang, '--driver-mode=g++', '--target='+target],
                cpp=[clang, '--target='+target, '-E'])
            tools = compiler_bindings.bind_compilers(self.root/target, commands=commands,
                flags=dict(cppflags=[], cflags=[], cxxflags=['-nostdinc++'], ldflags=[]),
                python=sys.executable, link_flags={'cxx':['-stdlib=libc++']})
            for mode in ('-c','-S','-E','-fsyntax-only'):
                with self.subTest(target=target, mode=mode):
                    argv = [tools['cxx'], '-Werror', mode, str(source)]
                    if mode != '-fsyntax-only':
                        argv += ['-o', str(self.root/'output')]
                    result = subprocess.run(argv, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
            for role, language in (('cc','c'), ('cxx','c++')):
                with self.subTest(target=target, role=role):
                    result = subprocess.run([tools[role], '-###', '-x', language, str(source)],
                        capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual('"-lc++"' in result.stderr, role == 'cxx')
                    self.assertNotIn('"-lstdc++"', result.stderr)


class NativeCxxRuntimeBindingTests(TemporaryCase):
    def setUp(self):
        super().setUp()
        self.compiler = self.root / 'compiler'
        self.compiler.write_text('#!' + sys.executable + '\nimport json,sys\n'
                                 'print(json.dumps(sys.argv[1:]))\n')
        self.compiler.chmod(0o755)
        self.include = self.root / 'runtime headers'; self.include.mkdir()
        self.lib = self.root / 'runtime lib'; self.lib.mkdir()
        self.sdk = self.root / 'native sdk'; self.sdk.mkdir()
        self.runtime = dict(include_dir=str(self.include), library_dir=str(self.lib),
                            sysroot=str(self.sdk), targets=['arm64-apple-darwin'], deployment='26.0')
        self.commands = {role:[str(self.compiler), '--selected-'+role] for role in ('cc','cxx','cpp')}
        self.flags = dict(cppflags=[], cflags=[], cxxflags=[], ldflags=[])
        self.tools = compiler_bindings.bind_compilers(self.root/'commands', commands=self.commands,
                    flags=self.flags, python=sys.executable, native_cxx_runtime=self.runtime)

    def invoke(self, role, args):
        result = subprocess.run([self.tools[role], *args], cwd=self.root,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_compile_link_and_other_roles(self):
        compile_args = self.invoke('cxx', ['-c', 'source.cc'])
        self.assertIn('-stdlib++-isystem', compile_args)
        self.assertIn(str(self.include), compile_args)
        self.assertNotIn('-L'+str(self.lib), compile_args)
        link_args = self.invoke('cxx', ['-L/custom', 'source.cc'])
        self.assertLess(link_args.index('-L/custom'), link_args.index('-L'+str(self.lib)))
        for role in ('cc', 'cpp'):
            args = self.invoke(role, ['-c', 'source.c'])
            self.assertNotIn(str(self.include), args)
            self.assertNotIn('-L'+str(self.lib), args)
        self.assertNotIn(str(self.include), self.invoke('cxx', ['--version']))

    def test_caller_overrides_and_native_context(self):
        for args, includes, library in (
            (['-nostdinc++', 'source.cc'], False, True),
            (['-nostdinc', 'source.cc'], False, True),
            (['-stdlib++-isystem', str(self.include), 'source.cc'], True, True),
            (['-stdlib=libstdc++', 'source.cc'], False, False),
            (['-stdlib=libstdc++', '-stdlib++-isystem', '/user', 'source.cc'], False, False),
            (['-stdlib=libstdc++', '-stdlib=libc++', 'source.cc'], True, True),
            (['-nostdlib', 'source.cc'], True, False),
            (['-nodefaultlibs', 'source.cc'], True, False),
            (['-nostdlib++', 'source.cc'], True, False),
            (['-r', 'source.o'], True, False),
            (['-target', 'arm64-apple-darwin', 'source.cc'], True, True),
            (['-arch', 'arm64', 'source.cc'], True, True),
            (['-I', '-target', 'source.cc'], True, True),
            (['-isysroot', str(self.sdk), 'source.cc'], True, True),
        ):
            with self.subTest(args=args):
                result = self.invoke('cxx', args)
                self.assertEqual(str(self.include) in result, includes)
                self.assertEqual('-L'+str(self.lib) in result, library)

    def test_incompatible_native_context_fails_closed(self):
        for args in (['--target=x86_64-unknown-netbsd'], ['-arch', 'x86_64'],
                     ['-m32'], ['--sysroot=/other'], ['-isysroot=/other'],
                     ['-stdlib++-isystem', '/user'],
                     ['-stdlib++-isystem', '/user', '-stdlib++-isystem', str(self.include)],
                     ['-mmacosx-version-min=25.9'],
                     ['-mmacos-version-min=25.9']):
            with self.subTest(args=args):
                result = subprocess.run([self.tools['cxx'], *args, '-c', 'source.cc'],
                    cwd=self.root, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
        self.assertIn('-mmacosx-version-min=26.0', self.invoke('cxx', ['-c', 'source.cc']))
        self.assertIn('-mmacos-version-min=26.0', self.invoke('cxx', ['-mmacos-version-min=26.0', '-c', 'source.cc']))
        response = self.root/'deployment.rsp'
        response.write_text('-mmacos-version-min=25.0 -c source.cc')
        result = subprocess.run([self.tools['cxx'], '@'+str(response)], cwd=self.root,
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        response.write_text('-stdlib++-isystem /user -stdlib++-isystem ' +
                            shlex.quote(str(self.include)) + ' -c source.cc')
        result = subprocess.run([self.tools['cxx'], '@'+str(response)], cwd=self.root,
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)

    def test_selected_stdlib_is_removed_only_for_bound_compile(self):
        record = json.loads((self.root/'commands'/'commands.json').read_text())
        record['flags']['cxxflags'] = ['-Wall', '-stdlib=libc++', '-Wextra']
        for action in ('-c', '-E', '-S', '-fsyntax-only'):
            args = compiler_bindings.compiler_argv(record, 'cxx',
                                                   [action, 'source.cc'])
            self.assertNotIn('-stdlib=libc++', args)
            self.assertLess(args.index('-Wall'), args.index('-Wextra'))
        args = compiler_bindings.compiler_argv(record, 'cxx', ['source.cc'])
        self.assertIn('-stdlib=libc++', args)
        for arguments in (['-nostdinc++', '-c', 'source.cc'],
                          ['-stdlib=libstdc++', '-c', 'source.cc'],
                          ['-stdlib++-isystem', str(self.include), '-c', 'source.cc']):
            args = compiler_bindings.compiler_argv(record, 'cxx', arguments)
            self.assertIn('-stdlib=libc++', args)
        args = compiler_bindings.compiler_argv(record, 'cxx',
                          ['-stdlib=libc++', '-c', 'source.cc'])
        self.assertEqual(args.count('-stdlib=libc++'), 1)

    def test_m64_with_explicit_x86_64_native_selection(self):
        record = json.loads((self.root/'commands'/'commands.json').read_text())
        record['native_cxx_runtime']['targets'] = ['x86_64-apple-darwin']
        args = compiler_bindings.compiler_argv(record, 'cxx',
                ['--target=x86_64-apple-darwin', '-m64', '-c', 'source.cc'])
        self.assertIn(str(self.include), args)
        with self.assertRaises(SDKError):
            compiler_bindings.compiler_argv(record, 'cxx',
                ['--target=x86_64-apple-darwin', '-m32', '-c', 'source.cc'])

    def test_bound_link_flags_participate_in_native_selection(self):
        record = json.loads((self.root/'commands'/'commands.json').read_text())
        for flags in (['--target=x86_64-unknown-netbsd'], ['-mmacos-version-min=25.0'],
                      ['--sysroot=/other'], ['-stdlib++-isystem', '/foreign']):
            with self.subTest(flags=flags):
                record['flags']['ldflags'] = flags
                with self.assertRaises(SDKError):
                    compiler_bindings.compiler_argv(record, 'cxx', ['source.cc'])
                # Link-only options do not affect compile-only invocations.
                compiler_bindings.compiler_argv(record, 'cxx', ['-c', 'source.cc'])
        record['flags']['ldflags'] = []
        record['link_flags'] = {'cxx':['-stdlib=libstdc++']}
        argv = compiler_bindings.compiler_argv(record, 'cxx', ['source.cc'])
        self.assertNotIn(str(self.include), argv)
        self.assertNotIn('-L'+str(self.lib), argv)

    def test_macho_loader_paths_and_response_files(self):
        for args in (['-dynamiclib', '-install_name', '@rpath/libprobe.dylib', 'source.cc'],
                     ['-dynamiclib', '-Xlinker', '-install_name', '-Xlinker',
                      '@loader_path/libprobe.dylib', 'source.cc']):
            with self.subTest(args=args):
                self.assertIn('-L'+str(self.lib), self.invoke('cxx', args))
        rsp = self.root/'install-name.rsp'
        rsp.write_text('@rpath/libprobe.dylib')
        self.invoke('cxx', ['-dynamiclib', '-install_name', '@'+str(rsp), 'source.cc'])
        bad = subprocess.run([self.tools['cxx'], '@rpath/libprobe.dylib'], cwd=self.root,
                             capture_output=True, text=True)
        self.assertNotEqual(bad.returncode, 0)
        # Like Clang, an existing @name is still expanded in an option value.
        folder = self.root/'rpath'; folder.mkdir()
        (folder/'libprobe.dylib').write_text('expanded-name')
        old = os.getcwd()
        try:
            os.chdir(self.root)
            self.assertEqual(compiler_bindings._operation_words(
                ['-install_name', '@rpath/libprobe.dylib']), ['-install_name', 'expanded-name'])
        finally:
            os.chdir(old)

    def test_native_abi_identity_and_legacy_mismatch(self):
        directory = self.sdk/'usr/include/c++/v1'; directory.mkdir(parents=True)
        for name in ('cxxabi.h', '__cxxabi_config.h'):
            (directory/name).write_text('// selected ' + name + '\n')
        config = self.include/'__config_site'
        config.write_text('#define _LIBCPP_ABI_NAMESPACE __1\n')
        old = self.root/'legacy-platform-binding'
        compiler_bindings.bind_compilers(old, commands=self.commands, flags=self.flags,
            python=sys.executable, native_cxx_runtime=dict(self.runtime, platform_cxxabi=True))
        legacy = json.loads((old/'commands.json').read_text())
        self.assertEqual(legacy['version'], 3)
        config.write_text('#define _LIBCPP_ABI_NAMESPACE __ember1\n')
        final = self.lib/'libc++_ember1.1.0.dylib'; final.write_text('library')
        soname = self.lib/'libc++_ember1.1.dylib'; soname.symlink_to(final.name)
        linker = self.lib/'libc++.dylib'; linker.symlink_to(soname.name)
        selection = dict(self.runtime, platform_cxxabi=True,
            abi_identity=dict(namespace='__ember1', linker_target=soname.name))
        output = self.root/'ember-binding'
        compiler_bindings.bind_compilers(output, commands=self.commands, flags=self.flags,
                                        python=sys.executable, native_cxx_runtime=selection)
        record = json.loads((output/'commands.json').read_text())
        self.assertEqual(record['version'], 4)
        self.assertIn('-L'+str(self.lib), compiler_bindings.compiler_argv(record, 'cxx', ['x.cc']))
        with self.assertRaisesRegex(SDKError, 'legacy.*changed ABI'):
            compiler_bindings.compiler_argv(legacy, 'cxx', ['-c', 'x.cc'])
        legacy_v2 = json.loads((self.root/'commands'/'commands.json').read_text())
        with self.assertRaisesRegex(SDKError, 'legacy.*changed ABI'):
            compiler_bindings.compiler_argv(legacy_v2, 'cxx', ['-c', 'x.cc'])
        self.assertIn('-stdlib=libstdc++', compiler_bindings.compiler_argv(
            legacy_v2, 'cxx', ['-stdlib=libstdc++', '-c', 'x.cc']))
        for content in ('#define _LIBCPP_ABI_NAMESPACE __ember1\n#undef _LIBCPP_ABI_NAMESPACE\n',
                        '#define _LIBCPP_ABI_NAMESPACE __ember1\n#define _LIBCPP_ABI_NAMESPACE __1\n'):
            config.write_text(content)
            with self.assertRaisesRegex(SDKError, 'one definition'):
                compiler_bindings.compiler_argv(record, 'cxx', ['-c', 'x.cc'])
            with self.assertRaisesRegex(SDKError, 'one definition'):
                compiler_bindings.compiler_argv(legacy, 'cxx', ['-c', 'x.cc'])
        config.write_text('#define _LIBCPP_ABI_NAMESPACE __1\n')
        with self.assertRaisesRegex(SDKError, 'identity changed'):
            compiler_bindings.compiler_argv(record, 'cxx', ['-c', 'x.cc'])
        config.write_text('#define _LIBCPP_ABI_NAMESPACE __ember1\n')
        linker.unlink(); linker.symlink_to('libc++.1.dylib')
        with self.assertRaisesRegex(SDKError, 'identity changed'):
            compiler_bindings.compiler_argv(record, 'cxx', ['x.cc'])
        linker.unlink(); linker.symlink_to(soname.name)
        soname.unlink(); soname.symlink_to(self.root/'foreign.dylib')
        (self.root/'foreign.dylib').write_text('foreign')
        with self.assertRaisesRegex(SDKError, 'identity changed'):
            compiler_bindings.compiler_argv(record, 'cxx', ['x.cc'])

    def test_selected_platform_abi_header_view_and_identity(self):
        directory = self.sdk/'usr/include/c++/v1'; directory.mkdir(parents=True)
        for name in ('cxxabi.h', '__cxxabi_config.h'):
            (directory/name).write_text('// selected ' + name + '\n')
        output = self.root/'platform-binding'
        selection = dict(self.runtime, platform_cxxabi=True)
        compiler_bindings.bind_compilers(output, commands=self.commands, flags=self.flags,
                                        python=sys.executable, native_cxx_runtime=selection)
        record = json.loads((output/'commands.json').read_text())
        self.assertEqual(record['version'], 3)
        view = output/'darwin-cxxabi'
        self.assertEqual(set(path.name for path in view.iterdir()), {'cxxabi.h', '__cxxabi_config.h'})
        self.assertEqual((view/'cxxabi.h').resolve(), directory/'cxxabi.h')
        argv = compiler_bindings.compiler_argv(record, 'cxx', ['-c', 'source.cc'])
        self.assertIn('-idirafter', argv)
        self.assertEqual(argv[argv.index('-idirafter') + 1], str(view))
        self.assertLess(argv.index(str(self.include)), argv.index(str(view)))
        for role in ('cc', 'cpp'):
            self.assertNotIn(str(view), compiler_bindings.compiler_argv(record, role, ['-c', 'source.c']))
        self.assertNotIn(str(view), compiler_bindings.compiler_argv(record, 'cxx', ['-nostdinc++', '-c', 'source.cc']))
        self.assertNotIn(str(view), compiler_bindings.compiler_argv(record, 'cxx', ['-stdlib=libstdc++', '-c', 'source.cc']))
        compiler_bindings.bind_compilers(output, commands=self.commands, flags=self.flags,
                                        python=sys.executable, native_cxx_runtime=selection)
        link = view/'cxxabi.h'
        original = os.readlink(link)
        link.unlink()
        link.symlink_to(directory/'__cxxabi_config.h')
        with self.assertRaisesRegex(SDKError, 'view differs'):
            compiler_bindings.bind_compilers(output, commands=self.commands, flags=self.flags,
                                            python=sys.executable, native_cxx_runtime=selection)
        link.unlink()
        link.symlink_to(original)
        (directory/'cxxabi.h').write_text('// changed SDK input\n')
        with self.assertRaisesRegex(SDKError, 'differs'):
            compiler_bindings.bind_compilers(output, commands=self.commands, flags=self.flags,
                                            python=sys.executable, native_cxx_runtime=selection)
        foreign = self.root/'foreign-cxxabi.h'
        foreign.write_text('// foreign\n')
        (directory/'cxxabi.h').unlink()
        (directory/'cxxabi.h').symlink_to(foreign)
        with self.assertRaisesRegex(SDKError, 'escapes'):
            compiler_bindings.bind_compilers(self.root/'other-platform-binding', commands=self.commands,
                flags=self.flags, python=sys.executable, native_cxx_runtime=selection)

    def test_native_compiler_context_checks_all_three_roles(self):
        record = json.loads((self.root/'commands'/'commands.json').read_text())
        for role in ('cc', 'cxx', 'cpp'):
            record['commands'][role]['argv'] += ['-isysroot', str(self.sdk), '-mmacos-version-min=26.0']
        self.assertEqual(compiler_bindings.validate_native_compiler_context(record)['deployment'], (26, 0, 0))
        for role in ('cc', 'cxx', 'cpp'):
            original = list(record['commands'][role]['argv'])
            for bad in (['--sysroot=/other'], ['-mmacos-version-min=25.0'],
                        ['-mmacos-version-min=27.0']):
                record['commands'][role]['argv'] = original + bad
                with self.subTest(role=role, bad=bad), self.assertRaises(SDKError):
                    compiler_bindings.validate_native_compiler_context(record)
            record['commands'][role]['argv'] = original
        for role in ('cc', 'cxx'):
            for bad in ('--sysroot=/other', '-mmacos-version-min=27.0', '--target=x86_64-unknown-netbsd'):
                record['link_flags'] = {role:[bad]}
                with self.subTest(role=role, link=bad), self.assertRaises(SDKError):
                    compiler_bindings.validate_native_compiler_context(record)
        record.pop('link_flags')
        record['commands']['cc']['argv'] = [str(self.compiler), '-I', '-isysroot', str(self.sdk),
                                             '-mmacos-version-min=26.0']
        with self.assertRaisesRegex(SDKError, 'explicit macOS SDK'):
            compiler_bindings.validate_native_compiler_context(record)

    def test_new_records_fail_closed_under_version_one_command_shape(self):
        record = json.loads((self.root/'commands'/'commands.json').read_text())
        self.assertEqual(record['version'], 2)
        for role in ('cc', 'cxx', 'cpp'):
            with self.subTest(role=role), self.assertRaises(SDKError):
                # The previous reader passes commands[role] to _words directly.
                compiler_bindings._words(record['commands'][role])
        self.assertEqual(compiler_bindings.compiler_argv(record, 'cxx', ['--version']),
                         [str(self.compiler), '--selected-cxx', '--version'])

    def test_response_options_and_immutable_selection(self):
        response = self.root/'caller opts.rsp'
        response.write_text('-stdlib=libstdc++ -c source.cc')
        args = self.invoke('cxx', ['@'+str(response)])
        self.assertIn('@'+str(response), args)
        self.assertNotIn(str(self.include), args)
        self.assertNotIn('-L'+str(self.lib), args)
        response.write_text('-Xclang -triple -Xclang x86_64-unknown-netbsd -c source.cc')
        forwarded = subprocess.run([self.tools['cxx'], '@'+str(response)], cwd=self.root,
                                   capture_output=True, text=True)
        self.assertNotEqual(forwarded.returncode, 0)
        self.assertIn('cannot inspect forwarded', forwarded.stderr)
        other = self.root/'other'; other.mkdir()
        with self.assertRaisesRegex(SDKError, 'differs'):
            compiler_bindings.bind_compilers(self.root/'commands', commands=self.commands,
                flags=self.flags, python=sys.executable,
                native_cxx_runtime={**self.runtime, 'library_dir':str(other)})
