# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Bounded owner data, without commands or recursive expansion."""

import importlib
import unittest
import test_core  # Adds the SDK source to sys.path.
from emberbsd_sdk.errors import SDKError


class ProgramDataTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('emberbsd_sdk.bootstrap_program_data'))
        return importlib.import_module('emberbsd_sdk.bootstrap_program_data')

    def test_literals_and_segments_preserve_exact_utf8_bytes(self):
        data = self.module()
        for value, expected in [('', b''), ('a\0b', b'a\0b'),
                                (['left:', {'text': 'é', 'repeat': 2}, '\n'], b'left:\xc3\xa9\xc3\xa9\n'),
                                ([{'text': 'x', 'repeat': 0}], b'')]:
            with self.subTest(value=value):
                self.assertEqual(data.decode_data(value), expected)
                self.assertEqual(data.data_size(value), len(expected))

    def test_invalid_or_unbounded_data_refuses(self):
        data = self.module()
        values = ['\ud800', True, None, 1, {}, [], ['x'] * 17, [['nested']],
                  [{'text': 'x', 'repeat': True}], [{'text': 'x', 'repeat': -1}],
                  [{'text': 'x', 'repeat': 1.0}], [{'text': '', 'repeat': 3}],
                  [{'text': 'x', 'repeat': 1, 'command': 'ignored'}],
                  [{'text': 'x', 'repeat': 33554433}],
                  [{'text': 'é', 'repeat': 16777217}],
                  [{'text': 'x', 'repeat': 33554432}, 'overflow']]
        for value in values:
            for operation in (data.decode_data, data.data_size):
                with self.subTest(value=value, operation=operation.__name__), self.assertRaises(SDKError):
                    operation(value)

    def test_size_boundary_is_checked_before_expansion(self):
        data = self.module()
        self.assertEqual(data.data_size([{'text': 'é', 'repeat': 16777216}]), 33554432)


if __name__ == '__main__':
    unittest.main()
