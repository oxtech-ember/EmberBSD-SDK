# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Keep all process groups of one owned session inside its lock lifetime."""

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


class SessionTests(TemporaryCase):
    def wait_file(self, path):
        end = time.monotonic() + 20
        while time.monotonic() < end:
            if path.is_file() and path.stat().st_size:
                return path.read_text()
            time.sleep(.02)
        self.fail(f'fixture did not create {path}')

    def stop_fixture(self, pid, sid):
        try:
            if os.getsid(pid) != sid:
                return
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try: os.kill(pid, 0)
            except ProcessLookupError: return
            time.sleep(.03)
        self.fail(f'fixture process did not disappear: {pid}')

    def run_case(self, name, status=None, sent_signal=None):
        root = self.root / name; root.mkdir()
        pidfile, writer = root / 'writer.pid', root / 'writes'
        producer = f'''import os,signal,time
from pathlib import Path
if os.fork() == 0:
    os.setpgid(0,0)
    signal.signal(signal.SIGTERM,signal.SIG_IGN)
    Path({str(pidfile)!r}).write_text(str(os.getpid()))
    deadline=time.monotonic()+30
    while time.monotonic()<deadline:
        with Path({str(writer)!r}).open('ab') as f: f.write(b'x')
        time.sleep(.02)
    os._exit(0)
while not Path({str(pidfile)!r}).exists(): time.sleep(.01)
Path({str(root / 'leader.ready')!r}).write_text(str(os.getsid(0)))
'''
        producer += f'os._exit({status})\n' if status is not None else 'time.sleep(30)\n'
        driver = '''import signal,sys
sys.dont_write_bytecode=True
sys.path.insert(0,sys.argv[1])
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.process import ContextLocks,run_logged,sanitized_environment
def interrupt(signum,frame): raise KeyboardInterrupt(signal.Signals(signum).name)
for number in (signal.SIGTERM,signal.SIGHUP): signal.signal(number,interrupt)
try:
    with ContextLocks([sys.argv[2]+'/context'],sys.argv[2]):
        run_logged(sys.argv[4:],sys.argv[3],env=sanitized_environment())
except KeyboardInterrupt: raise SystemExit(130)
except SDKError as exc:
    print(str(exc),file=sys.stderr)
    raise SystemExit(1)
'''
        child = subprocess.Popen([sys.executable, '-B', '-I', '-S', '-c', driver, str(SDK_SOURCE / 'src'), str(root), str(root / 'producer.log'), sys.executable, '-B', '-c', producer], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: child.poll() is None and (child.kill(), child.wait()))
        pid = int(self.wait_file(pidfile))
        leader = int(self.wait_file(root / 'leader.ready'))
        self.addCleanup(self.stop_fixture, pid, leader)
        self.assertEqual(os.getsid(pid), leader)
        self.assertEqual(os.getpgid(pid), pid)
        self.assertNotEqual(pid, leader)
        if sent_signal is not None:
            child.send_signal(sent_signal)
        # A TERM-ignoring member remains active during the grace period.
        time.sleep(.1)
        with self.assertRaisesRegex(SDKError, 'context is busy'):
            process.ContextLocks([str(root / 'context')], root).acquire()
        out, err = child.communicate(timeout=20)
        self.assertEqual(child.returncode, 130 if sent_signal is not None else 1, err)
        if sent_signal is None:
            self.assertIn(b'live descendants', err)
        try: os.kill(pid, 0)
        except ProcessLookupError: pass
        else: self.fail('writer survived the owned-session drain')
        before = writer.read_bytes(); time.sleep(.15)
        self.assertEqual(writer.read_bytes(), before)
        with process.ContextLocks([str(root / 'context')], root):
            pass

    def test_exit_zero_and_seven_drain_new_process_groups(self):
        for status in (0, 7):
            with self.subTest(status=status):
                self.run_case(f'exit-{status}', status=status)

    def test_term_and_hup_drain_new_process_groups(self):
        for number in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=number):
                self.run_case(f'signal-{number}', sent_signal=number)

    @skipUnless(sys.platform == 'darwin', 'Darwin base-system ps prerequisite')
    def test_missing_ps_refuses_before_log_or_launch(self):
        marker = self.root / 'started'
        with self.assertRaisesRegex(SDKError, 'ps'):
            process.run_logged([sys.executable, '-c', f'open({str(marker)!r},"w").close()'], self.root / 'log', env={'PATH': str(self.root / 'missing')})
        self.assertFalse(marker.exists())
        self.assertFalse((self.root / 'log').exists())

    @skipUnless(sys.platform == 'darwin', 'Darwin base-system ps prerequisite')
    def test_unsupported_ps_output_refuses_before_start(self):
        directory = self.root / 'bin'; directory.mkdir()
        ps = directory / 'ps'; ps.write_text('#!/bin/sh\nprintf "unsupported\\n"\n'); ps.chmod(0o755)
        with self.assertRaisesRegex(SDKError, 'enumerat|PID'):
            process.OwnedProcessGroup(['unused'], cwd=self.root, env={'PATH': str(directory)}, stdout=None, stderr=None)

    def test_inspection_failure_is_typed_after_start(self):
        from emberbsd_sdk.errors import ProcessOwnershipError
        group = process.OwnedProcessGroup([sys.executable, '-c', 'import time;time.sleep(30)'], cwd=self.root, env=process.sanitized_environment(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        group.start()
        try:
            with patch.object(group.inspector, 'members', side_effect=SDKError('injected inspection failure')):
                with self.assertRaises(ProcessOwnershipError) as failure:
                    group.alive()
                self.assertEqual(failure.exception.sid, group.pid)
        finally:
            group.stop()

    def test_log_identifies_unproven_drain(self):
        from emberbsd_sdk.errors import ProcessOwnershipError
        original = process.OwnedProcessGroup.stop
        def unproven(group):
            original(group)
            raise ProcessOwnershipError(group.pid, 'injected unproven drain')
        log = self.root / 'log'
        with patch.object(process.OwnedProcessGroup, 'stop', unproven):
            with self.assertRaises(ProcessOwnershipError) as failure:
                process.run_logged([sys.executable, '-c', 'pass'], log)
        self.assertIsInstance(failure.exception.sid, int)
        self.assertEqual(failure.exception.log, str(log))

    def test_empty_session_requires_a_complete_scan(self):
        inspector = object.__new__(process.SessionInspector)
        inspector.empty_snapshot_timeout = 2
        with patch.object(inspector, '_scan', side_effect=[(set(), True), ({123}, False)]):
            self.assertEqual(inspector.members(42), {123})
