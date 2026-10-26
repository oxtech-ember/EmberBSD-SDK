# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Portable application runtime selection after build and sysroot transfer."""

import copy
import sys
from pathlib import Path

from test_core import TemporaryCase
import test_llvm_builder as llvm_tests
from emberbsd_sdk import application_toolchain, compiler_bindings, jsonio, sysroot_transfer
from emberbsd_sdk.errors import SDKError


class ApplicationToolchainTests(TemporaryCase):
    def setUp(self):
        super().setUp()
        self.builder, _ = llvm_tests.LLVMBuilderTests.builder(self)
        self.receipt = self.builder.run()
        self.sysroot = Path(self.builder.paths['destination'])
        self.runtime = self.receipt['signature']['runtime']['manifest']
        self.compiler = self.receipt['signature']['compiler']

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
        self.assertIn(self.compiler['resource_dir']+'/include', compile['cc'])
        self.assertEqual(compile['cpp'],link['cpp'])
        self.assertEqual(compile['as'], self.compiler['commands']['as'])
        self.assertIn('-stdlib=libc++',link['cxx'])

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

    def test_runtime_parent_cannot_redirect_outside_sysroot(self):
        package = self.sysroot/'usr/pkg'
        outside = self.root/'redirected-package'
        package.rename(outside)
        package.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(SDKError, 'real directory'):
            application_toolchain.load_clang_runtime(self.sysroot, target='aarch64')
