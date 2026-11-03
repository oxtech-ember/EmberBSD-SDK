# SPDX-License-Identifier: BSD-2-Clause
# Origin: EmberBSD (AI-assisted). Scoped LLVM metadata refresh regression.
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk import llvm_source_refresh
from emberbsd_sdk.errors import SDKError

class LLVMSourceRefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / 'objects'
        self.obj = self.root / 'lib/libc'
        self.obj.mkdir(parents=True)
        self.old, self.new = '/old/llvm', '/new/llvm'
        (self.obj/'changed.d').write_text('changed.o: /old/llvm/compiler-rt/a.c\n')
        for suffix in ('.o', '.po', '.pico', '.go', '.ln'):
            (self.obj/('changed'+suffix)).write_text('old')
        (self.obj/'unrelated.d').write_text('unrelated.o: /usr/include/header.h\n')
        (self.obj/'unrelated.o').write_text('keep')
        (self.obj/'.depend').write_text('changed.d changed.ln changed.go changed.pico changed.po changed.o: \\\n /old/llvm/compiler-rt/a.c\nunrelated.o: /usr/include/header.h\n')

    def test_moved_source_preserves_unrelated(self):
        plan = llvm_source_refresh.plan(str(self.root), self.old, self.new)
        self.assertEqual((len(plan['metadata']), len(plan['outputs']), len(plan['depends'])), (1, 5, 1))
        llvm_source_refresh.apply(str(self.root), plan, self.old, self.new)
        self.assertFalse((self.obj/'changed.d').exists())
        self.assertTrue((self.obj/'unrelated.o').exists())
        self.assertTrue((self.obj/'unrelated.d').exists())
        self.assertFalse((self.obj/'.depend').exists())
        self.assertEqual(llvm_source_refresh.plan(str(self.root), self.old, self.new)['metadata'], [])

    def test_same_root_changed_input_and_boundary(self):
        (self.obj/'unrelated.d').write_text('unrelated.o: /old/llvm-other/header.h\n')
        plan = llvm_source_refresh.plan(str(self.root), self.old, self.old)
        self.assertEqual(plan['metadata'], [str(self.obj/'changed.d')])

    def test_missing_metadata_and_symlink_refuse(self):
        (self.obj/'changed.d').unlink()
        with self.assertRaises(SDKError): llvm_source_refresh.plan(str(self.root), self.old, self.new)
        (self.obj/'changed.d').symlink_to('/etc/passwd')
        with self.assertRaises(SDKError): llvm_source_refresh.plan(str(self.root), self.old, self.new)

    def test_unsafe_rule_and_source_root_refuse(self):
        (self.obj/'.depend').write_text('../outside.o: /old/llvm/a.c\n')
        with self.assertRaises(SDKError): llvm_source_refresh.plan(str(self.root), self.old, self.new)
        with self.assertRaises(SDKError): llvm_source_refresh.plan(str(self.root), '/', self.new)
        with self.assertRaises(SDKError): llvm_source_refresh.plan(str(self.root), '/old/llvm source', self.new)

    def test_parent_depend_without_source_reference_is_invalidated(self):
        (self.obj/'.depend').write_text('.include "changed.d"\n')
        plan = llvm_source_refresh.plan(str(self.root), self.old, self.new)
        self.assertEqual(plan['depends'], [str(self.obj/'.depend')])
        llvm_source_refresh.apply(str(self.root), plan, self.old, self.new)
        self.assertFalse((self.obj/'.depend').exists())
        self.assertTrue((self.obj/'unrelated.o').exists())

    def test_unreadable_walk_is_refused(self):
        def failed_walk(root, *, followlinks, onerror):
            onerror(PermissionError('unreadable subtree'))
            return iter(())
        with patch('emberbsd_sdk.llvm_source_refresh.os.walk', side_effect=failed_walk):
            with self.assertRaisesRegex(SDKError, 'unreadable subtree'):
                llvm_source_refresh.plan(str(self.root), self.old, self.new)

    def test_interrupted_retry(self):
        plan = llvm_source_refresh.plan(str(self.root), self.old, self.new)
        unlink = os.unlink
        count = 0
        def interrupt(path):
            nonlocal count
            count += 1
            if count == 3: raise OSError('interrupted')
            unlink(path)
        with patch('emberbsd_sdk.llvm_source_refresh.os.unlink', side_effect=interrupt):
            with self.assertRaises(OSError): llvm_source_refresh.apply(str(self.root), plan, self.old, self.new)
        self.assertTrue((self.obj/'changed.d').exists())
        retry = llvm_source_refresh.plan(str(self.root), self.old, self.new)
        llvm_source_refresh.apply(str(self.root), retry, self.old, self.new)
        self.assertFalse((self.obj/'changed.d').exists())
        self.assertTrue((self.obj/'unrelated.o').exists())


