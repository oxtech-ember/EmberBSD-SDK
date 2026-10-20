# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Linux process identity and scoped orphan-reaping contracts."""

import os
import signal
import subprocess
import sys
import time
from unittest import skipUnless
from unittest.mock import patch

from test_core import TemporaryCase
from emberbsd_sdk import process
from emberbsd_sdk.errors import SDKError, ProcessOwnershipError


@skipUnless(sys.platform == 'linux', 'Linux native process profile')
class LinuxSessionTests(TemporaryCase):
    def inspector(self):
        return process.SessionInspector()

    def group(self, code='import time;time.sleep(30)'):
        return process.OwnedProcessGroup(
            [sys.executable, '-B', '-c', code], cwd=self.root,
            env=process.sanitized_environment(), stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)

    def test_native_preflight_and_birth_identity_need_no_ps(self):
        inspector = process.SessionInspector(env={'PATH': str(self.root)})
        self.assertEqual(inspector.describe()['backend'], 'linux-proc-pidfd-session-v1')
        row = inspector.native.read(os.getpid())
        self.assertEqual((row.pid, row.ppid, row.sid, row.uid),
                         (os.getpid(), os.getppid(), os.getsid(0), os.geteuid()))
        self.assertGreater(row.birth, 0)
        self.assertEqual(row.birth, inspector.native.read(os.getpid()).birth)
        self.assertEqual(inspector.members(-1), set())

    def test_stat_command_can_contain_parentheses_and_newlines(self):
        from emberbsd_sdk._linux_process import parse_stat
        data = b'42 (a ) strange\n(name) S 20 42 42 ' + b'0 ' * 15 + b'123 0\n'
        row = parse_stat(data, 42, 501)
        self.assertEqual((row.pid, row.ppid, row.sid, row.uid, row.birth, row.state),
                         (42, 20, 42, 501, 123, b'S'))
        for invalid in (b'', data.replace(b'42 (', b'43 (', 1),
                        data.replace(b'123 0', b'-1 0'), b'42 (x) R 1'):
            with self.subTest(invalid=invalid), self.assertRaises(SDKError):
                parse_stat(invalid, 42, 501)

    def test_vanished_snapshot_entry_cannot_prove_empty(self):
        inspector = self.inspector()
        with patch.object(inspector, '_rows', return_value={99999999: None}):
            self.assertEqual(inspector._scan(42), (set(), True))

    def test_birth_in_snapshot_tick_or_later_cannot_prove_foreign(self):
        from emberbsd_sdk._linux_process import ProcessRow
        inspector = self.inspector()
        for birth in (200, 201):
            row = ProcessRow(10, 20, 7, os.geteuid(), birth, b'R')
            with self.subTest(birth=birth), patch.object(inspector.native, 'now', return_value=200), patch.object(inspector, '_rows', return_value={10: row}):
                self.assertEqual(inspector._scan(42), (set(), True))

    def test_coherent_foreign_row_needs_no_second_pid_lookup(self):
        from emberbsd_sdk._linux_process import ProcessRow
        inspector = self.inspector()
        row = ProcessRow(10, 20, 7, os.geteuid(), 100, b'R')
        with patch.object(inspector.native, 'now', return_value=200), patch.object(inspector, '_rows', return_value={10: row}), patch.object(process.os, 'getsid', side_effect=AssertionError('racy lookup')):
            self.assertEqual(inspector._scan(42), (set(), False))

    def test_zombie_and_new_owned_members_are_not_false_emptiness(self):
        from emberbsd_sdk._linux_process import ProcessRow
        inspector = self.inspector()
        for state, birth in ((b'Z', 100), (b'R', 200)):
            row = ProcessRow(10, 42, 42, os.geteuid(), birth, state)
            with self.subTest(state=state), patch.object(inspector.native, 'now', return_value=200), patch.object(inspector, '_rows', return_value={10: row}):
                self.assertEqual(inspector._scan(42), ({10}, False))

    def test_visible_changed_uid_is_refused(self):
        from emberbsd_sdk._linux_process import ProcessRow
        inspector = self.inspector()
        row = ProcessRow(10, 42, 42, os.geteuid() + 1, 100, b'R')
        with patch.object(inspector, '_rows', return_value={10: row}), self.assertRaisesRegex(SDKError, 'effective UID'):
            inspector._scan(42)

    def test_missing_proc_refuses_before_launch_or_log(self):
        marker, log = self.root / 'started', self.root / 'log'
        with patch('emberbsd_sdk._linux_process.PROC_ROOT', str(self.root / 'missing')):
            with self.assertRaisesRegex(SDKError, 'proc'):
                process.run_logged([sys.executable, '-c', f'open({str(marker)!r},"w").close()'], log)
        self.assertFalse(marker.exists())
        self.assertFalse(log.exists())

    def test_supervisors_do_not_change_controller_subreaper(self):
        from emberbsd_sdk._linux_process import subreaper
        before = subreaper.get()
        first, second = self.group(), self.group()
        try:
            self.assertEqual(subreaper.get(), before)
            first.start()
            self.assertEqual(subreaper.get(), before)
            second.start()
            first.stop()
            self.assertEqual(subreaper.get(), before)
            second.stop()
            self.assertEqual(subreaper.get(), before)
        finally:
            first.stop()
            second.stop()

    def test_launch_failure_restores_subreaper(self):
        from emberbsd_sdk._linux_process import subreaper
        before = subreaper.get()
        group = self.group()
        group.argv = [str(self.root / 'missing')]
        with self.assertRaises(FileNotFoundError):
            group.start()
        self.assertEqual(subreaper.get(), before)

    def test_existing_subreaper_setting_is_retained(self):
        from emberbsd_sdk._linux_process import subreaper
        before = subreaper.get()
        group = self.group('pass')
        try:
            subreaper._call(36, 1)
            group.start()
            group.stop()
            self.assertEqual(subreaper.get(), 1)
        finally:
            group.stop()
            subreaper._call(36, before)

    def test_proc_permission_failure_is_not_absence(self):
        inspector = self.inspector()
        with patch.object(inspector.native, '_read_file', side_effect=PermissionError('denied')):
            with self.assertRaisesRegex(SDKError, 'cannot read Linux proc identity'):
                inspector.native.read(os.getpid())

    def test_missing_pidfd_refuses_before_launch_or_log(self):
        marker, log = self.root / 'started', self.root / 'log'
        original = os.pidfd_open
        try:
            del os.pidfd_open
            with self.assertRaisesRegex(SDKError, 'requires pidfd'):
                process.run_logged([sys.executable, '-c', f'open({str(marker)!r},"w").close()'], log)
        finally:
            os.pidfd_open = original
        self.assertFalse(marker.exists())
        self.assertFalse(log.exists())

    def test_foreign_child_is_neither_signalled_nor_reaped(self):
        group = self.group('pass')
        foreign = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'])
        try:
            group.start()
            group._signal(foreign.pid, signal.SIGKILL)
            self.assertIsNone(foreign.poll())
            foreign.kill()
            # Keep this direct child as a zombie while the owned session drains.
            time.sleep(.05)
            group.stop()
            self.assertEqual(foreign.wait(timeout=5), -signal.SIGKILL)
        finally:
            if foreign.poll() is None:
                foreign.kill()
                foreign.wait()
            group.stop()

    def test_owned_orphan_zombie_is_reaped_before_stop_returns(self):
        pidfile = self.root / 'orphan'
        code = f'''import os,time
from pathlib import Path
if os.fork()==0:
 Path({str(pidfile)!r}).write_text(str(os.getpid()))
 time.sleep(30)
 os._exit(0)
while not Path({str(pidfile)!r}).exists(): time.sleep(.005)
os._exit(0)
'''
        group = self.group(code)
        group.start()
        supervisor = None
        try:
            group.child.wait(timeout=5)
            supervisor = group.child.supervisor
            pid = int(pidfile.read_text())
            # Hold the reaper so the zombie membership check is deterministic.
            supervisor.send_signal(signal.SIGSTOP)
            os.kill(pid, signal.SIGKILL)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                row = group.inspector.native.read(pid)
                if row is not None and row.state == b'Z':
                    break
                time.sleep(.01)
            else:
                self.fail('owned orphan did not become a zombie')
            self.assertEqual(row.ppid, supervisor.pid)
            self.assertTrue(group.alive())
            supervisor.send_signal(signal.SIGCONT)
            group.stop()
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
        finally:
            if supervisor is not None and supervisor.poll() is None:
                supervisor.send_signal(signal.SIGCONT)
            group.stop()

    def test_unrelated_orphan_is_not_adopted_by_controller(self):
        from emberbsd_sdk._linux_process import subreaper
        from test_core import SDK_SOURCE
        before = subreaper.get()
        driver = '''import os,subprocess,sys,time
sys.path.insert(0,sys.argv[1])
from emberbsd_sdk import process
from emberbsd_sdk._linux_process import subreaper
assert subreaper.get() == 0
p=process.OwnedProcessGroup([sys.executable,'-c','import time;time.sleep(30)'],cwd=None,env=None,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
p.start()
f=subprocess.Popen([sys.executable,'-c','import os,time;pid=os.fork();print(pid,flush=True) if pid else time.sleep(.5)'],stdout=subprocess.PIPE)
pid=int(f.stdout.readline());f.stdout.close();f.wait()
row=p.inspector.native.read(pid)
print(pid,row.ppid,flush=True)
p.stop()
try: os.waitpid(pid,0)
except ChildProcessError: pass
'''
        orphan = None
        try:
            # This harness reaps the foreign orphan if the inner controller
            # correctly leaves its process-wide subreaper setting unchanged.
            subreaper._call(36, 1)
            result = subprocess.run([sys.executable, '-B', '-I', '-S', '-c', driver, str(SDK_SOURCE / 'src')], capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            orphan, parent = map(int, result.stdout.split())
            self.assertEqual(parent, os.getpid())
        finally:
            if orphan is not None:
                try: os.waitpid(orphan, 0)
                except ChildProcessError: pass
            subreaper._call(36, before)

    def test_supervisor_death_keeps_shutdown_unproven(self):
        from emberbsd_sdk._linux_process import subreaper
        before = subreaper.get()
        group = self.group()
        try:
            # Adopt the test's producer after deliberately killing its reaper.
            # This setting belongs only to this cleanup harness.
            subreaper._call(36, 1)
            group.start()
            group.child.supervisor.kill()
            group.child.supervisor.wait(timeout=5)
            with self.assertRaisesRegex(ProcessOwnershipError, 'supervisor failed'):
                group.poll()
            with self.assertRaisesRegex(ProcessOwnershipError, 'supervisor failed'):
                group.stop()
        finally:
            if group.pid is not None:
                producer = group.child.producer_pid
                group.inspector.native.signal(producer, group.pid, signal.SIGKILL)
                try: os.waitpid(producer, 0)
                except ChildProcessError: pass
            subreaper._call(36, before)

    def test_supervisor_death_before_pid_report_keeps_session_owned(self):
        from emberbsd_sdk import _linux_process as native
        before = native.subreaper.get()
        fixture = self.root / 'helper'
        fixture.mkdir()
        pidfile = self.root / 'early-producer'
        # A real helper death after spawning, before any protocol output.
        (fixture / '_linux_supervisor.py').write_text(
            'import os,signal,subprocess,sys,time\n'
            'from pathlib import Path\n'
            'subprocess.Popen(sys.argv[2:])\n'
            f'while not Path({str(pidfile)!r}).exists(): time.sleep(.005)\n'
            'os.kill(os.getpid(),signal.SIGKILL)\n')
        code = (
            'import os,time\nfrom pathlib import Path\n'
            f'Path({str(pidfile)!r}).write_text(str(os.getpid()))\n'
            'time.sleep(30)\n')
        group = self.group(code)
        try:
            native.subreaper._call(36, 1)
            with patch.object(native, '__file__', str(fixture / '_linux_process.py')):
                with self.assertRaisesRegex(ProcessOwnershipError, 'supervisor'):
                    group.start()
            self.assertIsNotNone(group.pid)
            producer = int(pidfile.read_text())
            self.assertEqual(os.getsid(producer), group.pid)
            with self.assertRaises(ProcessOwnershipError):
                group.stop()
        finally:
            if pidfile.exists():
                producer = int(pidfile.read_text())
                os.kill(producer, signal.SIGKILL)
                os.waitpid(producer, 0)
            native.subreaper._call(36, before)

    def test_system_exit_during_startup_drains_known_session(self):
        from emberbsd_sdk import _linux_process as native
        pidfile = self.root / 'interrupted-producer'
        code = (
            'import os,time\nfrom pathlib import Path\n'
            f'Path({str(pidfile)!r}).write_text(str(os.getpid()))\n'
            'time.sleep(30)\n')
        original = native.LinuxChild._receive
        interrupted = False
        def receive(child):
            nonlocal interrupted
            if not interrupted:
                deadline = time.monotonic() + 5
                while not pidfile.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(pidfile.exists())
                interrupted = True
                # Ports uses SystemExit for its signal policy.
                raise SystemExit(143)
            return original(child)
        with patch.object(native.LinuxChild, '_receive', receive):
            with self.assertRaises(SystemExit) as failure:
                process.run_logged([sys.executable, '-c', code], self.root / 'interrupted.log')
        self.assertEqual(failure.exception.code, 143)
        self.assertTrue(interrupted)
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)

    def test_foreign_exit_after_coherent_row_does_not_block_empty_session(self):
        inspector = self.inspector()
        for _ in range(5):
            child = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'])
            try:
                # A process from an earlier clock tick predates this snapshot.
                birth = inspector.native.read(child.pid).birth
                deadline = time.monotonic() + 5
                while inspector.native.now() <= birth and time.monotonic() < deadline:
                    time.sleep(.001)
                self.assertGreater(inspector.native.now(), birth)
                original = inspector._rows
                injected = False
                def rows():
                    nonlocal injected
                    result = original()
                    if not injected:
                        self.assertIn(child.pid, result)
                        child.kill()
                        child.wait(timeout=5)
                        injected = True
                    return result
                with patch.object(inspector, '_rows', side_effect=rows):
                    self.assertEqual(inspector.members(-1), set())
                self.assertTrue(injected)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait()
