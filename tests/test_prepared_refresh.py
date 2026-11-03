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
    def test_native_binding_transition_requires_original_selected_paths(self):
        from emberbsd_sdk.sysroot_builder import _compatible_native_contract
        old = ['EmberBSD-native-tools 2', 'perl perl -']
        new = ['EmberBSD-native-tools 2', 'perl perl HOST_PERL', 'python python HOST_PYTHON',
               'gmake gmake HOST_GMAKE', 'autoconf autoconf HOST_AUTOCONF']
        tools = {'perl': '/prefix/perl', 'python': '/prefix/python',
                 'gmake': '/prefix/gmake', 'autoconf': '/prefix/autoconf'}
        flags = ['HOST_PERL=/prefix/perl', 'HOST_PYTHON=/prefix/python',
                 'HOST_GMAKE=/prefix/gmake', 'HOST_AUTOCONF=/prefix/autoconf']
        self.assertTrue(_compatible_native_contract(old, new, tools, flags, flags, set()))
        self.assertFalse(_compatible_native_contract(old, new, tools, flags[1:], flags, set()))
        self.assertFalse(_compatible_native_contract(old, new, dict(tools, perl='/other/perl'), flags, flags, set()))
        wrong = [row.replace('HOST_PERL', 'HOST_OTHER') for row in new]
        self.assertFalse(_compatible_native_contract(old, wrong, tools, flags, flags, set()))
        self.assertFalse(_compatible_native_contract(old, new[1:], tools, flags, flags, set()))

    def test_tool_context_uses_only_its_matching_recorded_object_flags(self):
        from emberbsd_sdk.sysroot_builder import _native_tool_context_flags
        tools = {'perl': '/p/perl', 'python': '/p/python', 'gmake': '/p/gmake', 'autoconf': '/p/autoconf'}
        flags = ['HOST_PERL=/p/perl', 'HOST_PYTHON=/p/python', 'HOST_GMAKE=/p/gmake', 'HOST_AUTOCONF=/p/autoconf']
        old = {'format': 'emberbsd-sysroot-object-context', 'version': 1,
               'signature': {'tools': '/tools', 'flags': flags, 'native_toolset': {'tools': tools}}}
        new = {'tools': '/tools', 'flags': flags, 'native_toolset': {'tools': tools}}
        self.assertEqual(_native_tool_context_flags(old, new, '/tools', tools, tools), (flags, flags))
        self.assertIsNone(_native_tool_context_flags(old, new, '/other-tools', tools, tools))
        changed = dict(tools, perl='/other/perl')
        self.assertIsNone(_native_tool_context_flags(old, new, '/tools', changed, tools))
        missing = dict(old, signature=dict(old['signature'], flags=[]))
        self.assertEqual(_native_tool_context_flags(missing, new, '/tools', tools, tools), ([], flags))

    def test_unique_flag_assignments_are_canonicalized(self):
        from emberbsd_sdk.sysroot_builder import _canonicalize_unique_flag_assignments

        self.assertEqual(_canonicalize_unique_flag_assignments(['B=2', 'A=1']), ['A=1', 'B=2'])
        duplicates = ['A=1', 'B=2', 'A=3']
        self.assertEqual(_canonicalize_unique_flag_assignments(duplicates), duplicates)

    def test_release_objects_are_not_reused_as_sysroot_source_refresh(self):
        from copy import deepcopy
        from types import SimpleNamespace
        from unittest.mock import patch
        from emberbsd_sdk.sysroot_builder import SysrootBuilder

        flags = ['MAKECONF=/dev/null', 'MKCXX=yes', 'MKPIC=yes',
                 'EMBER_HOST_BFD_CONFIG=/host-bfd/host-bfd.mk',
                 'HOST_GMAKE=/host/bin/gmake', 'EMBER_HOST_GNU_TRIPLE=aarch64-host']
        common = {'host': {'id': 'host'}, 'target': {'machine': 'evbarm', 'arch': 'aarch64'},
                  'compiler': {'id': 'compiler'}, 'runtime': {'id': 'runtime'},
                  'runtime_prefix': '/runtime', 'llvm_source': {'id': 'llvm'},
                  'objects': '/objects', 'tools': '/tools', 'flags': flags,
                  'common_sources': {'mdns': {'tree_sha256': 'old'}}}
        old_signature = deepcopy(common)
        old_signature['release_profile'] = 'aarch64'
        old_signature['product_version'] = '1.0'
        current = deepcopy(common)
        def source(package, digest):
            return {'format': 'emberbsd-prepared-source', 'version': 1,
                    'package': package, 'tree_sha256': digest * 64}
        current['common_sources'] = {
            'mdns': source('net/mDNSResponder', 'a'),
        }
        current['flags'] = flags + ['EMBER_MDNS_SOURCE=/new/mdns']
        old_record = {'signature': old_signature}
        fake = SimpleNamespace(
            signature=current, paths={'os': '/os', 'objects': '/objects', 'tools': '/tools',
                                      'mdns-source': '/new/mdns'},
            native_selection={}, COMMON_SOURCES=SysrootBuilder.COMMON_SOURCES,
            context_signature=lambda path: current)
        fake.reuse_signature = lambda signature: SysrootBuilder.reuse_signature(fake, signature)
        fake.compatible_context = lambda record, path: record['signature'] == current
        with patch('emberbsd_sdk.sysroot_builder.prepared_refresh.plan',
                   return_value=[]) as plan:
            refreshed = SysrootBuilder.plan_prepared_refresh(fake, old_record, '/objects')
        self.assertFalse(refreshed)
        self.assertFalse(plan.called)
        self.assertEqual(fake.reuse_signature(old_signature)['release_profile'], 'aarch64')
        self.assertEqual(fake.reuse_signature(old_signature)['product_version'], '1.0')

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

    def test_selected_nested_owner_prunes_old_alias_contents(self):
        nested = self.obj / 'external/cbor/generated'
        nested.mkdir(parents=True)
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        (nested / 'source-view').symlink_to(outside, target_is_directory=True)
        self.contract['nested_object_owners'] = {
            'external/cbor/generated': 'external/cbor'}
        self.write_contract()
        outputs = []
        dirs = prepared_refresh.plan(self.os, self.obj, {'libcbor'}, lambda p: [], outputs)
        self.assertIn('external/cbor', dirs)
        self.assertEqual(outputs, [str(nested)])

    def test_unselected_nested_alias_is_still_refused(self):
        nested = self.obj / 'other/consumer/generated'
        nested.mkdir(parents=True)
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        (nested / 'source-view').symlink_to(outside, target_is_directory=True)
        self.contract['nested_object_owners'] = {
            'other/consumer/generated': 'other/consumer'}
        self.write_contract()
        with self.assertRaisesRegex(SDKError, 'unverified directory alias'):
            prepared_refresh.plan(self.os, self.obj, {'libcbor'}, lambda p: [])

    def test_selected_prepared_source_directory_alias_is_allowed_without_following(self):
        selected = Path(self.temp.name) / 'prepared-libcbor'
        (selected / 'generated').mkdir(parents=True)
        (selected / 'generated/ignored.o').write_bytes(b'object')
        (self.obj / 'other/consumer/source-view').symlink_to(selected, target_is_directory=True)
        result = prepared_refresh.plan(self.os, self.obj, {'libcbor'}, lambda p: [],
                                      allowed_source_roots=[selected])
        self.assertIn('external/cbor', result)

    def test_selected_prepared_source_header_alias_is_allowed(self):
        selected = Path(self.temp.name) / 'prepared-libcbor'
        (selected / 'include').mkdir(parents=True)
        header = selected / 'include/cbor.h'
        header.write_text('/* selected prepared header */\n')
        (self.obj / 'other/consumer/source-header.h').symlink_to(header)
        result = prepared_refresh.plan(self.os, self.obj, {'libcbor'}, lambda p: [],
                                      allowed_source_roots=[selected])
        self.assertIn('external/cbor', result)

    def test_arbitrary_external_file_alias_is_still_refused(self):
        outside = Path(self.temp.name) / 'unselected/include'
        outside.mkdir(parents=True)
        header = outside / 'cbor.h'
        header.write_text('/* not selected */\n')
        (self.obj / 'other/consumer/source-header.h').symlink_to(header)
        with self.assertRaisesRegex(SDKError, 'unverified file alias'):
            prepared_refresh.plan(self.os, self.obj, {'libcbor'}, lambda p: [])

    def test_selected_prepared_source_binary_alias_is_refused(self):
        selected = Path(self.temp.name) / 'prepared-libcbor'
        selected.mkdir()
        binary = selected / 'libcbor.so'
        binary.write_bytes(b'\x7fELFselected binary')
        (self.obj / 'other/consumer/libcbor.so').symlink_to(binary)
        with self.assertRaisesRegex(SDKError, 'binary alias outside object tree'):
            prepared_refresh.plan(self.os, self.obj, {'libcbor'}, lambda p: [],
                                  allowed_source_roots=[selected])

    def test_selected_prepared_source_dependency_alias_is_refused(self):
        selected = Path(self.temp.name) / 'prepared-libcbor'
        selected.mkdir()
        depfile = selected / 'consumer.d'
        depfile.write_text('consumer.o: /old/usr/include/cbor.h\n')
        (self.obj / 'other/consumer/consumer.d').symlink_to(depfile)
        with self.assertRaisesRegex(SDKError, 'symlink dependency file'):
            prepared_refresh.plan(self.os, self.obj, {'libcbor'}, lambda p: [],
                                  allowed_source_roots=[selected])

    def test_selected_nested_symlink_root_is_refused(self):
        nested = self.obj / 'external/cbor/generated'
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        nested.symlink_to(outside, target_is_directory=True)
        self.contract['nested_object_owners'] = {
            'external/cbor/generated': 'external/cbor'}
        self.write_contract()
        with self.assertRaisesRegex(SDKError, 'invalid nested object output'):
            prepared_refresh.plan(self.os, self.obj, {'libcbor'}, lambda p: [], [])

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
    def test_prepared_runtime_refresh_keeps_current_native_abi_bindings(self):
        from copy import deepcopy
        from types import SimpleNamespace
        from unittest.mock import patch
        from emberbsd_sdk.sysroot_builder import SysrootBuilder
        old_flags = ['EMBER_HOST_ABI_CPPFLAGS=', 'EMBER_HOST_ABI_CFLAGS=',
                     'EMBER_HOST_ABI_CXXFLAGS=', 'EMBER_HOST_ABI_LDFLAGS=',
                     'EMBER_HOST_COMPILER_FAMILY=clang', 'EMBER_LLVM_SOURCE=/llvm', 'EMBER_OPENSSL_SOURCE=/old/openssl']
        new_flags = ['EMBER_HOST_ABI_CPPFLAGS=', 'EMBER_HOST_ABI_CFLAGS=',
                     'EMBER_HOST_ABI_CXXFLAGS=', 'EMBER_HOST_ABI_LDFLAGS=',
                     'EMBER_HOST_COMPILER_FAMILY=clang', 'EMBER_LLVM_SOURCE=/llvm', 'EMBER_OPENSSL_SOURCE=/new/openssl']

        def source(tree):
            return {'format': 'emberbsd-prepared-source', 'version': 1,
                    'package': 'security/openssl', 'tree_sha256': tree * 64}

        old_signature = {'flags': old_flags, 'common_sources': {'openssl': source('a')},
                         'runtime': {'old': True}, 'runtime_prefix': '/old/runtime',
                         'llvm_source': {'old': True}}
        new_signature = {'flags': new_flags, 'common_sources': {'openssl': source('b')},
                         'runtime': {'new': True}, 'runtime_prefix': '/new/runtime',
                         'llvm_source': {'new': True}}
        fake = SimpleNamespace(COMMON_SOURCES=SysrootBuilder.COMMON_SOURCES,
            paths={'os': '/os', 'objects': '/objects', 'tools': '/tools',
                   'openssl-source': '/new/openssl'},
            context_signature=lambda path: new_signature,
            reuse_signature=lambda signature: signature,
            compatible_context=lambda record, path: True)
        fake.reuse_signature = lambda signature: SysrootBuilder.reuse_signature(fake, signature)
        with patch('emberbsd_sdk.sysroot_builder.runtime_refresh.cpp_update',
                   return_value={'headers': ['usr/include/c++/v1'],
                                 'linked': ['libc++.so.1']}), \
             patch('emberbsd_sdk.sysroot_builder.llvm_source_refresh.plan', return_value={}) as llvm_plan, \
             patch('emberbsd_sdk.sysroot_builder.prepared_refresh.plan',
                   return_value=['lib/libcxx']) as plan:
            refreshed = SysrootBuilder.plan_prepared_refresh(
                fake, {'signature': old_signature}, '/objects')
        self.assertTrue(refreshed)
        llvm_plan.assert_called_once_with('/objects', '/llvm', '/llvm')
        self.assertEqual(fake.refresh_dirs, ['lib/libcxx'])
        self.assertEqual(plan.call_args.args[2], {'openssl'})
        self.assertEqual(plan.call_args.kwargs,
                         {'allowed_source_roots': ['/new/openssl', '/old/openssl'],
                          'extra_headers': ['usr/include/c++/v1'],
                          'extra_linked': ['libc++.so.1']})
        for flag in ('EMBER_HOST_ABI_CFLAGS=-mabi=other', 'UNRELATED_FLAG=yes'):
            with self.subTest(flag=flag):
                invalid = deepcopy(new_signature)
                invalid['flags'] = new_flags[:-1] + [flag, new_flags[-1]]
                fake.context_signature = lambda path, value=invalid: value
                self.assertFalse(SysrootBuilder.plan_prepared_refresh(
                    fake, {'signature': old_signature}, '/objects'))

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
            wrapper = {'ready': False}
            fail_slc = {'enabled': True}
            def dispatch(command):
                calls.append(command)
                if command[-1:] == ['makewrapper']:
                    wrapper['ready'] = True
                if command[-2:] == ['dependall', 'install']:
                    name = Path(command[command.index('-C') + 1]).name
                    if name == 'slc' and fail_slc['enabled']:
                        fail_slc['enabled'] = False
                        raise SDKError('install failed')
                    (root/'tools/installed').write_text('new')
            fake = SimpleNamespace(paths={'objects': str(root/'obj'), 'tools': str(root/'tools'),
                         'os': str(root/'os'), 'destination': str(root/'dest'),
                         'native-routing': str(root/'tools/routing'), 'sdk': str(root)},
                         contexts={'objects': old, 'tools': old}, native_refresh_dirs=dirs, native_refresh_sources=['demo'],
                         target={'machine': 'evbarm'}, flags=[], environment={},
                         build_command=['/bin/sh', str(root/'os/build.sh'), '-O', str(root/'obj'),
                                        '-T', str(root/'tools'), '-D', str(root/'dest'),
                                        '-V', 'FLAG=current', 'tools', 'libs'],
                         os_build_operations=['tools', 'libs'],
                         inspect_context=lambda p: old, dispatch=dispatch,
                         context_signature=lambda p: selected, sources={},
                         options={'jobs': '4'}, CONTEXT='.prebuild-sdk-context.json',
                         QUARANTINE='.quarantine')
            fake.native_makewrapper_command = lambda: SysrootBuilder.native_makewrapper_command(fake)
            def capture(command, **kwargs):
                self.assertTrue(wrapper['ready'], 'current wrapper must precede refresh queries')
                name = Path(command[command.index('-C') + 1]).name
                return SimpleNamespace(stdout=(str(root/'obj/tools'/name)+'\n').encode())
            with patch('emberbsd_sdk.sysroot_builder.process.capture', side_effect=capture):
                with self.assertRaisesRegex(SDKError, 'install failed'):
                    SysrootBuilder.prepare_contexts(fake)
            self.assertEqual(calls[0], ['/bin/sh', str(root/'os/build.sh'), '-O', str(root/'obj'),
                                        '-T', str(root/'tools'), '-D', str(root/'dest'),
                                        '-V', 'FLAG=current', 'makewrapper'])
            self.assertNotIn('tools', calls[0])
            self.assertNotIn('libs', calls[0])
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
            wrapper['ready'] = False
            fake.dispatch = dispatch
            fake.mkdirs = lambda *args: None
            with patch('emberbsd_sdk.sysroot_builder.process.capture', side_effect=capture):
                SysrootBuilder.prepare_contexts(fake)
            self.assertEqual(calls[-5], ['/bin/sh', str(root/'os/build.sh'), '-O', str(root/'obj'),
                                         '-T', str(root/'tools'), '-D', str(root/'dest'),
                                         '-V', 'FLAG=current', 'makewrapper'])
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
                         build_command=['/bin/sh', str(root/'os/build.sh'), '-O', str(root/'obj'),
                                        '-T', str(root/'tools'), '-D', str(root/'dest'), 'libs'],
                         os_build_operations=['libs'],
                         inspect_context=lambda p: old if p == str(root/'obj') else None,
                         dispatch=lambda command: None)
            fake.native_makewrapper_command = lambda: SysrootBuilder.native_makewrapper_command(fake)
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
                if command[-1:] == ['makewrapper']:
                    return
                self.assertIn('MKCLEANSRC=no', command)
                raise SDKError('clean failed')
            fake = SimpleNamespace(paths={'objects': str(root/'obj'), 'tools': str(root/'tools'),
                         'os': str(root/'os'), 'destination': str(root/'dest'),
                         'native-routing': str(root/'tools/routing'), 'sdk': str(root)},
                         contexts={'objects': old, 'tools': None}, refresh_dirs=['producer'],
                         target={'machine': 'evbarm'}, flags=[], environment={},
                         build_command=['/bin/sh', str(root/'os/build.sh'), '-O', str(root/'obj'),
                                        '-T', str(root/'tools'), '-D', str(root/'dest'), 'libs'],
                         os_build_operations=['libs'],
                         inspect_context=lambda p: old if p == str(root/'obj') else None,
                         dispatch=fail_clean)
            fake.native_makewrapper_command = lambda: SysrootBuilder.native_makewrapper_command(fake)
            with patch('emberbsd_sdk.sysroot_builder.process.capture',
                       return_value=SimpleNamespace(stdout=(str(root/'obj/producer')+'\n').encode())):
                with self.assertRaisesRegex(SDKError, 'clean failed'):
                    SysrootBuilder.prepare_contexts(fake)
            self.assertEqual(context.read_text(), 'old context')


if __name__ == "__main__":
    unittest.main()
