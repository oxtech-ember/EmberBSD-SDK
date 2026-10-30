# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Checked argv invocation and owned POSIX process groups and locks."""

import hashlib
import os
import signal
import stat
import subprocess
import sys
import time
from .errors import SDKError, CommandError, ProcessOwnershipError
from .paths import canonical_existing, entry, regular_file


def sanitized_environment(environment=None):
    source = os.environ if environment is None else environment
    result = {key: value for key, value in source.items() if key in ('PATH', 'HOME', 'TMPDIR', 'TMP', 'TEMP', 'USER', 'LOGNAME')}
    result.update(LC_ALL='C', MAKECONF='/dev/null', GIT_OPTIONAL_LOCKS='0', GIT_NO_LAZY_FETCH='1')
    return result


def git_environment(environment=None, config=()):
    """Return an environment where Git reads only repository config.

    System, global and XDG config, ignore and attribute files must not change
    which files Git reports, adds, archives or checks out.  Inherited GIT_*
    variables (repository, index or config overrides) are dropped.  Each
    ``(key, value)`` in ``config`` is passed as command config.
    """
    source = sanitized_environment() if environment is None else environment
    result = {key: value for key, value in source.items()
              if not key.startswith('GIT_') and key != 'XDG_CONFIG_HOME'}
    result.update(GIT_OPTIONAL_LOCKS='0', GIT_NO_LAZY_FETCH='1',
                  GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_SYSTEM=os.devnull, GIT_CONFIG_GLOBAL=os.devnull)
    pairs = (('core.excludesFile', os.devnull), ('core.attributesFile', os.devnull), *config)
    result['GIT_CONFIG_COUNT'] = str(len(pairs))
    for index, (key, value) in enumerate(pairs):
        result[f'GIT_CONFIG_KEY_{index}'] = key
        result[f'GIT_CONFIG_VALUE_{index}'] = value
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
        raise SDKError('producer operations require a qualified POSIX session backend')


def _empty_snapshot_timeout(value):
    if type(value) not in (int, float) or not 0 < value <= sys.float_info.max:
        raise SDKError('empty snapshot timeout must be finite and positive')
    return value


