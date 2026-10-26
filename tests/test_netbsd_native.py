# SPDX-License-Identifier: BSD-2-Clause
"""Real NetBSD kernel qualification; these tests never qualify other hosts."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import test_core  # Adds the SDK source to sys.path.
from emberbsd_sdk.process import SessionInspector, OwnedProcessGroup, run_logged, sanitized_environment


@unittest.skipUnless(sys.platform == 'netbsd11', 'requires native NetBSD 11')
class NativeNetBSDTests(unittest.TestCase):
    def group(self, code):
        group = OwnedProcessGroup([sys.executable, '-c', code], cwd=os.getcwd(),
            env=sanitized_environment(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        group.start()
        self.addCleanup(group.stop)
        return group

    def wait_exit(self, group):
        deadline = time.monotonic() + 10
        while group.poll() is None:
            if time.monotonic() > deadline: self.fail('producer exit timeout')
            time.sleep(.01)

    def test_preflight_and_negative_session(self):
        inspector = SessionInspector()
        self.assertIn(os.getpid(), inspector.members(os.getsid(0)))
        self.assertEqual(inspector.members(-1), set())
        self.assertEqual(inspector.describe()['backend'], 'netbsd-kqueue-session-v1')

    def test_root_anchor_outlives_exit_with_changed_pgid_child(self):
        group = self.group('import os,time; p=os.fork(); '
            '\nif p: os._exit(0)\nos.setpgid(0,0); time.sleep(30)')
        self.wait_exit(group)
        self.assertIsNone(group.child.process.returncode)
        self.assertTrue(group.alive())
        self.assertNotIn(group.pid, group._members())
        self.assertFalse(group.child.reaped)
        group.stop()
        self.assertTrue(group.child.reaped)
        self.assertFalse(group.alive())

    def test_rapid_fork_exit_tracks_all_births(self):
        group = self.group('import os; '
            '\nfor i in range(60):\n p=os.fork()\n if p==0: os._exit(0)'
            '\nwhile True:\n try: os.wait()\n except ChildProcessError: break')
        self.wait_exit(group)
        group.stop()
        self.assertFalse(group.alive())
        self.assertGreater(group.child.tree.serial, 60)

    def test_ignored_term_deadline_and_kill_leave_foreign_sentinel(self):
        sentinel = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        try:
            group = self.group('import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)')
            time.sleep(.2)
            self.assertFalse(group._drain(.05, signal.SIGTERM))
            self.assertTrue(group._drain(5, signal.SIGKILL))
            self.assertIsNone(sentinel.poll())
        finally:
            sentinel.terminate()
            sentinel.wait()

    def test_cancellation_drains_writer_before_log_closes(self):
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / 'producer.log'
            code = ('import os,time; os.fork(); '
                    '\nwhile True:\n os.write(1,b"owned\\n"); time.sleep(.01)')
            previous = signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
            signal.setitimer(signal.ITIMER_REAL, .5)
            try:
                with self.assertRaises(KeyboardInterrupt):
                    run_logged([sys.executable, '-c', code], log,
                               cwd=folder, env=sanitized_environment())
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, previous)
            before = log.read_bytes()
            time.sleep(.15)
            self.assertEqual(log.read_bytes(), before)
            self.assertTrue(before)

    def test_failed_exec_is_drained(self):
        group = OwnedProcessGroup(['/definitely-missing-sdk-producer'], cwd=os.getcwd(),
            env=sanitized_environment(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        group.start()
        try:
            self.wait_exit(group)
            self.assertEqual(group.poll(), 127)
            group.stop()
            self.assertFalse(group.alive())
        finally:
            group.stop()
