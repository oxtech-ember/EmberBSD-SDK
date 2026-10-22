# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Keep session proof strict while callers select an inspection wait budget."""

import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk import process, stream
from emberbsd_sdk.errors import SDKError


class Clock:
    def __init__(self):
        self.value = 0

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class InspectionBudgetTests(unittest.TestCase):
    def inspector(self, **options):
        # Initialize the real policy without invoking a host-specific probe.
        with patch.object(process, 'require_posix'), \
                patch.object(process.sys, 'platform', 'linux'), \
                patch('emberbsd_sdk._linux_process.LinuxSession'), \
                patch.object(process.SessionInspector, 'members', return_value={os.getpid()}):
            return process.SessionInspector(**options)

    def run_clocked(self, inspector, scan):
        clock = Clock()
        with patch.object(process.time, 'monotonic', clock.now), \
                patch.object(process.time, 'sleep', clock.sleep), \
                patch.object(inspector, '_scan', side_effect=lambda sid: scan(clock.value, sid)):
            result = inspector.members(4242)
        return result, clock.value

    def test_default_refuses_uncertainty_that_exceeds_two_seconds(self):
        inspector = self.inspector()
        self.assertEqual(inspector.empty_snapshot_timeout, 2)
        with self.assertRaisesRegex(SDKError, 'complete empty snapshot'):
            self.run_clocked(inspector, lambda elapsed, sid: (set(), elapsed < 2.2))

    def test_explicit_budget_waits_for_the_same_complete_empty_proof(self):
        inspector = self.inspector(empty_snapshot_timeout=3)
        result, elapsed = self.run_clocked(inspector, lambda elapsed, sid: (set(), elapsed < 2.2))
        self.assertEqual(result, set())
        self.assertGreaterEqual(elapsed, 2.2)
        self.assertLess(elapsed, 3)

    def test_persistent_uncertainty_still_refuses_at_selected_budget(self):
        inspector = self.inspector(empty_snapshot_timeout=.06)
        clock = Clock()
        with patch.object(process.time, 'monotonic', clock.now), \
                patch.object(process.time, 'sleep', clock.sleep), \
                patch.object(inspector, '_scan', return_value=(set(), True)) as scan:
            with self.assertRaisesRegex(SDKError, 'complete empty snapshot'):
                inspector.members(4242)
        self.assertGreaterEqual(clock.value, .06)
        self.assertLessEqual(clock.value, .08)
        self.assertTrue(all(call.args == (4242,) for call in scan.call_args_list))

    def test_budget_does_not_hide_observed_members(self):
        inspector = self.inspector(empty_snapshot_timeout=30)
        result, elapsed = self.run_clocked(inspector, lambda elapsed, sid: ({999}, True))
        self.assertEqual((result, elapsed), ({999}, 0))

    def test_invalid_budgets_refuse_before_probes_input_or_producer(self):
        values = (None, False, 0, -1, '30', float('nan'), float('inf'), -float('inf'), 10 ** 1000)
        for value in values:
            with self.subTest(value=repr(value)), \
                    patch.object(process, 'require_posix') as probe, \
                    patch.object(stream, 'OwnedProcessGroup') as producer, \
                    patch.object(stream.tempfile, 'TemporaryFile') as temporary:
                with self.assertRaisesRegex(SDKError, 'finite and positive'):
                    process.SessionInspector(empty_snapshot_timeout=value)
                with self.assertRaisesRegex(SDKError, 'finite and positive'):
                    process.OwnedProcessGroup(['unused'], cwd=None, env=None, stdout=None,
                                              stderr=None, empty_snapshot_timeout=value)
                for invoke in (lambda: stream.run_streamed(['unused'], lambda reader: None,
                                   input=b'bytes', empty_snapshot_timeout=value),
                               lambda: stream.capture_streamed(['unused'], input=b'bytes',
                                   empty_snapshot_timeout=value)):
                    with self.assertRaisesRegex(SDKError, 'finite and positive'):
                        invoke()
                probe.assert_not_called()
                producer.assert_not_called()
                temporary.assert_not_called()

    def test_group_forwards_budget_only_to_an_inspector_it_creates(self):
        with patch.object(process, 'SessionInspector') as factory:
            process.OwnedProcessGroup(['unused'], cwd=None, env=None, stdout=None,
                                      stderr=None, empty_snapshot_timeout=30)
        factory.assert_called_once_with(env=None, empty_snapshot_timeout=30)
        explicit = Mock()
        with patch.object(process, 'SessionInspector') as factory:
            group = process.OwnedProcessGroup(['unused'], cwd=None, env=None, stdout=None,
                      stderr=None, inspector=explicit, empty_snapshot_timeout=30)
        factory.assert_not_called()
        self.assertIs(group.inspector, explicit)

    def test_stream_forwards_budget_before_producer_start(self):
        with patch.object(stream, 'OwnedProcessGroup', side_effect=SDKError('fixture boundary')) as factory:
            with self.assertRaisesRegex(SDKError, 'fixture boundary'):
                stream.run_streamed(['unused'], lambda reader: None, empty_snapshot_timeout=30)
        self.assertEqual(factory.call_args.kwargs['empty_snapshot_timeout'], 30)
        with patch.object(stream, 'run_streamed', return_value='fixture') as run:
            self.assertEqual(stream.capture_streamed(['unused'], empty_snapshot_timeout=30), 'fixture')
        self.assertEqual(run.call_args.kwargs['empty_snapshot_timeout'], 30)
        self.assertEqual(stream.PRODUCER_INSPECTION_API, 1)


if __name__ == '__main__':
    unittest.main()