class SessionInspector:
    """Enumerate a qualified build session with native birth identities.

    The host must expose all PIDs and session IDs. No producer starts until the
    executable and its required output profile pass a read-only probe.
    Darwin uses public monotonic birth ticks and coherent PID/PPID/UID/state
    rows. Its build producers must retain their effective UID. Traced or
    reparented rows have no foreign-parent proof. Escaped setsid() producers,
    including a parent that escapes before observation, are outside the scope.
    Foreign ancestry is proven within one coherent snapshot, so unrelated
    process churn on a loaded host does not make an empty session unprovable.
    """

    def __init__(self, *, env=None, empty_snapshot_timeout=2):
        self.empty_snapshot_timeout = _empty_snapshot_timeout(empty_snapshot_timeout)
        require_posix()
        if sys.platform not in ('darwin', 'linux') and sys.platform != 'netbsd11':
            raise SDKError('producer session backend is not qualified on this host; layout and inspection tools remain available')
        if not hasattr(os, 'getsid'):
            raise SDKError('the producer backend requires POSIX getsid()')
        if sys.platform == 'netbsd11':
            from ._netbsd_process import NetBSDSession
            self.native = NetBSDSession()
            self.uid = self.native.uid
            self.uncertainty = None
            if os.getpid() not in self.members(os.getsid(0)):
                raise SDKError('NetBSD cannot enumerate the current session')
            self.members(-1)
            return
        if sys.platform == 'linux':
            from ._linux_process import LinuxSession
            self.native = LinuxSession()
            self.uid = self.native.uid
            self.uncertainty = None
            if os.getpid() not in self.members(os.getsid(0)):
                raise SDKError('Linux proc cannot enumerate the current session')
            self.members(-1)
            return
        self.environment = sanitized_environment(env)
        # Process inspection is an OS service, independent of the build PATH.
        self.executable = canonical_existing('/bin/ps')
        regular_file(self.executable)
        if not os.access(self.executable, os.X_OK):
            raise SDKError(f'POSIX ps is not executable: {self.executable}')
        self.known = {}
        self.uncertainty = None
        self.uid = os.geteuid()
        self.controller_sid = os.getsid(0)
        from ._darwin_process import DarwinIdentity
        self.identity = DarwinIdentity()
        self.argv = [self.executable, '-e', '-o', 'pid=,ppid=,uid=,stat=']
        if os.getpid() not in self.members(os.getsid(0)):
            raise SDKError('POSIX ps cannot enumerate the current session')
        # A negative SID cannot exist. Its empty result says nothing about an
        # owned session and can remain uncertain under unrelated PID churn.
        # The current-session check above validates this preflight instead.

    def describe(self):
        if hasattr(self, 'native'):
            if hasattr(self.native, 'describe'):
                return self.native.describe()
            return dict(backend='linux-proc-pidfd-session-v1',
                        scope='all process groups of the owned session', uid=self.uid,
                        credentials='producer must retain its effective UID',
                        birth='proc stat starttime in CLOCK_BOOTTIME ticks',
                        reaping='isolated child subreaper for each producer')
        return dict(backend='darwin-ps-rusage-session-v1', argv=self.argv.copy(),
                    scope='all process groups of the owned session', uid=self.uid,
                    credentials='producer must retain its effective UID',
                    birth='public RUSAGE_INFO_V0 monotonic Mach ticks')

    def _rows(self):
        if hasattr(self, 'native'):
            return self.native.rows()
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
        rows = {}
        for row in stdout.splitlines():
            columns = row.split()
            if not columns:
                continue
            if len(columns) != 4 or any(not x.isdigit() or len(x) > 20 for x in columns[:2]):
                raise SDKError('unsupported PID output from POSIX ps enumeration')
            pid = int(columns[0])
            if pid <= 0 or pid > 2147483647 or pid in rows:
                raise SDKError('invalid or duplicate PID in POSIX ps enumeration')
            uid_text = columns[2]
            signed_uid = sys.platform == 'darwin' and uid_text.startswith(b'-')
            digits = uid_text[1:] if signed_uid else uid_text
            if not digits.isdigit() or len(uid_text) > 20:
                raise SDKError('unsupported UID output from POSIX ps enumeration')
            uid = int(uid_text)
            if signed_uid:
                if uid < -2147483648 or uid >= 0:
                    raise SDKError('out-of-range UID from POSIX ps enumeration')
                # Darwin ps uses signed UIDFMT for the unsigned 32-bit uid_t.
                uid += 4294967296
            if int(columns[1]) > 2147483647 or uid > 4294967295:
                raise SDKError('out-of-range process identity from POSIX ps enumeration')
            if any(value > 127 or value < 33 for value in columns[3]):
                raise SDKError('unsupported process state from POSIX ps enumeration')
            rows[pid] = (int(columns[1]), uid, columns[3])
        if os.getpid() not in rows:
            raise SDKError('POSIX ps PID enumeration does not include the controller')
        # This exact, reaped probe inherited the controller session.
        rows.pop(probe.pid, None)
        return rows

    def _scan(self, sid):
        if hasattr(self, 'native'):
            return self.native.scan(sid, self._rows)
        return self._scan_darwin(sid)

    def _scan_darwin(self, sid):
        # Cached identities were read before this snapshot. A matching birth
        # read after it proves that the same parent spanned the child PPID row.
        known = self.known
        barrier = self.identity.now()
        rows = self._rows()
        members, observed = set(), {}
        # Same-UID rows proven foreign at the snapshot, and rows not yet
        # classified, mapped to their snapshot parent and state.
        foreign, pending = set(), {}
        failures = []
        def birth_of(pid):
            try:
                return self.identity.birth(pid)
            except SDKError as exc:
                # Access can fail during exit or exec. This is no evidence of
                # absence. Keep the negative result unknown unless another
                # identity proof accounts for this row.
                failures.append(str(exc))
                return None
        for pid, (ppid, uid, state) in sorted(rows.items()):
            if uid != self.uid:
                # This profile is for builds that retain their effective UID.
                # Do not silently accept a visible violation of that contract.
                try:
                    if sid != self.controller_sid and os.getsid(pid) == sid:
                        raise SDKError('owned producer changed its effective UID')
                except ProcessLookupError:
                    pass
                except OSError as exc:
                    raise SDKError(f'cannot inspect session identity for PID {pid}: {exc}') from exc
                continue
            before = birth_of(pid)
            try:
                actual = os.getsid(pid)
            except ProcessLookupError:
                actual = None
            except OSError as exc:
                raise SDKError(f'cannot inspect session identity for PID {pid}: {exc}') from exc
            after = birth_of(pid)
            if actual == sid:
                members.add(pid)
            if before is not None and before == after and actual is not None:
                # This identity is valid for the next snapshot's parent proof,
                # even when it cannot identify the PID row in this snapshot.
                observed[pid] = (before, actual)
                if before <= barrier:
                    if actual != sid:
                        foreign.add(pid)
                    continue
            if actual == sid:
                continue
            parent = known.get(ppid)
            if pid != sid and ppid > 1 and b'X' not in state and parent is not None:
                birth, parent_sid = parent
                if parent_sid != sid and birth_of(ppid) == birth:
                    # The continuous foreign parent excludes ownership of the
                    # row, whether its PID vanished or was reused afterwards.
                    # A process cannot join another existing POSIX session.
                    # PPID1 and traced rows have no such parent proof.
                    foreign.add(pid)
                    continue
            pending[pid] = (ppid, state)
        # The snapshot rows are coherent: a row's PPID names the process listed
        # under that PID in the same snapshot. A row whose listed parent is
        # proven foreign was forked in that foreign session, so it was foreign
        # too, whether its PID vanished or was reused afterwards. This proves
        # short-lived descendants of unrelated builds, which no earlier
        # snapshot can know. The leader, PPID1 and traced rows keep no proof.
        progress = True
        while progress:
            progress = False
            for pid, (ppid, state) in sorted(pending.items()):
                if pid != sid and ppid > 1 and b'X' not in state and ppid in foreign:
                    foreign.add(pid)
                    del pending[pid]
                    progress = True
        # Reuse after the barrier, a changed birth, or an unclassified
        # vanished process must not turn an owned fork into false emptiness.
        uncertain = bool(pending)
        self.known = observed
        self.uncertainty = failures[-1] if uncertain and failures else None
        return members, uncertain

    def members(self, sid):
        deadline = time.monotonic() + self.empty_snapshot_timeout
        while True:
            members, vanished = self._scan(sid)
            if members or not vanished:
                return members
            # A vanished parent can have forked after the PID snapshot. Do not
            # accept an empty session until every listed PID was accounted for.
            if time.monotonic() >= deadline:
                reason = getattr(self, 'uncertainty', None)
                detail = f': {reason}' if reason else ''
                raise SDKError('POSIX session enumeration did not reach a complete empty snapshot' + detail)
            time.sleep(.02)


