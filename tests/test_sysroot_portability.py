# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Sysroot and release builds select the OS reproducible-path mapping."""

import unittest

import test_core  # noqa: F401  (adds the SDK source to sys.path)
from emberbsd_sdk.os_release_builder import flags_for
from emberbsd_sdk.sysroot_builder import SysrootBuilder


def selections(flags, name):
    return [flag for flag in flags if flag.split('=', 1)[0] == name]


class SysrootPortabilityTests(unittest.TestCase):
    def test_sysroot_build_selects_mkrepro(self):
        # Without MKREPRO=yes, __FILE__ strings and libc tags keep the
        # absolute source and object paths of the build host.
        self.assertEqual(selections(SysrootBuilder.FLAGS, 'MKREPRO'), ['MKREPRO=yes'])

    def test_release_builds_use_the_same_single_selection(self):
        for target in ('aarch64', 'amd64'):
            for family in (None, 'gcc', 'clang'):
                with self.subTest(target=target, family=family):
                    self.assertEqual(selections(flags_for(target, family), 'MKREPRO'), ['MKREPRO=yes'])


if __name__ == '__main__':
    unittest.main()
