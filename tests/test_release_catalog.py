# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Release catalog boundaries and stateless channel validity."""

import hashlib
import json
import unittest
from unittest.mock import patch

from test_core import TemporaryCase
from emberbsd_sdk import jsonio, release_catalog as catalog
from emberbsd_sdk.errors import SDKError


HOST = {'os': 'darwin', 'arch': 'aarch64', 'abi': 'macos-15.0', 'runtime': 'libSystem-libcxx'}
TARGET = {'arch': 'aarch64', 'triplet': 'aarch64--netbsd',
          'abi': 'emberbsd-11', 'runtime': 'llvm-libcxx'}
PAYLOAD = b'public artifact fixture\n'


def artifact(name='runtime', *, kind='target-package', depends=()):
    return {'id': name, 'path': 'packages/' + name + '.tgz', 'kind': kind,
            'host': dict(HOST) if kind in ('host-package', 'bootstrap') else None,
            'target': None if kind in ('host-package', 'bootstrap') else dict(TARGET),
            'size': len(PAYLOAD), 'unpacked_size': len(PAYLOAD),
            'sha256': hashlib.sha256(PAYLOAD).hexdigest(),
            'depends': list(depends), 'licenses': ['BSD-2-Clause']}


def release():
    return {'schema': catalog.RELEASE_SCHEMA, 'release_id': '2026.11-test',
            'created_at': 100, 'sources': {'os': 'a' * 40, 'ports': 'b' * 40, 'sdk': 'c' * 40},
            'artifacts': [artifact(), artifact('app', depends=['runtime']),
                          artifact('bootstrap', kind='bootstrap')]}


def channel():
    return {'schema': catalog.CHANNEL_SCHEMA, 'channel': 'stable',
            'issued_at': 100, 'expires_at': 300,
            'release': {'id': '2026.11-test', 'path': 'releases/2026.11-test/release.p7m',
                        'size': 123, 'sha256': 'a' * 64}}


def encoded(value):
    return json.dumps(value).encode('utf-8')