class OwnedProcessGroup:
    """Own all process groups of one session. Keep the API v1 class name.

    A descendant can change its PGID. A new session from setsid() is outside
    this backend contract. Import does not enumerate or change any process.
    """

    def __init__(self, argv, *, cwd, env, stdout, stderr, stdin=subprocess.DEVNULL,
                 inspector=None, empty_snapshot_timeout=2):
        empty_snapshot_timeout = _empty_snapshot_timeout(empty_snapshot_timeout)
        require_posix()
        self.argv = _argv(argv)
        self.options = dict(cwd=cwd, env=env, stdout=stdout, stderr=stderr, stdin=stdin)
        self.inspector = (SessionInspector(env=env, empty_snapshot_timeout=empty_snapshot_timeout)
                          if inspector is None else inspector)
        self.child = None

    def start(self):
        if self.child is not None:
            raise SDKError('owned session is already started')
        if hasattr(self.inspector, 'native'):
            if hasattr(self.inspector.native, 'child'):
                self.child = self.inspector.native.child(self.argv, self.options)
            else:
                from ._linux_process import LinuxChild
                self.child = LinuxChild(self.argv, self.options)
            try:
                self.child.start()
            except SDKError as exc:
                raise ProcessOwnershipError(self.child.pid, str(exc)) from exc
            finally:
                if self.child.pid is not None:
                    self.inspector.native.supervised[self.child.pid] = self.child
        else:
            self.child = subprocess.Popen(self.argv, start_new_session=True, **self.options)
        return self.child.pid

    @property
    def pid(self):
        return None if self.child is None else self.child.pid

    def poll(self):
        try:
            return self.child.poll()
        except SDKError as exc:
            raise ProcessOwnershipError(self.pid, str(exc)) from exc

    def _members(self):
        try:
            return self.inspector.members(self.pid)
        except SDKError as exc:
            raise ProcessOwnershipError(self.pid, str(exc)) from exc

    def alive(self):
        return self.child is not None and bool(self._members())

    def _signal(self, pid, number):
        if hasattr(self.inspector, 'native'):
            try:
                self.inspector.native.signal(pid, self.pid, number)
            except SDKError as exc:
                raise ProcessOwnershipError(self.pid, str(exc)) from exc
            return
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
            self.poll()
            members = self._members()
            if not members:
                return True
            native = getattr(self.inspector, 'native', None)
            # Linux repeats process-bound signals. Clock ticks alone cannot
            # distinguish two uses of one PID within the same tick.
            for pid in sorted(members if native is not None else members - signalled):
                self._signal(pid, number)
                signalled.add(pid)
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def stop(self):
        if self.pid is None:
            return
        for number, delay in ((signal.SIGTERM, 2), (signal.SIGKILL, 10)):
            if self._drain(delay, number):
                return
        raise ProcessOwnershipError(self.pid, 'session members did not disappear')


