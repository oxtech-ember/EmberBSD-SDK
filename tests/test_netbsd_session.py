# SPDX-License-Identifier: BSD-2-Clause
"""Kernel-event lifetime, not wall-clock process identity."""
import ctypes
import os
import signal
import sys
import types
import unittest
from unittest.mock import patch
import test_core  # Adds the SDK source to sys.path.
from emberbsd_sdk import process
from emberbsd_sdk.errors import SDKError


class DispatchTests(unittest.TestCase):
    def test_netbsd_uses_qualified_native_adapter(self):
        class Native:
            uid = os.geteuid()
            def scan(self, sid, read_rows):
                return ({os.getpid()} if sid == os.getsid(0) else set()), False
            def rows(self):
                return {}
            def describe(self):
                return {'backend': 'netbsd-kqueue-session-v1'}
        fake = types.ModuleType('emberbsd_sdk._netbsd_process')
        fake.NetBSDSession = Native
        with patch.object(sys, 'platform', 'netbsd11'), patch.dict(sys.modules, {'emberbsd_sdk._netbsd_process': fake}):
            inspector = process.SessionInspector()
            self.assertEqual(inspector.describe()['backend'], 'netbsd-kqueue-session-v1')
            self.assertEqual(inspector.members(-1), set())


class KernelEventTests(unittest.TestCase):
    def setUp(self):
        from emberbsd_sdk import _netbsd_process as n
        self.n = n
        self.tree = n.TrackedTree(50)

    def event(self, pid, flags, parent=0, eventflags=0):
        return types.SimpleNamespace(ident=pid, filter=self.n.PROC, fflags=flags,
                                     data=parent, flags=eventflags)

    def test_kernel_birth_and_exit_cover_parent_that_exits_first(self):
        t, n = self.tree, self.n
        t.accept([self.event(51, n.CHILD, 50), self.event(50, n.EXIT)])
        self.assertEqual(t.active, {51: 2})
        t.accept([self.event(52, n.CHILD, 51), self.event(51, n.EXIT), self.event(52, n.EXIT)])
        self.assertEqual(t.active, {})

    def test_pid_reuse_gets_distinct_lifetime_token(self):
        t, n = self.tree, self.n
        t.accept([self.event(51, n.CHILD, 50)])
        first = t.active[51]
        t.accept([self.event(51, n.EXIT), self.event(51, n.CHILD, 50)])
        self.assertNotEqual(t.active[51], first)

    def test_track_error_stays_fail_closed(self):
        n = self.n
        with self.assertRaises(SDKError):
            self.tree.accept([self.event(50, n.TRACKERR)])
        with self.assertRaises(SDKError):
            self.tree.accept([])

    def test_bad_protocol_is_never_empty_proof(self):
        n = self.n
        cases = [self.event(51, n.CHILD, 999), self.event(50, n.CHILD, 50),
                 self.event(51, n.EXIT), self.event(50, 8),
                 self.event(50, 0, eventflags=n.EV_ERROR),
                 self.event(50, 0, eventflags=n.EV_EOF),
                 self.event(51, n.CHILD | n.EXIT, 50)]
        for event in cases:
            with self.subTest(event=event):
                with self.assertRaises(SDKError):
                    n.TrackedTree(50).accept([event])

    def test_duplicate_birth_and_exit_refused(self):
        n = self.n
        for events in [[self.event(51, n.CHILD, 50)] * 2,
                       [self.event(50, n.EXIT)] * 2]:
            with self.assertRaises(SDKError):
                n.TrackedTree(50).accept(events)

    def test_coalesced_parent_fork_and_exit_is_valid(self):
        n = self.n
        self.tree.accept([self.event(51, n.CHILD, 50), self.event(50, n.FORK | n.EXIT)])
        self.assertEqual(set(self.tree.active), {51})

    def test_signal_refuses_reused_pid_after_census(self):
        n = self.n
        owner = types.SimpleNamespace(tree=self.tree, pump=lambda: None)
        native = object.__new__(n.NetBSDSession)
        native.uid = os.geteuid()
        native.supervised = {50: owner}
        row = n.ProcessRow(50, 1, 50, native.uid, 2)
        def rows():
            self.tree.accept([self.event(50, n.EXIT)])
            return {50: row}
        native.rows = rows
        with patch.object(os, 'kill') as kill:
            native.signal(50, 50, signal.SIGTERM)
            kill.assert_not_called()

    def embryo_session(self):
        n = self.n
        self.tree.accept([self.event(51, n.CHILD, 50), self.event(50, n.EXIT)])
        owner = types.SimpleNamespace(tree=self.tree, pump=lambda: None,
                                      reap=unittest.mock.Mock())
        native = object.__new__(n.NetBSDSession)
        native.uid = os.geteuid()
        native.supervised = {50: owner}
        native.rows = lambda: {}
        return native, owner

    def test_child_note_before_census_visibility_defers_signal(self):
        native, owner = self.embryo_session()
        with patch.object(os, 'kill') as kill, patch.object(os, 'getsid', return_value=50):
            self.assertEqual(native.scan(50, native.rows), ({51}, False))
            native.signal(51, 50, signal.SIGTERM)
            kill.assert_not_called()
            self.assertEqual(owner.tree.active, {51: 2})
            owner.reap.assert_not_called()
            native.rows = lambda: {51: self.n.ProcessRow(51, 50, 50, native.uid, 2)}
            native.signal(51, 50, signal.SIGTERM)
            kill.assert_called_once_with(51, signal.SIGTERM)

    def embryo_group(self, native):
        group = object.__new__(process.OwnedProcessGroup)
        group.child = types.SimpleNamespace(pid=50, poll=lambda: 0)
        group.inspector = types.SimpleNamespace(native=native,
            members=lambda sid: native.scan(sid, native.rows)[0])
        return group

    def test_missing_census_child_retains_existing_drain_deadline(self):
        native, owner = self.embryo_session()
        group = self.embryo_group(native)
        with patch.object(process.time, 'monotonic', side_effect=[0, 2]), patch.object(os, 'kill') as kill:
            self.assertFalse(group._drain(1, signal.SIGKILL))
            kill.assert_not_called()
        self.assertEqual(owner.tree.active, {51: 2})
        owner.reap.assert_not_called()

    def test_cancel_cleanup_retries_child_when_census_becomes_visible(self):
        native, owner = self.embryo_session()
        group = self.embryo_group(native)
        def visible_after_retry(delay):
            self.assertEqual(delay, 0.05)
            native.rows = lambda: {51: self.n.ProcessRow(51, 50, 50, native.uid, 2)}
        def exit_after_signal(pid, number):
            self.assertEqual((pid, number), (51, signal.SIGTERM))
            self.tree.accept([self.event(pid, self.n.EXIT)])
        with patch.object(process.time, 'sleep', side_effect=visible_after_retry), \
             patch.object(os, 'getsid', return_value=50), \
             patch.object(os, 'kill', side_effect=exit_after_signal) as kill:
            try:
                raise KeyboardInterrupt
            except KeyboardInterrupt:
                group.stop()
            kill.assert_called_once_with(51, signal.SIGTERM)
        self.assertEqual(owner.tree.active, {})
        owner.reap.assert_called_once()

    def test_foreign_session_and_uid_cannot_be_signalled(self):
        n = self.n
        for sid, uid in [(9, os.geteuid()), (50, os.geteuid() + 1)]:
            native = object.__new__(n.NetBSDSession)
            native.uid = os.geteuid()
            owner = types.SimpleNamespace(tree=n.TrackedTree(50), pump=lambda: None)
            native.supervised = {50: owner}
            native.rows = lambda: {50: n.ProcessRow(50, 1, sid, uid, 2)}
            with patch.object(os, 'kill') as kill:
                with self.assertRaises(SDKError): native.signal(50, 50, signal.SIGTERM)
                kill.assert_not_called()

    def test_census_prefix_bounds_and_current_identity(self):
        n = self.n
        p = n.ProcPrefix()
        p.pid, p.ppid, p.sid, p.uid, p.state = os.getpid(), os.getppid(), os.getsid(0), os.geteuid(), 2
        raw = bytes(p)
        self.assertEqual(n.decode_rows(raw)[p.pid].uid, os.geteuid())
        for bad in [raw[:-1], raw + raw]:
            with self.assertRaises(SDKError): n.decode_rows(bad)
        for field, value in [('pid', -1), ('sid', -1), ('ppid', -1), ('state', 0)]:
            q = n.ProcPrefix.from_buffer_copy(raw)
            setattr(q, field, value)
            with self.assertRaises(SDKError): n.decode_rows(bytes(q))


    def test_census_cannot_hide_untracked_owned_member(self):
        n = self.n
        native = object.__new__(n.NetBSDSession)
        native.uid = os.geteuid()
        owner = types.SimpleNamespace(tree=n.TrackedTree(50), pump=lambda: None)
        native.supervised = {50: owner}
        rows = {99: n.ProcessRow(99, 50, 50, native.uid, 2)}
        with self.assertRaises(SDKError): native.scan(50, lambda: rows)


