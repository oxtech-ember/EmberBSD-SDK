# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Production stream contracts with real producers and explicit fault controls."""

import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
from test_core import TemporaryCase, SDK_SOURCE
from emberbsd_sdk import stream
from emberbsd_sdk.errors import SDKError, CommandError, ProcessOwnershipError


class StreamTests(TemporaryCase):
    def command(self, code):
        return [sys.executable, '-B', '-I', '-S', '-c', code]

    def test_exact_binary_regular_file_input_and_output(self):
        payload = bytes(range(256)) + b'\0\xffinput\n'
        files = []
        factory = tempfile.TemporaryFile
        def temporary():
            result = factory()
            files.append(result)
            return result
        command = self.command('import os,stat,sys;'
            'assert stat.S_ISREG(os.fstat(0).st_mode);'
            'sys.stdout.buffer.write(sys.stdin.buffer.read());'
            'sys.stderr.buffer.write(b"input\\x00diagnostic")')
        with patch.object(stream.tempfile, 'TemporaryFile', temporary):
            result = stream.capture_streamed(command, input=payload, timeout=5)
        self.assertEqual(stream.PRODUCER_INPUT_API, 1)
        self.assertEqual((result.value, result.stderr), (payload, b'input\0diagnostic'))
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].closed)

    def test_input_above_pipe_capacity_does_not_block_output(self):
        payload = bytes(range(256)) * 16384
        prefix = b'output-before-input' * 16384
        code = ('import os,signal,sys;signal.alarm(10);'
                'sys.stdout.buffer.write(b"output-before-input"*16384);sys.stdout.buffer.flush();'
                'sys.stdout.buffer.write(sys.stdin.buffer.read())')
        # A pipe writer that blocks before reading stdout cannot satisfy this
        # producer. Its alarm also bounds a regressed implementation's child.
        result = stream.capture_streamed(self.command(code), input=payload,
                                         stdout_limit=len(prefix) + len(payload), timeout=5)
        self.assertEqual(result.value, prefix + payload)

    def test_run_streamed_input_and_default_eof(self):
        result = stream.run_streamed(self.command('import sys;sys.stdout.buffer.write(sys.stdin.buffer.read())'),
                                     lambda reader: reader.read(7), input=b'\0value\xff', timeout=5)
        self.assertEqual(result.value, b'\0value\xff')
        with patch.object(stream.tempfile, 'TemporaryFile') as temporary:
            result = stream.capture_streamed(self.command('import sys;assert sys.stdin.buffer.read()==b""'))
        self.assertEqual(result.value, b'')
        temporary.assert_not_called()

    def test_invalid_input_refuses_before_allocation_and_producer(self):
        for value in ('text', bytearray(b'bytes'), memoryview(b'bytes'), False, 1, []):
            with self.subTest(input_type=type(value).__name__), \
                    patch.object(stream, 'OwnedProcessGroup') as producer, \
                    patch.object(stream.tempfile, 'TemporaryFile') as temporary:
                with self.assertRaisesRegex(SDKError, 'bytes or None'):
                    stream.capture_streamed(['not-started'], input=value)
                with self.assertRaisesRegex(SDKError, 'bytes or None'):
                    stream.run_streamed(['not-started'], lambda reader: None, input=value)
                producer.assert_not_called()
                temporary.assert_not_called()

    def test_input_file_closes_if_preflight_fails(self):
        source = tempfile.TemporaryFile()
        with patch.object(stream.tempfile, 'TemporaryFile', return_value=source), \
                patch.object(stream, 'OwnedProcessGroup', side_effect=SDKError('fixture preflight failure')):
            with self.assertRaisesRegex(SDKError, 'fixture preflight failure'):
                stream.capture_streamed(['not-started'], input=b'payload')
        self.assertTrue(source.closed)

    def test_deadline_with_input_stops_owned_descendant(self):
        leader, descendant, writer = (self.root / name for name in ('leader', 'descendant', 'writes'))
        payload = b'binary\0input\xff' * 8192
        code = f'''import os,signal,sys,time
from pathlib import Path
assert sys.stdin.buffer.read()==b'binary\\0input\\xff'*8192
Path({str(leader)!r}).write_text(str(os.getpid()))
if os.fork()==0:
    os.setpgid(0,0)
    signal.signal(signal.SIGTERM,signal.SIG_IGN)
    Path({str(descendant)!r}).write_text(str(os.getpid()))
    deadline=time.monotonic()+20
    while time.monotonic()<deadline:
        with Path({str(writer)!r}).open('ab') as output: output.write(b'x')
        time.sleep(.02)
    os._exit(0)
time.sleep(20)
'''
        files = []
        factory = tempfile.TemporaryFile
        def temporary():
            source = factory()
            files.append(source)
            return source
        with patch.object(stream.tempfile, 'TemporaryFile', temporary):
            with self.assertRaisesRegex(SDKError, 'deadline'):
                stream.capture_streamed(self.command(code), input=payload, timeout=2)
        self.assertTrue(files[0].closed)
        for path in (leader, descendant):
            pid = int(path.read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
        before = writer.read_bytes()
        time.sleep(.1)
        self.assertEqual(writer.read_bytes(), before)

    def test_binary_capture_and_concurrent_bounded_stderr(self):
        result = stream.capture_streamed(self.command(
            'import os\nfor i in range(50):\n os.write(2,b"e"*65536)\n os.write(1,b"\\0x"*8192)'),
            stdout_limit=1_000_000, stderr_limit=97, timeout=15)
        self.assertEqual(stream.PRODUCER_STREAM_API, 1)
        self.assertEqual(result.value, b'\0x' * 8192 * 50)
        self.assertEqual(result.stderr, b'e' * 97)
        self.assertTrue(result.stderr_truncated)

    def test_status_97_wins_over_parser_failure_and_keeps_stderr(self):
        def reject(reader):
            self.assertEqual(reader.read(1), b'x')
            raise SDKError('fixture parser rejects bytes')
        with self.assertRaises(CommandError) as caught:
            stream.run_streamed(self.command('import os;os.write(1,b"x");os.write(2,b"failure");raise SystemExit(97)'), reject)
        self.assertEqual((caught.exception.returncode, caught.exception.stderr), (97, b'failure'))
        self.assertFalse(caught.exception.stderr_truncated)
        self.assertIn('fixture parser', str(caught.exception.__cause__))

    def test_failed_capture_retains_bounded_binary_stdout_and_status(self):
        for limit, expected in ((32, b'\0payload'), (8, b'\0payload'),
                                (3, b'\0pa'), (0, b'')):
            with self.subTest(limit=limit), self.assertRaises(CommandError) as caught:
                stream.capture_streamed(self.command(
                    'import os;os.write(1,b"\\0payload");os.write(2,b"reason");raise SystemExit(97)'),
                    stdout_limit=limit)
            self.assertEqual(caught.exception.returncode, 97)
            self.assertEqual(caught.exception.stdout, expected)
            self.assertEqual(caught.exception.stderr, b'reason')
            self.assertEqual(caught.exception.stdout_truncated, limit < 8)

    def test_parser_failure_on_success_is_not_hidden(self):
        def reject(reader):
            raise ValueError('invalid payload')
        with self.assertRaisesRegex(ValueError, 'invalid payload'):
            stream.run_streamed(self.command('import os;os.write(1,b"x"*100000)'), reject)

    def test_capture_limit_refuses_but_reports_completed_status(self):
        for status in (0, 97):
            with self.subTest(status=status):
                with self.assertRaises(CommandError if status else SDKError) as caught:
                    stream.capture_streamed(self.command(f'import os;os.write(1,b"large");raise SystemExit({status})'), stdout_limit=2)
                if status:
                    self.assertEqual(caught.exception.returncode, status)
                else:
                    self.assertIn('capture limit', str(caught.exception))

    def test_early_consumer_return_drains_unread_output(self):
        saved = []
        def consume(reader):
            saved.append(reader)
            return reader.read(1)
        result = stream.run_streamed(self.command('import os;os.write(1,b"a"*2000000)'), consume, timeout=15)
        self.assertEqual(result.value, b'a')
        with self.assertRaisesRegex(SDKError, 'no longer active'):
            saved[0].read(1)

    def test_bad_options_refuse_before_producer(self):
        marker = self.root / 'started'
        argv = self.command(f'open({str(marker)!r},"w").close()')
        for options in ({'stderr_limit': -1}, {'stderr_limit': True},
                        {'stderr_limit': stream.CHUNK + 1}, {'timeout': 0},
                        {'timeout': float('nan')}, {'timeout': float('inf')},
                        {'timeout': True}, {'stdout_limit': -1}):
            with self.subTest(options=options), self.assertRaises(SDKError):
                stream.capture_streamed(argv, **options)
        self.assertFalse(marker.exists())

    def test_bad_read_and_zero_capture(self):
        for size in (-1, stream.CHUNK + 1, True, None):
            with self.subTest(size=size), self.assertRaises(SDKError):
                stream.run_streamed(self.command('pass'), lambda reader: reader.read(size))
        result = stream.capture_streamed(self.command('pass'), stdout_limit=0, stderr_limit=0)
        self.assertEqual((result.value, result.stderr, result.stderr_truncated), (b'', b'', False))

    def test_deadline_stops_producer(self):
        pidfile = self.root / 'pid'
        code = f'import os,time;open({str(pidfile)!r},"w").write(str(os.getpid()));time.sleep(30)'
        with self.assertRaisesRegex(SDKError, 'deadline'):
            stream.capture_streamed(self.command(code), timeout=.5)
        pid = int(pidfile.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_unproven_stop_remains_typed_and_overrides_success(self):
        original = stream.OwnedProcessGroup.stop
        def uncertain(group):
            original(group)
            raise ProcessOwnershipError(group.pid, 'fixture uncertainty after real drain')
        with patch.object(stream.OwnedProcessGroup, 'stop', uncertain):
            with self.assertRaises(ProcessOwnershipError) as caught:
                stream.capture_streamed(self.command('print("done")'))
        self.assertIsInstance(caught.exception.sid, int)

    def test_unproven_consumer_does_not_continue_waiting_for_output(self):
        def uncertain(reader):
            raise ProcessOwnershipError(123, 'fixture caller ownership failure')
        before = time.monotonic()
        with self.assertRaises(ProcessOwnershipError) as caught:
            stream.run_streamed(self.command('import time;time.sleep(30)'), uncertain, timeout=5)
        self.assertEqual(caught.exception.sid, 123)
        self.assertLess(time.monotonic() - before, 4)

    def test_completed_signal_retains_negative_returncode(self):
        with self.assertRaises(CommandError) as caught:
            stream.capture_streamed(self.command('import os,signal;os.write(1,b"signal");os.kill(os.getpid(),signal.SIGTERM)'))
        self.assertEqual(caught.exception.returncode, -signal.SIGTERM)
        self.assertEqual(caught.exception.stdout, b'signal')
        self.assertFalse(caught.exception.stdout_truncated)

    def test_missing_executable_closes_pipe_descriptors(self):
        with self.assertRaises(OSError):
            stream.capture_streamed([str(self.root / 'absent')])

    def wait_file(self, path):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if path.exists() and path.stat().st_size:
                return path.read_text()
            time.sleep(.02)
        self.fail(f'fixture did not create {path}')

    def test_leader_exit_and_term_drain_term_ignoring_descendant(self):
        cases = ((0, None), (97, None), (None, signal.SIGINT),
                 (None, signal.SIGTERM), (None, signal.SIGHUP))
        for status, number in cases:
            with self.subTest(status=status, signal=number):
                root = self.root / f"{status}-{number}"; root.mkdir()
                pidfile, writer, ready = root / 'pid', root / 'writes', root / 'ready'
                producer = f'''import os,signal,time
from pathlib import Path
if os.fork() == 0:
    os.setpgid(0,0)
    signal.signal(signal.SIGTERM,signal.SIG_IGN)
    Path({str(pidfile)!r}).write_text(str(os.getpid()))
    deadline=time.monotonic()+30
    while time.monotonic()<deadline:
        with Path({str(writer)!r}).open('ab') as handle: handle.write(b'x')
        time.sleep(.02)
    os._exit(0)
while not Path({str(pidfile)!r}).exists(): time.sleep(.01)
Path({str(ready)!r}).write_text(str(os.getsid(0)))
'''
                producer += 'time.sleep(30)\n' if status is None else f'os._exit({status})\n'
                driver = '''import sys,signal
sys.dont_write_bytecode=True
sys.path.insert(0,sys.argv[1])
from emberbsd_sdk.stream import capture_streamed
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.process import ContextLocks
def stop(number,frame): raise SystemExit(128+number)
for number in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP): signal.signal(number,stop)
try:
    with ContextLocks([sys.argv[2]+'/context'],sys.argv[2]):
        capture_streamed(sys.argv[3:],timeout=15)
except SDKError as error:
    print(error,file=sys.stderr)
    raise SystemExit(1)
'''
                child = subprocess.Popen(self.command(driver) + [str(SDK_SOURCE / 'src'), str(root)] + self.command(producer), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.addCleanup(lambda child=child: child.poll() is None and (child.kill(), child.wait()))
                pid = int(self.wait_file(pidfile)); sid = int(self.wait_file(ready))
                def cleanup(pid=pid, sid=sid):
                    try:
                        if os.getsid(pid) == sid: os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                self.addCleanup(cleanup)
                if status is None:
                    child.send_signal(number)
                time.sleep(.15)
                from emberbsd_sdk.process import ContextLocks
                with self.assertRaisesRegex(SDKError, 'context is busy'):
                    ContextLocks([str(root / 'context')], root).acquire()
                out, err = child.communicate(timeout=20)
                self.assertEqual(child.returncode, 128 + number if status is None else 1, err)
                if status is not None:
                    self.assertIn(b'live descendants', err)
                with self.assertRaises(ProcessLookupError): os.kill(pid, 0)
                before = writer.read_bytes(); time.sleep(.1)
                self.assertEqual(writer.read_bytes(), before)
                with ContextLocks([str(root / 'context')], root):
                    pass
