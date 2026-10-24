# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Native resource exclusion, inherited-session and descendant-drain checks."""

import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from test_core import TemporaryCase, SDK_SOURCE
from emberbsd_sdk import link_launcher
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.process import ContextLocks


@unittest.skipUnless(sys.platform == 'darwin', 'native Darwin launcher contracts')
class LauncherTests(TemporaryCase):
    def argv(self, code, *args):
        return [sys.executable, '-B', str(SDK_SOURCE / 'scripts/link-launcher.py'),
                '--lock-root', str(self.root), '--context', str(self.root / 'link'),
                '--', sys.executable, '-B', '-c', code, *map(str, args)]

    def start(self, code, *args):
        child = subprocess.Popen(self.argv(code, *args), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: child.poll() is None and (child.terminate(), child.communicate(timeout=20)))
        return child

    def ready(self, path):
        end = time.monotonic() + 15
        while time.monotonic() < end:
            if path.exists() and path.stat().st_size:
                return path.read_text()
            time.sleep(.02)
        self.fail('fixture did not become ready')

    def unlocked(self):
        with ContextLocks([str(self.root / 'link')], str(self.root)):
            pass

    def test_stdio_argv_environment_cwd_and_session(self):
        code = ('import os,sys; assert os.getsid(0)==int(sys.argv[1]);'
                'assert os.getpgrp()==os.getpid(); assert os.getcwd()==sys.argv[2];'
                'assert os.environ["LAUNCHER_TEST"]=="inherited";'
                'assert sys.argv[3:]==["", "a b", "$(no shell)"];'
                'sys.stdout.buffer.write(sys.stdin.buffer.read());'
                'sys.stderr.buffer.write(b"err\\x00"); raise SystemExit(7)')
        result = subprocess.run(self.argv(code, os.getsid(0), self.root, '', 'a b', '$(no shell)'),
                                cwd=self.root, env=dict(os.environ, LAUNCHER_TEST='inherited'),
                                input=b'in\xff\x00', stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (7, b'in\xff\x00', b'err\x00'))
        self.unlocked()

    def test_waiter_does_not_start_until_release(self):
        ready = self.root / 'ready'
        with ContextLocks([str(self.root / 'link')], str(self.root)):
            child = self.start('from pathlib import Path;import sys;Path(sys.argv[1]).write_text("started")', ready)
            time.sleep(.3)
            self.assertIsNone(child.poll())
            self.assertFalse(ready.exists())
        out, err = child.communicate(timeout=20)
        self.assertEqual(child.returncode, 0, err)
        self.assertEqual(ready.read_text(), 'started')
        self.unlocked()

    def test_interrupt_waiter_starts_no_command(self):
        ready = self.root / 'must-not-exist'
        with ContextLocks([str(self.root / 'link')], str(self.root)):
            child = self.start('from pathlib import Path;import sys;Path(sys.argv[1]).touch()', ready)
            time.sleep(.3)
            child.terminate()
            out, err = child.communicate(timeout=20)
            self.assertEqual(child.returncode, 143, err)
            self.assertFalse(ready.exists())
        self.unlocked()

    def test_two_commands_are_serial(self):
        log = self.root / 'order'
        code = ('from pathlib import Path;import sys,time; p=Path(sys.argv[1]);'
                'f=p.open("a");f.write(sys.argv[2]+" start\\n");f.flush();'
                'time.sleep(.35);f.write(sys.argv[2]+" end\\n");f.close()')
        first = self.start(code, log, 'a')
        self.ready(log)
        second = self.start(code, log, 'b')
        for child in (first, second):
            out, err = child.communicate(timeout=20)
            self.assertEqual(child.returncode, 0, err)
        self.assertEqual(log.read_text().splitlines(), ['a start', 'a end', 'b start', 'b end'])
        self.unlocked()

    def descendant(self, orphan):
        ready = self.root / 'descendant'
        heartbeat = self.root / 'heartbeat'
        code = '''import os,signal,sys,time
from pathlib import Path
if os.fork()==0:
    signal.signal(signal.SIGTERM,signal.SIG_IGN)
    Path(sys.argv[1]).write_text(str(os.getpid()))
    while True:
        with Path(sys.argv[2]).open('ab') as f: f.write(b'x')
        time.sleep(.02)
while not Path(sys.argv[1]).exists():time.sleep(.01)
'''
        code += 'raise SystemExit(0)\n' if orphan else 'time.sleep(60)\n'
        child = self.start(code, ready, heartbeat)
        pid = int(self.ready(ready))
        if not orphan:
            # The direct child and its TERM-ignoring descendant are in the
            # inherited session, which the outer build controller can see.
            self.assertEqual(os.getsid(pid), os.getsid(0))
            with self.assertRaises(SDKError):
                self.unlocked()
            child.terminate()
        out, err = child.communicate(timeout=25)
        self.assertEqual(child.returncode, 125 if orphan else 143, err)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        before = heartbeat.read_bytes()
        time.sleep(.1)
        self.assertEqual(heartbeat.read_bytes(), before)
        self.unlocked()

    def test_termination_drains_term_ignoring_descendant(self):
        self.descendant(False)

    def test_leader_success_with_live_descendant_is_failure_and_drained(self):
        self.descendant(True)

    def test_command_signal_and_spawn_failure(self):
        result = subprocess.run(self.argv('import os,signal;os.kill(os.getpid(),signal.SIGTERM)'),
                                capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 143, result.stderr)
        with self.assertRaises(FileNotFoundError):
            link_launcher.run(['/no/such/executable'], context=self.root / 'link', lock_root=self.root)
        self.unlocked()

    def test_signal_after_spawn_before_child_assignment_drains(self):
        original = subprocess.Popen
        spawned = []
        def after_spawn(*args, **kwargs):
            child = original(*args, **kwargs)
            if kwargs.get('process_group') == 0:
                spawned.append(child.pid)
                os.kill(os.getpid(), signal.SIGTERM)
            return child

        with patch.object(link_launcher.subprocess, 'Popen', side_effect=after_spawn):
            code = link_launcher.main(['--lock-root', str(self.root), '--context', str(self.root / 'link'),
                                       '--', sys.executable, '-c', 'import time;time.sleep(60)'])
        self.assertEqual(code, 143)
        self.assertEqual(len(spawned), 1)
        with self.assertRaises(ProcessLookupError):
            os.kill(spawned[0], 0)
        self.unlocked()

    def test_signal_on_entry_to_cleanup_does_not_release_early(self):
        original = link_launcher.LinkGroup.stop
        def cleanup(group):
            os.kill(os.getpid(), signal.SIGTERM)
            with self.assertRaises(SDKError):
                self.unlocked()
            original(group)

        with patch.object(link_launcher.LinkGroup, 'stop', cleanup):
            code = link_launcher.main(['--lock-root', str(self.root), '--context', str(self.root / 'link'),
                                       '--', sys.executable, '-c', 'pass'])
        self.assertEqual(code, 143)
        self.unlocked()


if __name__ == '__main__':
    unittest.main()
