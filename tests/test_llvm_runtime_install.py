# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Base aliases exercise real files and relocation, not target ABI execution."""

import copy

from test_llvm_runtime import LLVMRuntimeFixture
from emberbsd_sdk import inventory, llvm_runtime_install
from emberbsd_sdk.errors import SDKError


class LLVMRuntimeInstallTests(LLVMRuntimeFixture):
    def test_base_cxx_selection_is_derived_from_recorded_flags(self):
        self.assertTrue(llvm_runtime_install.base_cxx_enabled(['MKCXX=yes', 'MKLIBCXX=yes']))
        self.assertFalse(llvm_runtime_install.base_cxx_enabled(['MKCXX=yes', 'MKLIBCXX=no']))
        self.assertFalse(llvm_runtime_install.base_cxx_enabled([]))
        for flags in (['MKLIBCXX=yes', 'MKCXX=no'], ['MKCXX=maybe'],
                      ['MKCXX=yes', 'MKCXX=no'], ['MKLIBCXX=yes', 'MKLIBCXX=no'],
                      ['MKCXX+=yes'], ['MKCXX =yes']):
            with self.subTest(flags=flags), self.assertRaises(SDKError):
                llvm_runtime_install.base_cxx_enabled(flags)

    def test_header_and_library_leaves_are_relative_and_relocatable(self):
        self.full()
        self.write('stage/usr/pkg/llvm/include/c++/v1/__detail/nested.h', '// nested\n')
        (self.prefix / 'include/c++/v1/alias').symlink_to('vector')
        self.save()
        plan = llvm_runtime_install.base_alias_plan(self.record)
        self.assertEqual(plan['usr/include/c++/vector'], '../../pkg/llvm/include/c++/v1/vector')
        self.assertEqual(plan['usr/lib/libc++.so'], '../pkg/llvm/lib/libc++.so')
        llvm_runtime_install.install_base_aliases(self.stage, self.record)
        self.assertFalse((self.stage / 'usr/include/c++').is_symlink())
        self.assertFalse((self.stage / 'usr/include/c++/__detail').is_symlink())
        self.assertEqual((self.stage / 'usr/include/c++/alias').read_text(), '// fixture\n')
        before = inventory.snapshot_tree(self.stage)
        llvm_runtime_install.install_base_aliases(self.stage, self.record)
        self.assertEqual(inventory.snapshot_tree(self.stage), before)
        moved = self.root / 'moved'
        self.stage.rename(moved)
        llvm_runtime_install.validate_base_aliases(moved, self.record)
        self.assertEqual((moved / 'usr/include/c++/__detail/nested.h').read_text(), '// nested\n')

    def test_linker_script_and_soname_chain_follow_measured_roles(self):
        self.full()
        lib = self.prefix / 'lib'
        (lib / 'libc++.so').unlink()
        (lib / 'libc++.so').write_text('INPUT(libc++.so.1 -lc++abi)\n')
        (lib / 'libc++.so.1').rename(lib / 'libc++.so.1.0')
        (lib / 'libc++.so.1').symlink_to('libc++.so.1.0')
        self.record['artifacts']['libcxx']['shared'] = 'usr/pkg/llvm/lib/libc++.so.1.0'
        self.save()
        plan = llvm_runtime_install.install_base_aliases(self.stage, self.record)
        self.assertEqual((self.stage / 'usr/lib/libc++.so').read_text(), 'INPUT(libc++.so.1 -lc++abi)\n')
        for name in ('libc++.so', 'libc++.so.1', 'libc++.so.1.0', 'libc++.a'):
            self.assertIn('usr/lib/' + name, plan)
        self.assertNotIn('usr/lib/libc++_p.a', plan)

    def test_all_conflicts_are_checked_before_any_mutation(self):
        self.full()
        for relative in ('usr/include/c++/vector', 'usr/include/c++/old/header',
                         'usr/lib/libunwind.so'):
            with self.subTest(relative=relative):
                path = self.write('stage/' + relative, 'preserve\n')
                before = inventory.snapshot_tree(self.stage)
                with self.assertRaisesRegex(SDKError, 'conflict'):
                    llvm_runtime_install.install_base_aliases(self.stage, self.record)
                self.assertEqual(inventory.snapshot_tree(self.stage), before)
                path.unlink()
                while path.parent != self.stage and not any(path.parent.iterdir()):
                    path = path.parent
                    path.rmdir()

    def test_symlink_ancestors_cannot_redirect_installation(self):
        self.full()
        outside = self.root / 'outside'; outside.mkdir()
        (self.stage / 'usr/include').symlink_to(outside)
        before = inventory.snapshot_installed_tree(self.stage)
        with self.assertRaisesRegex(SDKError, 'conflict'):
            llvm_runtime_install.install_base_aliases(self.stage, self.record)
        self.assertEqual(inventory.snapshot_installed_tree(self.stage), before)
        self.assertEqual(list(outside.iterdir()), [])

    def test_preflight_can_run_before_copying_the_runtime(self):
        self.full()
        empty = self.root / 'empty'; empty.mkdir()
        plan = llvm_runtime_install.preflight_base_aliases(empty, self.record)
        self.assertIn('usr/include/c++/vector', plan)
        self.assertEqual(list(empty.iterdir()), [])
        with self.assertRaises(SDKError):
            llvm_runtime_install.install_base_aliases(empty, self.record)
        self.assertEqual(list(empty.iterdir()), [])

    def test_validation_rejects_missing_and_extra_base_headers(self):
        self.full()
        llvm_runtime_install.install_base_aliases(self.stage, self.record)
        header = self.stage / 'usr/include/c++/vector'
        header.unlink()
        with self.assertRaisesRegex(SDKError, 'missing'):
            llvm_runtime_install.validate_base_aliases(self.stage, self.record)
        llvm_runtime_install.install_base_aliases(self.stage, self.record)
        self.write('stage/usr/include/c++/legacy', '// obsolete\n')
        with self.assertRaisesRegex(SDKError, 'conflict'):
            llvm_runtime_install.validate_base_aliases(self.stage, self.record)

    def test_invalid_inventory_and_non_full_profiles_are_refused(self):
        with self.assertRaises(SDKError):
            llvm_runtime_install.base_alias_plan(self.record)
        self.full()
        for mutation in ('gcc', 'missing-linker', 'cycle', 'dangling', 'casefold', 'escape'):
            bad = copy.deepcopy(self.record)
            with self.subTest(mutation=mutation):
                if mutation == 'gcc':
                    bad['compiler_family'] = 'gcc'
                elif mutation == 'missing-linker':
                    del bad['symlinks']['usr/pkg/llvm/lib/libc++.so']
                elif mutation == 'casefold':
                    bad['files']['usr/pkg/llvm/include/c++/v1/Vector'] = 'f' * 64
                else:
                    bad['symlinks']['usr/pkg/llvm/include/c++/v1/bad'] = {
                        'cycle': 'bad', 'dangling': 'missing', 'escape': '../../../../../../outside'
                    }[mutation]
                with self.assertRaises(SDKError):
                    llvm_runtime_install.base_alias_plan(bad)

    def test_runtime_mutation_is_rejected_before_base_writes(self):
        self.full()
        (self.prefix / 'include/c++/v1/vector').write_text('// changed\n')
        before = inventory.snapshot_tree(self.stage)
        with self.assertRaisesRegex(SDKError, 'inventory changed'):
            llvm_runtime_install.install_base_aliases(self.stage, self.record)
        self.assertEqual(inventory.snapshot_tree(self.stage), before)

    def test_distinct_artifact_paths_cannot_overwrite_one_base_alias(self):
        self.full()
        path = 'usr/pkg/llvm/other/libc++.a'
        self.record['files'][path] = 'f' * 64
        self.record['artifacts']['libunwind']['static'] = path
        self.record['files']['usr/pkg/llvm/other/libc++.so'] = 'a' * 64
        with self.assertRaisesRegex(SDKError, 'collision'):
            llvm_runtime_install.base_alias_plan(self.record)

    def test_empty_expected_directories_are_preserved(self):
        self.full()
        directory = self.stage / 'usr/include/c++'
        directory.mkdir(parents=True)
        identity = directory.stat().st_ino
        llvm_runtime_install.install_base_aliases(self.stage, self.record)
        self.assertEqual(directory.stat().st_ino, identity)
        llvm_runtime_install.validate_base_aliases(self.stage, self.record)

    def test_linker_alias_cannot_select_a_different_shared_library(self):
        self.full()
        self.record['symlinks']['usr/pkg/llvm/lib/libc++.so'] = 'libunwind.so.1'
        with self.assertRaisesRegex(SDKError, 'linker entry.*role'):
            llvm_runtime_install.base_alias_plan(self.record)
