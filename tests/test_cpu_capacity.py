# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from emberbsd_sdk.cpu_capacity import available_cpu_count
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.cpu_capacity import resolve_jobs


class CPUCapacityTests(unittest.TestCase):
    def test_process_cpu_count_is_preferred(self):
        with patch('emberbsd_sdk.cpu_capacity.os.process_cpu_count', create=True, return_value=6), \
             patch('emberbsd_sdk.cpu_capacity.os.sched_getaffinity', create=True, side_effect=AssertionError), \
             patch('emberbsd_sdk.cpu_capacity.os.cpu_count', side_effect=AssertionError):
            self.assertEqual(available_cpu_count(), 6)

    def test_linux_affinity_fallback_limits_visible_cpus(self):
        with patch('emberbsd_sdk.cpu_capacity.os.process_cpu_count', create=True, return_value=None), \
             patch('emberbsd_sdk.cpu_capacity.os.sched_getaffinity', create=True, return_value={2, 4, 6}), \
             patch('emberbsd_sdk.cpu_capacity.os.cpu_count', side_effect=AssertionError):
            self.assertEqual(available_cpu_count(), 3)

    def test_cpu_count_is_fallback_when_affinity_is_unavailable(self):
        with patch('emberbsd_sdk.cpu_capacity.os.process_cpu_count', create=True, return_value=None), \
             patch('emberbsd_sdk.cpu_capacity.os.sched_getaffinity', create=True, side_effect=OSError), \
             patch('emberbsd_sdk.cpu_capacity.os.cpu_count', return_value=10):
            self.assertEqual(available_cpu_count(), 10)

    def test_unavailable_capacity_returns_none(self):
        with patch('emberbsd_sdk.cpu_capacity.os.process_cpu_count', create=True, return_value=None), \
             patch('emberbsd_sdk.cpu_capacity.os.sched_getaffinity', create=True, side_effect=OSError), \
             patch('emberbsd_sdk.cpu_capacity.os.cpu_count', return_value=None):
            self.assertIsNone(available_cpu_count())

    def test_resolve_jobs_uses_capacity_by_default_and_preserves_override(self):
        with patch('emberbsd_sdk.cpu_capacity.available_cpu_count', return_value=12):
            self.assertEqual(resolve_jobs(), 12)
        with patch('emberbsd_sdk.cpu_capacity.available_cpu_count',
                   side_effect=AssertionError('explicit override must not query capacity')):
            self.assertEqual(resolve_jobs(48), 48)
            self.assertEqual(resolve_jobs('7'), 7)

    def test_resolve_jobs_rejects_invalid_or_unknown_counts(self):
        with patch('emberbsd_sdk.cpu_capacity.available_cpu_count', return_value=None), \
                self.assertRaisesRegex(SDKError, 'CPU count is unavailable'):
            resolve_jobs()
        for value in (0, -1, True, 'bad'):
            with self.subTest(value=value), self.assertRaisesRegex(SDKError, 'positive integer'):
                resolve_jobs(value)


if __name__ == '__main__':
    unittest.main()