class AnchorTests(unittest.TestCase):
    def setUp(self):
        from emberbsd_sdk import _netbsd_process as n
        self.n = n
        self.child = n.NetBSDChild(types.SimpleNamespace(), [], {})
        self.child.pid = 50
        self.child.tree = n.TrackedTree(50)
        self.child.process = unittest.mock.Mock()
        self.child.queue = unittest.mock.Mock()
        self.child.queue.control.return_value = []

    def test_poll_preserves_unreaped_anchor(self):
        status = types.SimpleNamespace(si_status=7, si_code=os.CLD_EXITED)
        with patch.object(os, 'waitid', return_value=status) as wait:
            self.assertEqual(self.child.poll(), 7)
            self.assertTrue(wait.call_args.args[2] & os.WNOWAIT)
        self.child.process.wait.assert_not_called()

    def test_reap_refused_with_any_live_descendant(self):
        self.child.tree.active = {51: 2}
        with self.assertRaises(SDKError): self.child.reap()
        self.child.process.wait.assert_not_called()

    def test_empty_tree_reaps_exact_anchor_once(self):
        self.child.tree.active.clear()
        status = types.SimpleNamespace(si_status=0, si_code=os.CLD_EXITED)
        with patch.object(os, 'waitid', return_value=status): self.child.reap()
        self.child.process.wait.assert_called_once_with()
        self.child.queue = None
        self.child.pump()
        self.assertEqual(self.child.poll(), 0)

    def test_lost_root_wait_is_ownership_error(self):
        with patch.object(os, 'waitid', side_effect=ChildProcessError()):
            with self.assertRaises(SDKError): self.child.poll()

    def test_full_batch_is_drained_before_empty_proof(self):
        event = types.SimpleNamespace(ident=50, filter=self.n.PROC,
                  fflags=self.n.FORK, flags=0, data=0)
        self.child.queue.control.side_effect = [[event] * 256, []]
        self.child.pump()
        self.assertEqual(self.child.queue.control.call_count, 2)

