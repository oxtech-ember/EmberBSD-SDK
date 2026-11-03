# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Portable application runtime selection after build and sysroot transfer."""

import copy
import hashlib
import json
import os
import sys
from pathlib import Path

from test_core import TemporaryCase, archive
import test_llvm_builder as llvm_tests
from emberbsd_sdk import application_toolchain, compiler_bindings, inventory, jsonio, sysroot_transfer
from emberbsd_sdk.errors import SDKError


class ApplicationToolchainTests(TemporaryCase):
    def setUp(self):
        super().setUp()
        self.builder, _ = llvm_tests.LLVMBuilderTests.builder(self)
        self.receipt = self.builder.run()
        self.sysroot = Path(self.builder.paths['destination'])
        self.runtime = self.receipt['signature']['runtime']['manifest']
        self.compiler = self.receipt['signature']['compiler']

    def _c_foundation_fixture(self, *, target='riscv32'):
        """Build a small real-shaped C-foundation receipt and RV32 runtime."""
        import struct
        stage, root = self.root/'c-foundation-stage', self.root/'c-foundation-sysroot'
        stage.mkdir(); root.mkdir()
        triplet = target + '--netbsd'
        if target != 'riscv32':
            raise AssertionError('fixture target helper only constructs RV32 ELF input')
        elf = bytearray(52)
        elf[:7] = b'\x7fELF\x01\x01\x01'
        struct.pack_into('<HHI', elf, 16, 1, 243, 1)
        struct.pack_into('<I', elf, 36, 1)  # RISC-V soft-float ABI.
        struct.pack_into('<H', elf, 40, 52)
        builtins = 'usr/pkg/llvm/lib/clang/23/lib/netbsd/libclang_rt.builtins-riscv32.a'
        unwind = 'usr/pkg/llvm/lib/libunwind.a'
        for name in (builtins, unwind):
            path = stage/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(archive(bytes(elf)))
        base_header = b'/* target C foundation fixture */\n'
        (root/'usr/include').mkdir(parents=True)
        (root/'usr/include/stdint.h').write_bytes(base_header)
        manifest = dict(format='emberbsd-llvm-runtime', version=1, compiler_family='clang',
            llvm_version='23.1.4', components=['builtins', 'libunwind-static'], target=target,
            triplet=triplet, machine_arch='riscv32', elf_machine=243, prefix='usr/pkg/llvm',
            source=dict(archive='llvm-project-23.1.4.src.tar.xz', sha512='a'*128),
            recipe=dict(ports_revision='b'*40, files={'pkgsrc/lang/llvm-runtimes/Makefile':'c'*64}),
            base=dict(os_revision='d'*40, files={'usr/include/stdint.h':hashlib.sha256(base_header).hexdigest()}, symlinks={}),
            artifacts=dict(builtins=builtins, libunwind=dict(static=unwind)), files={}, symlinks={})
        rows = inventory.snapshot_tree(stage, exclude=('runtime.json',))
        manifest['files'] = {name:row['sha256'] for name,row in rows.items() if row['kind']=='file'}
        manifest['symlinks'] = {name:row['target'] for name,row in rows.items() if row['kind']=='link'}
        jsonio.write_json(stage/'runtime.json', manifest)

        compiler = copy.deepcopy(self.compiler)
        compiler['triplet'] = triplet
        compiler['reported_triplet'] = 'riscv32-unknown-netbsd'
        compiler['commands']['cc'] = [compiler['binaries']['cc']['selected_path'],
            '--target='+triplet, '--ld-path='+compiler['binaries']['ld']['selected_path']]
        compiler['commands']['cxx'] = [compiler['binaries']['cxx']['selected_path'],
            '--target='+triplet, '--ld-path='+compiler['binaries']['ld']['selected_path']]
        compiler['commands']['cpp'] = [compiler['binaries']['cpp']['selected_path'], '-E', '--target='+triplet]
        compiler['commands']['as'] = [compiler['binaries']['as']['selected_path'], '--target='+triplet,
            '-c', '-x', 'assembler']
        target_definition = dict(machine='riscv', arch='riscv32', triplet=triplet, elf=243)
        result = dict(format='emberbsd-llvm-c-foundation', version=1, target=target,
            base=dict(format='emberbsd-llvm-os-stage', phase='base', qualification='built-and-inspected',
                paths={'destination':str(root), 'llvm-runtime-stage':str(stage)},
                signature=dict(compiler_family='clang', target=target_definition, compiler=compiler,
                    runtime=dict(manifest=manifest))))
        result_path = self.root/'c-foundation-result.json'
        jsonio.write_json(result_path, result)
        return root, stage, result_path, manifest, compiler

    def test_c_foundation_binding_selects_manifest_runtime_without_cxx_runtime(self):
        root, stage, result, manifest, compiler = self._c_foundation_fixture()
        commands = application_toolchain.clang_c_foundation_commands(
            compiler, result, root, target='riscv32')
        for role in ('cc', 'cxx', 'cpp'):
            self.assertIn('--target=riscv32--netbsd', commands['commands'][role])
            self.assertIn('-nostdinc', commands['commands'][role])
            self.assertIn(str(root/'usr/include'), commands['commands'][role])
            self.assertIn('-march=rv32imac_zicsr', commands['commands'][role])
            self.assertIn('-mabi=ilp32', commands['commands'][role])
            self.assertNotIn('-mabi=ilp32f', commands['commands'][role])
        self.assertIn('-march=rv32imac_zicsr', commands['commands']['as'])
        self.assertIn('-mabi=ilp32', commands['commands']['as'])
        for role in ('cc', 'cxx'):
            self.assertIn('--rtlib=compiler-rt', commands['link_flags'][role])
            self.assertIn('--unwindlib=libunwind', commands['link_flags'][role])
            self.assertIn('-L'+str(stage/'usr/pkg/llvm/lib'), commands['link_flags'][role])
        self.assertNotIn('cxx_headers', manifest['artifacts'])
        destination = self.root/'bound-c-foundation-tools'
        application_toolchain.bind_clang_c_foundation_commands(destination, compiler, result,
            root, target='riscv32', python=sys.executable)
        record = jsonio.read_json(destination/'commands.json')
        self.assertIn('--rtlib=compiler-rt',
            compiler_bindings.compiler_argv(record, 'cc', ['input.o', '-o', 'output']))

    def test_c_foundation_binding_rejects_mismatched_or_modified_inputs(self):
        root, stage, result_path, manifest, compiler = self._c_foundation_fixture()
        with self.assertRaisesRegex(SDKError, 'matching LLVM C-foundation'):
            application_toolchain.load_clang_c_foundation_runtime(result_path, root, target='aarch64')
        result = jsonio.read_json(result_path)
        result['base']['signature']['runtime']['manifest']['components'].append('libcxx')
        expanded = self.root/'expanded-runtime-result.json'
        jsonio.write_json(expanded, result)
        with self.assertRaisesRegex(SDKError, 'unsupported LLVM runtime component profile'):
            application_toolchain.load_clang_c_foundation_runtime(expanded, root, target='riscv32')
        for field, bad_value in (('components', None), ('triplet', 'wrong--netbsd')):
            malformed = copy.deepcopy(jsonio.read_json(result_path))
            malformed['base']['signature']['runtime']['manifest'][field] = bad_value
            malformed_path = self.root/('malformed-runtime-' + field + '.json')
            jsonio.write_json(malformed_path, malformed)
            with self.subTest(field=field), self.assertRaises(SDKError):
                application_toolchain.load_clang_c_foundation_runtime(
                    malformed_path, root, target='riscv32')
        changed = copy.deepcopy(compiler)
        changed['version'] = '22.1.0'
        with self.assertRaisesRegex(SDKError, 'compiler and C-foundation runtime versions differ'):
            application_toolchain.clang_c_foundation_commands(changed, result_path, root, target='riscv32')
        relocated = copy.deepcopy(compiler)
        relocated_prefix = '/selected/native-ports-prefix'
        for role, entry in relocated['binaries'].items():
            name = os.path.basename(entry['selected_path'])
            entry['selected_path'] = relocated_prefix + '/bin/' + name
            entry['path'] = entry['selected_path']
        relocated['resource_dir'] = relocated_prefix + '/lib/clang/23'
        relocated['commands'] = {
            'cc':[relocated['binaries']['cc']['selected_path'], '--target=riscv32--netbsd',
                  '--ld-path=' + relocated['binaries']['ld']['selected_path']],
            'cxx':[relocated['binaries']['cxx']['selected_path'], '--target=riscv32--netbsd',
                   '--ld-path=' + relocated['binaries']['ld']['selected_path']],
            'cpp':[relocated['binaries']['cpp']['selected_path'], '-E', '--target=riscv32--netbsd'],
            'as':[relocated['binaries']['as']['selected_path'], '--target=riscv32--netbsd',
                  '-c', '-x', 'assembler'],
            'ld':[relocated['binaries']['ld']['selected_path']],
        }
        for role in ('ar', 'ranlib', 'nm', 'strip', 'objcopy', 'objdump', 'readelf', 'size', 'strings'):
            relocated['commands'][role] = [relocated['binaries'][role]['selected_path']]
        commands = application_toolchain.clang_c_foundation_commands(
            relocated, result_path, root, target='riscv32')['commands']
        self.assertEqual(commands['cc'][0], relocated_prefix + '/bin/clang')
        self.assertIn('-mabi=ilp32', commands['cc'])
        (stage/manifest['artifacts']['libunwind']['static']).write_bytes(b'tampered')
        with self.assertRaises(SDKError):
            application_toolchain.load_clang_c_foundation_runtime(result_path, root, target='riscv32')

    def test_c_foundation_runtime_base_can_bind_an_installed_extension(self):
        import shutil
        root, stage, result_path, manifest, compiler = self._c_foundation_fixture()
        extension = self.root/'c-foundation-extension'
        shutil.copytree(root, extension)
        (extension/'METALOG').write_text('native install output\n')
        (extension/'usr/include/math-extension.h').write_text('/* native headers */\n')
        (extension/'usr/share/man/man3/sin.3').parent.mkdir(parents=True)
        (extension/'usr/share/man/man3/sin.3').write_text('native manual\n')
        (extension/'usr/pkg').mkdir()
        commands = application_toolchain.clang_c_foundation_commands(
            compiler, result_path, extension, target='riscv32', runtime_base=root)
        self.assertIn('--sysroot='+str(extension), commands['commands']['cc'])
        self.assertIn(str(extension/'usr/include'), commands['commands']['cc'])
        self.assertIn('-Wl,-rpath-link,'+str(extension)+'/usr/lib',
            commands['link_flags']['cc'])
        self.assertEqual(commands['selection']['base'], str(root))
        with self.assertRaisesRegex(SDKError, 'runtime inputs do not match'):
            application_toolchain.clang_c_foundation_commands(
                compiler, result_path, extension, target='riscv32')
        with self.assertRaisesRegex(SDKError, 'runtime inputs do not match'):
            application_toolchain.clang_c_foundation_commands(
                compiler, result_path, extension, target='riscv32', runtime_base=extension)
        bound = self.root/'bound-extension-tools'
        application_toolchain.bind_clang_c_foundation_commands(bound, compiler,
            result_path, extension, target='riscv32', python=sys.executable,
            include_auxiliary=True, runtime_base=root)
        record = jsonio.read_json(bound/'commands.json')
        self.assertIn('--sysroot='+str(extension), record['commands']['cc'])

    def test_final_and_imported_sysroots_retain_the_existing_runtime_record(self):
        self.assertEqual(application_toolchain.load_clang_runtime(self.sysroot, target='aarch64'), self.runtime)
        bundle, restored = self.root/'bundle', self.root/'restored'
        exported = sysroot_transfer.export_sysroot(self.sysroot, bundle, target='aarch64')
        sysroot_transfer.import_sysroot(bundle, restored, target='aarch64', manifest_sha256=exported['manifest_sha256'])
        self.assertEqual(application_toolchain.load_clang_runtime(restored, target='aarch64'), self.runtime)

    def test_opted_in_build_and_import_require_base_aliases(self):
        isolated = self.root / 'opted-in-fixture'
        isolated.mkdir()
        original_root = self.root
        self.root = isolated
        try:
            builder, runtime = llvm_tests.LLVMBuilderTests.builder(self, options=llvm_tests.fixture(isolated))
        finally:
            self.root = original_root
        builder.options['base-cxx'] = True
        builder.run()
        root = Path(builder.paths['destination'])
        self.assertEqual(application_toolchain.load_clang_runtime(root, target='aarch64'), runtime.record)
        bundle, restored = self.root / 'opted-in-bundle', self.root / 'opted-in-restored'
        exported = sysroot_transfer.export_sysroot(root, bundle, target='aarch64')
        sysroot_transfer.import_sysroot(bundle, restored, target='aarch64',
            manifest_sha256=exported['manifest_sha256'])
        self.assertEqual(application_toolchain.load_clang_runtime(restored, target='aarch64'), runtime.record)
        alias = restored / 'usr/include/c++/vector'
        alias.unlink()
        with self.assertRaisesRegex(SDKError, 'base alias'):
            application_toolchain.load_clang_runtime(restored, target='aarch64')

    def test_completed_amd64_release_dest_is_an_application_sysroot(self):
        from unittest.mock import patch
        import test_os_release as release_tests

        class AMD64Identity:
            def compiler(self):
                record = llvm_tests.ClangBuilder.compiler(self)
                record['triplet'] = 'x86_64--netbsd'
                record['reported_triplet'] = 'x86_64-unknown-netbsd'
                for role, command in record['commands'].items():
                    record['commands'][role] = [arg.replace('aarch64--netbsd', 'x86_64--netbsd')
                                                for arg in command]
                return record

        class ClangRelease(AMD64Identity, release_tests.OSReleaseBuilder):
            pass

        test_root = self.root / 'release-test'
        test_root.mkdir()
        options = release_tests.release_fixture(test_root, 'amd64')
        original_root = self.root
        self.root = test_root
        try:
            builder, _ = llvm_tests.LLVMBuilderTests.builder(
                self, options, ClangRelease, retarget=release_tests.retarget_runtime_amd64)
        finally:
            self.root = original_root
        with patch('emberbsd_sdk.native_runtime_install.NativeRuntimeInstaller',
                   release_tests.FixtureNativeRuntimeInstaller):
            receipt = builder.run()
        parent = Path(options['destination']); root = parent / 'dest'
        expected = receipt['signature']['runtime']['manifest']
        package = root / 'usr/pkg/include/ordinary-package.h'
        package.parent.mkdir(parents=True, exist_ok=True); package.write_text('package')
        self.assertEqual(application_toolchain.load_clang_runtime(root, target='amd64'), expected)
        with self.assertRaises(SDKError):
            application_toolchain.load_clang_runtime(root, target='aarch64')
        marker = parent / '.prebuild-sdk-build-incomplete'
        marker.write_text('pending')
        with self.assertRaises(SDKError):
            application_toolchain.load_clang_runtime(root, target='amd64')
        marker.unlink()
        profile_file = parent / 'build-profile.json'
        profile = jsonio.read_json(profile_file)
        bad = copy.deepcopy(profile); bad['sdk_receipt']['paths']['destination'] = str(self.root)
        profile_file.write_text(json.dumps(bad))
        with self.assertRaises(SDKError):
            application_toolchain.load_clang_runtime(root, target='amd64')
        profile_file.write_text(json.dumps(profile))
        receipt_file = parent / '.prebuild-sdk-build.json'
        for case in ('error', 'runtime-check'):
            bad_receipt = copy.deepcopy(receipt)
            if case == 'error':
                bad_receipt['error'] = 'interrupted'
            else:
                bad_receipt['commands'].append(dict(scope='runtime-check', status='failed'))
            bad_profile = copy.deepcopy(profile)
            bad_profile['sdk_receipt'] = bad_receipt
            receipt_file.write_text(json.dumps(bad_receipt))
            profile_file.write_text(json.dumps(bad_profile))
            with self.subTest(case=case), self.assertRaises(SDKError):
                application_toolchain.load_clang_runtime(root, target='amd64')
            receipt_file.write_text(json.dumps(receipt))
            profile_file.write_text(json.dumps(profile))
        profile_file.unlink()
        with self.assertRaises(SDKError):
            application_toolchain.load_clang_runtime(root, target='amd64')
        profile_file.write_text(json.dumps(profile))
        alias = root / 'usr/include/c++/vector'
        alias.unlink()
        with self.assertRaises(SDKError):
            application_toolchain.load_clang_runtime(root, target='amd64')

    def test_added_package_files_do_not_replace_or_invalidate_the_runtime(self):
        package = self.sysroot/'usr/pkg/include/package.h'; package.parent.mkdir(parents=True, exist_ok=True)
        package.write_text('/* separate package input */\n')
        application_toolchain.load_clang_runtime(self.sysroot, target='aarch64')
        builtins = self.sysroot/self.runtime['artifacts']['builtins']
        builtins.write_bytes(builtins.read_bytes()+b'changed')
        with self.assertRaises(SDKError):
            application_toolchain.load_clang_runtime(self.sysroot, target='aarch64')

    def test_compilation_and_driver_link_arguments_are_distinct(self):
        compile = application_toolchain.clang_application_commands(self.compiler, self.sysroot, self.runtime)
        link = application_toolchain.clang_application_commands(self.compiler, self.sysroot, self.runtime, link=True)
        for role in ('cc','cxx'):
            self.assertIn('--target=aarch64--netbsd', compile[role])
            self.assertIn('--sysroot='+str(self.sysroot), compile[role])
            self.assertFalse(any(word.startswith(('--ld-path=', '--rtlib=', '--unwindlib=', '-Wl,')) for word in compile[role]))
            self.assertIn('--rtlib=compiler-rt', link[role])
            self.assertIn('--unwindlib=libunwind', link[role])
            self.assertIn('--ld-path='+self.compiler['binaries']['ld']['selected_path'], link[role])
        self.assertIn(str(self.sysroot/self.runtime['artifacts']['cxx_headers']), compile['cxx'])
        self.assertLess(compile['cxx'].index(str(self.sysroot/self.runtime['artifacts']['cxx_headers'])),
                        compile['cxx'].index(self.compiler['resource_dir']+'/include'))
        self.assertIn(self.compiler['resource_dir']+'/include', compile['cc'])
        cxx_headers = str(self.sysroot/self.runtime['artifacts']['cxx_headers'])
        for role in ('cc', 'cxx', 'cpp'):
            self.assertIn('-nobuiltininc', compile[role])
        resource_include = self.compiler['resource_dir']+'/include'
        self.assertIn(resource_include, compile['cxx'])
        self.assertLess(compile['cxx'].index(cxx_headers), compile['cxx'].index(resource_include))
        self.assertEqual(compile['cpp'],link['cpp'])
        self.assertEqual(compile['as'], self.compiler['commands']['as'])
        self.assertIn('-stdlib=libc++',link['cxx'])

    def test_auxiliary_commands_bind_target_assembler_and_linker_without_cxx_policy(self):
        destination = self.root / 'application-tools'
        with self.assertRaisesRegex(SDKError, 'auxiliary.*boolean'):
            application_toolchain.bind_clang_application_commands(destination,
                self.compiler, self.sysroot, self.runtime, python=sys.executable,
                include_auxiliary='yes')
        tools = application_toolchain.bind_clang_application_commands(destination,
            self.compiler, self.sysroot, self.runtime, python=sys.executable,
            include_auxiliary=True)
        self.assertEqual(set(tools), {'cc', 'cxx', 'cpp', 'as', 'ld'})
        record = jsonio.read_json(destination / 'commands.json')
        arguments = ['-o', 'output.o', 'input.s']
        self.assertEqual(compiler_bindings.compiler_argv(record, 'as', arguments),
            self.compiler['commands']['as'] + arguments)
        self.assertEqual(compiler_bindings.compiler_argv(record, 'ld', arguments),
            self.compiler['commands']['ld'] + ['--sysroot=' + str(self.sysroot)] + arguments)

    def test_sysroot_alias_keeps_bindings_independent_of_the_stage_directory(self):
        tools = self.root / 'os-tools'
        tools.mkdir()
        alias = tools / 'sysroot'

        def bind(name, sysroot, **alias_option):
            destination = self.root / name
            application_toolchain.bind_clang_application_commands(destination, self.compiler,
                sysroot, self.runtime, python=sys.executable, include_auxiliary=True, **alias_option)
            return (destination / 'commands.json').read_text()

        alias.symlink_to(self.sysroot)
        first_direct = bind('direct-first', self.sysroot)
        first = bind('alias-first', self.sysroot, sysroot_alias=alias)
        self.assertNotIn(str(self.sysroot), first)
        record = jsonio.read_json(self.root / 'alias-first/commands.json')
        self.assertIn('--sysroot=' + str(alias), record['commands']['ld'])
        self.assertIn('-Wl,-rpath-link,' + str(alias / 'usr/lib'), record['link_flags']['cc'])
        renamed = self.root / 'renamed-stage'
        self.sysroot.rename(renamed)
        alias.unlink()
        alias.symlink_to(renamed)
        # The stage name was the only difference: real paths change the
        # binding, the alias keeps it.
        self.assertNotEqual(bind('direct-renamed', renamed), first_direct)
        self.assertEqual(bind('alias-renamed', renamed, sysroot_alias=alias), first)
        other = self.root / 'other-stage'
        other.mkdir()
        alias.unlink()
        alias.symlink_to(other)
        with self.assertRaisesRegex(SDKError, 'alias does not resolve to the selected sysroot'):
            bind('alias-wrong', renamed, sysroot_alias=alias)
        self.assertFalse((self.root / 'alias-wrong').exists())

    def test_runtime_must_match_selected_compiler_and_target(self):
        different = copy.deepcopy(self.compiler); different['version']='22.1.0'
        with self.assertRaisesRegex(SDKError, 'compiler.*runtime'):
            application_toolchain.clang_application_commands(different, self.sysroot, self.runtime)
        with self.assertRaises(SDKError):
            application_toolchain.load_clang_runtime(self.sysroot, target='amd64')
        (self.sysroot/sysroot_transfer.INCOMPLETE).write_text('unfinished')
        with self.assertRaisesRegex(SDKError, 'incomplete'):
            application_toolchain.load_clang_runtime(self.sysroot, target='aarch64')

    def test_portable_manifest_binds_runtime_to_its_hash_and_payload(self):
        bundle = self.root/'bundle'
        sysroot_transfer.export_sysroot(self.sysroot,bundle,target='aarch64')
        manifest=jsonio.read_json(bundle/'manifest.json')
        self.assertEqual(manifest['runtime'],self.receipt['signature']['runtime'])
        manifest['runtime']['manifest']['llvm_version']='22.1.0'
        with self.assertRaises(SDKError):
            sysroot_transfer.validate_manifest(manifest,target='aarch64')

    def test_transfer_runtime_subtree_cannot_contain_unrecorded_entries(self):
        bundle = self.root/'bundle'
        sysroot_transfer.export_sysroot(self.sysroot, bundle, target='aarch64')
        original = jsonio.read_json(bundle/'manifest.json')
        prefix = self.runtime['prefix']
        rows = ({'kind':'file', 'mode':0o644, 'size':1, 'sha256':'0'*64},
                {'kind':'directory', 'mode':0o755},
                {'kind':'link', 'mode':0o777, 'target':'../include'})
        for row in rows:
            with self.subTest(kind=row['kind']):
                manifest = copy.deepcopy(original)
                manifest['inventory'][prefix+'/lib/unrecorded'] = row
                manifest['unpacked_bytes'] += row.get('size', 0)
                with self.assertRaisesRegex(SDKError, 'runtime.*payload'):
                    sysroot_transfer.validate_manifest(manifest, target='aarch64')

    def test_split_cxx_and_ldflags_select_libcxx_explicitly(self):
        commands = application_toolchain.clang_application_commands(self.compiler, self.sysroot, self.runtime)
        flags = application_toolchain.clang_application_link_flags(self.compiler, self.sysroot, self.runtime)
        cxx_flags = application_toolchain.clang_application_link_flags(self.compiler, self.sysroot, self.runtime, language='c++')
        self.assertIn('-stdlib=libc++', commands['cxx'] + cxx_flags)
        self.assertNotIn('-stdlib=libc++', commands['cxx'])
        self.assertNotIn('-stdlib=libc++', commands['cc'] + flags)
        search = application_toolchain.clang_application_runtime_rpath(self.runtime)
        self.assertEqual(search['libraries'], ['libc++', 'libc++abi', 'libunwind'])
        self.assertTrue(search['flags'] and set(search['flags']) <= set(flags))
        without = application_toolchain.clang_application_link_flags(self.compiler, self.sysroot,
            self.runtime, runtime_rpath=False)
        self.assertEqual(without, [word for word in flags if word not in search['flags']])

    def test_application_bindings_select_language_runtime_only_at_link(self):
        directory = self.root/'application-bindings'
        application_toolchain.bind_clang_application_commands(directory, self.compiler,
            self.sysroot, self.runtime, python=sys.executable)
        record = jsonio.read_json(directory/'commands.json')
        for role in ('cc','cxx'):
            compile = compiler_bindings.compiler_argv(record, role, ['-c','-Werror','source.cc'])
            link = compiler_bindings.compiler_argv(record, role, ['source.o','-o','program'])
            self.assertFalse(any(word.startswith(('--ld-path=','--rtlib=','--unwindlib=','-stdlib=','-Wl,')) for word in compile))
            self.assertIn('--rtlib=compiler-rt', link)
            self.assertEqual('-stdlib=libc++' in link, role == 'cxx')
            # The runtime search path is not a fixed link flag. The binding
            # predicts it for C++ and keeps it only if the output needs it.
            self.assertFalse(any(word.startswith('-Wl,-rpath,') for word in record['link_flags'][role]))
            search = record['runtime_rpath']['flags']
            self.assertEqual(all(word in link for word in search), role == 'cxx')
        self.assertEqual(record['runtime_rpath'], application_toolchain.clang_application_runtime_rpath(self.runtime))

    def test_runtime_parent_cannot_redirect_outside_sysroot(self):
        package = self.sysroot/'usr/pkg'
        outside = self.root/'redirected-package'
        package.rename(outside)
        package.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(SDKError, 'real directory'):
            application_toolchain.load_clang_runtime(self.sysroot, target='aarch64')
