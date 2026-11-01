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
    def test_native_abi_binding_rename_is_exact_and_preserves_other_flags(self):
        from emberbsd_sdk.sysroot_builder import _normalize_legacy_native_abi_flags
        selection = {'format': 'emberbsd-host-prefix', 'version': 2,
                     'compiler': {'family': 'clang'},
                     'abi_flags': {'cppflags': [], 'cflags': [],
                                   'cxxflags': [], 'ldflags': []}}
        old = ['HOST_CPPFLAGS=', 'HOST_CFLAGS=', 'HOST_CXXFLAGS=', 'HOST_LDFLAGS=',
               'EMBER_HOST_COMPILER_FAMILY=clang', 'MKCXX=yes']
        new = ['EMBER_HOST_ABI_CPPFLAGS=', 'EMBER_HOST_ABI_CFLAGS=',
               'EMBER_HOST_ABI_CXXFLAGS=', 'EMBER_HOST_ABI_LDFLAGS=',
               'EMBER_HOST_COMPILER_FAMILY=clang', 'MKCXX=yes']
        self.assertEqual(_normalize_legacy_native_abi_flags(old, new, selection), new)
        changed_value = new.copy()
        changed_value[1] = 'EMBER_HOST_ABI_CFLAGS=-mabi=other'
        self.assertIsNone(_normalize_legacy_native_abi_flags(old, changed_value, selection))
        changed_other = new + ['MKDEBUG=yes']
        self.assertNotEqual(_normalize_legacy_native_abi_flags(old, changed_other, selection),
                            changed_other)
        mixed = old.copy()
        mixed[1] = 'EMBER_HOST_ABI_CFLAGS='
        self.assertIsNone(_normalize_legacy_native_abi_flags(mixed, new, selection))

    def test_native_abi_routing_transition_requires_v1_to_v2_with_identical_rows(self):
        from emberbsd_sdk.sysroot_builder import _native_abi_contract_transition
        old = 'EmberBSD-native-tools 1\ncc cc HOST_CC\nsh sh HOST_SH\n'
        selected = 'EmberBSD-native-tools 2\ncc cc HOST_CC\nsh sh HOST_SH\n'
        self.assertTrue(_native_abi_contract_transition(old, selected))
        self.assertFalse(_native_abi_contract_transition(
            old, selected.replace('cc cc HOST_CC', 'cc wrong HOST_CC')))
        self.assertFalse(_native_abi_contract_transition(
            old, selected.replace(' 2\n', ' 3\n')))

    def test_native_tools_transition_uses_build_sh_tools_clean_build_sequence(self):
        from emberbsd_sdk.sysroot_builder import _native_tools_transition_command
        command = ['/selected/sh', '/os/build.sh', '-U', '-u', '-j10', '-m', 'evbarm',
                   '-a', 'aarch64', '-O', '/obj', '-T', '/tools', '-D', '/dest',
                   '-V', 'EMBER_HOST_ABI_CFLAGS=', 'tools', 'libs']
        self.assertEqual(_native_tools_transition_command(command),
                         ['/selected/sh', '/os/build.sh', '-U', '-j10', '-m', 'evbarm',
                          '-a', 'aarch64', '-O', '/obj', '-T', '/tools', '-D', '/dest',
                          '-V', 'EMBER_HOST_ABI_CFLAGS=', '-V', 'MKCLEANSRC=no', 'tools'])

    def test_native_abi_pending_transition_retries_only_same_tools_context(self):
        from copy import deepcopy
        import hashlib
        from types import MethodType, SimpleNamespace
        from unittest.mock import patch
        from emberbsd_sdk.sysroot_builder import SysrootBuilder
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            os_root, obj_root, tools_root = root / 'os', root / 'obj', root / 'tools'
            contract_dir = os_root / 'tools/native-tools'
            contract_dir.mkdir(parents=True)
            obj_root.mkdir(); tools_root.mkdir()
            old_contract = ('EmberBSD-native-tools 1\n'
                            'cmp cmp -\nmake make MAKE\nreadlink readlink -\nsh sh HOST_SH\n')
            selected_contract = old_contract.replace('EmberBSD-native-tools 1',
                                                       'EmberBSD-native-tools 2')
            contract_path = contract_dir / 'contract'
            contract_path.write_text(selected_contract)
            old_digest = hashlib.sha256(old_contract.encode()).hexdigest()
            new_digest = hashlib.sha256(selected_contract.encode()).hexdigest()
            old_route = tools_root / ('native-routing-' + old_digest)
            old_route.mkdir()
            (old_route / 'contract').write_text(old_contract)
            selected = {'tools': str(tools_root),
                        'environment': {'EMBER_NATIVE_TOOLS': str(tools_root / ('native-routing-' + new_digest))},
                        'native_sources': {}}
            previous = deepcopy(selected)
            previous['environment']['EMBER_NATIVE_TOOLS'] = str(old_route)
            record = {'signature': previous,
                      'pending_native': {'signature': deepcopy(selected), 'sources': []}}
            fake = SimpleNamespace(paths={'os': str(os_root), 'objects': str(obj_root),
                                          'tools': str(tools_root),
                                          'native-routing': str(tools_root / ('native-routing-' + new_digest))},
                native_contract=str(contract_path),
                context_signature=lambda path: selected,
                native_abi_refresh=False)
            fake.recorded_native_contract = MethodType(SysrootBuilder.recorded_native_contract, fake)
            fake.native_abi_route_transition = MethodType(SysrootBuilder.native_abi_route_transition, fake)
            self.assertTrue(fake.native_abi_route_transition(record, previous, selected))
            def compatible(candidate, path):
                old = deepcopy(candidate['signature'])
                new = deepcopy(selected)
                old.pop('native_sources', None)
                new.pop('native_sources', None)
                if old == new:
                    return True
                if not fake.native_abi_route_transition(candidate, old, new):
                    return False
                old['environment']['EMBER_NATIVE_TOOLS'] = new['environment']['EMBER_NATIVE_TOOLS']
                return old == new
            fake.compatible_context = compatible
            self.assertTrue(SysrootBuilder.plan_native_refresh(fake, record, str(tools_root)))
            self.assertTrue(fake.native_abi_refresh)
            # The object context may already have committed when a crash leaves
            # the tools context pending with source names from the old run.
            record['pending_native']['sources'] = ['heimdal']
            fake.native_abi_refresh = False
            with patch('emberbsd_sdk.sysroot_builder.prepared_refresh.plan_native',
                       return_value=['tools/heimdal']) as plan:
                self.assertTrue(SysrootBuilder.plan_native_refresh(fake, record, str(tools_root)))
            self.assertTrue(fake.native_abi_refresh)
            plan.assert_called_once_with(str(os_root), str(obj_root), {'heimdal'})
            selected['compiler'] = {'family': 'other'}
            fake.native_abi_refresh = False
            self.assertFalse(SysrootBuilder.plan_native_refresh(fake, record, str(tools_root)))
            self.assertFalse(fake.native_abi_refresh)

    def test_native_abi_routing_reuse_requires_certified_migration(self):
        from copy import deepcopy
        import hashlib
        from types import MethodType, SimpleNamespace
        from emberbsd_sdk.native_os_routing import native_path
        from emberbsd_sdk.sysroot_builder import SysrootBuilder
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            tools_root = root / 'tools'
            old_contract = ('EmberBSD-native-tools 1\n'
                            'cmp cmp -\nmake make MAKE\nreadlink readlink -\nsh sh HOST_SH\n')
            selected_contract = old_contract.replace('EmberBSD-native-tools 1',
                                                       'EmberBSD-native-tools 2')
            old_digest = hashlib.sha256(old_contract.encode()).hexdigest()
            new_digest = hashlib.sha256(selected_contract.encode()).hexdigest()
            old_route = tools_root / ('native-routing-' + old_digest)
            old_route.mkdir(parents=True)
            (old_route / 'contract').write_text(old_contract)
            contract = root / 'os/tools/native-tools/contract'
            contract.parent.mkdir(parents=True)
            contract.write_text(selected_contract)
            prefix = root / 'host'
            native = {'format': 'emberbsd-host-prefix', 'version': 2,
                      'bundle_root': str(prefix), 'tools': {name: str(prefix / 'bin' / name)
                          for name in ('cc', 'cmp', 'cxx', 'make', 'python', 'readlink', 'sh')}}
            environment = {'PATH': native_path(native),
                'EMBER_NATIVE_TOOLS': str(tools_root / ('native-routing-' + new_digest))}
            old = {'python': {}, 'native_toolset': native, 'environment': {
                   **environment, 'EMBER_NATIVE_TOOLS': str(old_route)}, 'tools': str(tools_root)}
            new = deepcopy(old)
            new['environment'] = environment.copy()
            fake = SimpleNamespace(paths={'tools': str(tools_root),
                                          'native-routing': environment['EMBER_NATIVE_TOOLS']},
                native_contract=str(contract), context_signature=lambda path: new,
                compiler_family=lambda: 'gcc', llvm_phase='base', native_abi_refresh=False)
            fake.reuse_signature = MethodType(SysrootBuilder.reuse_signature, fake)
            fake.recorded_native_contract = MethodType(SysrootBuilder.recorded_native_contract, fake)
            record = {'signature': old}
            self.assertFalse(SysrootBuilder.compatible_context(fake, record, str(tools_root)))
            fake.native_abi_refresh = True
            self.assertTrue(SysrootBuilder.compatible_context(fake, record, str(tools_root)))
            changed = deepcopy(new)
            changed['compiler'] = {'family': 'other'}
            fake.context_signature = lambda path: changed
            self.assertFalse(SysrootBuilder.compatible_context(fake, record, str(tools_root)))

    def test_native_abi_transition_failure_keeps_old_objects_and_pending_tools(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from emberbsd_sdk import jsonio
        from emberbsd_sdk.sysroot_builder import SysrootBuilder
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            objects, tools, os_root, dest = (root / name for name in ('obj', 'tools', 'os', 'dest'))
            for directory in (objects, tools, os_root, dest):
                directory.mkdir()
            object_context = objects / '.prebuild-sdk-context.json'
            tools_context = tools / '.prebuild-sdk-context.json'
            old_objects = {'format': 'emberbsd-sysroot-object-context', 'version': 1,
                           'signature': {'old': 'objects'}, 'sources': {}}
            old_tools = {'format': 'emberbsd-sysroot-object-context', 'version': 1,
                         'signature': {'old': 'tools'}, 'sources': {},
                         'pending_native': {'signature': {'stale': 'v1'}, 'sources': ['heimdal']}}
            jsonio.write_json(object_context, old_objects)
            jsonio.write_json(tools_context, old_tools)
            current = {'new': 'tools'}
            command = ['/selected/sh', str(os_root / 'build.sh'), '-U', '-u', '-j2',
                       '-V', 'EMBER_HOST_ABI_CFLAGS=', 'tools', 'libs']
            def fail_build(argv):
                self.assertEqual(argv, ['/selected/sh', str(os_root / 'build.sh'), '-U', '-j2',
                                        '-V', 'EMBER_HOST_ABI_CFLAGS=', '-V', 'MKCLEANSRC=no', 'tools'])
                raise SDKError('tools transition failed')
            fake = SimpleNamespace(paths={'objects': str(objects), 'tools': str(tools),
                'os': str(os_root), 'destination': str(dest), 'native-routing': str(tools / 'routing'),
                'sdk': str(root)},
                contexts={'objects': old_objects, 'tools': old_tools},
                native_abi_refresh=True, native_refresh_dirs=[], native_refresh_sources=[],
                refresh_dirs=[], build_command=command,
                inspect_context=lambda path: old_objects if path == str(objects) else old_tools,
                context_signature=lambda path: current, target={'machine': 'aarch64'},
                flags=[], environment={}, options={'jobs': '2'},
                dispatch=fail_build, mkdirs=lambda *args: None,
                native_tools_transition_command=lambda: ['/selected/sh', str(os_root / 'build.sh'), '-U', '-j2',
                    '-V', 'EMBER_HOST_ABI_CFLAGS=', '-V', 'MKCLEANSRC=no', 'tools'],
                CONTEXT='.prebuild-sdk-context.json',
                receipt={'plan': [None, command, None]})
            with self.assertRaisesRegex(SDKError, 'tools transition failed'), \
                    patch('emberbsd_sdk.sysroot_builder.process.capture') as capture:
                SysrootBuilder.prepare_contexts(fake)
            capture.assert_not_called()
            self.assertEqual(jsonio.read_json(object_context), old_objects)
            pending = jsonio.read_json(tools_context)
            self.assertEqual(pending['signature'], old_tools['signature'])
            self.assertEqual(pending['pending_native'], {'signature': current, 'sources': []})

    def test_native_abi_transition_builds_tools_once_then_preserves_target_objects(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from emberbsd_sdk import jsonio
        from emberbsd_sdk.sysroot_builder import SysrootBuilder
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            objects, tools, os_root, dest = (root / name for name in ('obj', 'tools', 'os', 'dest'))
            for directory in (objects, tools, os_root, dest):
                directory.mkdir()
            (objects / 'native.lo').write_bytes(b'\x7fELF\x02\x01\x01\x00')
            (objects / 'native.lo.o').write_bytes(b'\xcf\xfa\xed\xfe' + b'\0' * 8)
            (objects / 'target.o').write_bytes(b'\x7fELF\x02\x01\x01\x00')
            (objects / 'libtool.lo').write_text('# Generated by libtool\n')
            old = {'format': 'emberbsd-sysroot-object-context', 'version': 1,
                   'signature': {'old': True}, 'sources': {}}
            current = {'new': True}
            for path in (objects, tools):
                jsonio.write_json(path / '.prebuild-sdk-context.json', old)
            command = ['/selected/sh', str(os_root / 'build.sh'), '-U', '-u', '-j2',
                       '-V', 'EMBER_HOST_ABI_CFLAGS=', 'tools', 'libs']
            calls = []
            fake = SimpleNamespace(paths={'objects': str(objects), 'tools': str(tools),
                'os': str(os_root), 'destination': str(dest), 'native-routing': str(tools / 'routing'),
                'sdk': str(root)},
                contexts={'objects': old, 'tools': old}, native_abi_refresh=True,
                native_refresh_dirs=['tools/heimdal', 'crypto/other'],
                native_refresh_sources=['heimdal'], refresh_dirs=[],
                build_command=command,
                inspect_context=lambda path: old,
                context_signature=lambda path: current,
                dispatch=lambda argv: calls.append(argv),
                native_tools_transition_command=lambda: ['/selected/sh', str(os_root / 'build.sh'), '-U', '-j2',
                    '-V', 'EMBER_HOST_ABI_CFLAGS=', '-V', 'MKCLEANSRC=no', 'tools'],
                mkdirs=lambda *args: None, target={'machine': 'aarch64'}, flags=[],
                environment={}, options={'jobs': '2'}, CONTEXT='.prebuild-sdk-context.json',
                receipt={'plan': [None, command, None]}, sources={})
            with patch('emberbsd_sdk.sysroot_builder.process.capture',
                       return_value=SimpleNamespace(stdout=(str(objects / 'crypto/other')+'\n').encode())):
                SysrootBuilder.prepare_contexts(fake)
            self.assertEqual([call[call.index('-C') + 1] for call in calls[:2]],
                             [str(os_root / 'crypto/other')] * 2)
            self.assertIn('MKCLEANSRC=no', calls[0])
            self.assertTrue(all(str(os_root / 'tools/heimdal') not in call for call in calls))
            self.assertEqual(calls[-1], ['/selected/sh', str(os_root / 'build.sh'), '-U', '-j2',
                                          '-V', 'EMBER_HOST_ABI_CFLAGS=', '-V', 'MKCLEANSRC=no', 'tools'])
            self.assertFalse((objects / 'native.lo').exists())
            self.assertFalse((objects / 'native.lo.o').exists())
            self.assertTrue((objects / 'target.o').exists())
            self.assertTrue((objects / 'libtool.lo').exists())
            self.assertEqual(fake.build_command[-2:], ['tools', 'libs'])
            self.assertEqual(fake.receipt['plan'][1], fake.build_command)
            self.assertEqual(jsonio.read_json(objects / '.prebuild-sdk-context.json')['signature'], current)
            self.assertEqual(jsonio.read_json(tools / '.prebuild-sdk-context.json')['signature'], current)

    def test_prepared_runtime_refresh_composes_with_native_abi_transition(self):
        from copy import deepcopy
        from types import SimpleNamespace
        from unittest.mock import patch
        from emberbsd_sdk.sysroot_builder import SysrootBuilder
        old_flags = ['HOST_CPPFLAGS=', 'HOST_CFLAGS=', 'HOST_CXXFLAGS=', 'HOST_LDFLAGS=',
                     'EMBER_HOST_COMPILER_FAMILY=clang', 'EMBER_OPENSSL_SOURCE=/old/openssl']
        new_flags = ['EMBER_HOST_ABI_CPPFLAGS=', 'EMBER_HOST_ABI_CFLAGS=',
                     'EMBER_HOST_ABI_CXXFLAGS=', 'EMBER_HOST_ABI_LDFLAGS=',
                     'EMBER_HOST_COMPILER_FAMILY=clang', 'EMBER_OPENSSL_SOURCE=/new/openssl']
        selected = {'format': 'emberbsd-host-prefix', 'version': 2,
                    'compiler': {'family': 'clang'},
                    'abi_flags': {'cppflags': [], 'cflags': [], 'cxxflags': [], 'ldflags': []},
                    'tools': {}}
        def source(tree):
            return {'format': 'emberbsd-prepared-source', 'version': 1,
                    'package': 'security/openssl', 'tree_sha256': tree * 64}
        old_signature = {'flags': old_flags, 'native_toolset': selected,
                         'common_sources': {'openssl': source('a')},
                         'runtime': {'old': True}, 'runtime_prefix': '/old/runtime',
                         'llvm_source': {'old': True}}
        new_signature = {'flags': new_flags, 'native_toolset': selected,
                         'common_sources': {'openssl': source('b')},
                         'runtime': {'new': True}, 'runtime_prefix': '/new/runtime',
                         'llvm_source': {'new': True}}
        fake = SimpleNamespace(COMMON_SOURCES=SysrootBuilder.COMMON_SOURCES,
            paths={'os': '/os', 'objects': '/objects', 'tools': '/tools',
                   'openssl-source': '/new/openssl'},
            native_selection=selected,
            context_signature=lambda path: new_signature,
            reuse_signature=lambda signature: signature,
            compatible_context=lambda record, path: True,
            native_abi_refresh=False)
        fake.reuse_signature = lambda signature: SysrootBuilder.reuse_signature(fake, signature)
        with patch('emberbsd_sdk.sysroot_builder.runtime_refresh.cpp_update',
                   return_value={'headers': ['usr/include/c++/v1'],
                                 'linked': ['libc++.so.1']}), \
             patch('emberbsd_sdk.sysroot_builder.prepared_refresh.plan',
                   return_value=['lib/libcxx']) as plan:
            refreshed = SysrootBuilder.plan_prepared_refresh(
                fake, {'signature': old_signature}, '/objects')
        self.assertTrue(refreshed)
        self.assertTrue(fake.native_abi_refresh)
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
                fake.context_signature = lambda path, signature=invalid: signature
                fake.native_abi_refresh = False
                self.assertFalse(SysrootBuilder.plan_prepared_refresh(
                    fake, {'signature': old_signature}, '/objects'))
                self.assertFalse(fake.native_abi_refresh)

    def test_native_abi_cleanup_removes_only_binary_native_lo_objects(self):
        from emberbsd_sdk.sysroot_builder import _remove_native_host_objects
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            host = root / 'lib/host'
            host.mkdir(parents=True)
            elf = host / 'one.lo'
            elf.write_bytes(b'\x7fELF\x02\x01\x01\x00')
            macho = host / 'two.lo.o'
            macho.write_bytes(b'\xcf\xfa\xed\xfe' + b'\0' * 8)
            target = host / 'target.o'
            target.write_bytes(b'\x7fELF\x02\x01\x01\x00')
            text_lo = host / 'libtool.lo'
            text_lo.write_text('# Generated by libtool\n')
            rebuilt = root / 'tools/rebuilt.lo'
            rebuilt.parent.mkdir()
            rebuilt.write_bytes(b'\x7fELF\x02\x01\x01\x00')
            self.assertEqual(_remove_native_host_objects(root), 2)
            self.assertFalse(elf.exists())
            self.assertFalse(macho.exists())
            self.assertTrue(target.exists())
            self.assertTrue(text_lo.exists())
            self.assertTrue(rebuilt.exists())

    def test_native_abi_cleanup_refuses_symlinked_candidate_and_symlink_root(self):
        from emberbsd_sdk.sysroot_builder import _remove_native_host_objects
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / 'host.lo').symlink_to('/tmp/outside.lo')
            with self.assertRaisesRegex(SDKError, 'symlink'):
                _remove_native_host_objects(root)
            alias = root.parent / (root.name + '-alias')
            alias.symlink_to(root)
            self.addCleanup(alias.unlink)
            with self.assertRaisesRegex(SDKError, 'real directory'):
                _remove_native_host_objects(alias)

    def test_native_abi_cleanup_skips_unrelated_symlinked_object_subtree(self):
        from emberbsd_sdk.sysroot_builder import _remove_native_host_objects
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            target = root.parent / (root.name + '-target')
            target.mkdir()
            self.addCleanup(lambda: target.rmdir())
            (target / 'external.lo').write_bytes(b'\x7fELF\x02\x01\x01\x00')
            self.addCleanup(lambda: (target / 'external.lo').unlink())
            (root / 'alias').symlink_to(target, target_is_directory=True)
            self.assertEqual(_remove_native_host_objects(root), 0)
            self.assertTrue((target / 'external.lo').exists())

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