class GateTests(unittest.TestCase):
    def test_kernel_registration_and_identity_precede_exec_release(self):
        from emberbsd_sdk import _netbsd_process as n
        order = []
        queue = unittest.mock.Mock()
        queue.control.side_effect = lambda *a: order.append('register')
        native = types.SimpleNamespace(uid=os.geteuid(), supervised={})
        def rows():
            order.append('identity')
            return {50: n.ProcessRow(50, os.getpid(), 50, native.uid, 2)}
        native.rows = rows
        child = n.NetBSDChild(native, ['fixture'], {})
        proc = unittest.mock.Mock(pid=50)
        with patch.object(n.select, 'kevent', create=True), \
             patch.object(n.select, 'KQ_EV_ADD', 1, create=True), \
             patch.object(n.select, 'KQ_EV_ENABLE', 4, create=True), \
             patch.object(n.select, 'kqueue', create=True, return_value=queue), \
             patch.object(n.subprocess, 'Popen', return_value=proc), \
             patch.object(os, 'write', side_effect=lambda *a: order.append('exec')):
            child.start()
        self.assertEqual(order, ['register', 'identity', 'exec'])
        proc.wait.assert_not_called()
        self.assertIs(native.supervised[50], child)

    def test_registration_failure_does_not_release_producer(self):
        from emberbsd_sdk import _netbsd_process as n
        queue = unittest.mock.Mock()
        queue.control.side_effect = PermissionError()
        native = types.SimpleNamespace(uid=os.geteuid(), supervised={})
        child = n.NetBSDChild(native, ['fixture'], {})
        proc = unittest.mock.Mock(pid=50, returncode=125)
        with patch.object(n.select, 'kevent', create=True), \
             patch.object(n.select, 'KQ_EV_ADD', 1, create=True), \
             patch.object(n.select, 'KQ_EV_ENABLE', 4, create=True), \
             patch.object(n.select, 'kqueue', create=True, return_value=queue), \
             patch.object(n.subprocess, 'Popen', return_value=proc), \
             patch.object(os, 'write') as release:
            with self.assertRaises(SDKError): child.start()
        release.assert_not_called()
        proc.wait.assert_called_once_with(timeout=5)
        self.assertTrue(child.reaped)
        self.assertEqual(child.tree.active, {})

    def test_cancellation_after_gate_write_retains_tracking_for_cleanup(self):
        from emberbsd_sdk import _netbsd_process as n
        queue = unittest.mock.Mock()
        native = types.SimpleNamespace(uid=os.geteuid(), supervised={})
        native.rows = lambda: {50: n.ProcessRow(50, os.getpid(), 50, native.uid, 2)}
        child = n.NetBSDChild(native, ['fixture'], {})
        proc = unittest.mock.Mock(pid=50)
        with patch.object(n.select, 'kevent', create=True), \
             patch.object(n.select, 'KQ_EV_ADD', 1, create=True), \
             patch.object(n.select, 'KQ_EV_ENABLE', 4, create=True), \
             patch.object(n.select, 'kqueue', create=True, return_value=queue), \
             patch.object(n.subprocess, 'Popen', return_value=proc), \
             patch.object(os, 'write', side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt): child.start()
        self.assertTrue(child.released)
        self.assertFalse(child.reaped)
        proc.wait.assert_not_called()