class BuilderIntegrationTests(unittest.TestCase):
    def test_owner_compatible_llvm_source_change_plans_precise_refresh(self):
        import copy
        from types import SimpleNamespace
        from test_runtime_refresh import RuntimeRefreshTests
        from emberbsd_sdk.sysroot_builder import SysrootBuilder
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()/'objects'
            root.mkdir()
            (root/'item.d').write_text('item.o: /old/llvm/lib/input.c\n')
            (root/'item.o').write_text('old object')
            (root/'keep.o').write_text('keep')
            old = RuntimeRefreshTests.context()
            old['flags'].append('EMBER_LLVM_SOURCE=/old/llvm')
            new = copy.deepcopy(old)
            new['llvm_source']['manifest_sha256'] = 'b' * 64
            new['flags'][-1] = 'EMBER_LLVM_SOURCE=/new/llvm'
            fake = SimpleNamespace(paths={'os': '/os', 'objects': str(root)},
                context_signature=lambda path: new,
                COMMON_SOURCES=SysrootBuilder.COMMON_SOURCES,
                reuse_signature=SysrootBuilder.reuse_signature,
                compatible_context=lambda candidate, path: True,
                compiler_record={'binaries': {'ar': {'selected_path': '/tool/ar'}}}, environment={})
            fake.reuse_signature = fake.reuse_signature.__get__(fake)
            with patch('emberbsd_sdk.sysroot_builder.prepared_refresh.plan', return_value=[]):
                self.assertTrue(SysrootBuilder.plan_prepared_refresh(fake, {'signature': old}, str(root)))
            self.assertEqual(fake.llvm_refresh_plan['metadata'], [str(root/'item.d')])
            llvm_source_refresh.apply(str(root), fake.llvm_refresh_plan, *fake.llvm_refresh_roots)
            self.assertFalse((root/'item.o').exists())
            self.assertEqual((root/'keep.o').read_text(), 'keep')

    def test_headers_without_full_runtime_do_not_gain_reuse(self):
        import copy
        from types import SimpleNamespace
        from test_runtime_refresh import RuntimeRefreshTests
        from emberbsd_sdk.sysroot_builder import SysrootBuilder
        old = RuntimeRefreshTests.context()
        old['flags'].append('EMBER_LLVM_SOURCE=/old/llvm')
        old['runtime']['manifest']['components'] = ['builtins', 'libunwind-static']
        old['runtime']['manifest']['artifacts'].pop('cxx_headers')
        new = copy.deepcopy(old)
        new['llvm_source']['manifest_sha256'] = 'b' * 64
        new['flags'][-1] = 'EMBER_LLVM_SOURCE=/new/llvm'
        fake = SimpleNamespace(paths={'os': '/os', 'objects': '/obj'},
            context_signature=lambda path: new, COMMON_SOURCES=SysrootBuilder.COMMON_SOURCES,
            reuse_signature=SysrootBuilder.reuse_signature,
            compatible_context=lambda candidate, path: True)
        fake.reuse_signature = fake.reuse_signature.__get__(fake)
        self.assertFalse(SysrootBuilder.plan_prepared_refresh(fake, {'signature': old}, '/obj'))

    def test_relocation_only_uses_verified_runtime(self):
        import copy
        from types import SimpleNamespace
        from test_runtime_refresh import RuntimeRefreshTests
        from emberbsd_sdk.sysroot_builder import SysrootBuilder
        old = RuntimeRefreshTests.context()
        old['flags'].append('EMBER_LLVM_SOURCE=/old/llvm')
        new = copy.deepcopy(old)
        new['flags'][-1] = 'EMBER_LLVM_SOURCE=/new/llvm'
        fake = SimpleNamespace(paths={'os': '/os', 'objects': '/obj'},
            context_signature=lambda path: new, COMMON_SOURCES=SysrootBuilder.COMMON_SOURCES,
            reuse_signature=SysrootBuilder.reuse_signature,
            compatible_context=lambda candidate, path: True,
            compiler_record={'binaries': {'ar': {'selected_path': '/tool/ar'}}}, environment={})
        fake.reuse_signature = fake.reuse_signature.__get__(fake)
        with patch('emberbsd_sdk.sysroot_builder.prepared_refresh.plan', return_value=[]), \
             patch('emberbsd_sdk.sysroot_builder.llvm_source_refresh.plan', return_value={}) as planned:
            self.assertTrue(SysrootBuilder.plan_prepared_refresh(fake, {'signature': old}, '/obj'))
        planned.assert_called_once_with('/obj', '/old/llvm', '/new/llvm')

    def test_apply_precedes_context_write(self):
        from types import SimpleNamespace
        from emberbsd_sdk.sysroot_builder import SysrootBuilder
        calls = []
        fake = SimpleNamespace(paths={'objects': '/obj', 'tools': '/tools', 'sdk': '/sdk'},
            contexts={'objects': {}, 'tools': {}}, inspect_context=lambda path: {},
            llvm_refresh_plan={'metadata': [], 'outputs': [], 'depends': []},
            llvm_refresh_roots=('/old/llvm', '/new/llvm'), refresh_dirs=[],
            context_signature=lambda path: {}, mkdirs=lambda *args: None,
            sources={}, CONTEXT='.context')
        with patch('emberbsd_sdk.sysroot_builder.llvm_source_refresh.apply', side_effect=lambda *a: calls.append('apply')), \
             patch('emberbsd_sdk.sysroot_builder.jsonio.write_json', side_effect=lambda *a, **k: calls.append('write')):
            SysrootBuilder.prepare_contexts(fake)
        self.assertEqual(calls, ['apply', 'write', 'write'])
