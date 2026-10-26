# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
import copy

from test_llvm_runtime import LLVMRuntimeFixture
from emberbsd_sdk import inventory, llvm_runtime_sets
from emberbsd_sdk.errors import SDKError


class LLVMRuntimeSetsTests(LLVMRuntimeFixture):
    def runtime(self):
        return dict(manifest=self.record,
                    inventory=inventory.snapshot_tree(self.prefix, machine=183), root_mode=0o755)

    def setUp(self):
        super().setUp()
        self.full()
        self.source_lists = self.root / 'source-sets'
        self.write('source-sets/lists/base/mi', './etc\tbase-sys-root\n')
        self.write('source-sets/lists/comp/mi', './usr/include\tcomp-c-include\n')
        self.write('source-sets/lists/base/shl.mi', './lib\tbase-sys-root\n')

    def test_one_inventory_selects_payload_aliases_and_empty_directories(self):
        (self.prefix / 'include/c++/v1/empty').mkdir()
        self.write('stage/usr/pkg/llvm/share/licenses/LICENSE', 'license')
        (self.prefix / 'lib/libc++.so').unlink()
        self.write('stage/usr/pkg/llvm/lib/libc++.so', 'INPUT(libc++.so.1 -lc++abi)\n')
        self.save()
        plan = llvm_runtime_sets.runtime_set_plan(self.runtime())
        for name in ('usr/pkg/llvm/lib/libc++.so.1', 'usr/lib/libc++.so.1',
                     'usr/pkg/llvm/lib/libunwind.so', 'usr/lib/libunwind.so',
                     'usr/pkg/llvm/share/licenses/LICENSE'):
            self.assertEqual(plan[name], 'base', name)
        for name in ('usr/include/c++/vector', 'usr/pkg/llvm/include/c++/v1/empty',
                     'usr/pkg/llvm/lib/libc++.so', 'usr/lib/libc++.so',
                     'usr/lib/libc++.a', self.builtins):
            self.assertEqual(plan[name], 'comp', name)

    def test_overlay_preserves_source_lists_and_contains_each_derived_path(self):
        output = self.root / 'overlay'
        source_before = inventory.snapshot_tree(self.source_lists)
        result = llvm_runtime_sets.prepare_sets_overlay(self.source_lists, output, self.runtime())
        self.assertEqual(str(result), str(output))
        self.assertEqual(inventory.snapshot_tree(self.source_lists), source_before)
        self.assertEqual((output / 'lists/base/shl.mi').read_text(), './lib\tbase-sys-root\n')
        for path, selected in llvm_runtime_sets.runtime_set_plan(self.runtime()).items():
            self.assertIn('./' + path + '\t', (output / 'lists' / selected / 'mi').read_text())
        with self.assertRaises(SDKError):
            llvm_runtime_sets.prepare_sets_overlay(self.source_lists, output, self.runtime())

    def test_soname_chain_keeps_runtime_aliases_in_base(self):
        library = self.prefix / 'lib/libc++.so.1'
        library.rename(library.with_name('libc++.so.1.0'))
        library.symlink_to('libc++.so.1.0')
        self.record['artifacts']['libcxx']['shared'] = 'usr/pkg/llvm/lib/libc++.so.1.0'
        self.save()
        plan = llvm_runtime_sets.runtime_set_plan(self.runtime())
        for leaf in ('libc++.so', 'libc++.so.1', 'libc++.so.1.0'):
            for directory in ('usr/pkg/llvm/lib/', 'usr/lib/'):
                self.assertEqual(plan[directory + leaf], 'base')

    def test_directory_links_remain_outside_the_base_alias_contract(self):
        (self.prefix / 'loader-directory').symlink_to('lib')
        self.save()
        with self.assertRaisesRegex(SDKError, 'missing runtime target'):
            llvm_runtime_sets.runtime_set_plan(self.runtime())

    def test_source_output_overlap_and_unrepresentable_names_refuse(self):
        with self.assertRaisesRegex(SDKError, 'source lists'):
            llvm_runtime_sets.prepare_sets_overlay(self.source_lists,
                self.source_lists / 'overlay', self.runtime())
        self.write('stage/usr/pkg/llvm/include/c++/v1/space name', 'header')
        self.save()
        with self.assertRaisesRegex(SDKError, 'cannot represent'):
            llvm_runtime_sets.prepare_sets_overlay(self.source_lists,
                self.root / 'overlay', self.runtime())
        self.assertFalse((self.root / 'overlay').exists())

    def test_mismatched_payload_is_rejected_before_overlay_mutation(self):
        for mutation in ('missing', 'extra', 'changed', 'escape'):
            record = copy.deepcopy(self.runtime())
            rows = record['inventory']
            if mutation == 'missing': del rows['include/c++/v1/vector']
            elif mutation == 'extra': rows['include/c++/v1/stale'] = rows['include/c++/v1/vector']
            elif mutation == 'changed': rows['include/c++/v1/vector']['sha256'] = 'f' * 64
            else: rows['../escaped'] = dict(kind='directory', mode=0o755)
            output = self.root / mutation
            with self.subTest(mutation=mutation), self.assertRaises(SDKError):
                llvm_runtime_sets.prepare_sets_overlay(self.source_lists, output, record)
            self.assertFalse(output.exists())