class PreflightCompatibilityTests(unittest.TestCase):
    def test_kqueue_without_context_manager_is_closed(self):
        from contextlib import ExitStack
        from emberbsd_sdk import _netbsd_process as n
        class Queue:
            closed = False
            def control(self, *args): return []
            def close(self): self.closed = True
        queue = Queue()
        library = unittest.mock.Mock()
        library.sysctlbyname.return_value = 0
        row = n.ProcessRow(os.getpid(), os.getppid(), os.getsid(0), os.geteuid(), 2)
        with ExitStack() as stack:
            stack.enter_context(patch.object(sys, 'platform', 'netbsd11'))
            stack.enter_context(patch.object(n.platform, 'machine', return_value='amd64'))
            stack.enter_context(patch.object(n.ctypes, 'CDLL', return_value=library))
            stack.enter_context(patch.object(n.NetBSDSession, 'rows', return_value={row.pid: row}))
            for name, value in [('kqueue', lambda: queue), ('kevent', unittest.mock.Mock()),
                    ('KQ_EV_ADD', 1), ('KQ_EV_ENABLE', 4), ('KQ_NOTE_TRACK', 1),
                    ('KQ_NOTE_TRACKERR', 2), ('KQ_NOTE_CHILD', 4),
                    ('KQ_NOTE_FORK', n.FORK), ('KQ_NOTE_EXIT', n.EXIT)]:
                stack.enter_context(patch.object(n.select, name, value, create=True))
            n.NetBSDSession()
        self.assertTrue(queue.closed)