class ReleaseFormatTests(unittest.TestCase):
    def test_round_trip_preserves_descriptors_and_order(self):
        value = release()
        self.assertEqual(catalog.parse_release(encoded(value)), value)

    def test_strict_json_and_exact_byte_input(self):
        valid = encoded(release())
        invalid = [valid.decode(), b'\xff', b'{} trailing', b'{"schema": NaN}',
                   valid.replace(b'"created_at": 100', b'"created_at": 100, "created_at": 101'),
                   valid.replace(f'"size": {len(PAYLOAD)}'.encode(), b'"size": 24, "size": 25'),
                   b'[' * 33 + b'0' + b']' * 33, b' ' * (jsonio.MAX_JSON + 1)]
        # This nested duplicate is independent of the payload fixture length.
        invalid.append(valid.replace(b'"os": "darwin"', b'"os": "darwin", "os": "linux"'))
        for data in invalid:
            with self.subTest(data=str(data[:60])), self.assertRaises(SDKError):
                catalog.parse_release(data)

    def test_exact_fields_at_every_object_boundary(self):
        for selector in (lambda v: v, lambda v: v['sources'], lambda v: v['artifacts'][0],
                         lambda v: v['artifacts'][0]['target'], lambda v: v['artifacts'][2]['host']):
            for change in ('add', 'remove'):
                value = release()
                selected = selector(value)
                if change == 'add':
                    selected['future-field'] = 1
                else:
                    del selected[next(iter(selected))]
                with self.subTest(change=change, selected=selected), self.assertRaises(SDKError):
                    catalog.parse_release(encoded(value))

    def test_release_types_sources_and_empty_artifacts(self):
        for key, values in {'schema': [None, {}, 'emberbsd.release.v2'],
                            'release_id': [None, True, '.', '..', 'Release', 'two words', '../a'],
                            'created_at': [None, True, 0, -1, 1.0, '100'],
                            'sources': [None, [], {'os': 'a' * 40}],
                            'artifacts': [None, [], {}, [None]]}.items():
            for invalid in values:
                value = release()
                value[key] = invalid
                with self.subTest(key=key, value=invalid), self.assertRaises(SDKError):
                    catalog.parse_release(encoded(value))
        for invalid in ('A' * 40, 'a' * 39, 'g' * 40, True, None):
            value = release()
            value['sources']['os'] = invalid
            with self.subTest(source=invalid), self.assertRaises(SDKError):
                catalog.parse_release(encoded(value))

    def test_artifact_field_types_and_limits(self):
        for key, values in {'id': ['', None, [], 'UPPER', '..'],
                            'size': [True, -1, 1.0, None, '1'],
                            'unpacked_size': [True, -1, 1.0, None, '1'],
                            'sha256': [None, 'A' * 64, 'a' * 63, 'x' * 64],
                            'kind': [None, [], 'executable'],
                            'host': [True, {}, []], 'target': [True, {}, []],
                            'licenses': [None, [], [''], [' '], [7], ['bad\nlicense']],
                            'depends': [None, {}, [None], ['runtime', 'runtime']]}.items():
            for invalid in values:
                value = release()
                value['artifacts'][0][key] = invalid
                with self.subTest(key=key, value=invalid), self.assertRaises(SDKError):
                    catalog.parse_release(encoded(value))
        value = release()
        value['artifacts'][0]['size'] = 0
        value['artifacts'][0]['unpacked_size'] = 0
        catalog.parse_release(encoded(value))

    def test_unpacked_size_is_required_and_independent_of_transfer_size(self):
        value = release()
        del value['artifacts'][0]['unpacked_size']
        with self.assertRaises(SDKError):
            catalog.parse_release(encoded(value))
        value = release()
        value['artifacts'][0]['unpacked_size'] = len(PAYLOAD) * 100
        self.assertEqual(catalog.parse_release(encoded(value)), value)

    def test_platform_descriptor_fields_are_nonempty_strings(self):
        for platform, row in (('target', 0), ('host', 2)):
            for field in release()['artifacts'][row][platform]:
                for invalid in (None, False, '', ' ', '\x00', 'bad\nfield'):
                    value = release()
                    value['artifacts'][row][platform][field] = invalid
                    with self.subTest(field=field, invalid=invalid), self.assertRaises(SDKError):
                        catalog.parse_release(encoded(value))

    def test_host_abi_and_runtime_cannot_be_omitted(self):
        for field in ('abi', 'runtime'):
            value = release()
            del value['artifacts'][2]['host'][field]
            with self.subTest(field=field), self.assertRaises(SDKError):
                catalog.parse_release(encoded(value))

    def test_kind_platform_roles(self):
        host_kinds = ('host-package', 'bootstrap')
        target_kinds = ('target-package', 'sysroot', 'os-set', 'image')
        for kind in (*host_kinds, *target_kinds, 'package-index'):
            for host in (None, HOST):
                for target in (None, TARGET):
                    value = release()
                    value['artifacts'][0].update(kind=kind, host=host, target=target)
                    valid = ((kind in host_kinds and host is not None and target is None) or
                             (kind in target_kinds and target is not None and host is None) or
                             (kind == 'package-index' and (host is None) != (target is None)))
                    with self.subTest(kind=kind, host=host, target=target):
                        if valid:
                            catalog.parse_release(encoded(value))
                        else:
                            with self.assertRaisesRegex(SDKError, 'role'):
                                catalog.parse_release(encoded(value))

    def test_url_paths_cannot_escape_or_change_meaning(self):
        for invalid in ('', '/', '/root/file', '//host/file', 'a//b', 'a/', '.', '..',
                        '../a', 'a/../b', 'a/./b', 'a\\b', 'https://host/file',
                        'a%2fb', 'a?x=1', 'a#fragment', 'a b', 'caf\u00e9/file', 'a\x00b'):
            value = release()
            value['artifacts'][0]['path'] = invalid
            with self.subTest(path=invalid), self.assertRaises(SDKError):
                catalog.parse_release(encoded(value))

    def test_unique_ids_casefold_paths_and_ancestor_overlap(self):
        for updates in ({'id': 'runtime'}, {'path': 'packages/RUNTIME.tgz'},
                        {'path': 'packages/runtime.tgz/child'}):
            value = release()
            value['artifacts'][1].update(updates)
            with self.subTest(updates=updates), self.assertRaises(SDKError):
                catalog.parse_release(encoded(value))
        # A lexical neighbor must not hide an ancestor behind another artifact.
        value = release()
        for row, path in zip(value['artifacts'], ('a', 'a-b', 'a/b')):
            row['path'] = path
        with self.assertRaisesRegex(SDKError, 'overlap'):
            catalog.parse_release(encoded(value))

    def test_unknown_dependency_self_cycle_and_multi_node_cycle(self):
        for dependency in ('missing', 'runtime', 'app'):
            value = release()
            value['artifacts'][0]['depends'] = [dependency]
            with self.subTest(dependency=dependency), self.assertRaises(SDKError):
                catalog.parse_release(encoded(value))

    def test_long_dependency_chain_uses_no_python_recursion(self):
        value = release()
        value['artifacts'] = [artifact(f'item-{index}', depends=[f'item-{index - 1}'] if index else [])
                              for index in range(1500)]
        catalog.parse_release(encoded(value))
        value['artifacts'][0]['depends'] = ['item-1499']
        with self.assertRaisesRegex(SDKError, 'cycle'):
            catalog.parse_release(encoded(value))


