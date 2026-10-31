# SPDX-License-Identifier: BSD-2-Clause
"""Scoped invalidation of prepared-source object consumers."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk import prepared_refresh
from emberbsd_sdk.errors import SDKError


class PreparedRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        self.os, self.obj = root / 'os', root / 'obj'
        (self.os / 'share/mk').mkdir(parents=True)
        self.obj.mkdir()
        self.contract = {'version': 1, 'sources': {'EMBER_LIBCBOR_SOURCE': {
            'producer_dirs': ['external/cbor'], 'consumer_dirs': ['external/fido'],
            'header_prefixes': ['usr/include/cbor.h', 'usr/include/cbor/'],
            'linked_names': ['libcbor.so', 'libcbor.a']}},
            'header_independent_object_patterns': ['lib/csu/*.o'],
            'header_independent_archive_patterns': []}
        self.write_contract()
        for name in ('external/cbor', 'external/fido', 'other/consumer', 'unknown/generated', 'lib/csu'):
            (self.os / name).mkdir(parents=True)
            (self.os / name / 'Makefile').write_text('')
            (self.obj / name).mkdir(parents=True)

    def write_contract(self):
        (self.os / prepared_refresh.CONTRACT).write_text(json.dumps(self.contract))

    def test_changed_headers_and_unknown_objects_are_cleaned(self):
        (self.obj / 'other/consumer/test.d').write_text('test.o: /old/usr/include/cbor/data.h\n')
        (self.obj / 'other/consumer/test.o').write_bytes(b'')
        (self.obj / 'unknown/generated/file.o').write_bytes(b'')
        (self.obj / 'lib/csu/crt0.o').write_bytes(b'')
        self.assertEqual(prepared_refresh.plan(self.os, self.obj, {'libcbor'}, lambda p: []),
                         ['external/cbor', 'external/fido', 'other/consumer', 'unknown/generated'])

    def test_linked_and_uncovered_archive_are_cleaned(self):
        (self.obj / 'other/consumer/libx.so.1').write_bytes(b'\x7fELF....libcbor.so.1\0')
        (self.obj / 'unknown/generated/libx.a').write_bytes(b'archive')
        dirs = prepared_refresh.plan(self.os, self.obj, {'libcbor'}, lambda p: ['missing.o'])
        self.assertIn('other/consumer', dirs)
        self.assertIn('unknown/generated', dirs)

    def test_binary_alias_to_source_tree_is_refused(self):
        target = self.os / 'external/cbor/hidden'
        target.write_bytes(b'\x7fELFbinary')
        (self.obj / 'external/cbor/alias').symlink_to(target)
        with self.assertRaisesRegex(SDKError, 'binary alias outside object tree'):
            prepared_refresh.plan(self.os, self.obj, {'libcbor'}, lambda p: [])

    def test_external_directory_alias_is_refused(self):
        (self.obj / 'escape').symlink_to(Path(self.temp.name))
        with self.assertRaises(SDKError):
            prepared_refresh.plan(self.os, self.obj, {'libcbor'}, lambda p: [])

    def test_native_producers_are_scoped_and_paths_validated(self):
        self.contract['sources']['EMBER_LIBCBOR_SOURCE']['native_producer_dirs'] = ['external/cbor']
        self.write_contract()
        self.assertEqual(prepared_refresh.plan_native(self.os, self.obj, {'libcbor'}), ['external/cbor'])
        self.contract['sources']['EMBER_LIBCBOR_SOURCE']['native_producer_dirs'] = ['../outside']
        self.write_contract()
        with self.assertRaises(SDKError):
            prepared_refresh.plan_native(self.os, self.obj, {'libcbor'})

    def test_unknown_source_or_unsafe_path_is_refused(self):
        with self.assertRaises(SDKError):
            prepared_refresh.plan(self.os, self.obj, {'unknown'})
        self.contract['sources']['EMBER_LIBCBOR_SOURCE']['producer_dirs'] = ['../outside']
        self.write_contract()
        with self.assertRaises(SDKError):
            prepared_refresh.plan(self.os, self.obj, {'libcbor'})

    def test_nested_outputs_require_explicit_leaf_owner(self):
        nested = self.obj / 'unknown/generated/waf-build/default'
        nested.mkdir(parents=True)
        (nested / 'unit.o').write_bytes(b'object')
        with self.assertRaisesRegex(SDKError, 'missing OS makefiles'):
            prepared_refresh.plan(self.os, self.obj, {'libcbor'}, lambda p: [])
        self.contract['nested_object_owners'] = {
            'unknown/generated/waf-build': 'unknown/generated'}
        self.write_contract()
        outputs = []
        dirs = prepared_refresh.plan(self.os, self.obj, {'libcbor'}, lambda p: [], outputs)
        self.assertIn('unknown/generated', dirs)
        self.assertEqual(outputs, [str(self.obj/'unknown/generated/waf-build')])
        self.contract['nested_object_owners'] = {'unknown/generated/waf-build': 'unknown'}
        self.write_contract()
        with self.assertRaises(SDKError):
            prepared_refresh.plan(self.os, self.obj, {'libcbor'}, lambda p: [])

    def test_source_without_headers_or_libraries_keeps_unrelated_unknown_objects(self):
        self.contract['sources']['EMBER_WPA_SOURCE'] = {
            'producer_dirs': ['external/cbor'], 'consumer_dirs': [],
            'header_prefixes': [], 'linked_names': []}
        self.write_contract()
        unknown = self.obj / 'unknown/generated/nested'
        unknown.mkdir()
        (unknown / 'file.o').write_bytes(b'object')
        # There is intentionally no owning source Makefile in the nested
        # directory, as in a Waf object subdirectory.
        self.assertEqual(prepared_refresh.plan(self.os, self.obj, {'wpa'}, lambda p: []),
                         ['external/cbor'])
        with self.assertRaisesRegex(SDKError, 'missing OS makefiles'):
            prepared_refresh.plan(self.os, self.obj, {'wpa', 'libcbor'}, lambda p: [])


class TransitionTests(unittest.TestCase):
    def test_pending_native_refresh_accepts_repaired_source_not_toolchain(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from emberbsd_sdk.sysroot_builder import SysrootBuilder
        original = {'compiler': 'same', 'native_sources': {'demo': {'path': 'old'}}}
        interrupted = {'compiler': 'same', 'native_sources': {'demo': {'path': 'failed'}}}
        record = {'signature': original, 'pending_native': {'signature': interrupted, 'sources': ['demo']}}
        selected = {'compiler': 'same', 'native_sources': {'demo': {'path': 'old'}}}
        fake = SimpleNamespace(paths={'os': '/os', 'objects': '/obj'},
            context_signature=lambda p: selected,
            compatible_context=lambda r, p: r['signature'] == selected)
        with patch('emberbsd_sdk.sysroot_builder.prepared_refresh.plan_native',
                   return_value=['tools/demo']) as plan:
            self.assertTrue(SysrootBuilder.plan_native_refresh(fake, record, '/tools'))
            plan.assert_called_with('/os', '/obj', {'demo'})
            # A second failure while restoring the old source must remain retryable.
            record['pending_native'] = {'signature': selected, 'sources': ['demo']}
            self.assertTrue(SysrootBuilder.plan_native_refresh(fake, record, '/tools'))
            selected = {'compiler': 'same', 'native_sources': {'demo': {'path': 'fixed'}}}
            self.assertTrue(SysrootBuilder.plan_native_refresh(fake, record, '/tools'))
            selected = {'compiler': 'changed', 'native_sources': {'demo': {'path': 'fixed'}}}
            self.assertFalse(SysrootBuilder.plan_native_refresh(fake, record, '/tools'))

    def test_native_partial_install_is_pending_and_retries_same_inputs(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from emberbsd_sdk.sysroot_builder import SysrootBuilder
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            dirs = ['tools/compile_et', 'tools/slc']
            for name in ('obj/tools/compile_et', 'obj/tools/slc', 'tools',
                         'os/tools/compile_et', 'os/tools/slc', 'dest'):
                (root / name).mkdir(parents=True)
            for name in ('obj', 'tools'):
                (root / name / '.prebuild-sdk-context.json').write_text('old context')
            old = {'format': 'emberbsd-sysroot-object-context', 'version': 1,
                   'signature': {}, 'sources': {}}
            selected = {'native_sources': {'demo': 'new'}}
            calls = []
            def dispatch(command):
                calls.append(command)
                if command[-2:] == ['dependall', 'install']:
                    name = Path(command[command.index('-C') + 1]).name
                    if name == 'slc':
                        raise SDKError('install failed')
                    (root/'tools/installed').write_text('new')
            fake = SimpleNamespace(paths={'objects': str(root/'obj'), 'tools': str(root/'tools'),
                         'os': str(root/'os'), 'destination': str(root/'dest'),
                         'native-routing': str(root/'tools/routing'), 'sdk': str(root)},
                         contexts={'objects': old, 'tools': old}, native_refresh_dirs=dirs, native_refresh_sources=['demo'],
                         target={'machine': 'evbarm'}, flags=[], environment={},
                         inspect_context=lambda p: old, dispatch=dispatch,
                         context_signature=lambda p: selected, sources={},
                         options={'jobs': '4'}, CONTEXT='.prebuild-sdk-context.json',
                         QUARANTINE='.quarantine')
            def capture(command, **kwargs):
                name = Path(command[command.index('-C') + 1]).name
                return SimpleNamespace(stdout=(str(root/'obj/tools'/name)+'\n').encode())
            with patch('emberbsd_sdk.sysroot_builder.process.capture', side_effect=capture):
                with self.assertRaisesRegex(SDKError, 'install failed'):
                    SysrootBuilder.prepare_contexts(fake)
            self.assertEqual((root/'tools/installed').read_text(), 'new')
            pending = json.loads((root/'tools/.prebuild-sdk-context.json').read_text())
            self.assertEqual(pending['signature'], {})
            self.assertEqual(pending['pending_native'], {'signature': selected, 'sources': ['demo']})
            self.assertEqual((root/'obj/.prebuild-sdk-context.json').read_text(), 'old context')
            from types import MethodType
            fake.compatible_context = lambda record, path: record['signature'] == selected
            fake.plan_native_refresh = MethodType(SysrootBuilder.plan_native_refresh, fake)
            fake.validate_source_ancestry = lambda record, path: None
            with patch('emberbsd_sdk.sysroot_builder.prepared_refresh.plan_native', return_value=dirs):
                self.assertEqual(SysrootBuilder.inspect_context(fake, str(root/'tools')), pending)
                selected = {'native_sources': {'demo': 'old'}}
                self.assertEqual(SysrootBuilder.inspect_context(fake, str(root/'tools')), pending)
                self.assertEqual(fake.native_refresh_dirs, dirs)
                selected = {'native_sources': {'demo': 'fixed'}}
                self.assertEqual(SysrootBuilder.inspect_context(fake, str(root/'tools')), pending)
                selected = {'native_sources': {'demo': 'new'}, 'toolchain': 'different'}
                with self.assertRaisesRegex(SDKError, 'cannot safely resume'):
                    SysrootBuilder.inspect_context(fake, str(root/'tools'))
            selected = {'native_sources': {'demo': 'new'}}
            fake.contexts = {'objects': old, 'tools': pending}
            fake.inspect_context = lambda p: old if p == str(root/'obj') else pending
            fake.dispatch = lambda command: calls.append(command)
            fake.mkdirs = lambda *args: None
            with patch('emberbsd_sdk.sysroot_builder.process.capture', side_effect=capture):
                SysrootBuilder.prepare_contexts(fake)
            final = json.loads((root/'tools/.prebuild-sdk-context.json').read_text())
            self.assertNotIn('pending_native', final)
            self.assertEqual(final['signature'], selected)
            self.assertEqual([Path(c[c.index('-C')+1]).name for c in calls[-4:]],
                             ['compile_et', 'compile_et', 'slc', 'slc'])

    def test_nested_output_left_after_clean_preserves_context(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from emberbsd_sdk.sysroot_builder import SysrootBuilder
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            for name in ('obj/producer/nested', 'tools', 'os/producer', 'dest'):
                (root / name).mkdir(parents=True)
            context = root/'obj/.prebuild-sdk-context.json'
            context.write_text('old context')
            old = {'signature': {}}
            fake = SimpleNamespace(paths={'objects': str(root/'obj'), 'tools': str(root/'tools'),
                         'os': str(root/'os'), 'destination': str(root/'dest'),
                         'native-routing': str(root/'tools/routing'), 'sdk': str(root)},
                         contexts={'objects': old, 'tools': None}, refresh_dirs=['producer'],
                         refresh_nested_outputs=[str(root/'obj/producer/nested')],
                         target={'machine': 'evbarm'}, flags=[], environment={},
                         inspect_context=lambda p: old if p == str(root/'obj') else None,
                         dispatch=lambda command: None)
            with patch('emberbsd_sdk.sysroot_builder.process.capture',
                       return_value=SimpleNamespace(stdout=(str(root/'obj/producer')+'\n').encode())):
                with self.assertRaisesRegex(SDKError, 'nested object output remains'):
                    SysrootBuilder.prepare_contexts(fake)
            self.assertEqual(context.read_text(), 'old context')

    def test_failure_before_context_update_preserves_record(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from emberbsd_sdk.sysroot_builder import SysrootBuilder
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            for name in ('obj/producer', 'tools', 'os/producer', 'dest'):
                (root / name).mkdir(parents=True)
            context = root / 'obj/.prebuild-sdk-context.json'
            context.write_text('old context')
            old = {'signature': {}}
            def fail_clean(command):
                self.assertIn('MKCLEANSRC=no', command)
                raise SDKError('clean failed')
            fake = SimpleNamespace(paths={'objects': str(root/'obj'), 'tools': str(root/'tools'),
                         'os': str(root/'os'), 'destination': str(root/'dest'),
                         'native-routing': str(root/'tools/routing'), 'sdk': str(root)},
                         contexts={'objects': old, 'tools': None}, refresh_dirs=['producer'],
                         target={'machine': 'evbarm'}, flags=[], environment={},
                         inspect_context=lambda p: old if p == str(root/'obj') else None,
                         dispatch=fail_clean)
            with patch('emberbsd_sdk.sysroot_builder.process.capture',
                       return_value=SimpleNamespace(stdout=(str(root/'obj/producer')+'\n').encode())):
                with self.assertRaisesRegex(SDKError, 'clean failed'):
                    SysrootBuilder.prepare_contexts(fake)
            self.assertEqual(context.read_text(), 'old context')


if __name__ == "__main__":
    unittest.main()
