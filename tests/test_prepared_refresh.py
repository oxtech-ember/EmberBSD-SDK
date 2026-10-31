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

    def test_unknown_source_or_unsafe_path_is_refused(self):
        with self.assertRaises(SDKError):
            prepared_refresh.plan(self.os, self.obj, {'unknown'})
        self.contract['sources']['EMBER_LIBCBOR_SOURCE']['producer_dirs'] = ['../outside']
        self.write_contract()
        with self.assertRaises(SDKError):
            prepared_refresh.plan(self.os, self.obj, {'libcbor'})


class TransitionTests(unittest.TestCase):
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
            fake = SimpleNamespace(paths={'objects': str(root/'obj'), 'tools': str(root/'tools'),
                         'os': str(root/'os'), 'destination': str(root/'dest'),
                         'native-routing': str(root/'tools/routing'), 'sdk': str(root)},
                         contexts={'objects': old, 'tools': None}, refresh_dirs=['producer'],
                         target={'machine': 'evbarm'}, flags=[], environment={},
                         inspect_context=lambda p: old if p == str(root/'obj') else None,
                         dispatch=lambda command: (_ for _ in ()).throw(SDKError('clean failed')))
            with patch('emberbsd_sdk.sysroot_builder.process.capture',
                       return_value=SimpleNamespace(stdout=(str(root/'obj/producer')+'\n').encode())):
                with self.assertRaisesRegex(SDKError, 'clean failed'):
                    SysrootBuilder.prepare_contexts(fake)
            self.assertEqual(context.read_text(), 'old context')


if __name__ == "__main__":
    unittest.main()
