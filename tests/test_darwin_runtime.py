# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Developed with AI assistance. Fixtures do not qualify a real native toolset.

import copy
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from emberbsd_sdk.darwin_runtime import (capture_darwin_runtime_audit,
    darwin_runtime_closure, validate_darwin_runtime_closure, _runtime_cache_uuid)
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.inventory import snapshot_tree


A = '/usr/lib/A.dylib'
ALIAS = '/usr/lib/Alias.dylib'
B = '/usr/lib/B.dylib'
BASE = 0x180000000


def command(kind, name, *, current=0x10000, compatibility=0x10000, flags=None):
    text = name.encode() + b'\0'
    if flags is not None:
        size = (28 + len(text) + 7) // 8 * 8
        return struct.pack('<7I', kind, size, 28, 0x1a741800, current,
            compatibility, flags) + text.ljust(size - 28, b'\0')
    size = (24 + len(text) + 7) // 8 * 8
    return struct.pack('<6I', kind, size, 24, 0, current, compatibility) + text.ljust(size - 24, b'\0')


def macho(address, loads=(), *, install_name=None, cached=False, uuid=1, lazy=None, rpaths=()):
    commands = [struct.pack('<II16s', 0x1b, 24, uuid.to_bytes(16, 'little'))]
    commands.append(struct.pack('<II16s4Q4I', 0x19, 72, b'__TEXT',
        address, 0x400, 0, 0x400, 5, 5, 0, 0))
    commands.append(struct.pack('<6I', 0x32, 24, 1, 0x10000, 0x10000, 0))
    if install_name:
        commands.append(command(0xd, install_name))
    commands.extend(load if isinstance(load, bytes) else command(*load) for load in loads)
    for rpath in rpaths:
        text = rpath.encode() + b'\0'
        size = (12 + len(text) + 7) // 8 * 8
        commands.append(struct.pack('<3I', 0x8000001c, size, 12) + text.ljust(size - 12, b'\0'))
    blob = b''
    if lazy:
        if isinstance(lazy, bytes):
            blob = lazy
        else:
            name, flags = lazy
            blob = struct.pack('<IIHHIII', 24, 0, flags, 1, 0, 0, 24) + name.encode() + b'\0'
        blob_size = (len(blob) + 255) // 256 * 256
        commands.append(struct.pack('<II16s4Q4I', 0x19, 72, b'__LINKEDIT',
            address + 0x400, blob_size, 0x400, blob_size, 1, 1, 0, 0))
        commands.append(struct.pack('<4I', 0x3a, 16, 0x400, len(blob)))
    data = b''.join(commands)
    header = struct.pack('<8I', 0xfeedfacf, 0x100000c, 0, 6 if install_name else 2,
        len(commands), len(data), 0x80000000 if cached else 0, 0)
    return (header + data).ljust(0x400, b'\0') + (blob.ljust(blob_size, b'\0') if lazy else b'')


def repeated_lazy_symbols(count, symbol_size):
    name = B.encode() + b'\0'
    path_offset = 24 + count * 4
    symbol_offset = path_offset + len(name)
    return (struct.pack('<IIHHIII', path_offset, 0, 0, 1, 0, count, 24) +
            struct.pack('<I', symbol_offset) * count + name + b's' * symbol_size + b'\0')


def fat_macho(data):
    return struct.pack('>7I', 0xcafebabe, 1, 0x100000c, 0, 4096, len(data), 12).ljust(
        4096, b'\0') + data


def trie(entries):
    rows = sorted(entries.items())
    start = 2 + sum(len(name.encode()) + 2 for name, _ in rows)
    assert start + 3 * len(rows) < 128
    data = bytearray([0, len(rows)])
    for i, (name, index) in enumerate(rows):
        data += name.encode() + b'\0' + bytes([start + i * 3])
    for name, index in rows:
        data += bytes([1, index, 0])
    return bytes(data)


class Fixture:
    def __init__(self, test):
        backend = patch('emberbsd_sdk.darwin_runtime._runtime_cache_uuid',
                        return_value=(1).to_bytes(16, 'little').hex())
        backend.start()
        test.addCleanup(backend.stop)
        temporary = tempfile.TemporaryDirectory()
        test.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.bundle = self.root / 'bundle'
        self.cache = self.root / 'cache'
        self.bundle.mkdir()
        self.cache.mkdir()
        self.main = self.cache / 'dyld_shared_cache_arm64e'
        self.sub = self.cache / (self.main.name + '.01')
        self.tool = self.bundle / 'cc'
        self.tool.write_bytes(macho(0x100000000, [(0xc, ALIAS)]))
        self.inventory_path = self.root / 'cache-inventory.json'
        self.audit_path = self.root / 'runtime-audit.json'
        data = bytearray(8192)
        self.header(data, BASE + 4096, 8192, 2)
        data[0x400:0x800] = macho(BASE + 4096 + 0x400, [(0xc, B)],
            install_name=A, cached=True, uuid=3)
        data[0x1400:0x1800] = macho(BASE + 4096 + 0x1400, [(0x8000001f, A)],
            install_name=B, cached=True, uuid=4)
        self.sub.write_bytes(data)
        data = bytearray(4096)
        self.header(data, BASE, 4096, 1)
        struct.pack_into('<QQ', data, 0xe0, BASE, 12288)
        struct.pack_into('<II', data, 0x188, 1200, 1)
        struct.pack_into('<16sQ32s', data, 1200, (2).to_bytes(16, 'little'), 4096, b'.01')
        struct.pack_into('<II', data, 0x1c0, 680, 2)
        struct.pack_into('<3Q2I', data, 680, BASE + 4096 + 0x400, 0, 0, 1024, 0)
        struct.pack_into('<3Q2I', data, 712, BASE + 4096 + 0x1400, 0, 0, 1088, 0)
        data[1024:1024 + len(A) + 1] = A.encode() + b'\0'
        data[1088:1088 + len(B) + 1] = B.encode() + b'\0'
        tree = trie({A: 0, ALIAS: 0, B: 1})
        struct.pack_into('<QQ', data, 0x108, BASE + 1500, len(tree))
        data[1500:1500 + len(tree)] = tree
        self.main.write_bytes(data)
        self.descriptor = dict(host=dict(os='Darwin', cpu='arm64'),
            bundle_root=str(self.bundle), tools=dict(cc=dict(path=str(self.tool), kind='native')),
            host_abi=dict(libraries=[dict(root=str(self.cache), inventory=None)]))
        self.refresh_inventory()

    def header(self, data, address, size, uuid):
        data[:16] = b'dyld_v1  arm64e\0'
        struct.pack_into('<II', data, 0x10, 568, 1)
        data[0x58:0x68] = uuid.to_bytes(16, 'little')
        struct.pack_into('<I', data, 0xd8, 1)
        struct.pack_into('<I', data, 0x16c, 0x1b0000)
        struct.pack_into('<3Q2I', data, 568, address, size, 0, 5, 5)

    def reference(self, path, value):
        data = (json.dumps(value, sort_keys=True) + '\n').encode()
        path.write_bytes(data)
        return dict(path=str(path), sha256=hashlib.sha256(data).hexdigest())

    def refresh_inventory(self):
        self.descriptor['host_abi']['libraries'][0]['inventory'] = self.reference(
            self.inventory_path, snapshot_tree(str(self.cache)))

    def capture(self):
        return capture_darwin_runtime_audit(self.descriptor, cache_main=str(self.main))

    def accepted(self):
        audit = self.capture()
        ref = self.reference(self.audit_path, audit)
        return audit, darwin_runtime_closure(audit, ref)


