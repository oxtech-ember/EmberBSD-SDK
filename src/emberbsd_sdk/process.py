# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Checked argv invocation and owned POSIX process groups and locks."""

import hashlib
import os
import signal
import shutil
import stat
import subprocess
import time
from .errors import SDKError, CommandError, ProcessOwnershipError
from .paths import canonical_existing, entry, regular_file


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


class SessionInspector:
    """Enumerate one POSIX session with base-system ps and Python getsid().

    The host must expose all PIDs and session IDs. No producer starts until the
    executable and its required output profile pass a read-only probe.
    """

    def __init__(self, *, env=None):
        require_posix()
        if not hasattr(os, 'getsid'):
            raise SDKError('the producer backend requires POSIX getsid()')
        self.environment = sanitized_environment(env)
        selected = shutil.which('ps', path=self.environment.get('PATH', os.defpath))
        if selected is None:
            raise SDKError('the producer backend requires base-system POSIX ps')
        self.executable = canonical_existing(selected)
        regular_file(self.executable)
        if not os.access(self.executable, os.X_OK):
            raise SDKError(f'POSIX ps is not executable: {self.executable}')
        self.argv = [self.executable, '-e', '-o', 'pid=']
        if os.getpid() not in self.members(os.getsid(0)):
            raise SDKError('POSIX ps cannot enumerate the current session')

    def describe(self):
        return dict(backend='posix-ps-session-v1', argv=self.argv.copy(), scope='all process groups of the owned session')

    def _scan(self, sid):
        try:
            probe = subprocess.Popen(self.argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.environment)
            try:
                stdout, stderr = probe.communicate(timeout=5)
            except BaseException:
                probe.kill()
                probe.communicate()
                raise
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SDKError(f'cannot enumerate POSIX session members: {exc}') from exc
        if probe.returncode != 0:
            raise SDKError(f'POSIX ps enumeration failed ({probe.returncode}): {stderr.decode("utf-8", "replace").strip()}')
        if len(stdout) > 8 * 1024 * 1024:
            raise SDKError('POSIX ps PID enumeration exceeds the size limit')
        pids = set()
        for row in stdout.splitlines():
            row = row.strip()
            if not row:
                continue
            if not row.isdigit() or len(row) > 20 or int(row) <= 0:
                raise SDKError('unsupported PID output from POSIX ps enumeration')
            pid = int(row)
            if pid in pids:
                raise SDKError('duplicate PID in POSIX ps enumeration')
            pids.add(pid)
        if os.getpid() not in pids:
            raise SDKError('POSIX ps PID enumeration does not include the controller')
        members, vanished = set(), False
        for pid in sorted(pids):
            # This exact child has exited after communicate(). It inherited the
            # controller session and cannot be an owned producer member.
            if pid == probe.pid:
                continue
            try:
                actual = os.getsid(pid)
            except ProcessLookupError:
                vanished = True
                continue
            except OSError as exc:
                raise SDKError(f'cannot inspect session identity for PID {pid}: {exc}') from exc
            if actual == sid:
                members.add(pid)
        return members, vanished

    def members(self, sid):
        deadline = time.monotonic() + 2
        while True:
            members, vanished = self._scan(sid)
            if members or not vanished:
                return members
            # A vanished parent can have forked after the PID snapshot. Do not
            # accept an empty session until every listed PID was accounted for.
            if time.monotonic() >= deadline:
                raise SDKError('POSIX session enumeration did not reach a complete empty snapshot')
            time.sleep(.02)


class OwnedProcessGroup:
    """Own all process groups of one session. Keep the API v1 class name.

    A descendant can change its PGID. A new session from setsid() is outside
    this backend contract. Import does not enumerate or change any process.
    """

    def __init__(self, argv, *, cwd, env, stdout, stderr, inspector=None):
        require_posix()
        self.argv = _argv(argv)
        self.options = dict(cwd=cwd, env=env, stdout=stdout, stderr=stderr)
        self.inspector = SessionInspector(env=env) if inspector is None else inspector
        self.child = None

    def start(self):
        if self.child is not None:
            raise SDKError('owned session is already started')
        self.child = subprocess.Popen(self.argv, stdin=subprocess.DEVNULL, start_new_session=True, **self.options)
        return self.child.pid

    @property
    def pid(self):
        return None if self.child is None else self.child.pid

    def poll(self):
        return self.child.poll()

    def _members(self):
        try:
            return self.inspector.members(self.pid)
        except SDKError as exc:
            raise ProcessOwnershipError(self.pid, str(exc)) from exc

    def alive(self):
        return self.child is not None and bool(self._members())

    def _signal(self, pid, number):
        try:
            if os.getsid(pid) != self.pid:
                return
            os.kill(pid, number)
        except ProcessLookupError:
            pass
        except OSError as exc:
            raise ProcessOwnershipError(self.pid, f'cannot signal owned PID {pid}: {exc}') from exc

    def _drain(self, seconds, number):
        deadline = time.monotonic() + seconds
        signalled = set()
        while True:
            self.child.poll()
            members = self._members()
            if not members:
                return True
            for pid in sorted(members - signalled):
                self._signal(pid, number)
                signalled.add(pid)
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def stop(self):
        if self.child is None:
            return
        for number, delay in ((signal.SIGTERM, 2), (signal.SIGKILL, 10)):
            if self._drain(delay, number):
                return
        raise ProcessOwnershipError(self.pid, 'session members did not disappear')


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
    """Run a checked producer and drain its owned session before closing its log.

    Signal handlers are entrypoint policy. A handler can raise KeyboardInterrupt.
    The caller must retain its context locks until this function returns.
    If ProcessOwnershipError occurs, quarantine writable work before reuse.
    """
    require_posix()
    argv = _argv(argv)
    group = OwnedProcessGroup(argv, cwd=cwd, env=env, stdout=None, stderr=None)
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
        group.options.update(stdout=output, stderr=output)
        try:
            group.start()
            while (code := group.poll()) is None:
                time.sleep(0.05)
            if group.alive():
                raise SDKError(f'producer left live descendants in its owned session; inspect {log}')
            if code:
                raise CommandError(code, argv, log=os.fspath(log))
            return subprocess.CompletedProcess(argv, code)
        finally:
            # A second signal must not interrupt descendant drainage.
            previous = {}
            if group.pid is not None:
                import threading
                if threading.current_thread() is threading.main_thread():
                    for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                        previous[number] = signal.signal(number, signal.SIG_IGN)
                try:
                    group.stop()
                except ProcessOwnershipError as exc:
                    raise ProcessOwnershipError(exc.sid, exc.reason, log=os.fspath(log)) from exc
                finally:
                    for number, handler in previous.items():
                        signal.signal(number, handler)
            output.flush()
            os.fsync(output.fileno())
