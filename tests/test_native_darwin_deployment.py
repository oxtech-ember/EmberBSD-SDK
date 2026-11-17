# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Darwin deployment flag grammar for the native host ABI flags."""

import unittest

import test_core  # Adds the SDK source to sys.path.
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.native_toolset import _flags

MINIMUM = '-mmacosx-version-min=26.0'
DARWIN = dict(os='Darwin', cpu='arm64')


def flags(*words):
    return dict(cppflags=[], cflags=list(words), cxxflags=[], ldflags=[])


class DarwinDeploymentTests(unittest.TestCase):
    def test_darwin_accepts_bounded_minimum_versions(self):
        for version in ('10.15', '14', '26.0', '26.0.1'):
            word = '-mmacosx-version-min=' + version
            self.assertEqual(_flags(flags(word), host=DARWIN)['cflags'], (word,))

    def test_other_or_missing_host_refuses_the_flag(self):
        for host in (None, dict(os='Linux', cpu='x86_64'), dict(os='NetBSD', cpu='aarch64')):
            with self.subTest(host=host), self.assertRaisesRegex(SDKError, 'Darwin deployment flag'):
                _flags(flags(MINIMUM), host=host)

    def test_malformed_versions_and_overrides_are_refused(self):
        words = ['-mmacosx-version-min=' + version for version in (
            '', '0', '026.0', '26.00', '26.', '.26', '-1', '26..0', '26.0.0.1',
            '1000.0', '26.1000', '26.0\n', '26.0 -B/other')]
        words += ['-B/usr/bin', '-specs=/other/specs', '@response',
                  '-Wl,-platform_version,macos,26.0,26.5', '-Wl,-L/usr/lib',
                  '-mmacosx-version-min=26.0;-B/other']
        for word in words:
            with self.subTest(word=word), self.assertRaises(SDKError):
                _flags(flags(word), host=DARWIN)


if __name__ == '__main__':
    unittest.main()
