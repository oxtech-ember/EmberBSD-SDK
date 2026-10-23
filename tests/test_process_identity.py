# SPDX-License-Identifier: BSD-2-Clause
# Origin: EmberBSD (AI-assisted). Native read-only process identity contracts.
import os
from pathlib import Path
import select
import struct
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.process_identity import _Darwin, _darwin_arguments, process_identity


class IdentityTests(unittest.TestCase):
    def test_native_exact_arguments_and_exit(self):
        values = ['two words', '', "single'quote", '$(false)', '; exit 99', 'a\nb']
        child = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.buffer.read()', *values], stdin=subprocess.PIPE)
        try:
            first = process_identity(child.pid)
            second = process_identity(child.pid)
            self.assertEqual(first, second)
            self.assertEqual(first.argv, (sys.executable, '-c', 'import sys; sys.stdin.buffer.read()', *values))
            self.assertEqual(Path(first.executable).resolve(), Path(sys.executable).resolve())
            self.assertGreater(first.birth, 0)
        finally:
            child.communicate(b'')
        self.assertIsNone(process_identity(child.pid))

    def test_native_exec_keeps_birth(self):
        replacement = 'import sys; print("ready", flush=True); sys.stdin.buffer.read()'
        code = ('import os,sys; print("before-ready", flush=True); sys.stdin.readline(); '
                f'os.execv(sys.executable, [sys.executable,"-c",{replacement!r},"after exec"])')
        child = subprocess.Popen([sys.executable, '-c', code], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE)
        try:
            # Framework Python can re-exec before the fixture code starts.
            self.assertTrue(select.select([child.stdout], [], [], 5)[0],
                            'initial interpreter did not become ready')
            self.assertEqual(child.stdout.readline(), b'before-ready\n')
            before = process_identity(child.pid)
            self.assertIsNotNone(before)
            child.stdin.write(b'go\n'); child.stdin.flush()
            # Native identity fields are not atomic across exec. The ready
            # line proves that the replacement has completed exec.
            self.assertTrue(select.select([child.stdout], [], [], 5)[0],
                            'replacement did not become ready after exec')
            self.assertEqual(child.stdout.readline(), b'ready\n')
            after = process_identity(child.pid)
            self.assertIsNotNone(after)
            self.assertEqual(after.argv[1:], ('-c', replacement, 'after exec'))
            self.assertEqual(Path(after.argv[0]).resolve(),
                             Path(after.executable).resolve())
            self.assertEqual(before.birth, after.birth)
        finally:
            try:
                child.communicate(b'', timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate(timeout=5)
                raise
        self.assertEqual(child.returncode, 0)
        self.assertIsNone(process_identity(child.pid))

    def test_darwin_parser_preserves_empty_and_shell_characters(self):
        args = [b'/usr/bin/test', b'two words', b'', b'$(false)', b'; exit 99']
        data = struct.pack('=i', len(args)) + b'/usr/bin/test\0\0\0' + b'\0'.join(args) + b'\0KEY=value\0'
        self.assertEqual(_darwin_arguments(data), tuple(os.fsdecode(a) for a in args))
        for corrupt in (b'', struct.pack('=i', -1) + b'bad\0', data[:25], data[:data.index(b'KEY=') - 1]):
            with self.subTest(corrupt=corrupt), self.assertRaises(SDKError):
                _darwin_arguments(corrupt)

    def test_darwin_repeated_argv_refuses_exec_race(self):
        reader = _Darwin.__new__(_Darwin)
        reader.identity = Mock()
        reader.identity.lifetime.side_effect = [(42, 0), (42, 0)]
        reader.executable = Mock(return_value='/usr/bin/example')
        reader.arguments = Mock(side_effect=[('example', 'before'), ('example', 'after')])
        with self.assertRaisesRegex(SDKError, 'identity changed'):
            reader.snapshot(123)

    def darwin_reader(self, lifetimes, *, argv_error=None):
        reader = _Darwin.__new__(_Darwin)
        reader.identity = Mock()
        reader.identity.lifetime.side_effect = lifetimes
        reader.executable = Mock(return_value='/usr/bin/example')
        reader.arguments = Mock(return_value=('example', 'argument'))
        if argv_error is not None:
            reader.arguments.side_effect = argv_error
        return reader

    def test_darwin_confirmed_unreaped_exit_needs_no_argv_access(self):
        reader = self.darwin_reader([(42, 50)])
        self.assertIsNone(reader.snapshot(123))
        reader.executable.assert_not_called()
        reader.arguments.assert_not_called()

    def test_darwin_live_permission_error_is_not_absence(self):
        reader = self.darwin_reader([(42, 0), (42, 0)],
                                    argv_error=SDKError('live argv: Operation not permitted'))
        with self.assertRaisesRegex(SDKError, 'live argv: Operation not permitted'):
            reader.snapshot(123)

    def test_darwin_exit_during_argv_error_is_confirmed_by_same_birth(self):
        reader = self.darwin_reader([(42, 0), (42, 50)],
                                    argv_error=SDKError('argv access denied after exit'))
        self.assertIsNone(reader.snapshot(123))

    def test_darwin_error_without_exit_tick_stays_unproven(self):
        reader = self.darwin_reader([(42, 0), None],
                                    argv_error=SDKError('argv access denied before absence'))
        with self.assertRaisesRegex(SDKError, 'argv access denied before absence'):
            reader.snapshot(123)

    def test_darwin_recycled_exited_pid_is_not_absence(self):
        for error in (None, SDKError('argv access denied')):
            with self.subTest(error=error):
                reader = self.darwin_reader([(42, 0), (43, 50)], argv_error=error)
                with self.assertRaisesRegex(SDKError, 'identity changed'):
                    reader.snapshot(123)

    def test_darwin_exit_after_reading_argv_is_absence(self):
        reader = self.darwin_reader([(42, 0), (42, 50)])
        self.assertIsNone(reader.snapshot(123))

    @unittest.skipUnless(sys.platform == 'darwin', 'requires native Darwin rusage exit ticks')
    def test_native_darwin_live_unreaped_and_reaped(self):
        from emberbsd_sdk._darwin_process import DarwinIdentity
        identity = DarwinIdentity()
        incoming, outgoing = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(outgoing)
            os.read(incoming, 1)
            os._exit(0)
        os.close(incoming)
        try:
            live = identity.lifetime(pid)
            self.assertIsNotNone(live)
            self.assertGreater(live[0], 0)
            self.assertEqual(live[1], 0)
            self.assertEqual(identity.birth(pid), live[0])
            self.assertEqual(process_identity(pid).birth, live[0])
            os.close(outgoing)
            outgoing = None
            deadline = time.monotonic() + 5
            while True:
                ended = identity.lifetime(pid)
                self.assertIsNotNone(ended)
                if ended[1]:
                    break
                self.assertLess(time.monotonic(), deadline, 'no unreaped exit tick observed')
                time.sleep(.01)
            self.assertEqual(ended[0], live[0])
            self.assertGreaterEqual(ended[1], live[0])
            # The owned-session backend still sees an unreaped PID's birth.
            self.assertEqual(identity.birth(pid), live[0])
            self.assertIsNone(process_identity(pid))
        finally:
            if outgoing is not None:
                os.close(outgoing)
            os.waitpid(pid, 0)
        self.assertIsNone(identity.lifetime(pid))
        self.assertIsNone(identity.birth(pid))
        self.assertIsNone(process_identity(pid))

    def test_invalid_pids(self):
        for value in (True, None, '1', 0, -1, 2**31):
            with self.subTest(value=value), self.assertRaises(SDKError):
                process_identity(value)


if __name__ == '__main__':
    unittest.main()
