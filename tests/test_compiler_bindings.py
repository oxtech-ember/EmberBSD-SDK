# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Compiler adapters preserve exact native ABI and caller argument words."""

import json
import os
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
