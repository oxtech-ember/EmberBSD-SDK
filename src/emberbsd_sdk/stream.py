# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Bounded binary output from a producer controlled by the SDK session backend."""

from dataclasses import dataclass
import os
import selectors
import signal
import threading
import time

from .errors import SDKError, CommandError, ProcessOwnershipError
from .process import OwnedProcessGroup, _argv

PRODUCER_STREAM_API = 1
CHUNK = 1024 * 1024


@dataclass(frozen=True)
class StreamResult:
    value: object
    stderr: bytes
    stderr_truncated: bool


class _Reader:
    def __init__(self, group, selector, stdout, stderr, limit, deadline):
        self.group = group
        self.selector = selector
        self.stdout = stdout
        self.stderr_fd = stderr
        self.limit = limit
        self.deadline = deadline
        self.buffer = b''
        self.errors = bytearray()
        self.truncated = False
        self.output_done = False
        self.active = True

    def _pump(self):
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise SDKError('producer stream deadline expired')
        code = self.group.poll()
        if code is not None and self.group.alive():
            raise SDKError('producer left live descendants in its owned session')
        for key, _events in self.selector.select(.05):
            try:
                data = os.read(key.fd, CHUNK)
            except BlockingIOError:
                continue
            if not data:
                self.selector.unregister(key.fd)
                if key.fd == self.stdout:
                    self.output_done = True
            elif key.fd == self.stdout:
                self.buffer = data
            else:
                room = self.limit - len(self.errors)
                self.errors.extend(data[:room])
                self.truncated |= len(data) > room

    def read(self, size):
        """Read at most one MiB. Short reads are valid, including at EOF."""
        if not self.active:
            raise SDKError('producer stream is no longer active')
        if type(size) is not int or not 0 <= size <= CHUNK:
            raise SDKError('stream read size must be between zero and one MiB')
        if not size:
            return b''
        while not self.buffer and not self.output_done:
            self._pump()
        data, self.buffer = self.buffer[:size], self.buffer[size:]
        return data

    def finish(self):
        while self.read(CHUNK):
            pass
        while self.selector.get_map() or self.group.poll() is None:
            self._pump()
        return self.group.poll()


def run_streamed(argv, consume, *, cwd=None, env=None,
                 stderr_limit=CHUNK, timeout=None):
    """Consume stdout bytes, then check producer status and drain its session.

    ``consume(reader)`` must use bounded reads and must not retain the reader.
    A parser error still drains output to obtain the producer's true status.
    A signal or deadline stops the session. ProcessOwnershipError takes priority
    over other errors; the caller must retain and quarantine writable work.
    Stderr is drained concurrently. Only its first ``stderr_limit`` bytes remain.
    Signal handlers belong to the CLI, as with ``process.run_logged``.
    """
    argv = _argv(argv)
    if not callable(consume):
        raise SDKError('stream consumer must be callable')
    if type(stderr_limit) is not int or not 0 <= stderr_limit <= CHUNK:
        raise SDKError('stderr limit must be between zero and one MiB')
    if timeout is not None and (type(timeout) not in (int, float) or
                               not 0 < timeout < float('inf')):
        raise SDKError('stream timeout must be finite and positive')
    group = OwnedProcessGroup(argv, cwd=cwd, env=env, stdout=None, stderr=None)
    descriptors = []
    reader = None
    with selectors.DefaultSelector() as selector:
        try:
            stdout, output = os.pipe()
            descriptors.extend((stdout, output))
            stderr, errors = os.pipe()
            descriptors.extend((stderr, errors))
            for fd in (stdout, stderr):
                os.set_blocking(fd, False)
                selector.register(fd, selectors.EVENT_READ)
            group.options.update(stdout=output, stderr=errors)
            group.start()
            for fd in (output, errors):
                os.close(fd)
                descriptors.remove(fd)
            deadline = None if timeout is None else time.monotonic() + timeout
            reader = _Reader(group, selector, stdout, stderr, stderr_limit, deadline)
            failure = None
            try:
                value = consume(reader)
            except ProcessOwnershipError:
                raise
            except Exception as exc:
                failure = exc
            code = reader.finish()
            if code:
                error = CommandError(code, argv, stderr=bytes(reader.errors))
                error.stderr_truncated = reader.truncated
                raise error from failure
            if failure is not None:
                raise failure
            return StreamResult(value, bytes(reader.errors), reader.truncated)
        finally:
            if reader is not None:
                reader.active = False
            previous = {}
            try:
                if group.pid is not None:
                    if threading.current_thread() is threading.main_thread():
                        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                            previous[number] = signal.signal(number, signal.SIG_IGN)
                    group.stop()
            finally:
                for number, handler in previous.items():
                    signal.signal(number, handler)
                for fd in descriptors:
                    os.close(fd)


def capture_streamed(argv, *, stdout_limit=CHUNK, **options):
    """Capture bounded output with the same ownership and status contract."""
    if type(stdout_limit) is not int or stdout_limit < 0:
        raise SDKError('stdout limit must be a nonnegative integer')

    def consume(reader):
        data = bytearray()
        while chunk := reader.read(min(CHUNK, stdout_limit - len(data) + 1)):
            data.extend(chunk)
            if len(data) > stdout_limit:
                raise SDKError('producer stdout exceeds the capture limit')
        return bytes(data)

    return run_streamed(argv, consume, **options)
