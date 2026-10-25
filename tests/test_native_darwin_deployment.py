# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Synthetic Darwin deployment metadata. No fixture program is executed."""

import copy
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from test_native_toolset import Fixture, digest
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.native_toolset import _flags, validate_selection

MINIMUM = '-mmacosx-version-min=26.0'


class DarwinDeploymentTests(unittest.TestCase):
    def fixture(self, darwin=True):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        fixture = Fixture(temporary.name)
        if not darwin:
            return fixture
        fixture.host.update(os='Darwin', cpu='arm64')
        fixture.compiler['triplet'] = 'aarch64-apple-darwin26.0.0'
        image = struct.pack('<8I', 0xfeedfacf, 0x100000c, 0, 2, 0, 0, 0, 0).ljust(64, b'\0')
        obj = bytearray(image)
        struct.pack_into('<I', obj, 12, 1)
        for row in fixture.records.values():
            path = Path(row['path'])
            path.write_bytes(image)
            row['sha256'] = digest(path)
        for role in ('cc', 'cxx'):
            checks = fixture.receipt['checks'][role]
            for name, content in [('object', obj), ('artifact', image)]:
                path = Path(checks[name]['path'])
                path.write_bytes(content)
                checks[name]['sha256'] = digest(path)
            query = fixture.receipt['checks']['identity'][role]['-dumpmachine']
            query['stdout'] = fixture.raw((fixture.compiler['triplet'] + '\n').encode())
        for name in ('cflags', 'cxxflags', 'ldflags'):
            fixture.descriptor['abi_flags'][name] = [MINIMUM]
        fixture.bind_probe_flags()
        fixture.seal()
        return fixture

    def load(self, fixture):
        with patch('emberbsd_sdk.native_toolset.platform.system', return_value=fixture.host['os']), \
                patch('emberbsd_sdk.native_toolset.platform.machine', return_value=fixture.host['cpu']):
            return fixture.load()

    def test_live_and_saved_darwin_preserve_the_accepted_minimum(self):
        fixture = self.fixture()
        selected = self.load(fixture)
        for name in ('cflags', 'cxxflags', 'ldflags'):
            self.assertEqual(selected.abi_flags[name], (MINIMUM,))
        saved = selected.as_dict()
        with patch('emberbsd_sdk.native_toolset.canonical_existing', side_effect=AssertionError('saved selection read files')):
            validate_selection(saved, required_roles=('cxx', 'python'))
        for role in ('cc', 'cxx'):
            for phase in ('compile', 'link'):
                self.assertIn(MINIMUM, fixture.receipt['checks'][role][phase]['argv'])

    def test_live_and_saved_non_darwin_refuse_the_flag(self):
        fixture = self.fixture(darwin=False)
        saved = self.load(fixture).as_dict()
        fixture.descriptor['abi_flags']['cxxflags'] = [MINIMUM]
        fixture.bind_probe_flags()
        fixture.seal()
        with self.assertRaisesRegex(SDKError, 'Darwin deployment flag'):
            self.load(fixture)
        for host, triplet in [(dict(os='Linux', cpu='x86_64'), 'x86_64-pc-linux-gnu'),
                              (dict(os='NetBSD', cpu='aarch64'), 'aarch64--netbsd11.0')]:
            value = copy.deepcopy(saved)
            value['host'], value['compiler']['triplet'] = host, triplet
            value['abi_flags']['cxxflags'] = [MINIMUM]
            with self.subTest(host=host), self.assertRaisesRegex(SDKError, 'Darwin deployment flag'):
                validate_selection(value, required_roles=('cxx',))

    def test_malformed_versions_fail_live_and_saved(self):
        fixture = self.fixture()
        saved = self.load(fixture).as_dict()
        for version in ('', '0', '026.0', '26.00', '26.', '.26', '-1', '26..0',
                        '26.0.0.1', '1000.0', '26.1000', '26.0\n', '26.0 -B/other'):
            flag = '-mmacosx-version-min=' + version
            fixture.descriptor['abi_flags']['cxxflags'] = [flag]
            fixture.bind_probe_flags()
            fixture.seal()
            value = copy.deepcopy(saved)
            value['abi_flags']['cxxflags'] = [flag]
            with self.subTest(version=version):
                with self.assertRaises(SDKError):
                    self.load(fixture)
                with self.assertRaises(SDKError):
                    validate_selection(value, required_roles=('cxx',))

    def test_driver_and_search_overrides_remain_refused_on_darwin(self):
        fixture = self.fixture()
        saved = self.load(fixture).as_dict()
        for flag in ('-B/usr/bin', '-specs=/other/specs', '@response',
                     '-Wl,-platform_version,macos,26.0,26.5', '-Wl,-L/usr/lib',
                     '-mmacosx-version-min=26.0;-B/other'):
            fixture.descriptor['abi_flags']['cxxflags'] = [flag]
            fixture.bind_probe_flags()
            fixture.seal()
            value = copy.deepcopy(saved)
            value['abi_flags']['cxxflags'] = [flag]
            with self.subTest(flag=flag):
                with self.assertRaises(SDKError):
                    self.load(fixture)
                with self.assertRaises(SDKError):
                    validate_selection(value, required_roles=('cxx',))

    def test_probe_records_must_retain_the_exact_minimum(self):
        for phase in ('compile', 'link'):
            fixture = self.fixture()
            fixture.receipt['checks']['cc'][phase]['argv'].remove(MINIMUM)
            fixture.seal()
            with self.subTest(phase=phase), self.assertRaisesRegex(SDKError, 'route does not bind probe files'):
                self.load(fixture)

    def test_host_is_required_and_version_grammar_is_bounded(self):
        flags = dict(cppflags=[], cflags=[MINIMUM], cxxflags=[], ldflags=[])
        with self.assertRaisesRegex(SDKError, 'Darwin deployment flag'):
            _flags(flags)
        for version in ('10.15', '14', '26.0', '26.0.1'):
            flags['cflags'] = ['-mmacosx-version-min=' + version]
            selected = _flags(flags, host=dict(os='Darwin', cpu='arm64'))
            self.assertEqual(selected['cflags'], tuple(flags['cflags']))


if __name__ == '__main__':
    unittest.main()
