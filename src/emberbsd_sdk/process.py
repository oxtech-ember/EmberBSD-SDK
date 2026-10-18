# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Checked argv invocation and owned POSIX process groups and locks."""

import hashlib
import os
import signal
import stat
import subprocess
import time
from .errors import SDKError, CommandError
from .paths import entry, regular_file


def sanitized_environment(environment=None):
    source = os.environ if environment is None else environment
    result = {key: value for key, value in source.items() if key in ('PATH', 'HOME', 'TMPDIR', 'TMP', 'TEMP', 'USER', 'LOGNAME')}
    result.update(LC_ALL='C', MAKECONF='/dev/null', GIT_OPTIONAL_LOCKS='0', GIT_NO_LAZY_FETCH='1')
    return result


def _argv(argv):
    if isinstance(argv, (str, bytes)):
        raise SDKError('command must be an argv sequence')
    result = list(argv)
    if not result or any(not isinstance(a, str) or '\0' in a for a in result):
        raise SDKError('command argv must contain strings without NUL')
    return result


def capture(argv, *, cwd=None, env=None, input=None):
    argv = _argv(argv)
    if input is not None and not isinstance(input, bytes):
        raise SDKError('command input must be bytes or None')
    try:
        options = {'stdin': subprocess.DEVNULL} if input is None else {'input': input}
        result = subprocess.run(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, **options)
    except OSError as exc:
        raise SDKError(f'cannot run {argv!r}: {exc}') from exc
    if result.returncode:
        raise CommandError(result.returncode, argv, result.stdout, result.stderr)
    return result


def require_posix():
    if os.name != 'posix':
        raise SDKError('the producer backend requires a POSIX host; use Linux/WSL2 on Windows')


class OwnedProcessGroup:
    """Own exactly one session. stop() retains ownership until the group drains."""

    def __init__(self, argv, *, cwd, env, stdout, stderr):
        require_posix()
        self.argv = _argv(argv)
        self.options = dict(cwd=cwd, env=env, stdout=stdout, stderr=stderr)
        self.child = None

    def start(self):
        if self.child is not None:
            raise SDKError('owned process group is already started')
        self.child = subprocess.Popen(self.argv, stdin=subprocess.DEVNULL, start_new_session=True, **self.options)
        return self.child.pid

    @property
    def pid(self):
        return None if self.child is None else self.child.pid

    def poll(self):
        return self.child.poll()

    def alive(self):
        if self.child is None:
            return False
        try:
            os.killpg(self.pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError as exc:
            raise SDKError(f'cannot inspect owned process group {self.pid}: {exc}') from exc

    def _drain(self, seconds):
        deadline = time.monotonic() + seconds
        while True:
            self.child.poll()
            if not self.alive():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def stop(self):
        if self.child is None:
            return
        for sig, delay in ((signal.SIGTERM, 2), (signal.SIGKILL, 10)):
            try:
                os.killpg(self.pid, sig)
            except ProcessLookupError:
                pass
            except PermissionError as exc:
                raise SDKError(f'cannot send signal {sig} to owned process group {self.pid}: {exc}') from exc
            if self._drain(delay):
                return
        raise SDKError('owned process group did not disappear')


class ContextLocks:
    """POSIX flock handles remain open for the entire owned-producer lifetime."""

    def __init__(self, paths, lock_root):
        require_posix()
        self.paths = sorted(paths)
        self.lock_root = lock_root
        self.handles = []

    def acquire(self):
        import fcntl
        try:
            for path in self.paths:
                name = '.prebuild-sdk-lock-' + hashlib.sha256(path.encode('utf-8')).hexdigest()
                lock = os.path.join(self.lock_root, name)
                info = entry(lock)
                if info is not None and not stat.S_ISREG(info.st_mode):
                    raise SDKError(f'invalid object lock: {lock}')
                fd = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o644)
                handle = os.fdopen(fd, 'r+b')
                self.handles.append(handle)
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise SDKError(f'invalid object lock: {lock}')
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise SDKError(f'object context is busy: {path}') from exc
            return self
        except BaseException:
            self.close()
            raise

    def close(self):
        for handle in reversed(self.handles):
            handle.close()
        self.handles.clear()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *args):
        self.close()


def run_logged(argv, log, *, cwd=None, env=None, append=False):
    """Run a checked producer and drain its owned group before closing its log.

    Signal handlers are entrypoint policy. A handler can raise KeyboardInterrupt.
    The caller must retain its context locks until this function returns.
    """
    require_posix()
    argv = _argv(argv)
    expected = regular_file(log) if append else None
    flags = os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    flags |= os.O_APPEND if append else os.O_CREAT | os.O_EXCL
    fd = os.open(log, flags, 0o644)
    with os.fdopen(fd, 'ab' if append else 'wb') as output:
        opened = os.fstat(output.fileno())
        if not stat.S_ISREG(opened.st_mode):
            raise SDKError(f'expected a regular producer log: {log}')
        if expected is not None and (expected.st_dev, expected.st_ino) != (opened.st_dev, opened.st_ino):
            raise SDKError(f'producer log changed before append: {log}')
        group = OwnedProcessGroup(argv, cwd=cwd, env=env, stdout=output, stderr=output)
        try:
            group.start()
            while (code := group.poll()) is None:
                time.sleep(0.05)
            if group.alive():
                raise SDKError(f'producer left live descendants; inspect {log}')
            if code:
                raise CommandError(code, argv, log=os.fspath(log))
            return subprocess.CompletedProcess(argv, code)
        finally:
            # A second signal must not interrupt descendant drainage.
            previous = {}
            if group.alive():
                import threading
                if threading.current_thread() is threading.main_thread():
                    for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                        previous[number] = signal.signal(number, signal.SIG_IGN)
                try:
                    group.stop()
                finally:
                    for number, handler in previous.items():
                        signal.signal(number, handler)
            output.flush()
            os.fsync(output.fileno())
