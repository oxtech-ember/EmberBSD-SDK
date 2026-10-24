# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Owned producer lifetime and retained-log regression tests."""

import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from unittest import skipUnless
from unittest.mock import Mock, patch

from test_core import TemporaryCase, SDK_SOURCE
from emberbsd_sdk import process
from emberbsd_sdk.errors import ProcessOwnershipError, SDKError


class ProcessTests(TemporaryCase):
    def wait_pid(self, path):
        end = time.monotonic() + 20
        while time.monotonic() < end:
            if path.is_file() and path.stat().st_size:
                return int(path.read_text())
            time.sleep(0.05)
        self.fail('owned fixture did not create its PID file')

    def assert_stopped(self, pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        self.fail(f'owned fixture process still exists: {pid}')

    def producer(self, orphan=None):
        child = self.root / 'child.pid'; writer = self.root / 'writer'
        code = f'''import os,signal,time
from pathlib import Path
if os.fork() == 0:
    signal.signal(signal.SIGTERM,signal.SIG_IGN)
    Path({str(child)!r}).write_text(str(os.getpid()))
    while True:
        with Path({str(writer)!r}).open('ab') as f: f.write(b'writing\\n')
        time.sleep(.03)
while not Path({str(child)!r}).is_file(): time.sleep(.01)
'''
        code += f'raise SystemExit({orphan})\n' if orphan is not None else 'time.sleep(60)\n'
        return [sys.executable, '-B', '-c', code]

    def test_logged_success_failure_exclusive_and_append(self):
        log = self.root / 'producer.log'
        result = process.run_logged([sys.executable, '-c', 'import sys;print("out");print("err",file=sys.stderr)'], log, cwd=self.root, env={})
        self.assertEqual(result.returncode, 0)
        self.assertIn(b'out', log.read_bytes()); self.assertIn(b'err', log.read_bytes())
        original = log.read_bytes()
        with self.assertRaises(FileExistsError):
            process.run_logged([sys.executable, '-c', 'pass'], log)
        self.assertEqual(log.read_bytes(), original)
        with self.assertRaisesRegex(SDKError, 'command failed'):
            process.run_logged([sys.executable, '-c', 'print("failed");raise SystemExit(7)'], log, append=True)
        self.assertIn(b'failed', log.read_bytes())
        link = self.root / 'alias'; link.symlink_to(log)
        with self.assertRaises(SDKError):
            process.run_logged(['unused'], link, append=True)
        with self.assertRaises(SDKError):
            process.run_logged(['unused'], self.root / 'missing', append=True)

    def test_exited_success_and_failure_drain_descendants(self):
        for status in (0, 7):
            with self.subTest(status=status), self.assertRaisesRegex(SDKError, 'live descendants'):
                process.run_logged(self.producer(status), self.root / f'producer-{status}.log', cwd=self.root, env=process.sanitized_environment(), **({'empty_snapshot_timeout': 30} if status == 7 else {}))
            child = int((self.root / 'child.pid').read_text()); self.assert_stopped(child)
            writer = self.root / 'writer'; before = writer.read_bytes(); time.sleep(.15)
            self.assertEqual(writer.read_bytes(), before)
            (self.root / 'child.pid').unlink()

    def test_interruption_drains_descendants_before_return(self):
        argv = self.producer()
        driver = ('import signal,sys;sys.dont_write_bytecode=True;sys.path.insert(0,sys.argv[1]);'
                  'from emberbsd_sdk.process import run_logged,sanitized_environment\n'
                  'def interrupt(signum,frame): raise KeyboardInterrupt("TERM")\n'
                  'signal.signal(signal.SIGTERM,interrupt)\n'
                  'try: run_logged(sys.argv[3:],sys.argv[2],env=sanitized_environment())\n'
                  'except KeyboardInterrupt: raise SystemExit(130)\n')
        child = subprocess.Popen([sys.executable, '-B', '-I', '-S', '-c', driver, str(SDK_SOURCE / 'src'), str(self.root / 'interrupted.log'), *argv], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: child.poll() is None and (child.kill(), child.wait()))
        descendant = self.wait_pid(self.root / 'child.pid')
        child.send_signal(signal.SIGTERM)
        out, err = child.communicate(timeout=20)
        self.assertEqual(child.returncode, 130, err)
        self.assert_stopped(descendant)
        writer = self.root / 'writer'; before = writer.read_bytes(); time.sleep(.15)
        self.assertEqual(writer.read_bytes(), before)

    @skipUnless(sys.platform in ('darwin', 'linux'), 'qualified session backend')
    def test_foreign_uid_controller_session_peer_is_not_a_build(self):
        # A CI runner, sudo or systemd process of another user may share the
        # controller session. No build exists yet, so it is no ownership change.
        peer, controller_sid, euid = 2147483646, os.getsid(0), os.geteuid()
        real_getsid = os.getsid
        if sys.platform == 'linux':
            from emberbsd_sdk import _linux_process as native
            real_rows = native.LinuxSession.rows
            row = native.ProcessRow(peer, os.getpid(), controller_sid, euid + 1, 1, b'S')
            def rows(session):
                return {**real_rows(session), peer: row}
            injected = patch.object(native.LinuxSession, 'rows', rows)
        else:
            real_rows = process.SessionInspector._rows
            def rows(inspector):
                return {**real_rows(inspector), peer: (os.getpid(), euid + 1, b'S')}
            injected = patch.object(process.SessionInspector, '_rows', rows)
        def getsid(pid):
            return controller_sid if pid == peer else real_getsid(pid)
        marker, log = self.root / 'built', self.root / 'build.log'
        with injected, patch.object(process.os, 'getsid', getsid):
            inspector = process.SessionInspector()
            self.assertIn(os.getpid(), inspector.members(controller_sid))
            process.run_logged([sys.executable, '-B', '-c', f'open({str(marker)!r},"w").close()'], log)
        self.assertTrue(marker.is_file())

    def test_non_posix_refusal_precedes_log_creation(self):
        with patch.object(process.os, 'name', 'nt'), self.assertRaisesRegex(SDKError, 'POSIX'):
            process.run_logged(['unused'], self.root / 'log')
        self.assertFalse((self.root / 'log').exists())

    def test_invalid_runtime_timeout_precedes_backend_log_and_start(self):
        for value in (True, False, 0, -1, float('nan'), float('inf'), -float('inf'), '1', 10 ** 10000):
            with self.subTest(kind=type(value).__name__), patch.object(process, 'OwnedProcessGroup') as group:
                with self.assertRaisesRegex(SDKError, 'timeout must be finite and positive'):
                    process.run_logged(['unused'], self.root / 'invalid-timeout.log', timeout_seconds=value)
                group.assert_not_called()
                self.assertFalse((self.root / 'invalid-timeout.log').exists())

    def test_default_runtime_timeout_does_not_add_a_clock_or_change_success(self):
        group = Mock(pid=1234567, options={})
        group.poll.side_effect = [None, 0]
        group.alive.return_value = False
        with patch.object(process, 'OwnedProcessGroup', return_value=group), \
                patch.object(process.time, 'monotonic', side_effect=AssertionError('no default deadline')), \
                patch.object(process.time, 'sleep'):
            result = process.run_logged(['fixture'], self.root / 'default-timeout.log')
        self.assertEqual(result.returncode, 0)
        group.stop.assert_called_once_with()

    def test_runtime_timeout_uses_monotonic_deadline_then_drains_before_return(self):
        group = Mock(pid=1234567, options={})
        group.poll.return_value = None
        events = []
        def start():
            group.options['stdout'].write(b'retained combined fixture log\n')
        group.start.side_effect = start
        group.stop.side_effect = lambda: events.append('drained')
        log = self.root / 'deadline.log'
        with patch.object(process, 'OwnedProcessGroup', return_value=group), \
                patch.object(process.time, 'monotonic', side_effect=[10.0, 10.5, 11.0]), \
                patch.object(process.time, 'sleep') as sleep:
            with self.assertRaisesRegex(SDKError, 'runtime timeout.*deadline.log'):
                process.run_logged(['fixture'], log, timeout_seconds=1)
            events.append('returned')
        self.assertEqual(events, ['drained', 'returned'])
        self.assertEqual(sleep.call_count, 1)
        self.assertEqual(log.read_bytes(), b'retained combined fixture log\n')
        self.assertTrue(group.options['stdout'].closed)

    def test_runtime_timeout_refuses_late_success_that_unbounded_mode_accepts(self):
        for limit in (None, 1):
            with self.subTest(limit=limit):
                clock = [10.0]
                group = Mock(pid=1234567, options={})
                group.poll.side_effect = [None, None, 0]
                group.alive.return_value = False
                def advance(seconds):
                    clock[0] += 1.0
                with patch.object(process, 'OwnedProcessGroup', return_value=group), \
                        patch.object(process.time, 'monotonic', side_effect=lambda: clock[0]), \
                        patch.object(process.time, 'sleep', side_effect=advance):
                    if limit is None:
                        self.assertEqual(process.run_logged(['fixture'], self.root / 'unbounded.log').returncode, 0)
                        self.assertEqual(clock[0], 12.0)
                    else:
                        with self.assertRaisesRegex(SDKError, 'runtime timeout'):
                            process.run_logged(['fixture'], self.root / 'bounded.log', timeout_seconds=limit)
                        self.assertEqual(clock[0], 11.0)
                group.stop.assert_called_once_with()

    def test_runtime_timeout_refuses_completion_first_observed_after_deadline(self):
        for slow_start in (False, True):
            with self.subTest(slow_start=slow_start):
                clock = [10.0]
                group = Mock(pid=1234567, options={})
                group.poll.side_effect = [0] if slow_start else [None, 0]
                def advance(*args):
                    clock[0] = 12.0
                if slow_start:
                    group.start.side_effect = advance
                log = self.root / ('slow-start.log' if slow_start else 'late-completion.log')
                with patch.object(process, 'OwnedProcessGroup', return_value=group), \
                        patch.object(process.time, 'monotonic', side_effect=lambda: clock[0]), \
                        patch.object(process.time, 'sleep', side_effect=advance):
                    with self.assertRaisesRegex(SDKError, 'runtime timeout'):
                        process.run_logged(['fixture'], log, timeout_seconds=1)
                group.stop.assert_called_once_with()
                group.alive.assert_not_called()

    def test_runtime_timeout_preserves_ownership_failure_and_its_log(self):
        log = self.root / 'deadline-hold.log'
        original = ProcessOwnershipError(1234567, 'incomplete timeout drain')
        group = Mock(pid=original.sid, options={})
        group.poll.side_effect = [None, 0]
        group.stop.side_effect = original
        with patch.object(process, 'OwnedProcessGroup', return_value=group), \
                patch.object(process.time, 'monotonic', side_effect=[10.0, 11.0, 12.0]):
            with self.assertRaises(ProcessOwnershipError) as raised:
                process.run_logged(['fixture'], log, timeout_seconds=1)
        self.assertEqual((raised.exception.sid, raised.exception.reason, raised.exception.log),
                         (original.sid, original.reason, str(log)))
        self.assertIs(raised.exception.__cause__, original)
        self.assertIn('runtime timeout', str(original.__context__))

    def test_runtime_timeout_drains_real_term_ignoring_descendant(self):
        log = self.root / 'real-timeout.log'
        argv = self.producer()
        argv[-1] = 'print("retained timeout fixture", flush=True)\n' + argv[-1]
        with self.assertRaisesRegex(SDKError, 'runtime timeout'):
            process.run_logged(argv, log, env=process.sanitized_environment(), timeout_seconds=.5)
        child = int((self.root / 'child.pid').read_text())
        self.assert_stopped(child)
        writer = self.root / 'writer'
        before = writer.read_bytes()
        time.sleep(.15)
        self.assertEqual(writer.read_bytes(), before)
        self.assertIn(b'retained timeout fixture', log.read_bytes())

    @skipUnless(sys.platform == 'darwin', 'requires native Darwin exit records')
    def test_stop_failure_reaps_leader_that_exits_during_inspection(self):
        from emberbsd_sdk._darwin_process import DarwinIdentity
        original_group = process.OwnedProcessGroup
        original_members = process.SessionInspector.members
        original_sleep = time.sleep
        groups = []
        interrupted = False
        reason = 'forced incomplete snapshot after owned leader exit'
        log = self.root / 'stop-race.log'

        class ObservedGroup(original_group):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                groups.append(self)

        def inspect(inspector, sid):
            if not groups or sid != groups[0].pid:
                return original_members(inspector, sid)
            # The stop loop polled before this call. Exit inside the scan,
            # without waiting for the child or treating any row as absent.
            os.kill(sid, signal.SIGTERM)
            identity = DarwinIdentity()
            deadline = time.monotonic() + 5
            while True:
                lifetime = identity.lifetime(sid)
                if lifetime is not None and lifetime[1]:
                    break
                self.assertLess(time.monotonic(), deadline, 'owned leader did not exit')
                original_sleep(.01)
            raise SDKError(reason)

        def interrupt(signum, frame):
            raise KeyboardInterrupt('requested stop')

        def sleep(seconds):
            nonlocal interrupted
            if groups and groups[0].pid is not None and not interrupted:
                interrupted = True
                signal.raise_signal(signal.SIGTERM)
            original_sleep(seconds)

        previous = signal.signal(signal.SIGTERM, interrupt)
        try:
            with patch.object(process, 'OwnedProcessGroup', ObservedGroup), \
                    patch.object(process.SessionInspector, 'members', inspect), \
                    patch.object(process.time, 'sleep', sleep):
                with self.assertRaises(ProcessOwnershipError) as raised:
                    process.run_logged([sys.executable, '-B', '-c',
                                        'import time;time.sleep(60)'], log,
                                       empty_snapshot_timeout=30)
            pid = groups[0].pid
            error = raised.exception
            self.assertTrue(interrupted)
            self.assertEqual((error.sid, error.reason, error.log), (pid, reason, str(log)))
            self.assertIsInstance(error.__cause__, ProcessOwnershipError)
            self.assertEqual((error.__cause__.sid, error.__cause__.reason), (pid, reason))
            # On the old code this call reaps the retained zombie and fails
            # the assertion. The test therefore also cleans up its baseline.
            with self.assertRaises(ChildProcessError):
                os.waitpid(pid, os.WNOHANG)
        finally:
            signal.signal(signal.SIGTERM, previous)
            if groups and groups[0].child is not None:
                child = groups[0].child
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)

    def test_stop_failure_reap_error_preserves_original_ownership_error(self):
        for failure in (SDKError('poll failed'), KeyboardInterrupt('poll interrupted')):
            with self.subTest(failure=type(failure).__name__):
                log = self.root / (type(failure).__name__ + '.log')
                original = ProcessOwnershipError(1234567, 'original ownership reason', log=str(log))
                group = Mock(pid=original.sid, options={})
                group.poll.side_effect = [0, failure]
                group.alive.return_value = False
                group.stop.side_effect = original
                with patch.object(process, 'OwnedProcessGroup', return_value=group):
                    with self.assertRaises(ProcessOwnershipError) as raised:
                        process.run_logged(['fixture'], log)
                error = raised.exception
                self.assertEqual((error.sid, error.reason, error.log),
                                 (original.sid, original.reason, original.log))
                self.assertIs(error.__cause__, original)
                self.assertEqual(group.poll.call_count, 2)

    def test_stop_failure_retries_only_pending_leader_and_preserves_error(self):
        log = self.root / 'pending-leader.log'
        original = ProcessOwnershipError(1234567, 'original ownership reason', log=str(log))
        group = Mock(pid=original.sid, options={})
        group.poll.side_effect = [0, None, None, -signal.SIGTERM]
        group.alive.return_value = False
        group.stop.side_effect = original
        with patch.object(process, 'OwnedProcessGroup', return_value=group), \
                patch.object(process.time, 'sleep'):
            with self.assertRaises(ProcessOwnershipError) as raised:
                process.run_logged(['fixture'], log)
        error = raised.exception
        self.assertEqual((error.sid, error.reason, error.log), (original.sid, original.reason, original.log))
        self.assertIs(error.__cause__, original)
        self.assertEqual(group.poll.call_count, 4)
        group.stop.assert_called_once_with()
        group._signal.assert_not_called()

    def test_stop_failure_pending_leader_cleanup_stops_at_deadline(self):
        log = self.root / 'pending-deadline.log'
        original = ProcessOwnershipError(1234567, 'original ownership reason', log=str(log))
        group = Mock(pid=original.sid, options={})
        group.poll.side_effect = [0, None, None, None]
        group.alive.return_value = False
        group.stop.side_effect = original
        with patch.object(process, 'OwnedProcessGroup', return_value=group), \
                patch.object(process.time, 'monotonic', side_effect=[10.0, 10.0, 10.5, 11.0]), \
                patch.object(process.time, 'sleep') as sleep:
            with self.assertRaises(ProcessOwnershipError) as raised:
                process.run_logged(['fixture'], log)
        error = raised.exception
        self.assertEqual((error.sid, error.reason, error.log), (original.sid, original.reason, original.log))
        self.assertIs(error.__cause__, original)
        self.assertEqual(group.poll.call_count, 4)
        self.assertEqual(sleep.call_count, 2)
        group.stop.assert_called_once_with()
        group._signal.assert_not_called()


