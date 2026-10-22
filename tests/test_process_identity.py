# SPDX-License-Identifier: BSD-2-Clause
# Origin: EmberBSD (AI-assisted). Native read-only process identity contracts.
import os
from pathlib import Path
import struct
import subprocess
import sys
import unittest
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
        code = 'import os,sys; sys.stdin.readline(); os.execv(sys.executable, [sys.executable,"-c","import sys; sys.stdin.buffer.read()","after exec"])'
        child = subprocess.Popen([sys.executable, '-c', code], stdin=subprocess.PIPE)
        try:
            before = process_identity(child.pid)
            child.stdin.write(b'go\n'); child.stdin.flush()
            import time
            deadline = time.monotonic() + 5
            while True:
                after = process_identity(child.pid)
                if after.argv[-1] == 'after exec':
                    break
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.01)
            self.assertEqual(before.birth, after.birth)
        finally:
            child.communicate(b'')

    def test_darwin_parser_preserves_empty_and_shell_characters(self):
        args = [b'/usr/bin/test', b'two words', b'', b'$(false)', b'; exit 99']
        data = struct.pack('=i', len(args)) + b'/usr/bin/test\0\0\0' + b'\0'.join(args) + b'\0KEY=value\0'
        self.assertEqual(_darwin_arguments(data), tuple(os.fsdecode(a) for a in args))
        for corrupt in (b'', struct.pack('=i', -1) + b'bad\0', data[:25], data[:data.index(b'KEY=') - 1]):
            with self.subTest(corrupt=corrupt), self.assertRaises(SDKError):
                _darwin_arguments(corrupt)

    def test_darwin_repeated_argv_refuses_exec_race(self):
        from unittest.mock import Mock
        reader = _Darwin.__new__(_Darwin)
        reader.identity = Mock()
        reader.identity.birth.side_effect = [42, 42]
        reader.executable = Mock(return_value='/usr/bin/example')
        reader.arguments = Mock(side_effect=[('example', 'before'), ('example', 'after')])
        with self.assertRaisesRegex(SDKError, 'identity changed'):
            reader.snapshot(123)

    def test_invalid_pids(self):
        for value in (True, None, '1', 0, -1, 2**31):
            with self.subTest(value=value), self.assertRaises(SDKError):
                process_identity(value)


if __name__ == '__main__':
    unittest.main()
