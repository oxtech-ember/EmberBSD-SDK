# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Cross-process shared and exclusive context-lock contracts."""

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch

from test_core import TemporaryCase, SDK_SOURCE
from emberbsd_sdk import process
from emberbsd_sdk.errors import SDKError


WORKER = '''import sys,time
from pathlib import Path
sys.dont_write_bytecode=True
sys.path.insert(0,sys.argv[1])
from emberbsd_sdk.process import ContextLocks
from emberbsd_sdk.errors import SDKError
root=Path(sys.argv[2]); mode=sys.argv[3]
ready=Path(sys.argv[4]); release=Path(sys.argv[5])
shared=mode.startswith('shared')
hold=mode.endswith('hold')
try:
    kwargs={'shared': True} if shared else {}
    with ContextLocks([str(root/'context')],root,**kwargs):
        ready.write_text('acquired')
        if hold:
            deadline=time.monotonic()+20
            while not release.exists() and time.monotonic()<deadline:
                time.sleep(.01)
            if not release.exists():
                raise SystemExit('hold worker timed out')
except SDKError as error:
    ready.write_text('error:'+str(error))
'''


class ContextLockTests(TemporaryCase):
    def setUp(self):
        super().setUp()
        self.children = []

    def tearDown(self):
        for child, release in reversed(self.children):
            release.touch(exist_ok=True)
            if child.poll() is None:
                try:
                    child.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.communicate(timeout=5)
        super().tearDown()

    def start_worker(self, name, mode):
        ready = self.root / (name + '.ready')
        release = self.root / (name + '.release')
        child = subprocess.Popen(
            [sys.executable, '-B', '-I', '-S', '-c', WORKER,
             str(SDK_SOURCE / 'src'), str(self.root), mode,
             str(ready), str(release)],
            cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.children.append((child, release))
        return child, ready, release

    def worker_state(self, child, ready, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if ready.is_file():
                return ready.read_text()
            if child.poll() is not None:
                out, err = child.communicate(timeout=1)
                self.fail(f'lock worker exited {child.returncode}: {out!r} {err!r}')
            time.sleep(.01)
        self.fail('lock worker did not report its acquisition result')

    def finish_worker(self, child, release):
        release.touch()
        out, err = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 0, (out, err))

    def try_lock(self, name, mode):
        child, ready, release = self.start_worker(name, mode)
        self.assertFalse(release.exists())
        state = self.worker_state(child, ready)
        out, err = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 0, (out, err))
        return state

    def test_two_process_shared_readers_and_exclusive_waits_for_both(self):
        first, first_ready, first_release = self.start_worker('reader-a', 'shared-hold')
        self.assertEqual(self.worker_state(first, first_ready), 'acquired')
        second, second_ready, second_release = self.start_worker('reader-b', 'shared-hold')
        self.assertEqual(self.worker_state(second, second_ready), 'acquired')

        lock_name = '.prebuild-sdk-lock-' + hashlib.sha256(
            str(self.root / 'context').encode('utf-8')).hexdigest()
        self.assertTrue((self.root / lock_name).is_file())
        self.assertEqual(len(list(self.root.glob('.prebuild-sdk-lock-*'))), 1)
        self.assertRegex(self.try_lock('exclusive-both', 'exclusive-try'),
                         r'^error:object context is busy:')

        self.finish_worker(first, first_release)
        self.assertRegex(self.try_lock('exclusive-one', 'exclusive-try'),
                         r'^error:object context is busy:')
        self.finish_worker(second, second_release)
        self.assertEqual(self.try_lock('exclusive-free', 'exclusive-try'), 'acquired')

    def test_shared_and_default_exclusive_modes_conflict(self):
        exclusive, ready, release = self.start_worker('exclusive-holder', 'exclusive-hold')
        self.assertEqual(self.worker_state(exclusive, ready), 'acquired')
        self.assertRegex(self.try_lock('shared-blocked', 'shared-try'),
                         r'^error:object context is busy:')
        self.assertRegex(self.try_lock('default-blocked', 'exclusive-try'),
                         r'^error:object context is busy:')
        self.finish_worker(exclusive, release)
        self.assertEqual(self.try_lock('shared-after', 'shared-try'), 'acquired')
        self.assertEqual(self.try_lock('default-after', 'exclusive-try'), 'acquired')

    def test_invalid_shared_mode_refuses_before_lock_creation(self):
        for value in (None, 0, 1, 'true'):
            with self.subTest(value=value), self.assertRaisesRegex(
                    SDKError, 'shared lock mode must be a boolean'):
                process.ContextLocks([str(self.root / 'context')], self.root,
                                     shared=value)
        self.assertEqual(list(self.root.glob('.prebuild-sdk-lock-*')), [])

    def test_busy_lock_failure_closes_open_handle(self):
        shared, ready, release = self.start_worker('shared-holder', 'shared-hold')
        self.assertEqual(self.worker_state(shared, ready), 'acquired')
        opened = []
        original = process.os.fdopen

        def track(fd, *args, **kwargs):
            handle = original(fd, *args, **kwargs)
            opened.append(handle)
            return handle

        locks = process.ContextLocks([str(self.root / 'context')], self.root)
        with patch.object(process.os, 'fdopen', side_effect=track):
            with self.assertRaisesRegex(SDKError, 'context is busy'):
                locks.acquire()
        self.assertEqual(locks.handles, [])
        self.assertEqual(len(opened), 1)
        self.assertTrue(opened[0].closed)
        self.finish_worker(shared, release)

    def test_symlink_lock_file_is_refused(self):
        lock_name = '.prebuild-sdk-lock-' + hashlib.sha256(
            str(self.root / 'context').encode('utf-8')).hexdigest()
        target = self.write('target-lock', b'')
        (self.root / lock_name).symlink_to(target)
        with self.assertRaisesRegex(SDKError, 'invalid object lock'):
            process.ContextLocks([str(self.root / 'context')], self.root,
                                 shared=True).acquire()
