# SPDX-License-Identifier: BSD-2-Clause
# Origin: EmberBSD; AI-assisted independent runtime refresh regressions.
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

SDK_SRC = Path(__file__).resolve().parents[1] / 'src'
sys.path.insert(0, str(SDK_SRC))
from emberbsd_sdk import prepared_refresh, runtime_refresh
from emberbsd_sdk.sysroot_builder import SysrootBuilder


class RuntimeRefreshTests(unittest.TestCase):
    def setUp(self):
        self.before = self.context()
        self.after = copy.deepcopy(self.before)
        self.after['runtime_prefix'] = '/candidate/runtime'
        old, new = self.before['runtime']['manifest'], self.after['runtime']['manifest']
        new['files']['usr/pkg/llvm/include/c++/v1/locale'] = '1' * 64
        new['files']['usr/pkg/llvm/lib/libc++.so.1'] = '2' * 64
        new['files']['usr/pkg/llvm/lib/libc++abi.so.1'] = '3' * 64

    @staticmethod
    def manifest():
        prefix = 'usr/pkg/llvm'
        files = {
            prefix + '/lib/clang/23/lib/netbsd/libclang_rt.builtins-aarch64.a': 'a' * 64,
            prefix + '/lib/libunwind.a': 'b' * 64,
            prefix + '/lib/libunwind.so.1': 'c' * 64,
            prefix + '/lib/libc++.a': 'd' * 64,
            prefix + '/lib/libc++abi.a': 'e' * 64,
            prefix + '/lib/libc++.so.1': 'f' * 64,
            prefix + '/lib/libc++abi.so.1': '0' * 64,
            prefix + '/include/c++/v1/vector': '1' * 64,
        }
        return {
            'format': 'emberbsd-llvm-runtime', 'version': 1, 'compiler_family': 'clang',
            'llvm_version': '23.1.4', 'components': ['builtins', 'libunwind', 'libcxxabi', 'libcxx'],
            'target': 'aarch64', 'triplet': 'aarch64--netbsd', 'machine_arch': 'aarch64',
            'elf_machine': 183, 'prefix': prefix,
            'source': {'archive': 'llvm-project-23.1.4.src.tar.xz', 'sha512': 'a' * 128},
            'recipe': {'ports_revision': 'b' * 40, 'files': {'pkgsrc/llvm/Makefile': 'c' * 64}},
            'base': {'os_revision': 'd' * 40, 'files': {'usr/include/stdint.h': 'e' * 64}, 'symlinks': {}},
            'artifacts': {
                'builtins': prefix + '/lib/clang/23/lib/netbsd/libclang_rt.builtins-aarch64.a',
                'libunwind': {'static': prefix + '/lib/libunwind.a', 'shared': prefix + '/lib/libunwind.so.1'},
                'libcxx': {'static': prefix + '/lib/libc++.a', 'shared': prefix + '/lib/libc++.so.1'},
                'libcxxabi': {'static': prefix + '/lib/libc++abi.a', 'shared': prefix + '/lib/libc++abi.so.1'},
                'cxx_headers': prefix + '/include/c++/v1',
            },
            'files': files,
            'bootstrap': {'manifest_sha256': 'f' * 64, 'files': {
                prefix + '/lib/clang/23/lib/netbsd/libclang_rt.builtins-aarch64.a': 'a' * 64,
                prefix + '/lib/libunwind.a': 'b' * 64}},
            'symlinks': {
                prefix + '/lib/libc++.so': 'libc++.so.1',
                prefix + '/lib/libc++abi.so': 'libc++abi.so.1',
            },
        }

    @classmethod
    def context(cls):
        manifest = cls.manifest()
        return {
            'compiler_family': 'clang', 'runtime_prefix': '/previous/runtime',
            'llvm_source': {'llvm_version': '23.1.4', 'manifest_sha256': 'f' * 64},
            'runtime': {'manifest': manifest},
            'common_sources': {},
            'flags': ['CC=clang', 'CFLAGS=-O2', 'EMBER_EXTERNAL_LLVM_RUNTIME=/previous/runtime',
                      'EMBER_LLVM_RESOURCE_DIR=/previous/resource'],
        }

    def test_recorded_locale_dso_pair_and_prefix_move_refresh_without_tree_hashing(self):
        with patch('emberbsd_sdk.inventory.snapshot_tree', side_effect=AssertionError('unexpected tree hash')):
            update = runtime_refresh.cpp_update(self.before, self.after)
        self.assertEqual(update, {
            'headers': ('usr/pkg/llvm/include/c++/v1/', 'usr/include/c++/'),
            'linked': ('libc++.so', 'libc++abi.so'),
        })

    def test_metadata_only_os_revision_does_not_select_runtime_inputs(self):
        new = copy.deepcopy(self.before)
        new['runtime']['manifest']['base']['os_revision'] = '9' * 40
        self.assertEqual(runtime_refresh.cpp_update(self.before, new), {'headers': (), 'linked': ()})

    def test_metadata_only_transition_plans_no_object_refresh(self):
        old, new = copy.deepcopy(self.before), copy.deepcopy(self.before)
        new['runtime']['manifest']['base']['os_revision'] = '9' * 40
        fake = SimpleNamespace(
            paths={'os': '/os', 'objects': '/obj'}, context_signature=lambda path: new,
            COMMON_SOURCES=SysrootBuilder.COMMON_SOURCES,
            reuse_signature=SysrootBuilder.reuse_signature,
            compatible_context=lambda candidate, path: True,
            compiler_record={'binaries': {'ar': {'selected_path': '/tool/ar'}}}, environment={},
        )
        fake.reuse_signature = fake.reuse_signature.__get__(fake)
        with patch('emberbsd_sdk.sysroot_builder.prepared_refresh.plan', return_value=[]) as plan:
            self.assertTrue(SysrootBuilder.plan_prepared_refresh(fake, {'signature': old}, '/obj'))
        self.assertEqual(fake.refresh_dirs, [])
        self.assertEqual(plan.call_args.kwargs['extra_headers'], ())
        self.assertEqual(plan.call_args.kwargs['extra_linked'], ())

    def test_builtin_unwind_archive_version_target_base_and_symlink_changes_are_rejected(self):
        cases = []
        builtins = copy.deepcopy(self.after)
        builtins['runtime']['manifest']['files']['usr/pkg/llvm/lib/clang/23/lib/netbsd/libclang_rt.builtins-aarch64.a'] = '9' * 64
        cases.append(('builtins', builtins))
        unwind = copy.deepcopy(self.after)
        unwind['runtime']['manifest']['files']['usr/pkg/llvm/lib/libunwind.so.1'] = '9' * 64
        cases.append(('unwinder', unwind))
        static = copy.deepcopy(self.after)
        static['runtime']['manifest']['files']['usr/pkg/llvm/lib/libc++.a'] = '9' * 64
        cases.append(('static archive', static))
        archive = copy.deepcopy(self.after)
        archive['runtime']['manifest']['source']['sha512'] = '9' * 128
        cases.append(('source archive', archive))
        version = copy.deepcopy(self.after)
        version['runtime']['manifest']['llvm_version'] = '24.0.0'
        cases.append(('LLVM version', version))
        target = copy.deepcopy(self.after)
        target['runtime']['manifest']['target'] = 'riscv64'
        cases.append(('target', target))
        base = copy.deepcopy(self.after)
        base['runtime']['manifest']['base']['files']['usr/include/stdint.h'] = '9' * 64
        cases.append(('base ABI', base))
        link = copy.deepcopy(self.after)
        link['runtime']['manifest']['symlinks']['usr/pkg/llvm/lib/libc++.so'] = 'other.so'
        cases.append(('symlink map', link))
        for label, candidate in cases:
            with self.subTest(label=label):
                self.assertIsNone(runtime_refresh.cpp_update(self.before, candidate))

    def test_builtins_refresh_recognizes_only_compatible_runtime_payload(self):
        after = copy.deepcopy(self.before)
        after['runtime_prefix'] = '/candidate/runtime'
        manifest = after['runtime']['manifest']
        for name in (manifest['artifacts']['builtins'],
                     manifest['artifacts']['libcxx']['shared'],
                     manifest['artifacts']['libcxxabi']['shared']):
            manifest['files'][name] = '9' * 64
        manifest['bootstrap']['files'][manifest['artifacts']['builtins']] = '9' * 64
        self.assertEqual(runtime_refresh.builtins_update(self.before, after),
                         {'builtins': manifest['artifacts']['builtins']})
        unsafe = copy.deepcopy(after)
        unsafe['runtime']['manifest']['bootstrap']['files'][manifest['artifacts']['libunwind']['static']] = '8' * 64
        self.assertIsNone(runtime_refresh.builtins_update(self.before, unsafe))
        unsafe = copy.deepcopy(after)
        unsafe['runtime']['manifest']['recipe']['files']['pkgsrc/llvm/Makefile'] = '8' * 64
        self.assertIsNone(runtime_refresh.builtins_update(self.before, unsafe))
        unsafe = copy.deepcopy(after)
        unsafe['runtime']['manifest']['files'][manifest['artifacts']['libunwind']['static']] = '8' * 64
        self.assertIsNone(runtime_refresh.builtins_update(self.before, unsafe))
        unsafe = copy.deepcopy(after)
        unsafe['runtime']['manifest']['files']['usr/pkg/llvm/include/c++/v1/vector'] = '8' * 64
        self.assertIsNone(runtime_refresh.builtins_update(self.before, unsafe))

    def test_builtins_refresh_rejects_missing_or_non_clang_artifacts(self):
        after = copy.deepcopy(self.before)
        artifact = after['runtime']['manifest']['artifacts']['builtins']
        after['runtime']['manifest']['files'].pop(artifact)
        self.assertIsNone(runtime_refresh.builtins_update(self.before, after))
        before = copy.deepcopy(self.before)
        after = copy.deepcopy(self.before)
        before['runtime']['manifest']['triplet'] = 'aarch64-apple-darwin'
        after['runtime']['manifest']['triplet'] = 'aarch64-apple-darwin'
        after['runtime']['manifest']['files'][artifact] = '9' * 64
        self.assertIsNone(runtime_refresh.builtins_update(before, after))
        before = copy.deepcopy(self.before)
        after = copy.deepcopy(self.before)
        before['runtime']['manifest']['compiler_family'] = 'gcc'
        after['runtime']['manifest']['compiler_family'] = 'gcc'
        after['runtime']['manifest']['files'][artifact] = '9' * 64
        self.assertIsNone(runtime_refresh.builtins_update(before, after))

    def test_builder_plans_builtins_refresh_from_selected_context(self):
        old, new = copy.deepcopy(self.before), copy.deepcopy(self.before)
        old['flags'].extend(['EMBER_LLVM_SOURCE=/source/old', 'EMBER_LIBUNWIND_ARCHIVE=/old/libunwind.a'])
        new['flags'].extend(['EMBER_LLVM_SOURCE=/source/new', 'EMBER_LIBUNWIND_ARCHIVE=/stage/usr/pkg/llvm/lib/libunwind.a'])
        new['flags'] = [f.replace('/previous/runtime', '/candidate/runtime') for f in new['flags']]
        new['runtime_prefix'] = '/candidate/runtime'
        new['llvm_source']['manifest_sha256'] = 'c' * 64
        artifact = new['runtime']['manifest']['artifacts']['builtins']
        new['runtime']['manifest']['files'][artifact] = '9' * 64
        new['runtime']['manifest']['bootstrap']['files'][artifact] = '9' * 64
        fake = SimpleNamespace(
            paths={'os': '/os', 'objects': '/obj', 'llvm-source': '/source/new', 'llvm-bootstrap-stage': '/stage'},
            context_signature=lambda path: new, COMMON_SOURCES=SysrootBuilder.COMMON_SOURCES,
            reuse_signature=SysrootBuilder.reuse_signature,
            compatible_context=lambda candidate, path: True,
            compiler_record={'binaries': {'ar': {'selected_path': '/tool/ar'}}}, environment={},
        )
        fake.reuse_signature = fake.reuse_signature.__get__(fake)
        with patch('emberbsd_sdk.sysroot_builder.runtime_refresh.changed_builtins', return_value=['compiler-rt/lib/builtins/a.c']) as sources, \
             patch('emberbsd_sdk.sysroot_builder.prepared_refresh.runtime_objects', return_value=['lib/libc/a.o']) as objects, \
             patch('emberbsd_sdk.sysroot_builder.runtime_refresh.equivalent_unwind_binding', return_value=True), \
             patch('emberbsd_sdk.sysroot_builder.llvm_source_refresh.plan',
                   side_effect=AssertionError('builtins outputs must stay in their quarantine path')), \
             patch('emberbsd_sdk.sysroot_builder.prepared_refresh.plan', return_value=[]), \
             patch('emberbsd_sdk.sysroot_builder.prepared_refresh.runtime_independent_patterns', return_value=[]):
            self.assertTrue(SysrootBuilder.plan_prepared_refresh(fake, {'signature': old}, '/obj'))
        sources.assert_called_once_with('/source/old', '/source/new')
        objects.assert_called_once()
        self.assertEqual(fake.runtime_refresh_files, ['lib/libc/a.o'])
        self.assertIsNone(fake.llvm_refresh_plan)

    def test_unwind_relocation_requires_selected_stage_and_matching_bytes(self):
        import hashlib
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old = root / 'old/libunwind.a'
            stage = root / 'stage'
            rel = 'usr/pkg/llvm/lib/libunwind.a'
            new = stage / rel
            old.parent.mkdir()
            new.parent.mkdir(parents=True)
            old.write_bytes(b'unchanged unwind')
            new.write_bytes(b'unchanged unwind')
            digest = hashlib.sha256(b'unchanged unwind').hexdigest()
            self.assertTrue(runtime_refresh.equivalent_unwind_binding(str(old), str(new), str(stage), rel, digest))
            self.assertFalse(runtime_refresh.equivalent_unwind_binding(str(old), str(new), str(root), rel, digest))
            new.write_bytes(b'changed unwind')
            self.assertFalse(runtime_refresh.equivalent_unwind_binding(str(old), str(new), str(stage), rel, digest))

    def test_builtins_source_diff_rejects_additions_and_reports_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old, new = root / 'old', root / 'new'
            for source in (old, new):
                (source / 'compiler-rt/lib/builtins').mkdir(parents=True)
                (source / 'compiler-rt/lib/builtins/same.c').write_text('same')
                (source / 'compiler-rt/lib/builtins/changed.h').write_text('old')
            (new / 'compiler-rt/lib/builtins/changed.h').write_text('new')
            (old / 'compiler-rt/lib/builtins/CMakeLists.txt').write_text('old')
            (new / 'compiler-rt/lib/builtins/CMakeLists.txt').write_text('changed flags')
            with self.assertRaisesRegex(Exception, 'unsupported builtins source change'):
                runtime_refresh.changed_builtins(old, new)
            (new / 'compiler-rt/lib/builtins/CMakeLists.txt').write_text('old')
            self.assertEqual(runtime_refresh.changed_builtins(old, new),
                             ['compiler-rt/lib/builtins/changed.h'])
            cmake = Path('compiler-rt/cmake/Modules/AddCompilerRT.cmake')
            for source in (old, new):
                (source / cmake).parent.mkdir(parents=True)
            dollar = chr(36)
            original = 'if (APPLE AND NOT CMAKE_LINKER MATCHES)\nCOMMAND ' + dollar + '{CODESIGN} --sign - ' + dollar + '{EXTRA_CODESIGN_ARGUMENTS}\n'
            (old / cmake).write_text(original)
            (new / cmake).write_text(original.replace('--sign -', '--force --sign -'))
            self.assertEqual(runtime_refresh.changed_builtins(old, new),
                             ['compiler-rt/lib/builtins/changed.h'])
            outside = 'if (APPLE AND NOT CMAKE_LINKER MATCHES)\nendif()\n' + original.split('\n', 1)[1]
            (old / cmake).write_text(outside)
            (new / cmake).write_text(outside.replace('--sign -', '--force --sign -'))
            with self.assertRaisesRegex(Exception, 'unrelated runtime source changed'):
                runtime_refresh.changed_builtins(old, new)
            (old / cmake).write_text(original)
            (new / cmake).write_text(original.replace('--sign -', '--force --sign -') + 'unexpected\n')
            with self.assertRaisesRegex(Exception, 'unrelated runtime source changed'):
                runtime_refresh.changed_builtins(old, new)
            (new / cmake).write_text(original.replace('--sign -', '--force --sign -'))
            (old / 'other.h').write_text('old')
            (new / 'other.h').write_text('new')
            with self.assertRaisesRegex(Exception, 'unrelated runtime source changed'):
                runtime_refresh.changed_builtins(old, new)
            (old / 'other.h').unlink()
            (new / 'other.h').unlink()
            (new / 'compiler-rt/lib/builtins/extra.c').write_text('extra')
            with self.assertRaisesRegex(Exception, 'builtins source inventory changed'):
                runtime_refresh.changed_builtins(old, new)

    def test_builtins_object_plan_selects_only_dependent_variants(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            obj = root / 'objects'
            old = root / 'old-source'
            (obj / 'lib/libc').mkdir(parents=True)
            (old / 'compiler-rt/lib/builtins').mkdir(parents=True)
            changed = old / 'compiler-rt/lib/builtins/fp_lib.h'
            changed.write_bytes(b'old')
            unit = obj / 'lib/libc'
            (unit / 'dependent.d').write_text('dependent.o: ' + str(changed) + chr(10))
            (unit / '.depend').write_text('dependent.o: ' + str(changed) + chr(10))
            for name in ('dependent.o', 'dependent.pico', 'independent.o', 'independent.d'):
                (unit / name).write_bytes(b'keep')
            (unit / 'independent.d').write_text('independent.o: /unrelated/header.h' + chr(10))
            result = prepared_refresh.runtime_objects(obj, old, ('compiler-rt/lib/builtins/fp_lib.h',))
            self.assertEqual(set(result), {'lib/libc/dependent.d', 'lib/libc/dependent.o',
                                           'lib/libc/dependent.pico', 'lib/libc/.depend'})
            self.assertTrue((unit / 'independent.o').exists())
            (unit / 'dependent.po').write_bytes(b'profiled object')
            with self.assertRaisesRegex(Exception, 'unsupported runtime object variant'):
                prepared_refresh.runtime_objects(obj, old, ('compiler-rt/lib/builtins/fp_lib.h',))
            (unit / 'dependent.po').unlink()
            other = obj / 'other'
            other.mkdir()
            (other / 'x.o').write_bytes(b'object')
            (other / 'x.d').write_text('x.o: ' + str(changed) + chr(10))
            with self.assertRaisesRegex(Exception, 'unknown runtime object archive owner'):
                prepared_refresh.runtime_objects(obj, old, ('compiler-rt/lib/builtins/fp_lib.h',))
            (other / 'x.o').unlink()
            (other / 'x.d').unlink()
            (unit / 'unknown.o').write_bytes(b'old object')
            with self.assertRaisesRegex(Exception, 'missing runtime dependency file'):
                prepared_refresh.runtime_objects(obj, old, ('compiler-rt/lib/builtins/fp_lib.h',))
            self.assertEqual(set(prepared_refresh.runtime_objects(
                obj, old, ('compiler-rt/lib/builtins/fp_lib.h',), ('lib/libc/unknown.o',))), set(result))
            (unit / 'unknown.o').unlink()
            direct = old / 'compiler-rt/lib/builtins/direct.c'
            direct.write_bytes(b'old')
            (unit / 'direct.o').write_bytes(b'old object')
            with self.assertRaisesRegex(Exception, 'missing runtime dependency file'):
                prepared_refresh.runtime_objects(obj, old, ('compiler-rt/lib/builtins/direct.c',))
            (unit / 'direct.d').write_text('direct.o: /another/direct.c' + chr(10))
            with self.assertRaisesRegex(Exception, 'runtime dependency does not reference'):
                prepared_refresh.runtime_objects(obj, old, ('compiler-rt/lib/builtins/direct.c',))

    def test_runtime_object_plan_understands_long_dependency_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            obj, old = root / 'objects', root / 'old-source'
            unit = obj / 'lib/libc'
            unit.mkdir(parents=True)
            source = old / 'compiler-rt/lib/builtins/direct.c'
            source.parent.mkdir(parents=True)
            source.write_text('old')
            (unit / 'direct.o').write_bytes(b'object')
            (unit / 'direct.o.d').write_text('direct.o: ' + str(source) + chr(10))
            result = prepared_refresh.runtime_objects(obj, old, ['compiler-rt/lib/builtins/direct.c'])
            self.assertEqual(set(result), {'lib/libc/direct.o', 'lib/libc/direct.o.d'})

    def test_runtime_source_scan_propagates_walk_errors(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old, new = root / 'old', root / 'new'
            for source in (old, new):
                (source / 'compiler-rt/lib/builtins').mkdir(parents=True)
            def walk_fail(path, *, followlinks, onerror):
                onerror(OSError('injected source walk failure'))
                return iter(())
            with patch('emberbsd_sdk.runtime_refresh.os.walk', side_effect=walk_fail):
                with self.assertRaisesRegex(OSError, 'injected source walk failure'):
                    runtime_refresh.changed_builtins(old, new)

    def test_runtime_archive_proof_rejects_stale_member(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            obj, source = root / 'objects', root / 'new-source'
            unit = obj / 'lib/libc'
            unit.mkdir(parents=True)
            changed = source / 'compiler-rt/lib/builtins/a.h'
            changed.parent.mkdir(parents=True)
            changed.write_text('new')
            (unit / 'x.o').write_bytes(b'new object')
            (unit / 'x.o.d').write_text('x.o: ' + str(changed) + chr(10))
            (unit / 'libc.a').write_bytes(b'archive')
            selected = ['lib/libc/x.o', 'lib/libc/x.o.d']
            members = lambda archive: ['x.o']
            with self.assertRaisesRegex(Exception, 'archive member is stale'):
                prepared_refresh.verify_runtime_outputs(obj, selected, source,
                    ['compiler-rt/lib/builtins/a.h'], members, lambda archive, member: b'old object')
            prepared_refresh.verify_runtime_outputs(obj, selected, source,
                ['compiler-rt/lib/builtins/a.h'], members, lambda archive, member: b'new object')

    def test_runtime_archive_proof_requires_shared_relink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            obj, source = root / 'objects', root / 'new-source'
            unit = obj / 'lib/libc'
            unit.mkdir(parents=True)
            changed = source / 'compiler-rt/lib/builtins/a.h'
            changed.parent.mkdir(parents=True)
            changed.write_text('new')
            (unit / 'x.pico').write_bytes(b'new object')
            (unit / 'x.d').write_text('x.o: ' + str(changed) + chr(10))
            (unit / 'libc_pic.a').write_bytes(b'archive')
            shared = unit / 'libc.so.12'
            shared.write_bytes(b'shared')
            (unit / 'libc.so').symlink_to(shared.name)
            selected = ['lib/libc/x.pico', 'lib/libc/x.d']
            before = prepared_refresh.runtime_shared_identity(obj, selected)
            same = shared.stat().st_mtime_ns
            os.utime(unit / 'x.pico', ns=(same, same))
            with self.assertRaisesRegex(Exception, 'shared library was not relinked'):
                prepared_refresh.verify_runtime_outputs(obj, selected, source,
                    ['compiler-rt/lib/builtins/a.h'], lambda archive: ['x.pico'],
                    lambda archive, member: b'new object', before)
            shared.write_bytes(b'new shared')
            prepared_refresh.verify_runtime_outputs(obj, selected, source,
                ['compiler-rt/lib/builtins/a.h'], lambda archive: ['x.pico'],
                lambda archive, member: b'new object', before)

    def test_runtime_object_scan_propagates_walk_errors(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            obj, source = root / 'objects', root / 'source'
            obj.mkdir()
            (source / 'compiler-rt/lib/builtins').mkdir(parents=True)
            (source / 'compiler-rt/lib/builtins/a.c').write_text('source')
            def walk_fail(path, *, followlinks, onerror):
                onerror(OSError('injected walk failure'))
                return iter(())
            with patch('emberbsd_sdk.prepared_refresh.os.walk', side_effect=walk_fail):
                with self.assertRaisesRegex(OSError, 'injected walk failure'):
                    prepared_refresh.runtime_objects(obj, source, ('compiler-rt/lib/builtins/a.c',))

    def test_runtime_quarantine_moves_only_planned_files_and_preserves_others(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'objects'
            (root / 'lib').mkdir(parents=True)
            for name in ('changed.o', 'changed.d', 'other.o'):
                (root / 'lib' / name).write_text(name)
            backup = root / '.runtime-refresh-owned'
            prepared_refresh.quarantine_runtime_objects(root, ['lib/changed.o', 'lib/changed.d'], backup)
            self.assertFalse((root / 'lib/changed.o').exists())
            self.assertEqual((backup / 'lib/changed.o').read_text(), 'changed.o')
            self.assertEqual((root / 'lib/other.o').read_text(), 'other.o')
            with self.assertRaisesRegex(Exception, 'runtime quarantine already exists'):
                prepared_refresh.quarantine_runtime_objects(root, ['lib/other.o'], backup)

    def test_runtime_quarantine_rolls_back_partial_move(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'objects'
            (root / 'lib').mkdir(parents=True)
            for name in ('a.o', 'b.o'):
                (root / 'lib' / name).write_text(name)
            backup = root / '.runtime-refresh-owned'
            replace = prepared_refresh.os.replace
            calls = []
            def fail_second(source, target):
                calls.append((source, target))
                if len(calls) == 2:
                    raise OSError('injected failure')
                return replace(source, target)
            with patch('emberbsd_sdk.prepared_refresh.os.replace', side_effect=fail_second):
                with self.assertRaisesRegex(OSError, 'injected failure'):
                    prepared_refresh.quarantine_runtime_objects(root, ['lib/a.o', 'lib/b.o'], backup)
            self.assertEqual((root / 'lib/a.o').read_text(), 'a.o')
            self.assertEqual((root / 'lib/b.o').read_text(), 'b.o')
            self.assertFalse(backup.exists())

    def test_failed_runtime_rollback_preserves_both_versions_and_aggregate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'objects'
            unit = root / 'lib/libc'
            unit.mkdir(parents=True)
            (unit / 'a.o').write_bytes(b'old')
            (unit / 'a.d').write_bytes(b'old-dep')
            backup = root / '.runtime-refresh-owned'
            prepared_refresh.quarantine_runtime_objects(root, ['lib/libc/a.o', 'lib/libc/a.d'], backup)
            (unit / 'a.o').write_bytes(b'failed-new')
            (unit / 'a.d').write_bytes(b'failed-dep')
            (unit / '.depend').write_bytes(b'old-aggregate')
            evidence = Path(temporary) / 'failed-evidence'
            prepared_refresh.rollback_failed_runtime(root, backup, evidence,
                ['lib/libc/a.o', 'lib/libc/a.d'], ['lib/libc/.depend'])
            self.assertEqual((unit / 'a.o').read_bytes(), b'old')
            self.assertEqual((unit / 'a.d').read_bytes(), b'old-dep')
            self.assertFalse((unit / '.depend').exists())
            self.assertEqual((evidence / 'lib/libc/a.o').read_bytes(), b'failed-new')
            self.assertEqual((evidence / 'lib/libc/.depend').read_bytes(), b'old-aggregate')
            self.assertFalse(backup.exists())

    def test_quarantine_cleanup_requires_exact_owned_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'objects'
            (root / 'lib').mkdir(parents=True)
            (root / 'lib/a.o').write_text('old')
            backup = root / '.runtime-refresh-owned'
            prepared_refresh.quarantine_runtime_objects(root, ['lib/a.o'], backup)
            (backup / 'other').write_text('not owned')
            with self.assertRaisesRegex(Exception, 'quarantine inventory changed'):
                prepared_refresh.cleanup_runtime_quarantine(root, ['lib/a.o'], backup)
            self.assertTrue((backup / 'lib/a.o').exists())
            (backup / 'other').unlink()
            prepared_refresh.cleanup_runtime_quarantine(root, ['lib/a.o'], backup)
            self.assertFalse(backup.exists())

    def test_runtime_refresh_context_changes_only_after_proof(self):
        with tempfile.TemporaryDirectory() as temporary:
            obj = Path(temporary) / 'objects'
            (obj / 'lib/libc').mkdir(parents=True)
            (obj / 'lib/libc/a.o').write_bytes(b'old')
            context = obj / SysrootBuilder.CONTEXT
            context.write_text(json.dumps({'signature': {'old': True}}))
            fake = SimpleNamespace(
                paths={'objects': str(obj)}, runtime_refresh_files=['lib/libc/a.o'],
                runtime_refresh_source='/new', runtime_refresh_changed=['compiler-rt/lib/builtins/a.c'],
                refresh_dirs=[], RUNTIME_QUARANTINE=SysrootBuilder.RUNTIME_QUARANTINE,
                CONTEXT=SysrootBuilder.CONTEXT, compiler_record={'binaries': {'ar': {'selected_path': '/ar'}}},
                environment={}, context_signature=lambda path: {'new': True}, sources={},
            )
            SysrootBuilder.begin_runtime_refresh(fake)
            backup = obj / fake.RUNTIME_QUARANTINE
            self.assertEqual((backup / 'lib/libc/a.o').read_bytes(), b'old')
            (obj / 'lib/libc/a.o').write_bytes(b'new')
            with patch('emberbsd_sdk.sysroot_builder.prepared_refresh.verify_runtime_outputs',
                       side_effect=ValueError('archive proof failed')):
                with self.assertRaisesRegex(ValueError, 'archive proof failed'):
                    SysrootBuilder.finish_runtime_refresh(fake)
            self.assertEqual(json.loads(context.read_text())['signature'], {'old': True})
            self.assertTrue((backup / 'lib/libc/a.o').exists())
            with patch('emberbsd_sdk.sysroot_builder.prepared_refresh.verify_runtime_outputs'):
                SysrootBuilder.finish_runtime_refresh(fake)
            self.assertEqual(json.loads(context.read_text())['signature'], {'new': True})
            self.assertFalse(backup.exists())

    def test_prepared_planner_unions_runtime_headers_and_links_with_common_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            os_root, obj = root / 'os', root / 'obj'
            contract = {
                'version': 1,
                'sources': {'EMBER_LIBCBOR_SOURCE': {
                    'producer_dirs': ['external/cbor'], 'consumer_dirs': ['external/fido'],
                    'header_prefixes': ['usr/include/cbor.h'], 'linked_names': ['libcbor.so']}},
                'header_independent_object_patterns': [], 'header_independent_archive_patterns': [],
            }
            (os_root / 'share/mk').mkdir(parents=True)
            (os_root / prepared_refresh.CONTRACT).write_text(json.dumps(contract))
            for directory in ('external/cbor', 'external/fido', 'other/cxx', 'other/base-cxx', 'other/c', 'other/linked'):
                (os_root / directory).mkdir(parents=True, exist_ok=True)
                (os_root / directory / 'Makefile').write_text('all:\n')
                (obj / directory).mkdir(parents=True, exist_ok=True)
            (obj / 'other/cxx/locale.d').write_text(
                'locale.o: /usr/pkg/llvm/include/c++/v1/locale\n')
            (obj / 'other/cxx/locale.o').write_bytes(b'obj')
            (obj / 'other/base-cxx/locale.d').write_text(
                'locale.o: /old/sysroot/usr/include/c++/__cxx03/locale\n')
            (obj / 'other/base-cxx/locale.o').write_bytes(b'obj')
            (obj / 'other/c/c.d').write_text('c.o: /usr/include/stdio.h\n')
            (obj / 'other/c/c.o').write_bytes(b'obj')
            (obj / 'other/linked/libuser.so').write_bytes(b'\x7fELFlibc++.so.1\0')
            calls = []
            real_plan = prepared_refresh.plan
            def count_plan(*args, **kwargs):
                calls.append((args, kwargs))
                return real_plan(*args, **kwargs)
            with patch.object(prepared_refresh, 'plan', side_effect=count_plan):
                result = prepared_refresh.plan(os_root, obj, {'libcbor'}, lambda archive: [],
                    extra_headers=runtime_refresh.cpp_update(self.before, self.after)['headers'],
                    extra_linked=('libc++.so', 'libc++abi.so'))
            self.assertEqual(len(calls), 1)
            self.assertEqual(result, ['external/cbor', 'external/fido', 'other/base-cxx', 'other/cxx', 'other/linked'])
            self.assertNotIn('other/c', result)

    def test_plan_prepared_refresh_keeps_compiler_and_unrelated_flags_in_compatibility_check(self):
        new = copy.deepcopy(self.after)
        new['flags'] = ['CC=clang', 'CFLAGS=-O2', 'LDFLAGS=-Wl,keep',
                        'EMBER_EXTERNAL_LLVM_RUNTIME=/candidate/runtime',
                        'EMBER_LLVM_RESOURCE_DIR=/candidate/resource']
        old = copy.deepcopy(self.before)
        old['flags'] = ['CC=clang', 'CFLAGS=-O2', 'LDFLAGS=-Wl,keep',
                        'EMBER_EXTERNAL_LLVM_RUNTIME=/previous/runtime',
                        'EMBER_LLVM_RESOURCE_DIR=/previous/resource']
        old['compiler'] = {'family': 'clang', 'version': '23.1.4'}
        new['compiler'] = copy.deepcopy(old['compiler'])
        record = {'signature': old}
        checks = []
        fake = SimpleNamespace(
            paths={'os': '/os', 'objects': '/obj'},
            context_signature=lambda path: new,
            COMMON_SOURCES=SysrootBuilder.COMMON_SOURCES,
            reuse_signature=SysrootBuilder.reuse_signature,
            compatible_context=lambda candidate, path: checks.append(copy.deepcopy(candidate['signature'])) or
                candidate['signature']['compiler'] == old['compiler'] and
                candidate['signature']['flags'] == new['flags'],
            compiler_record={'binaries': {'ar': {'selected_path': '/tool/ar'}}},
            environment={},
        )
        fake.reuse_signature = fake.reuse_signature.__get__(fake)
        with patch('emberbsd_sdk.sysroot_builder.prepared_refresh.plan', return_value=['other/cxx']) as plan, \
             patch('emberbsd_sdk.sysroot_builder.llvm_source_refresh.plan', return_value={}) as llvm_plan:
            self.assertTrue(SysrootBuilder.plan_prepared_refresh(fake, record, '/obj'), repr(checks))
        llvm_plan.assert_not_called()
        self.assertEqual(fake.refresh_dirs, ['other/cxx'])
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]['compiler'], old['compiler'])
        self.assertEqual(checks[0]['flags'], new['flags'])
        plan.assert_called_once()

    def test_combined_source_and_runtime_refresh_normalizes_only_declared_flags(self):
        def source(package, tree):
            return {'format': 'emberbsd-prepared-source', 'version': 1,
                    'package': package, 'tree_sha256': tree}

        old, new = copy.deepcopy(self.before), copy.deepcopy(self.after)
        old['compiler'] = new['compiler'] = {'family': 'clang', 'version': '23.1.4'}
        old['llvm_source'] = {'llvm_version': '23.1.4', 'tree_sha256': 'a' * 64}
        new['llvm_source'] = {'llvm_version': '23.1.4', 'tree_sha256': 'b' * 64}
        old['native_toolset'] = {'tools': {'cc': '/native/bin/clang', 'cxx': '/native/bin/clang++'}}
        new['native_toolset'] = copy.deepcopy(old['native_toolset'])
        new['native_toolset']['tools'].update({
            'autoconf': '/native/bin/autoconf', 'autom4te': '/native/bin/autom4te',
            'gm4': '/native/bin/gm4',
        })
        old['common_sources'] = {
            'openssh': source('security/openssh', '1' * 64),
            'openssl': source('security/openssl', '2' * 64),
            'openldap': source('databases/openldap', '3' * 64),
        }
        new['common_sources'] = {
            'openssh': source('security/openssh', '4' * 64),
            'openssl': source('security/openssl', '2' * 64),
            'openldap': source('databases/openldap', '3' * 64),
        }
        old['flags'] = [
            'CC=clang', 'CFLAGS=-O2', 'LDFLAGS=-Wl,keep',
            'EMBER_EXTERNAL_LLVM_RUNTIME=/previous/runtime',
            'EMBER_LLVM_RESOURCE_DIR=/previous/resource',
            'EMBER_LLVM_SOURCE=/prepared/llvm-old',
            'EMBER_OPENSSH_SOURCE=/prepared/openssh-old',
            'EMBER_OPENSSL_SOURCE=/prepared/openssl-old',
            'EMBER_OPENLDAP_SOURCE=/prepared/openldap-old',
        ]
        new['flags'] = [
            'CC=clang', 'CFLAGS=-O2', 'LDFLAGS=-Wl,keep',
            'EMBER_EXTERNAL_LLVM_RUNTIME=/candidate/runtime',
            'EMBER_LLVM_RESOURCE_DIR=/candidate/resource',
            'EMBER_LLVM_SOURCE=/tmp/llvm-current',
            'EMBER_OPENSSH_SOURCE=/tmp/openssh-current',
            'EMBER_OPENSSL_SOURCE=/tmp/openssl-current',
            'EMBER_OPENLDAP_SOURCE=/tmp/openldap-current',
            'HOST_AUTOCONF=/native/bin/autoconf',
        ]
        record = {'signature': old}
        checks = []
        fake = SimpleNamespace(
            paths={'os': '/os', 'objects': '/obj', 'openssl-source': '/tmp/openssl-current',
                   'openldap-source': '/tmp/openldap-current'},
            context_signature=lambda path: new,
            COMMON_SOURCES=SysrootBuilder.COMMON_SOURCES,
            reuse_signature=SysrootBuilder.reuse_signature,
            compatible_context=lambda candidate, path:
                checks.append(copy.deepcopy(candidate['signature'])) or
                candidate['signature']['compiler'] == fake.context_signature(path)['compiler'] and
                candidate['signature']['flags'] == fake.context_signature(path)['flags'] and
                candidate['signature']['native_toolset']['tools'] ==
                fake.context_signature(path)['native_toolset']['tools'],
            compiler_record={'binaries': {'ar': {'selected_path': '/tool/ar'}}},
            environment={}, native=SimpleNamespace(tool=lambda role: '/native/bin/' + role),
        )
        fake.reuse_signature = fake.reuse_signature.__get__(fake)
        with patch('emberbsd_sdk.sysroot_builder.prepared_refresh.plan', return_value=['other/cxx']) as plan, \
             patch('emberbsd_sdk.sysroot_builder.llvm_source_refresh.plan', return_value={}) as llvm_plan:
            self.assertTrue(SysrootBuilder.plan_prepared_refresh(fake, record, '/obj'), repr(checks))
        llvm_plan.assert_called_once_with('/obj', '/prepared/llvm-old', '/tmp/llvm-current')
        self.assertEqual(fake.refresh_dirs, ['other/cxx'])
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]['flags'], new['flags'])
        self.assertEqual(checks[0]['native_toolset']['tools'], new['native_toolset']['tools'])
        plan.assert_called_once()
        source_roots = set(plan.call_args.kwargs['allowed_source_roots'])
        self.assertTrue({'/prepared/openssl-old', '/tmp/openssl-current',
                         '/prepared/openldap-old', '/tmp/openldap-current'} <= source_roots)

        for label, mutate in (
                ('compiler', lambda candidate: candidate.update(compiler={'family': 'clang', 'version': '24'})),
                ('unrelated flag', lambda candidate: candidate['flags'].append('LDFLAGS=-Wl,changed')),
                ('wrong autoconf', lambda candidate: candidate['flags'].__setitem__(
                    candidate['flags'].index('HOST_AUTOCONF=/native/bin/autoconf'),
                    'HOST_AUTOCONF=/wrong/autoconf'))):
            with self.subTest(label=label):
                candidate = copy.deepcopy(new)
                mutate(candidate)
                fake.context_signature = lambda path, candidate=candidate: candidate
                self.assertFalse(SysrootBuilder.plan_prepared_refresh(fake, record, '/obj'))

        wrong_existing_host = copy.deepcopy(old)
        wrong_existing_host['flags'].append('HOST_AUTOCONF=/old/other-autoconf')
        fake.context_signature = lambda path: new
        self.assertFalse(SysrootBuilder.plan_prepared_refresh(
            fake, {'signature': wrong_existing_host}, '/obj'))

        old_role = copy.deepcopy(old)
        old_role['native_toolset']['tools']['autoconf'] = '/old/autoconf'
        self.assertFalse(SysrootBuilder.plan_prepared_refresh(fake, {'signature': old_role}, '/obj'))

        unrelated_role = copy.deepcopy(new)
        unrelated_role['native_toolset']['tools']['ninja'] = '/native/bin/ninja'
        fake.context_signature = lambda path: unrelated_role
        self.assertFalse(SysrootBuilder.plan_prepared_refresh(fake, record, '/obj'))

        no_openssh_refresh = copy.deepcopy(new)
        no_openssh_refresh['common_sources']['openssh'] = copy.deepcopy(old['common_sources']['openssh'])
        no_openssh_refresh['common_sources']['openldap']['tree_sha256'] = '5' * 64
        fake.context_signature = lambda path: no_openssh_refresh
        self.assertFalse(SysrootBuilder.plan_prepared_refresh(fake, record, '/obj'))

        path_only_old, path_only_new = copy.deepcopy(old), copy.deepcopy(old)
        path_only_new['flags'] = [flag.replace('/prepared/openssl-old', '/tmp/openssl-current')
                                  for flag in path_only_new['flags']]
        fake.context_signature = lambda path: path_only_new
        self.assertFalse(SysrootBuilder.plan_prepared_refresh(
            fake, {'signature': path_only_old}, '/obj'))

        no_refresh_old = copy.deepcopy(old)
        no_refresh_new = copy.deepcopy(old)
        no_refresh_new['llvm_source']['tree_sha256'] = 'c' * 64
        no_refresh_new['runtime']['manifest']['source']['sha512'] = '9' * 128
        no_refresh_new['flags'] = list(no_refresh_old['flags']) + ['EMBER_LLVM_SOURCE=/tmp/other-llvm']
        fake.context_signature = lambda path: no_refresh_new
        self.assertFalse(SysrootBuilder.plan_prepared_refresh(fake, {'signature': no_refresh_old}, '/obj'))


if __name__ == '__main__':
    unittest.main()
