# SPDX-License-Identifier: BSD-2-Clause
# Origin: EmberBSD contributors (AI-assisted).
"""Trace evidence readers inspect only local fixture bytes. No child runs."""

import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_core  # Adds the SDK source to sys.path.
from emberbsd_sdk import bootstrap_nawk_runtime as runtime
from emberbsd_sdk.bootstrap_history_context import validate_history
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.native_toolset import MAX_JSON, _json


class TraceReaderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root / 'trace'
        blocker = patch('subprocess.Popen', side_effect=AssertionError('no child'))
        blocker.start()
        self.addCleanup(blocker.stop)

    def reference(self, data=b'raw trace'):
        self.path.write_bytes(data)
        return dict(path=str(self.path), sha256=hashlib.sha256(data).hexdigest())

    def wrapped_open(self, action=None, requests=None):
        original = runtime.open_regular
        def open_file(path):
            handle, before = original(path)
            class Reader:
                acted = False
                def __enter__(self): return self
                def __exit__(self, *args): handle.close()
                def fileno(self): return handle.fileno()
                def read(self, size):
                    if requests is not None: requests.append(size)
                    block = handle.read(size)
                    if action is not None and not self.acted:
                        self.acted = True
                        action()
                    return block
            return Reader(), before
        return open_file

    def test_trace_larger_than_json_is_streamed_and_json_limit_stays(self):
        reference = self.reference(b'\x00\xff' * (MAX_JSON // 2 + 1))
        requests = []
        with patch.object(runtime, 'open_regular', self.wrapped_open(requests=requests)):
            runtime._trace_reference(reference)
        self.assertGreater(len(requests), 1)
        self.assertTrue(all(0 <= size <= 1024 * 1024 for size in requests))
        with self.assertRaisesRegex(SDKError, 'evidence exceeds size limit'):
            _json(reference)

    def test_reference_shape_path_and_digest_are_required(self):
        good = self.reference()
        for value in ({}, dict(good, extra=True), dict(good, path='relative'),
                      dict(good, sha256='bad'), dict(good, sha256='0' * 64)):
            with self.subTest(value=value), self.assertRaises(SDKError):
                runtime._trace_reference(value)

    def test_empty_file_symlink_directory_and_fifo_refuse(self):
        with self.assertRaisesRegex(SDKError, 'trace evidence is empty'):
            runtime._trace_reference(self.reference(b''))
        self.path.unlink()
        target = self.root / 'target'
        target.write_bytes(b'raw trace')
        for kind in ('symlink', 'dangling-link', 'directory', 'fifo'):
            if kind == 'symlink': self.path.symlink_to(target)
            elif kind == 'dangling-link': self.path.symlink_to(self.root / 'absent')
            elif kind == 'directory': self.path.mkdir()
            else: os.mkfifo(self.path)
            try:
                with self.subTest(kind=kind), self.assertRaises(SDKError):
                    runtime._trace_reference(dict(path=str(self.path), sha256=hashlib.sha256(b'raw trace').hexdigest()))
            finally:
                if kind == 'directory': self.path.rmdir()
                else: self.path.unlink()

    def test_size_limit_refuses_before_read_and_accepts_exact_bound(self):
        reference = self.reference(b'x' * 65)
        requests = []
        with patch.object(runtime, 'MAX_TRACE_BYTES', 64), \
             patch.object(runtime, 'open_regular', self.wrapped_open(requests=requests)):
            with self.assertRaisesRegex(SDKError, 'trace exceeds size limit'):
                runtime._trace_reference(reference)
            self.assertEqual(requests, [])
            runtime._trace_reference(self.reference(b'x' * 64))

    def test_changed_bytes_or_truncation_refuses_the_original_hash(self):
        for data in (b'changed!!', b'raw'):
            reference = self.reference()
            self.path.write_bytes(data)
            with self.subTest(data=data), self.assertRaisesRegex(SDKError, 'SHA256 mismatch'):
                runtime._trace_reference(reference)

    def test_growth_truncation_and_path_replacement_during_read_refuse(self):
        for kind in ('grow', 'truncate', 'replace'):
            reference = self.reference(b'x' * 64)
            def mutate():
                if kind == 'grow':
                    with self.path.open('ab') as stream: stream.write(b'y' * 64)
                elif kind == 'truncate': self.path.write_bytes(b'')
                else:
                    self.path.rename(self.root / 'retired')
                    self.path.write_bytes(b'x' * 64)
            requests = []
            with patch.object(runtime, 'open_regular', self.wrapped_open(mutate, requests)), \
                 self.subTest(kind=kind), self.assertRaisesRegex(SDKError, 'changed during inspection'):
                runtime._trace_reference(reference)
            self.assertLessEqual(sum(requests), 66)

    def test_history_memo_rechecks_the_streamed_trace(self):
        reference = self.reference()
        calls = []
        def leaf():
            calls.append(True)
            runtime._trace_reference(reference)
            return []
        def outer():
            validate_history('trace-leaf', leaf)
            self.path.write_bytes(b'changed!!')
            validate_history('trace-leaf', leaf)
        with self.assertRaisesRegex(SDKError, 'memoized history input changed'):
            validate_history('trace-root', outer)
        self.assertEqual(calls, [True])


if __name__ == '__main__':
    unittest.main()
