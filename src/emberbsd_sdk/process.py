# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Checked argv invocation and owned POSIX process groups and locks."""

import hashlib
import os
import signal
import stat
import subprocess
import time
from .errors import SDKError
from .paths import entry


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


def capture(argv, *, cwd=None, env=None):
    argv = _argv(argv)
    try:
        result = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    except OSError as exc:
        raise SDKError(f'cannot run {argv!r}: {exc}') from exc
    if result.returncode:
        raise SDKError(f'command failed ({result.returncode}): {argv!r}: {result.stderr.decode("utf-8", "replace").strip()}')
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