class ContextLocks:
    """POSIX flock handles remain open for the entire owned-producer lifetime."""

    def __init__(self, paths, lock_root, *, shared=False, wait=False):
        if type(wait) is not bool:
            raise SDKError('wait lock mode must be a boolean')
        if type(shared) is not bool:
            raise SDKError('shared lock mode must be a boolean')
        require_posix()
        self.paths = sorted(paths)
        self.lock_root = lock_root
        self.shared = shared
        self.wait = wait
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
                    mode = fcntl.LOCK_SH if self.shared else fcntl.LOCK_EX
                    fcntl.flock(fd, mode if self.wait else mode | fcntl.LOCK_NB)
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


def run_logged(argv, log, *, cwd=None, env=None, append=False, empty_snapshot_timeout=2,
               timeout_seconds=None):
    """Run a checked producer and drain its owned session before closing its log.

    Signal handlers are entrypoint policy. A handler can raise KeyboardInterrupt.
    The caller must retain its context locks until this function returns.
    If ProcessOwnershipError occurs, quarantine writable work before reuse.
    empty_snapshot_timeout bounds each empty-session proof, including startup.
    timeout_seconds bounds runtime polling. Startup and drainage can take longer.
    """
    if timeout_seconds is not None and (type(timeout_seconds) not in (int, float) or
                                        not 0 < timeout_seconds <= sys.float_info.max):
        raise SDKError('producer timeout must be finite and positive')
    empty_snapshot_timeout = _empty_snapshot_timeout(empty_snapshot_timeout)
    require_posix()
    argv = _argv(argv)
    group = OwnedProcessGroup(argv, cwd=cwd, env=env, stdout=None, stderr=None,
                              empty_snapshot_timeout=empty_snapshot_timeout)
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
            deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
            group.start()
            while True:
                code = group.poll()
                if deadline is not None and time.monotonic() >= deadline:
                    raise SDKError(f'producer runtime timeout ({timeout_seconds}s); inspect {log}')
                if code is not None:
                    break
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
                    # Native exit records can precede a waitable child status.
                    # Briefly retry only this leader. Keep the ownership failure.
                    try:
                        deadline = time.monotonic() + 1
                        while group.poll() is None:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                break
                            time.sleep(min(0.01, remaining))
                    except BaseException:
                        pass
                    raise ProcessOwnershipError(exc.sid, exc.reason, log=os.fspath(log)) from exc
                finally:
                    for number, handler in previous.items():
                        signal.signal(number, handler)
            output.flush()
            os.fsync(output.fileno())