class ProducerStatusTests(TemporaryCase):
    def test_capture_and_logged_preserve_status_97(self):
        from emberbsd_sdk.errors import CommandError
        argv = [sys.executable, '-c', 'import sys;sys.stdout.buffer.write(b"out\\xff");sys.stderr.buffer.write(b"err\\x00");raise SystemExit(97)']
        with self.assertRaises(CommandError) as captured:
            process.capture(argv)
        error = captured.exception
        self.assertEqual((error.returncode, error.argv, error.stdout, error.stderr), (97, tuple(argv), b'out\xff', b'err\x00'))
        with self.assertRaises(CommandError) as logged:
            process.run_logged(argv, self.root / 'failure.log')
        self.assertEqual((logged.exception.returncode, logged.exception.argv), (97, tuple(argv)))
        self.assertIn(b'out\xff', (self.root / 'failure.log').read_bytes())
        self.assertEqual(logged.exception.log, str(self.root / 'failure.log'))

    def test_capture_preserves_signal_status(self):
        from emberbsd_sdk.errors import CommandError
        with self.assertRaises(CommandError) as captured:
            process.capture([sys.executable, '-c', 'import os,signal;os.kill(os.getpid(),signal.SIGTERM)'])
        self.assertEqual(captured.exception.returncode, -signal.SIGTERM)
