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
from unittest.mock import patch

from test_core import TemporaryCase, SDK_SOURCE
from emberbsd_sdk import process
from emberbsd_sdk.errors import SDKError


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
                process.run_logged(self.producer(status), self.root / f'producer-{status}.log', cwd=self.root, env=process.sanitized_environment())
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