class ChannelFormatTests(unittest.TestCase):
    def test_round_trip_and_clock_boundaries(self):
        value = channel()
        for now in (100, 200, 299):
            self.assertEqual(catalog.parse_channel(encoded(value), 'stable', now), value)
        for now in (99, 300):
            with self.subTest(now=now), self.assertRaisesRegex(SDKError, 'clock'):
                catalog.parse_channel(encoded(value), 'stable', now)
        with patch.object(catalog.time, 'time', return_value=200):
            self.assertEqual(catalog.parse_channel(encoded(value), 'stable'), value)

    def test_exact_schema_fields_and_selected_name(self):
        for selector in (lambda v: v, lambda v: v['release']):
            for add in (True, False):
                value = channel()
                selected = selector(value)
                if add:
                    selected['extra'] = 1
                else:
                    del selected[next(iter(selected))]
                with self.subTest(add=add, selected=selected), self.assertRaises(SDKError):
                    catalog.parse_channel(encoded(value), 'stable', 200)
        for expected in ('testing', '', 'Stable', None):
            with self.subTest(expected=expected), self.assertRaises(SDKError):
                catalog.parse_channel(encoded(channel()), expected, 200)

    def test_channel_types_timestamps_and_reference_limits(self):
        for key, invalid in [('schema', 'emberbsd.channel.v1'), ('channel', []),
                             *[(name, v) for name in ('issued_at', 'expires_at')
                               for v in (True, 0, -1, 1.5, None, '100')]]:
            value = channel()
            value[key] = invalid
            with self.subTest(key=key, value=invalid), self.assertRaises(SDKError):
                catalog.parse_channel(encoded(value), 'stable', 200)
        for key, invalid in [('path', 'release.p7m'), ('path', 'a/release.json'),
                             ('path', '../release.p7m'), ('size', 0), ('size', True),
                             ('size', catalog.MAX_ENVELOPE + 1), ('sha256', 'A' * 64),
                             ('id', '../release')]:
            value = channel()
            value['release'][key] = invalid
            with self.subTest(key=key, value=invalid), self.assertRaises(SDKError):
                catalog.parse_channel(encoded(value), 'stable', 200)
        value = channel()
        value['expires_at'] = value['issued_at']
        with self.assertRaisesRegex(SDKError, 'clock'):
            catalog.parse_channel(encoded(value), 'stable', 200)


class PayloadTests(TemporaryCase):
    def setUp(self):
        super().setUp()
        self.value = release()
        for row in self.value['artifacts']:
            self.write(row['path'], PAYLOAD)

    def test_hash_and_payload_verification(self):
        path = catalog.local_artifact(self.root, 'packages/runtime.tgz')
        self.assertEqual(catalog.file_reference(path),
                         {'size': len(PAYLOAD), 'sha256': hashlib.sha256(PAYLOAD).hexdigest()})
        catalog.verify_payloads(self.value, self.root)

    def test_same_size_tamper_and_truncation_rejected(self):
        for changed in (b'X' + PAYLOAD[1:], PAYLOAD[:-1]):
            self.write('packages/runtime.tgz', changed)
            with self.subTest(changed=changed), self.assertRaisesRegex(SDKError, 'mismatch'):
                catalog.verify_payloads(self.value, self.root)

    def test_local_paths_reject_traversal_missing_files_and_directories(self):
        for path in ('../outside', '/absolute', 'packages', 'packages/missing'):
            with self.subTest(path=path), self.assertRaises(SDKError):
                catalog.local_artifact(self.root, path)

    def test_relative_symlinks_rejected_even_when_contained(self):
        (self.root / 'alias').symlink_to(self.root / 'packages')
        (self.root / 'packages/link.tgz').symlink_to(self.root / 'packages/runtime.tgz')
        for path in ('alias/runtime.tgz', 'packages/link.tgz'):
            with self.subTest(path=path), self.assertRaises(SDKError):
                catalog.local_artifact(self.root, path)
        with self.assertRaises(SDKError):
            catalog.file_reference(self.root / 'packages/link.tgz')

    def test_explicit_root_alias_uses_selected_directory(self):
        (self.root / 'root-alias').symlink_to(self.root / 'packages')
        self.assertEqual(catalog.local_artifact(self.root / 'root-alias', 'runtime.tgz'),
                         self.root / 'packages/runtime.tgz')

    def test_file_changed_during_hash_is_rejected(self):
        original = catalog.verify_stable
        def change(path, before):
            path.write_bytes(b'X' + PAYLOAD[1:])
            original(path, before)
        with patch.object(catalog, 'verify_stable', change), self.assertRaisesRegex(SDKError, 'changed'):
            catalog.file_reference(self.root / 'packages/runtime.tgz')


class CurrentChannelTests(unittest.TestCase):
    def test_retired_schema_and_counter_are_rejected(self):
        for changed in (dict(channel(), schema='emberbsd.channel.v1'), dict(channel(), generation=1)):
            with self.subTest(changed=changed), self.assertRaises(SDKError):
                catalog.parse_channel(encoded(changed), 'stable', 200)

    def test_valid_metadata_does_not_require_a_local_freshness_floor(self):
        self.assertEqual(catalog.parse_channel(encoded(channel()), 'stable', 200), channel())
        self.assertFalse(hasattr(catalog, 'commit_channel'))


if __name__ == '__main__':
    unittest.main()