class DarwinRuntimeTests(unittest.TestCase):
    def test_alias_subcache_cycle_and_versions(self):
        f = Fixture(self)
        audit, closure = f.accepted()
        self.assertEqual(set(closure), {str(f.tool), A, ALIAS, B})
        self.assertEqual(closure[ALIAS]['dependencies'], [B])
        self.assertEqual(audit['nodes'][ALIAS]['image']['install_name'], A)
        span = audit['nodes'][ALIAS]['image']['segments'][0]['spans'][0]
        self.assertEqual(span, dict(file=f.sub.name, offset=0x400, size=0x400))
        validate_darwin_runtime_closure(closure, f.descriptor)

    def output_fixture(self):
        f = Fixture(self)
        f.output_root = f.root / 'outputs'
        f.output_root.mkdir()
        f.output = f.output_root / 'app'
        f.output.write_bytes(macho(0x100000000, [(0xc, ALIAS)], uuid=5))
        f.output_ref = dict(path=str(f.output), sha256=hashlib.sha256(f.output.read_bytes()).hexdigest())
        f.trace = f.root / 'dyld.log'
        def line(path, uuid):
            text = uuid.to_bytes(16, 'little').hex()
            text = '-'.join([text[:8], text[8:12], text[12:16], text[16:20], text[20:]])
            return f'dyld[42]: <{text.upper()}> {path}\n'
        f.trace_line = line
        f.trace.write_text(line(f.output, 5) + line(A, 3) + line(B, 4) + 'application-output\n')
        f.observation_path = f.root / 'observations.json'
        f.observation = dict(format='emberbsd-darwin-observed-cache-roots', version=1,
            scope='producer-output', cache_uuid=(1).to_bytes(16, 'little').hex(), observations=[
                dict(output=f.output_ref, trace=dict(path=str(f.trace),
                    sha256=hashlib.sha256(f.trace.read_bytes()).hexdigest()), roots=[
                        dict(path=B, uuid=(4).to_bytes(16, 'little').hex())])])
        f.options = dict(outputs=[f.output_ref], output_read_root=str(f.output_root),
            observed_cache_roots=f.reference(f.observation_path, f.observation))
        return f

    def output_capture(self, f):
        return capture_darwin_runtime_audit(f.descriptor, cache_main=str(f.main), **f.options)

    def refresh_observation(self, f):
        f.observation['observations'][0]['trace']['sha256'] = hashlib.sha256(f.trace.read_bytes()).hexdigest()
        f.options['observed_cache_roots'] = f.reference(f.observation_path, f.observation)

    def test_explicit_output_audit_is_separate_and_cannot_qualify_tools(self):
        f = self.output_fixture()
        before = f.capture()
        audit = self.output_capture(f)
        self.assertEqual(audit['format'], 'emberbsd-darwin-output-runtime-audit')
        self.assertEqual(audit['scope'], 'producer-output')
        self.assertEqual(audit['tools'], [str(f.tool)])
        self.assertEqual(audit['output_runtime']['outputs'], [f.output_ref])
        self.assertEqual(audit['nodes'][str(f.output)]['kind'], 'output-executable')
        ref = f.reference(f.audit_path, audit)
        closure = darwin_runtime_closure(audit, ref)
        self.assertEqual(closure[str(f.output)]['output_executable'], ref)
        self.assertNotIn('sha256', closure[str(f.output)])
        validate_darwin_runtime_closure(closure, f.descriptor, **f.options)
        with self.assertRaisesRegex(SDKError, 'differs from current bytes'):
            validate_darwin_runtime_closure(closure, f.descriptor)
        from emberbsd_sdk.bootstrap_seed import _dynamic_closure
        with self.assertRaisesRegex(SDKError, 'differs from current bytes'):
            _dynamic_closure(closure, f.descriptor)
        self.assertEqual(f.capture(), before)
        self.assertNotIn('scope', before)
        self.assertNotIn('output_runtime', before)

    def test_observed_cache_root_is_not_a_fabricated_dependency_edge(self):
        f = self.output_fixture()
        data = bytearray(f.sub.read_bytes())
        data[0x400:0x800] = macho(BASE + 4096 + 0x400, [(0xc, A)],
            install_name=A, cached=True, uuid=3)
        f.sub.write_bytes(data)
        f.refresh_inventory()
        self.assertNotIn(B, f.capture()['nodes'])
        audit = self.output_capture(f)
        self.assertIn(B, audit['nodes'])
        self.assertEqual([row['target'] for row in audit['nodes'][ALIAS]['image']['loads']], [A])
        self.assertEqual([row['target'] for row in audit['nodes'][str(f.output)]['image']['loads']], [ALIAS])
        f.observation['observations'][0]['roots'] = []
        self.refresh_observation(f)
        with self.assertRaisesRegex(SDKError, 'absent or differs from the audited graph'):
            self.output_capture(f)

    def test_each_output_has_its_own_pinned_trace_and_identity(self):
        f = self.output_fixture()
        second = f.output_root / 'second'
        second.write_bytes(macho(0x100000000, [(0xc, B)], uuid=6))
        second_ref = dict(path=str(second), sha256=hashlib.sha256(second.read_bytes()).hexdigest())
        trace = f.root / 'second.log'
        trace.write_text(f.trace_line(second, 6) + f.trace_line(B, 4))
        f.options['outputs'].append(second_ref)
        f.observation['observations'].append(dict(output=second_ref,
            trace=dict(path=str(trace), sha256=hashlib.sha256(trace.read_bytes()).hexdigest()), roots=[]))
        self.refresh_observation(f)
        audit = self.output_capture(f)
        self.assertEqual(audit['nodes'][str(second)]['kind'], 'output-executable')
        f.observation['observations'][1]['trace'] = f.observation['observations'][0]['trace']
        self.refresh_observation(f)
        with self.assertRaisesRegex(SDKError, 'distinct pinned trace'):
            self.output_capture(f)

    def test_output_refs_root_and_observations_must_be_explicit_together(self):
        f = self.output_fixture()
        for key in f.options:
            with self.subTest(missing=key):
                options = dict(f.options)
                del options[key]
                with self.assertRaisesRegex(SDKError, 'together'):
                    capture_darwin_runtime_audit(f.descriptor, cache_main=str(f.main), **options)
        f.options['output_read_root'] = str(f.root)
        with self.assertRaisesRegex(SDKError, 'overlapping'):
            self.output_capture(f)

    def test_output_changed_hash_alias_and_read_root_escape_are_refused(self):
        for case in ('changed', 'alias', 'outside', 'duplicate'):
            with self.subTest(case=case):
                f = self.output_fixture()
                if case == 'changed':
                    f.output.write_bytes(f.output.read_bytes() + b'changed')
                elif case == 'alias':
                    alias = f.output_root / 'alias'
                    alias.symlink_to(f.output.name)
                    f.output_ref['path'] = str(alias)
                elif case == 'outside':
                    f.output_ref['path'] = str(f.tool)
                else:
                    f.options['outputs'].append(dict(f.output_ref))
                with self.assertRaises(SDKError):
                    self.output_capture(f)

    def test_output_rpath_can_check_absent_local_candidate_but_only_load_bundle_dso(self):
        f = self.output_fixture()
        library = f.bundle / 'library.dylib'
        library.write_bytes(macho(0x100000000, [(0xc, ALIAS)], install_name='@rpath/library.dylib', uuid=6))
        f.output.write_bytes(macho(0x100000000, [(0xc, '@rpath/library.dylib')], uuid=5,
            rpaths=('@loader_path', str(f.bundle))))
        f.output_ref['sha256'] = hashlib.sha256(f.output.read_bytes()).hexdigest()
        self.refresh_observation(f)
        audit = self.output_capture(f)
        edge = audit['nodes'][str(f.output)]['image']['loads'][0]
        self.assertEqual(edge['target'], str(library))
        self.assertIsNone(edge['rpath_resolution']['candidates'][0]['target'])
        (f.output_root / library.name).write_bytes(library.read_bytes())
        with self.assertRaisesRegex(SDKError, 'target must be a real in-bundle path'):
            self.output_capture(f)

    def test_output_root_is_not_a_general_dso_provider(self):
        f = self.output_fixture()
        library = f.output_root / 'library.dylib'
        library.write_bytes(macho(0x100000000, [(0xc, ALIAS)], install_name=str(library)))
        f.output.write_bytes(macho(0x100000000, [(0xc, str(library))], uuid=5))
        f.output_ref['sha256'] = hashlib.sha256(f.output.read_bytes()).hexdigest()
        self.refresh_observation(f)
        with self.assertRaisesRegex(SDKError, 'outside declared native inputs'):
            self.output_capture(f)

    def test_output_must_be_a_native_executable(self):
        for kind in (1, 6):
            with self.subTest(filetype=kind):
                f = self.output_fixture()
                data = bytearray(f.output.read_bytes())
                struct.pack_into('<I', data, 12, kind)
                f.output.write_bytes(data)
                f.output_ref['sha256'] = hashlib.sha256(data).hexdigest()
                self.refresh_observation(f)
                with self.assertRaisesRegex(SDKError, 'native Darwin Mach-O image'):
                    self.output_capture(f)

    def test_trace_sha_cache_uuid_root_and_output_binding_are_checked(self):
        for case in ('trace-hash', 'cache-uuid', 'root-uuid', 'root-absent', 'output-ref', 'output-uuid'):
            with self.subTest(case=case):
                f = self.output_fixture()
                row = f.observation['observations'][0]
                if case == 'trace-hash':
                    row['trace']['sha256'] = '0' * 64
                elif case == 'cache-uuid':
                    f.observation['cache_uuid'] = '0' * 32
                elif case == 'root-uuid':
                    row['roots'][0]['uuid'] = '0' * 32
                elif case == 'root-absent':
                    row['roots'][0]['path'] = '/usr/lib/not-in-cache.dylib'
                elif case == 'output-ref':
                    row['output'] = dict(f.output_ref, sha256='0' * 64)
                else:
                    f.trace.write_text(f.trace_line(f.output, 6) + f.trace_line(B, 4))
                    row['trace']['sha256'] = hashlib.sha256(f.trace.read_bytes()).hexdigest()
                f.options['observed_cache_roots'] = f.reference(f.observation_path, f.observation)
                with self.assertRaises(SDKError):
                    self.output_capture(f)

    def test_every_dyld_trace_line_must_be_supported_and_graph_bound(self):
        for suffix in ('dyld[42]: unsupported metadata\n',
                       'dyld: unsupported metadata\n',
                       ' dyld[42]: <03000000-0000-0000-0000-000000000000> ' + A + '\n',
                       'dyld[43]: <03000000-0000-0000-0000-000000000000> ' + A + '\n',
                       'dyld[42]: <03000000-0000-0000-0000-000000000000> /unselected/image\n',
                       'dyld[42]: <00000000-0000-0000-0000-000000000000> ' + A + '\n'):
            with self.subTest(suffix=suffix):
                f = self.output_fixture()
                f.trace.write_text(f.trace.read_text() + suffix)
                self.refresh_observation(f)
                with self.assertRaises(SDKError):
                    self.output_capture(f)

    def test_output_trace_and_root_resource_limits_are_explicit(self):
        f = self.output_fixture()
        f.options['outputs'] *= 65
        with self.assertRaisesRegex(SDKError, 'one to 64'):
            self.output_capture(f)
        f = self.output_fixture()
        f.observation['observations'][0]['roots'] *= 65
        self.refresh_observation(f)
        with self.assertRaisesRegex(SDKError, 'roots exceed'):
            self.output_capture(f)
        f = self.output_fixture()
        f.trace.write_text(f.trace.read_text() + '\n' * 16384)
        self.refresh_observation(f)
        with self.assertRaisesRegex(SDKError, 'line limit'):
            self.output_capture(f)
        f = self.output_fixture()
        with patch('emberbsd_sdk.darwin_runtime._MAX_METADATA', 8192):
            f.trace.write_text('x' * 8193)
            self.refresh_observation(f)
            with self.assertRaisesRegex(SDKError, 'byte limit'):
                self.output_capture(f)

    def test_shared_roots_are_listed_once_but_both_traces_keep_uuid_checks(self):
        from emberbsd_sdk.darwin_runtime import _output_inputs, _output_traces, _verify_output_traces
        f = self.output_fixture()
        names = [f'/usr/lib/libfixture{index}.dylib' for index in range(35)]
        uuids = {name: index + 10 for index, name in enumerate(names)}
        cache = SimpleNamespace(uuid='a' * 32,
                                lookup=lambda name: names.index(name) if name in names else None)
        roots = [dict(path=name, uuid=uuids[name].to_bytes(16, 'little').hex()) for name in names]
        nodes = {name: dict(kind='cache-image', index=index, image=dict(uuid=roots[index]['uuid']))
                 for index, name in enumerate(names)}
        outputs, rows = [], []
        for index in range(2):
            image = f.output_root / f'image{index}'
            image.write_bytes(b'synthetic data; never executed')
            output = dict(path=str(image), sha256=hashlib.sha256(image.read_bytes()).hexdigest())
            outputs.append(output)
            uuid = 100 + index
            nodes[str(image)] = dict(kind='output-executable', image=dict(uuid=uuid.to_bytes(16, 'little').hex()))
            trace = f.root / f'trace{index}.log'
            trace.write_text(f.trace_line(image, uuid) + ''.join(f.trace_line(name, uuids[name]) for name in names))
            rows.append(dict(output=output, trace=dict(path=str(trace),
                sha256=hashlib.sha256(trace.read_bytes()).hexdigest()), roots=copy.deepcopy(roots) if index == 0 else []))

        def context(observations):
            reference = f.reference(f.observation_path, dict(format='emberbsd-darwin-observed-cache-roots',
                version=1, scope='producer-output', cache_uuid=cache.uuid, observations=observations))
            return _output_inputs([str(f.bundle)], outputs, str(f.output_root), reference)

        selected = context(rows)
        with ExitStack() as stack:
            traces, found = _output_traces(selected, cache, stack)
            self.assertEqual(len(traces), 2)
            self.assertEqual(found, set(names))
            _verify_output_traces(selected, traces, nodes, cache)

        repeated = copy.deepcopy(rows)
        repeated[1]['roots'] = copy.deepcopy(roots)
        with self.assertRaisesRegex(SDKError, 'roots exceed'):
            context(repeated)

        # Entry-point deduplication must not discard the second full trace.
        trace = Path(rows[1]['trace']['path'])
        trace.write_text(trace.read_text().replace(f.trace_line(names[0], uuids[names[0]]),
                                                 f.trace_line(names[0], 999)))
        rows[1]['trace']['sha256'] = hashlib.sha256(trace.read_bytes()).hexdigest()
        selected = context(rows)
        with ExitStack() as stack:
            traces, _ = _output_traces(selected, cache, stack)
            with self.assertRaisesRegex(SDKError, 'absent or differs from the audited graph'):
                _verify_output_traces(selected, traces, nodes, cache)

    def test_output_rpath_absence_trace_and_observation_races_are_refused(self):
        for case in ('rpath', 'trace', 'observation'):
            with self.subTest(case=case):
                f = self.output_fixture()
                library = f.bundle / 'library.dylib'
                library.write_bytes(macho(0x100000000, [(0xc, ALIAS)], install_name='@rpath/library.dylib'))
                f.output.write_bytes(macho(0x100000000, [(0xc, '@rpath/library.dylib')], uuid=5,
                    rpaths=('@loader_path', str(f.bundle))))
                f.output_ref['sha256'] = hashlib.sha256(f.output.read_bytes()).hexdigest()
                self.refresh_observation(f)
                original = snapshot_tree
                def changed(root):
                    rows = original(root)
                    if case == 'rpath':
                        (f.output_root / library.name).write_bytes(library.read_bytes())
                    elif case == 'trace':
                        f.trace.write_text(f.trace.read_text() + 'changed\n')
                    else:
                        f.observation_path.write_text('{}')
                    return rows
                with patch('emberbsd_sdk.darwin_runtime.snapshot_tree', changed):
                    with self.assertRaises(SDKError):
                        self.output_capture(f)

    def test_live_cache_uuid_must_match(self):
        f = Fixture(self)
        audit, closure = f.accepted()
        with patch('emberbsd_sdk.darwin_runtime._runtime_cache_uuid', return_value='f' * 32):
            with self.assertRaisesRegex(SDKError, 'not the current process runtime cache'):
                validate_darwin_runtime_closure(closure, f.descriptor)

    def test_dyld_service_has_a_typed_signature_and_no_library_search(self):
        def fill(value):
            value[:] = bytes(range(16))
            return True
        query = Mock(side_effect=fill)
        with patch('emberbsd_sdk.darwin_runtime.sys.platform', 'darwin'), \
             patch('ctypes.CDLL', return_value=SimpleNamespace(_dyld_get_shared_cache_uuid=query)) as library:
            self.assertEqual(_runtime_cache_uuid(), bytes(range(16)).hex())
            library.assert_called_once_with(None)
            self.assertEqual(len(query.argtypes), 1)
            query.side_effect = lambda value: False
            with self.assertRaisesRegex(SDKError, 'no dyld shared cache'):
                _runtime_cache_uuid()

    def test_dyld_service_import_failure_is_typed(self):
        with patch('emberbsd_sdk.darwin_runtime.sys.platform', 'darwin'), \
             patch.dict(sys.modules, {'ctypes': None}):
            with self.assertRaisesRegex(SDKError, 'runtime cache service is unavailable'):
                _runtime_cache_uuid()

    def test_ordinary_file_dylib_is_kept(self):
        f = Fixture(self)
        library = f.bundle / 'library.dylib'
        library.write_bytes(macho(0x100000000, [(0xc, ALIAS)], install_name=str(library)))
        f.tool.write_bytes(macho(0x100000000, [(0xc, str(library))]))
        audit, closure = f.accepted()
        self.assertEqual(closure[str(library)]['sha256'], hashlib.sha256(library.read_bytes()).hexdigest())
        validate_darwin_runtime_closure(closure, f.descriptor)

    def test_one_arm64_fat_dylib_keeps_the_whole_file_hash_and_slice(self):
        f = Fixture(self)
        library = f.bundle / 'library.dylib'
        library.write_bytes(fat_macho(macho(0x100000000, [(0xc, ALIAS)],
                                            install_name=str(library))))
        f.tool.write_bytes(macho(0x100000000, [(0xc, str(library))]))
        audit, closure = f.accepted()
        node = audit['nodes'][str(library)]
        self.assertEqual(node['macho_slice']['offset'], 4096)
        self.assertEqual(node['macho_slice']['size'], 1024)
        self.assertEqual(node['sha256'], hashlib.sha256(library.read_bytes()).hexdigest())
        self.assertEqual(closure[str(library)]['dependencies'], [ALIAS])
        validate_darwin_runtime_closure(closure, f.descriptor)
        changed = bytearray(library.read_bytes())
        changed[100] = 1
        library.write_bytes(changed)
        with self.assertRaisesRegex(SDKError, 'differs from current bytes'):
            validate_darwin_runtime_closure(closure, f.descriptor)

    def test_fat_dylib_load_commands_cannot_leave_the_selected_slice(self):
        f = Fixture(self)
        library = f.bundle / 'library.dylib'
        data = bytearray(fat_macho(macho(0x100000000, [(0xc, ALIAS)],
                                            install_name=str(library))))
        struct.pack_into('>I', data, 20, 32)
        library.write_bytes(data)
        f.tool.write_bytes(macho(0x100000000, [(0xc, str(library))]))
        with self.assertRaisesRegex(SDKError, 'outside its selected slice'):
            f.capture()

    def test_own_rpaths_reach_pinned_files_and_anchor_to_each_owner(self):
        f = Fixture(self)
        libraries = f.bundle / 'lib'
        libraries.mkdir()
        std = libraries / 'libstdc++.6.dylib'
        gcc = libraries / 'libgcc_s.1.1.dylib'
        gcc.write_bytes(fat_macho(macho(0x100000000, [(0xc, ALIAS)],
                                           install_name='@rpath/libgcc_s.1.1.dylib')))
        std.write_bytes(macho(0x100000000, [(0xc, '@rpath/libgcc_s.1.1.dylib')],
            install_name='@rpath/libstdc++.6.dylib', rpaths=('@loader_path',)))
        f.tool.write_bytes(macho(0x100000000, [(0xc, '@rpath/libstdc++.6.dylib')],
                                 rpaths=('@loader_path/absent', str(libraries))))
        audit, closure = f.accepted()
        self.assertEqual(closure[str(f.tool)]['dependencies'], [str(std)])
        self.assertEqual(closure[str(std)]['dependencies'], [str(gcc)])
        self.assertEqual(audit['nodes'][str(std)]['image']['loads'][0]['name'],
                         '@rpath/libgcc_s.1.1.dylib')
        resolution = audit['nodes'][str(std)]['image']['loads'][0]['rpath_resolution']
        self.assertEqual(resolution['owner'], str(std))
        self.assertEqual(resolution['candidates'], [dict(rpath='@loader_path',
            path=str(gcc), target=str(gcc))])
        validate_darwin_runtime_closure(closure, f.descriptor)

    def test_executable_own_rpath_normalizes_parent_and_trailing_separator(self):
        for rpath in ('@executable_path', '@executable_path/../lib/'):
            with self.subTest(rpath=rpath):
                f = Fixture(self)
                binary = f.bundle / 'bin'
                binary.mkdir()
                f.tool.rename(binary / 'cc')
                f.tool = binary / 'cc'
                f.descriptor['tools']['cc']['path'] = str(f.tool)
                directory = binary if rpath == '@executable_path' else f.bundle / 'lib'
                directory.mkdir(exist_ok=True)
                library = directory / 'library.dylib'
                library.write_bytes(macho(0x100000000, [(0xc, ALIAS)],
                                          install_name='@rpath/library.dylib'))
                f.tool.write_bytes(macho(0x100000000, [(0xc, '@rpath/library.dylib')],
                                         rpaths=(rpath,)))
                audit, closure = f.accepted()
                edge = audit['nodes'][str(f.tool)]['image']['loads'][0]
                self.assertEqual(edge['target'], str(library))
                self.assertEqual(edge['rpath_resolution'], dict(owner=str(f.tool),
                    candidates=[dict(rpath=rpath, path=str(library), target=str(library))]))
                validate_darwin_runtime_closure(closure, f.descriptor)

    def test_executable_rpath_cannot_escape_or_hide_symlink_traversal(self):
        for suffix in ('/../../outside', '/../alias', '/alias/../lib', '/plain/../lib',
                       '/missing/../lib'):
            with self.subTest(suffix=suffix):
                f = Fixture(self)
                binary = f.bundle / 'bin'
                binary.mkdir()
                f.tool.rename(binary / 'cc')
                f.tool = binary / 'cc'
                f.descriptor['tools']['cc']['path'] = str(f.tool)
                libraries = f.bundle / 'lib'
                libraries.mkdir()
                (f.bundle / 'alias').symlink_to('lib')
                (binary / 'alias').symlink_to('../lib')
                (binary / 'plain').write_bytes(b'not a directory')
                library = libraries / 'library.dylib'
                library.write_bytes(macho(0x100000000, [(0xc, ALIAS)],
                                          install_name='@rpath/library.dylib'))
                f.tool.write_bytes(macho(0x100000000, [(0xc, '@rpath/library.dylib')],
                    rpaths=('@executable_path' + suffix, str(libraries))))
                with self.assertRaises(SDKError):
                    f.capture()

    def test_executable_rpath_distinct_existing_targets_remain_ambiguous(self):
        f = Fixture(self)
        second = f.bundle / 'second'
        second.mkdir()
        data = macho(0x100000000, [(0xc, ALIAS)], install_name='@rpath/library.dylib')
        for directory in (f.bundle, second):
            (directory / 'library.dylib').write_bytes(data)
        f.tool.write_bytes(macho(0x100000000, [(0xc, '@rpath/library.dylib')],
            rpaths=('@executable_path', '@executable_path/second')))
        with self.assertRaisesRegex(SDKError, 'ambiguous image-local'):
            f.capture()

    def test_dylib_own_executable_rpath_has_no_executable_context(self):
        f = Fixture(self)
        library = f.bundle / 'library.dylib'
        library.write_bytes(macho(0x100000000, [(0xc, ALIAS)],
                                  install_name='@rpath/library.dylib'))
        dependent = f.bundle / 'dependent.dylib'
        dependent.write_bytes(macho(0x100000000, [(0xc, '@rpath/library.dylib')],
            install_name=str(dependent), rpaths=('@executable_path',)))
        f.tool.write_bytes(macho(0x100000000, [(0xc, str(dependent))]))
        with self.assertRaises(SDKError):
            f.capture()

    def test_executable_rpath_target_cannot_be_a_symlink(self):
        f = Fixture(self)
        actual = f.bundle / 'actual.dylib'
        actual.write_bytes(macho(0x100000000, [(0xc, ALIAS)],
                                 install_name='@rpath/library.dylib'))
        (f.bundle / 'library.dylib').symlink_to(actual.name)
        f.tool.write_bytes(macho(0x100000000, [(0xc, '@rpath/library.dylib')],
                                 rpaths=('@executable_path',)))
        with self.assertRaisesRegex(SDKError, 'target must be a real in-bundle path'):
            f.capture()

    def test_output_executable_rpath_cannot_supply_an_external_owner_context(self):
        f = self.output_fixture()
        library = f.bundle / 'library.dylib'
        library.write_bytes(macho(0x100000000, [(0xc, ALIAS)],
                                  install_name='@rpath/library.dylib'))
        f.output.write_bytes(macho(0x100000000, [(0xc, '@rpath/library.dylib')], uuid=5,
            rpaths=('@executable_path/../bundle',)))
        f.output_ref['sha256'] = hashlib.sha256(f.output.read_bytes()).hexdigest()
        self.refresh_observation(f)
        with self.assertRaisesRegex(SDKError, 'selected in-bundle executable owner'):
            self.output_capture(f)

    def test_rpath_distinct_existing_targets_are_ambiguous(self):
        f = Fixture(self)
        directories = [f.bundle / name for name in ('first', 'second')]
        for directory in directories:
            directory.mkdir()
            (directory / 'library.dylib').write_bytes(macho(0x100000000, [(0xc, ALIAS)],
                install_name='@rpath/library.dylib'))
        f.tool.write_bytes(macho(0x100000000, [(0xc, '@rpath/library.dylib')],
                                 rpaths=tuple(str(path) for path in directories)))
        with self.assertRaisesRegex(SDKError, 'ambiguous image-local'):
            f.capture()

    def test_rpath_external_alias_and_unsupported_anchors_are_refused(self):
        for kind in ('external', 'alias', 'parent'):
            with self.subTest(kind=kind):
                f = Fixture(self)
                library = f.bundle / 'library.dylib'
                library.write_bytes(macho(0x100000000, [(0xc, ALIAS)],
                                         install_name='@rpath/library.dylib'))
                if kind == 'external':
                    anchor = str(f.root)
                elif kind == 'alias':
                    alias = f.bundle / 'alias'
                    alias.symlink_to('.')
                    anchor = str(alias)
                else:
                    anchor = '@loader_path/..'
                f.tool.write_bytes(macho(0x100000000, [(0xc, '@rpath/library.dylib')],
                                         rpaths=(anchor,)))
                with self.assertRaises(SDKError):
                    f.capture()

    def test_rpath_does_not_inherit_from_the_requesting_parent(self):
        f = Fixture(self)
        std = f.bundle / 'libstdc++.6.dylib'
        gcc = f.bundle / 'libgcc_s.1.1.dylib'
        gcc.write_bytes(macho(0x100000000, [(0xc, ALIAS)], install_name='@rpath/libgcc_s.1.1.dylib'))
        std.write_bytes(macho(0x100000000, [(0xc, '@rpath/libgcc_s.1.1.dylib')],
                               install_name='@rpath/libstdc++.6.dylib'))
        f.tool.write_bytes(macho(0x100000000, [(0xc, str(std))], rpaths=('@loader_path',)))
        with self.assertRaisesRegex(SDKError, 'inherited or missing @rpath'):
            f.capture()

    def test_rpath_owner_cannot_hide_an_alias_anchor(self):
        f = Fixture(self)
        library = f.bundle / 'library.dylib'
        library.write_bytes(macho(0x100000000, [(0xc, ALIAS)], install_name='@rpath/library.dylib'))
        actual = f.bundle / 'actual-executable'
        actual.write_bytes(macho(0x100000000, [(0xc, '@rpath/library.dylib')],
                                 rpaths=('@loader_path',)))
        f.tool.unlink()
        f.tool.symlink_to(actual.name)
        with self.assertRaisesRegex(SDKError, 'owner cannot have an aliased load path'):
            f.capture()

    def test_rpath_missing_candidate_cannot_appear_during_the_audit(self):
        f = Fixture(self)
        library = f.bundle / 'library.dylib'
        content = macho(0x100000000, [(0xc, ALIAS)], install_name='@rpath/library.dylib')
        library.write_bytes(content)
        f.tool.write_bytes(macho(0x100000000, [(0xc, '@rpath/library.dylib')],
                                 rpaths=('@loader_path/absent', '@loader_path')))
        original = snapshot_tree
        def appeared(root):
            rows = original(root)
            other = f.bundle / 'absent'
            other.mkdir()
            (other / library.name).write_bytes(content)
            return rows
        with patch('emberbsd_sdk.darwin_runtime.snapshot_tree', appeared):
            with self.assertRaisesRegex(SDKError, 'rpath candidate appeared'):
                f.capture()

    def test_rpath_count_and_whole_audit_candidate_budget_are_bounded(self):
        f = Fixture(self)
        library = f.bundle / 'library.dylib'
        library.write_bytes(macho(0x100000000, [(0xc, ALIAS)], install_name='@rpath/library.dylib'))
        f.tool.write_bytes(macho(0x100000000, [(0xc, '@rpath/library.dylib')],
                                 rpaths=('@loader_path',) * 65))
        with self.assertRaisesRegex(SDKError, 'inherited or missing @rpath'):
            f.capture()
        content = macho(0x100000000, [(0xc, '@rpath/library.dylib')],
                        rpaths=('@loader_path',) * 64)
        f.tool.write_bytes(content)
        for index in range(64):
            path = f.bundle / ('native-' + str(index))
            path.write_bytes(content)
            f.descriptor['tools']['native-' + str(index)] = dict(path=str(path), kind='native')
        with self.assertRaisesRegex(SDKError, 'candidates exceed the audit budget'):
            f.capture()

    def test_cached_image_rpath_does_not_enable_an_external_search(self):
        f = Fixture(self)
        data = bytearray(f.sub.read_bytes())
        data[0x400:0x800] = macho(BASE + 4096 + 0x400, [(0xc, '@rpath/library.dylib')],
            install_name=A, cached=True, uuid=3, rpaths=('@loader_path',))
        f.sub.write_bytes(data)
        f.refresh_inventory()
        with self.assertRaisesRegex(SDKError, '@rpath runtime loads in cached images'):
            f.capture()

    def test_cached_weak_edge_cannot_select_an_existing_off_cache_file(self):
        f = Fixture(self)
        library = f.bundle / 'library.dylib'
        library.write_bytes(macho(0x100000000, [(0xc, B)], install_name=str(library)))
        content = bytearray(f.sub.read_bytes())
        content[0x400:0x800] = macho(BASE + 4096 + 0x400,
            [(0xc, B), (0x80000018, str(library))], install_name=A, cached=True, uuid=3)
        f.sub.write_bytes(content)
        f.refresh_inventory()
        with self.assertRaisesRegex(SDKError, 'cached weak dependency exists outside the cache'):
            f.capture()

    def test_omitted_dependency_is_refused(self):
        f = Fixture(self)
        audit, closure = f.accepted()
        closure[str(f.tool)]['dependencies'] = []
        with self.assertRaisesRegex(SDKError, 'complete audited graph'):
            validate_darwin_runtime_closure(closure, f.descriptor)

    def test_changed_unparsed_cache_bytes_are_refused(self):
        f = Fixture(self)
        audit, closure = f.accepted()
        data = bytearray(f.sub.read_bytes())
        data[-1] ^= 1
        f.sub.write_bytes(data)
        with self.assertRaisesRegex(SDKError, 'cache inventory changed'):
            validate_darwin_runtime_closure(closure, f.descriptor)

    def test_subcache_uuid_is_checked(self):
        f = Fixture(self)
        data = bytearray(f.sub.read_bytes())
        data[0x58] ^= 1
        f.sub.write_bytes(data)
        f.refresh_inventory()
        with self.assertRaisesRegex(SDKError, 'subcache UUID mismatch'):
            f.capture()

    def test_wrong_audit_bytes_are_refused(self):
        f = Fixture(self)
        audit, closure = f.accepted()
        audit['nodes'][ALIAS]['image']['uuid'] = '0' * 32
        ref = f.reference(f.audit_path, audit)
        for row in closure.values():
            if 'cache_image' in row:
                row['cache_image'] = ref
        with self.assertRaisesRegex(SDKError, 'differs from current bytes'):
            validate_darwin_runtime_closure(closure, f.descriptor)

    def test_relative_dependency_is_not_a_fallback(self):
        f = Fixture(self)
        f.tool.write_bytes(macho(0x100000000, [(0xc, '@rpath/A.dylib')]))
        with self.assertRaisesRegex(SDKError, '@rpath runtime loads are not supported'):
            f.capture()

    def test_missing_weak_dependency_has_an_explicit_null_edge(self):
        f = Fixture(self)
        missing = str(f.bundle / 'optional.dylib')
        f.tool.write_bytes(macho(0x100000000, [(0xc, ALIAS), (0x80000018, missing)]))
        audit, closure = f.accepted()
        edge = audit['nodes'][str(f.tool)]['image']['loads'][1]
        self.assertEqual(edge['command'], 0x80000018)
        self.assertEqual(edge['name'], missing)
        self.assertIsNone(edge['target'])
        self.assertEqual(closure[str(f.tool)]['dependencies'], [ALIAS])
        validate_darwin_runtime_closure(closure, f.descriptor)
        Path(missing).write_bytes(macho(0x100000000, [(0xc, ALIAS)], install_name=missing))
        with self.assertRaisesRegex(SDKError, 'differs from current bytes'):
            validate_darwin_runtime_closure(closure, f.descriptor)

    def test_missing_nonweak_dependencies_are_refused(self):
        for kind in (0xc, 0x8000001f, 0x20, 0x80000023):
            with self.subTest(command=kind):
                f = Fixture(self)
                f.tool.write_bytes(macho(0x100000000, [(kind, str(f.bundle / 'absent.dylib'))]))
                with self.assertRaisesRegex(SDKError, 'required Darwin runtime dependency is absent'):
                    f.capture()

    def test_wrong_platform_and_newer_minos_are_refused(self):
        for offset, value, message in ((8, 2, 'supported macOS'), (12, 0x1c0000, 'newer macOS')):
            with self.subTest(value=value):
                f = Fixture(self)
                data = bytearray(f.tool.read_bytes())
                struct.pack_into('<I', data, 32 + 24 + 72 + offset, value)
                f.tool.write_bytes(data)
                with self.assertRaisesRegex(SDKError, message):
                    f.capture()

    def test_missing_weak_dependency_race_is_refused(self):
        f = Fixture(self)
        missing = str(f.bundle / 'optional.dylib')
        f.tool.write_bytes(macho(0x100000000, [(0xc, ALIAS), (0x80000018, missing)]))
        original = snapshot_tree
        def appeared(root):
            rows = original(root)
            Path(missing).write_bytes(macho(0x100000000, install_name=missing))
            return rows
        with patch('emberbsd_sdk.darwin_runtime.snapshot_tree', appeared):
            with self.assertRaisesRegex(SDKError, 'dependency appeared'):
                f.capture()

    def test_dylib_versions_are_preserved_without_an_invented_loader_gate(self):
        f = Fixture(self)
        content = bytearray(f.tool.read_bytes())
        # Header + UUID + segment + dylib command's compatibility field.
        struct.pack_into('<I', content, 32 + 24 + 72 + 24 + 20, 0x20000)
        f.tool.write_bytes(content)
        audit = f.capture()
        self.assertEqual(audit['nodes'][str(f.tool)]['image']['loads'][0]['compatibility_version'], 0x20000)
        self.assertEqual(audit['nodes'][ALIAS]['image']['dylib_version']['current'], 0x10000)

    def test_modern_dylib_use_weakness_is_recorded(self):
        f = Fixture(self)
        missing = str(f.bundle / 'optional.dylib')
        f.tool.write_bytes(macho(0x100000000, [(0xc, ALIAS), command(0xc, missing, flags=1)]))
        audit = f.capture()
        edge = audit['nodes'][str(f.tool)]['image']['loads'][1]
        self.assertEqual((edge['command'], edge['attributes'], edge['encoding'], edge['target']),
                         (0xc, 1, 'dylib-use', None))

    def test_unknown_dependency_encodings_fail_closed(self):
        for kind in (0x80000077,):
            with self.subTest(command=kind):
                f = Fixture(self)
                data = bytearray(f.tool.read_bytes())
                struct.pack_into('<I', data, 32 + 24 + 72, kind)
                f.tool.write_bytes(data)
                with self.assertRaisesRegex(SDKError, 'unsupported Mach-O load command'):
                    f.capture()

    def test_lazy_linkedit_load_and_weak_absence(self):
        for flags, name in ((0, ALIAS), (2, ALIAS), (0x8002, ALIAS), (1, None)):
            with self.subTest(flags=flags):
                f = Fixture(self)
                name = name or str(f.bundle / 'optional.dylib')
                f.tool.write_bytes(macho(0x100000000, [(0xc, B)], lazy=(name, flags)))
                audit = f.capture()
                edge = audit['nodes'][str(f.tool)]['image']['loads'][1]
                self.assertEqual(edge['command'], 0x3a)
                self.assertEqual(edge['encoding'], 'lazy-load-info')
                self.assertEqual(edge['target'], None if flags & 1 else ALIAS)
                self.assertEqual(audit['nodes'][str(f.tool)]['image']['lazy_info'][0]['flags'], flags)

    def test_lazy_linkedit_bounds_are_checked(self):
        f = Fixture(self)
        data = bytearray(macho(0x100000000, [(0xc, B)], lazy=(ALIAS, 0)))
        struct.pack_into('<I', data, 0x400 + 16, 0xffffffff)
        f.tool.write_bytes(data)
        with self.assertRaisesRegex(SDKError, 'invalid lazy dylib metadata'):
            f.capture()

    def test_lazy_symbol_count_has_an_explicit_limit(self):
        f = Fixture(self)
        f.tool.write_bytes(macho(0x100000000, [(0xc, ALIAS)],
            lazy=repeated_lazy_symbols(4097, 1)))
        with self.assertRaisesRegex(SDKError, 'symbol count exceeds the supported limit'):
            f.capture()

    def test_repeated_lazy_symbol_offsets_cannot_expand_past_the_budget(self):
        f = Fixture(self)
        f.tool.write_bytes(macho(0x100000000, [(0xc, ALIAS)],
            lazy=repeated_lazy_symbols(4096, 4096)))
        with self.assertRaisesRegex(SDKError, 'decoded symbols exceed the audit byte budget'):
            f.capture()

    def test_lazy_symbol_supported_limits_are_inclusive(self):
        f = Fixture(self)
        f.tool.write_bytes(macho(0x100000000, [(0xc, ALIAS)],
            lazy=repeated_lazy_symbols(4096, 256)))
        audit = f.capture()
        symbols = audit['nodes'][str(f.tool)]['image']['lazy_info'][0]['symbols']
        self.assertEqual(len(symbols), 4096)
        self.assertEqual(sum(len(symbol.encode('utf-8')) for symbol in symbols), 1024 * 1024)

    def test_lazy_symbol_byte_budget_is_shared_by_all_images(self):
        f = Fixture(self)
        f.tool.write_bytes(macho(0x100000000, [(0xc, ALIAS)],
            lazy=repeated_lazy_symbols(200, 3072)))
        other = f.bundle / 'other'
        other.write_bytes(f.tool.read_bytes())
        f.descriptor['tools']['other'] = dict(path=str(other), kind='native')
        with self.assertRaisesRegex(SDKError, 'decoded symbols exceed the audit byte budget'):
            f.capture()

    def test_dyld_implicit_system_edge_is_explicit(self):
        f = Fixture(self)
        system = '/usr/lib/libSystem.B.dylib'
        f.tool.write_bytes(macho(0x100000000))
        tree = trie({A: 0, ALIAS: 0, B: 1, system: 0})
        data = bytearray(f.main.read_bytes())
        struct.pack_into('<QQ', data, 0x108, BASE + 1500, len(tree))
        data[1500:1500 + len(tree)] = tree
        f.main.write_bytes(data)
        f.refresh_inventory()
        audit, closure = f.accepted()
        edge = audit['nodes'][str(f.tool)]['image']['implicit_loads'][0]
        self.assertIsNone(edge['command'])
        self.assertEqual(edge['reason'], 'dyld-synthesized-libSystem')
        self.assertEqual(closure[str(f.tool)]['dependencies'], [system])
        validate_darwin_runtime_closure(closure, f.descriptor)

    def test_trie_cycle_is_refused(self):
        f = Fixture(self)
        data = bytearray(f.main.read_bytes())
        # First root edge points back to root. Alias lookup must terminate.
        data[1500 + 2 + len(A) + 1] = 0
        f.main.write_bytes(data)
        f.refresh_inventory()
        with self.assertRaisesRegex(SDKError, 'cache dylib trie'):
            f.capture()

    def test_inventory_read_race_is_refused(self):
        f = Fixture(self)
        original = snapshot_tree
        def changed(root):
            rows = original(root)
            data = bytearray(f.sub.read_bytes())
            data[-1] ^= 1
            f.sub.write_bytes(data)
            return rows
        with patch('emberbsd_sdk.darwin_runtime.snapshot_tree', changed):
            with self.assertRaisesRegex(SDKError, 'changed'):
                f.capture()

    def test_non_darwin_and_undeclared_cache_root_are_refused(self):
        f = Fixture(self)
        descriptor = copy.deepcopy(f.descriptor)
        descriptor['host']['os'] = 'Linux'
        with self.assertRaisesRegex(SDKError, 'Darwin host'):
            capture_darwin_runtime_audit(descriptor, cache_main=str(f.main))
        f.descriptor['host_abi']['libraries'] = []
        with self.assertRaisesRegex(SDKError, 'explicit native ABI library root'):
            f.capture()


if __name__ == '__main__':
    unittest.main()
