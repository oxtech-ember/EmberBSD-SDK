# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Refuse identity races without treating foreign process churn as owned work."""

import os
import signal
import subprocess
import sys
import time
from unittest import skipUnless
from unittest.mock import patch
from test_core import TemporaryCase
from emberbsd_sdk import process
from emberbsd_sdk.errors import SDKError


class _Identity:
    def __init__(self, births):
        self.births = births

    def now(self):
        return 200

    def birth(self, pid):
        result = self.births.get(pid)
        if isinstance(result, BaseException):
            raise result
        if isinstance(result, list):
            return result.pop(0)
        return result


class SnapshotTests(TemporaryCase):
    def scan(self, rows, births, sids, known=None):
        inspector = object.__new__(process.SessionInspector)
        inspector.identity = _Identity(births)
        inspector.uid = 501
        inspector.controller_sid = 7
        inspector.known = {} if known is None else known
        self.inspector = inspector
        def session(pid):
            value = sids.get(pid)
            if isinstance(value, BaseException):
                raise value
            if value is None:
                raise ProcessLookupError()
            return value
        with patch.object(inspector, '_rows', return_value=rows), patch.object(process.os, 'getsid', side_effect=session):
            return inspector._scan(42)

    def test_vanished_foreign_child_has_continuous_parent_proof(self):
        self.assertEqual(self.scan({10: (20, 501, b'R')}, {20: 100}, {}, {20: (100, 7)}), (set(), False))

    def test_owned_leader_cannot_use_its_foreign_parent(self):
        self.assertEqual(self.scan({42: (20, 501, b'R')}, {20: 100}, {}, {20: (100, 7)}), (set(), True))

    def test_reused_parent_cannot_hide_vanished_child(self):
        self.assertEqual(self.scan({10: (20, 501, b'R')}, {20: 201}, {}, {20: (100, 7)}), (set(), True))

    def test_traced_or_reparented_child_has_no_parent_proof(self):
        for parent, state in ((1, b'R'), (20, b'RX')):
            with self.subTest(parent=parent, state=state):
                self.assertEqual(self.scan({10: (parent, 501, state)}, {parent: 100}, {}, {parent: (100, 7)}), (set(), True))

    def test_owned_parent_cannot_hide_vanished_child(self):
        self.assertEqual(self.scan({10: (20, 501, b'R')}, {20: 100}, {}, {20: (100, 42)}), (set(), True))

    def test_reuse_after_snapshot_cannot_be_classified_by_foreign_sid(self):
        self.assertEqual(self.scan({10: (20, 501, b'R')}, {10: 201}, {10: 7}), (set(), True))

    def test_post_barrier_foreign_child_has_continuous_parent_proof(self):
        self.assertEqual(self.scan({10: (20, 501, b'R')}, {10: 201, 20: 100},
            {10: 7}, {20: (100, 7)}), (set(), False))

    def test_post_barrier_identity_is_cached_without_accepting_the_row(self):
        self.assertEqual(self.scan({10: (20, 501, b'R')}, {10: 201}, {10: 7}), (set(), True))
        self.assertEqual(self.inspector.known, {10: (201, 7)})

    def test_post_barrier_parent_proof_retains_identity_guards(self):
        cases = [
            (42, 20, b'R', 100, 7),  # A session leader can have a foreign parent.
            (10, 1, b'R', 100, 7),   # A reparented row has no ancestry proof.
            (10, 20, b'RX', 100, 7), # Tracing can change the reported parent.
            (10, 20, b'R', 201, 7),  # Parent PID reuse breaks continuity.
            (10, 20, b'R', 100, 42), # An owned parent cannot exclude ownership.
            (10, 20, b'R', None, 7), # An absent parent cannot prove continuity.
        ]
        for pid, ppid, state, parent_birth, parent_sid in cases:
            with self.subTest(pid=pid, ppid=ppid, state=state, birth=parent_birth, sid=parent_sid):
                self.assertEqual(self.scan({pid: (ppid, 501, state)},
                    {pid: 201, ppid: parent_birth}, {pid: 7},
                    {ppid: (100, parent_sid)}), (set(), True))

    def test_changed_birth_around_getsid_is_uncertain(self):
        self.assertEqual(self.scan({10: (20, 501, b'R')}, {10: [100, 101]}, {10: 7}), (set(), True))

    def test_stable_member_is_owned(self):
        self.assertEqual(self.scan({10: (42, 501, b'R')}, {10: 100}, {10: 42}), ({10}, False))

    def test_foreign_uid_scope_and_visible_credential_violation(self):
        self.assertEqual(self.scan({10: (1, 0, b'R')}, {}, {}), (set(), False))
        with self.assertRaisesRegex(SDKError, 'effective UID'):
            self.scan({10: (1, 0, b'R')}, {}, {10: 42})

    def test_denied_foreign_getsid_is_a_checked_refusal(self):
        with self.assertRaisesRegex(SDKError, 'session identity'):
            self.scan({10: (1, 0, b'R')}, {}, {10: PermissionError('denied')})

    def test_denied_birth_is_unknown_but_positive_sid_stays_owned(self):
        denied = SDKError('injected birth permission refusal')
        self.assertEqual(self.scan({10: (1, 501, b'R')}, {10: denied}, {10: 7}), (set(), True))
        self.assertEqual(self.scan({10: (42, 501, b'R')}, {10: denied}, {10: 42}), ({10}, False))

    def test_unqualified_host_refuses_before_launch_or_log(self):
        marker, log = self.root / 'started', self.root / 'log'
        with patch.object(process.sys, 'platform', 'freebsd'):
            with self.assertRaisesRegex(SDKError, 'not qualified'):
                process.run_logged([sys.executable, '-c', f'open({str(marker)!r},"w").close()'], log)
        self.assertFalse(marker.exists())
        self.assertFalse(log.exists())

    @skipUnless(sys.platform == 'darwin', 'Darwin native birth-identity profile')
    def test_preflight_does_not_require_impossible_sid_snapshot(self):
        current = os.getsid(0)
        def scan(_inspector, sid):
            if sid == current:
                return {os.getpid()}, False
            return set(), True
        with patch.object(process.SessionInspector, '_scan', autospec=True,
                          side_effect=scan):
            process.SessionInspector(empty_snapshot_timeout=.04)

    @skipUnless(sys.platform == 'darwin', 'Darwin native birth-identity profile')
    def test_real_unrelated_exits_do_not_block_empty_session(self):
        inspector = process.SessionInspector()
        original = inspector._rows
        calls = []
        def rows():
            # Every snapshot contains a foreign child that vanishes before
            # getsid. Retrying cannot obtain a globally stable PID list.
            read, write = os.pipe()
            child = subprocess.Popen([sys.executable, '-B', '-c', 'import os,sys;os.read(int(sys.argv[1]),1)', str(read)], pass_fds=(read,))
            os.close(read)
            try:
                result = original()
                self.assertIn(child.pid, result)
                calls.append(child.pid)
                os.write(write, b'x')
                child.wait(timeout=5)
                return result
            finally:
                os.close(write)
                if child.poll() is None:
                    child.kill(); child.wait()
        for index in range(5):
            before = len(calls)
            with patch.object(inspector, '_rows', side_effect=rows):
                self.assertEqual(inspector.members(99999999), set())
            # A real Darwin host can retry during unrelated same-UID process
            # churn. The contract is an empty result, not one snapshot per call.
            self.assertGreater(len(calls), before)

    @skipUnless(sys.platform in ('darwin', 'linux'), 'Qualified native process profile')
    def test_owned_fork_after_snapshot_is_found_before_drain(self):
        ready, trigger, pidfile = (self.root / name for name in ('ready', 'trigger', 'pid'))
        code = f'''import os,time,signal
from pathlib import Path
Path({str(ready)!r}).touch()
while not Path({str(trigger)!r}).exists(): time.sleep(.005)
if os.fork()==0:
 os.setpgid(0,0)
 signal.signal(signal.SIGTERM,signal.SIG_IGN)
 Path({str(pidfile)!r}).write_text(str(os.getpid()))
 time.sleep(15)
 os._exit(0)
os._exit(0)
'''
        group = process.OwnedProcessGroup([sys.executable, '-B', '-c', code], cwd=self.root, env=process.sanitized_environment(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        group.start()
        end = time.monotonic() + 5
        try:
            while not ready.exists() and time.monotonic() < end:
                time.sleep(.01)
            self.assertTrue(ready.exists())
            original = group.inspector._rows
            injected = False
            def rows():
                nonlocal injected
                result = original()
                if not injected and group.pid in result:
                    injected = True
                    trigger.touch()
                    group.child.wait(timeout=5)
                    while not pidfile.exists() and time.monotonic() < end:
                        time.sleep(.01)
                return result
            with patch.object(group.inspector, '_rows', side_effect=rows):
                self.assertTrue(group.alive())
            self.assertTrue(injected)
        finally:
            group.stop()
        pid = int(pidfile.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
