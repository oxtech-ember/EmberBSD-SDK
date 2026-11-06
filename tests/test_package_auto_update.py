# SPDX-License-Identifier: BSD-2-Clause
import io
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import threading
import unittest
from unittest.mock import Mock, patch

import test_package_state as fixtures
from emberbsd_sdk.errors import SDKError


class StopAfter:
    def __init__(self, count):
        self.count, self.stopped, self.waits = count, False, []

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, interval):
        self.waits.append(interval)
        self.stopped = self.stopped or len(self.waits) >= self.count
        return self.stopped


class AutomaticUpdateTests(unittest.TestCase):
    def watch(self, update, interval=3600, *, stop):
        from emberbsd_sdk.package_auto_update import watch_updates
        handlers = {}
        def change(number, handler):
            handlers[number] = handler
        def wait(_read, _write, _errors, timeout):
            if stop.wait(timeout):
                handlers[signal.SIGTERM](signal.SIGTERM, None)
            return [], [], []
        with patch('sys.stderr', io.StringIO()), patch('signal.signal', side_effect=change), \
             patch('select.select', side_effect=wait):
            return watch_updates(update, interval)

    def test_attempts_run_immediately_then_wait_between_cycles(self):
        stop = StopAfter(2)
        def update():
            calls.append(len(stop.waits))
            return 0
        calls = []
        self.assertEqual(self.watch(update, stop=stop), 0)
        self.assertEqual(calls, [0, 1])
        self.assertEqual(stop.waits, [3600, 3600])

    def test_failure_is_reported_and_retried_after_the_interval(self):
        stop = StopAfter(2)
        update = Mock(side_effect=[1, 0])
        self.assertEqual(self.watch(update, 17, stop=stop), 0)
        self.assertEqual(update.call_count, 2)
        self.assertEqual(stop.waits, [17, 17])

    def test_stop_during_attempt_finishes_it_without_another_attempt(self):
        stop = StopAfter(10)
        update = Mock(side_effect=lambda: stop.set() or 0)
        self.assertEqual(self.watch(update, stop=stop), 0)
        update.assert_called_once()

    def test_invalid_interval_fails_before_any_attempt(self):
        update = Mock()
        for interval in (0, -1, float('nan'), float('inf'), threading.TIMEOUT_MAX * 2):
            with self.subTest(interval=interval), self.assertRaises(SDKError):
                self.watch(update, interval, stop=StopAfter(1))
        update.assert_not_called()

    def test_signal_requests_stop_and_restores_previous_handlers(self):
        from emberbsd_sdk.package_auto_update import watch_updates
        old = object()
        handlers = {}
        def change(number, handler):
            handlers[number] = handler
            return old
        def update():
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            return 0
        with patch('signal.signal', side_effect=change), patch('sys.stderr', io.StringIO()):
            self.assertEqual(watch_updates(update, 3600), 0)
        self.assertTrue(all(handler is old for handler in handlers.values()))

    def test_sigterm_during_an_event_wait_does_not_reenter_its_lock(self):
        script = r'''
import inspect, os, signal, sys, threading
from emberbsd_sdk.package_auto_update import watch_updates

event_type = threading.Event
shared = event_type()
wait_code = event_type.wait.__code__
lines, first = inspect.getsourcelines(event_type.wait)
locked_line = first + next(i for i, line in enumerate(lines) if 'signaled = self._flag' in line)
sent = False
def trace(frame, event, arg):
    global sent
    if not sent and event == 'line' and frame.f_code is wait_code and frame.f_lineno == locked_line:
        sent = True
        print('signal at locked wait boundary', flush=True)
        os.kill(os.getpid(), signal.SIGTERM)
    return trace

threading.Event = lambda: shared
try:
    sys.settrace(trace)
    watch_updates(lambda: shared.wait(.01) or 0, 3600)
finally:
    sys.settrace(None)
    threading.Event = event_type
assert sent
print('stopped cleanly', flush=True)
'''
        environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'))
        result = subprocess.run([sys.executable, '-B', '-c', script], env=environment,
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('signal at locked wait boundary', result.stdout)
        self.assertIn('stopped cleanly', result.stdout)

    def test_real_sigterm_wakes_an_idle_controller(self):
        script = '''
from emberbsd_sdk.package_auto_update import watch_updates
def update():
    print('ready', flush=True)
    return 0
watch_updates(update, 3600)
print('stopped cleanly', flush=True)
'''
        environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'))
        child = subprocess.Popen([sys.executable, '-B', '-c', script], env=environment,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            readable, _, _ = select.select([child.stdout], [], [], 5)
            self.assertTrue(readable, 'automatic updater did not start')
            self.assertEqual(child.stdout.readline(), 'ready\n')
            child.send_signal(signal.SIGTERM)
            output, errors = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 0, errors)
            self.assertIn('stopped cleanly', output)
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate()


class AutomaticClientTests(unittest.TestCase):
    setUp = fixtures.UpdateSelectionTests.setUp
    inventory = fixtures.UpdateSelectionTests.inventory
    run_client = fixtures.UpdateSelectionTests.run_client

    def test_each_cycle_rereads_pkgdb_even_when_channel_is_unchanged(self):
        self.installer.inventory = Mock(side_effect=[
            [dict(pkgname='lib-1', root='devel/lib@native')],
            [dict(pkgname='lib-1', root='devel/lib@native'), dict(pkgname='app-0', root='net/app@native')]])
        def watch(update, interval):
            self.assertEqual(interval, 17)
            for _ in range(2):
                self.assertFalse((self.root / 'state/client.lock').exists())
                self.assertFalse((self.cache.root / '.lock').exists())
                self.assertEqual(update(), 0)
                self.assertFalse((self.root / 'state/client.lock').exists())
                self.assertFalse((self.cache.root / '.lock').exists())
            return 0
        with patch('emberbsd_sdk.package_auto_update.watch_updates', side_effect=watch):
            status, _ = self.run_client(['auto-update', '--interval', '17'])
        self.assertEqual(status, 0)
        self.assertEqual(self.installer.inventory.call_count, 2)
        self.assertEqual(sum(a[0] == 'install' for a in self.installer.actions), 1)
        self.assertEqual(len(self.store.calls), 1)

    def test_unrelated_catalog_change_does_not_install_or_build_packages(self):
        self.inventory([dict(pkgname='lib-1', root='devel/lib@native')])
        def watch(update, interval):
            self.assertEqual(update(), 0)
            self.release['release_id'] = 'new-catalog-content'
            self.catalogs[0]['sets']['task'].append('devel/lib@native')
            self.assertEqual(update(), 0)
            return 0
        with patch('emberbsd_sdk.package_auto_update.watch_updates', side_effect=watch), \
             patch('emberbsd_sdk.sdk_client.run_source') as source:
            status, _ = self.run_client(['auto-update'])
        self.assertEqual(status, 0)
        self.assertEqual(self.installer.inventory.call_count, 2)
        self.assertFalse(any(a[0] == 'install' for a in self.installer.actions))
        self.assertEqual(self.store.calls, [])
        source.assert_not_called()


if __name__ == '__main__':
    unittest.main()
