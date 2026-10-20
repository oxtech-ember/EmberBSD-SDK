# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Linux procfs snapshots, pidfd signals and isolated session supervision.

AI-assisted implementation from the public Linux proc_pid_stat(5),
proc_pid_status(5), pidfd_open(2) and PR_SET_CHILD_SUBREAPER(2)
interfaces. No kernel structure or third-party package is required.
"""

import ctypes
from dataclasses import dataclass
import json
import os
import selectors
import signal
import subprocess
import sys
import time

from .errors import SDKError


PROC_ROOT = '/proc'


@dataclass(frozen=True)
class ProcessRow:
    pid: int
    ppid: int
    sid: int
    uid: int
    birth: int
    state: bytes


def parse_stat(data, pid, uid):
    """Read field 22 without splitting the unrestricted parenthesized comm."""
    try:
        prefix, tail = data.split(b' (', 1)
        _command, separator, fields = tail.rpartition(b') ')
        fields = fields.split()
        if not separator or int(prefix) != pid or len(fields) < 20:
            raise ValueError()
        state = fields[0]
        ppid, pgid, sid, birth = (int(fields[index]) for index in (1, 2, 3, 19))
        if (not 0 < pid <= 2147483647 or
                any(not 0 <= value <= 2147483647 for value in (ppid, pgid, sid)) or
                birth < 0 or len(state) != 1 or state not in b'RSDZTWtXxKIP'):
            raise ValueError()
    except (ValueError, IndexError) as exc:
        raise SDKError(f'invalid Linux proc stat for PID {pid}') from exc
    return ProcessRow(pid, ppid, sid, uid, birth, state)


class _Subreaper:
    """Read the native capability without changing the controller process."""

    def __init__(self):
        self.library = None

    def _call(self, option, value):
        if self.library is None:
            try:
                self.library = ctypes.CDLL(None, use_errno=True)
                self.library.prctl.restype = ctypes.c_int
                self.library.prctl.argtypes = (
                    ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
                    ctypes.c_ulong, ctypes.c_ulong)
            except (OSError, AttributeError) as exc:
                raise SDKError(f'Linux subreaper API is unavailable: {exc}') from exc
        if self.library.prctl(option, value, 0, 0, 0) != 0:
            error = ctypes.get_errno()
            raise SDKError(f'Linux subreaper operation failed: {os.strerror(error)}')

    def get(self):
        value = ctypes.c_int()
        self._call(37, ctypes.addressof(value))  # PR_GET_CHILD_SUBREAPER
        return value.value

subreaper = _Subreaper()


class LinuxSession:
    """Use coherent session rows and process-bound signal and wait handles."""

    def __init__(self):
        required = ((os, 'pidfd_open'),
                    (signal, 'pidfd_send_signal'), (time, 'CLOCK_BOOTTIME'))
        if any(not hasattr(module, name) for module, name in required):
            raise SDKError('Linux producer backend requires pidfd and CLOCK_BOOTTIME APIs')
        self.uid = os.geteuid()
        self.supervised = {}
        self.ticks = os.sysconf('SC_CLK_TCK')
        if self.ticks <= 0:
            raise SDKError('invalid Linux process clock tick frequency')
        subreaper.get()  # Read-only preflight. Only the supervisor adopts.
        own = self.read(os.getpid())
        if own is None or (own.sid, own.uid) != (os.getsid(0), self.uid):
            raise SDKError('Linux proc identity does not match the controller')
        try:
            fd = os.pidfd_open(os.getpid())
            try:
                signal.pidfd_send_signal(fd, 0)
            finally:
                os.close(fd)
        except OSError as exc:
            raise SDKError(f'Linux pidfd APIs failed their preflight: {exc}') from exc

    def now(self):
        # Linux stat starttime is BOOTTIME truncated to clock ticks. Require a
        # strictly earlier tick when excluding a process from an empty result.
        return time.clock_gettime_ns(time.CLOCK_BOOTTIME) * self.ticks // 1_000_000_000

    def _read_file(self, directory, name):
        fd = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=directory)
        with os.fdopen(fd, 'rb') as handle:
            data = handle.read(65537)
        if len(data) > 65536:
            raise SDKError(f'Linux proc {name} exceeds the size limit')
        return data

    def read(self, pid):
        """Pin the proc directory so stat and effective UID name one process."""
        try:
            directory = os.open(f'{PROC_ROOT}/{pid}', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
            try:
                data = self._read_file(directory, 'stat')
                status = self._read_file(directory, 'status')
            finally:
                os.close(directory)
        except (FileNotFoundError, ProcessLookupError):
            return None
        except OSError as exc:
            raise SDKError(f'cannot read Linux proc identity for PID {pid}: {exc}') from exc
        identities = [line.split()[1:] for line in status.splitlines() if line.startswith(b'Uid:')]
        if (len(identities) != 1 or len(identities[0]) != 4 or
                any(not field.isdigit() or len(field) > 10 or int(field) > 4294967295
                    for field in identities[0])):
            raise SDKError(f'invalid Linux proc effective UID for PID {pid}')
        return parse_stat(data, pid, int(identities[0][1]))

    def rows(self):
        try:
            names = os.listdir(PROC_ROOT)
        except OSError as exc:
            raise SDKError(f'cannot enumerate Linux proc PIDs: {exc}') from exc
        pids = [int(name) for name in names if name.isascii() and name.isdecimal()]
        if os.getpid() not in pids:
            raise SDKError('Linux proc enumeration does not include the controller')
        return {pid: self.read(pid) for pid in pids}

    def scan(self, sid, read_rows):
        owner = self.supervised.get(sid)
        if owner is not None and owner.drained():
            return set(), False
        barrier = self.now()
        members, uncertain = set(), False
        for pid, row in read_rows().items():
            if row is None:
                # A vanished PID may have forked after the directory snapshot.
                uncertain = True
            elif row.sid == sid:
                if row.uid != self.uid:
                    raise SDKError('owned producer changed its effective UID')
                if owner is None or pid != owner.pid:
                    members.add(pid)
            elif row.birth >= barrier:
                # PID reuse during the snapshot cannot classify the old PID.
                uncertain = True
        if sid < 0:
            # No POSIX process can acquire a negative session ID. Proc access
            # and row validation still ran before this preflight result.
            return set(), False
        if owner is not None:
            # Only the isolated reaper can prove the owned tree is empty.
            # Unrelated forks cannot delay that proof or hide an owned fork.
            return members, not members
        return members, uncertain

    def signal(self, pid, sid, number):
        owner = self.supervised.get(sid)
        if owner is not None and pid == owner.pid:
            return  # The private reaper must survive descendant signalling.
        try:
            fd = os.pidfd_open(pid)
            try:
                row = self.read(pid)
                if row is not None and row.sid == sid:
                    if row.uid != self.uid:
                        raise SDKError('owned producer changed its effective UID')
                    signal.pidfd_send_signal(fd, number)
            finally:
                os.close(fd)
        except ProcessLookupError:
            pass
        except OSError as exc:
            raise SDKError(f'cannot signal Linux owned PID {pid}: {exc}') from exc

class LinuxChild:
    """Expose the owned SID and producer status while the reaper remains."""

    def __init__(self, argv, options):
        self.argv = argv
        self.options = options
        self.pid = None
        self.producer_pid = None
        self.returncode = None
        self.launch_failed = False
        self.supervisor = None
        self.reader = None
        self.buffer = b''

    def start(self):
        read, write = os.pipe()
        try:
            helper = os.path.join(os.path.dirname(__file__), '_linux_supervisor.py')
            self.supervisor = subprocess.Popen(
                [sys.executable, '-B', '-I', '-S', helper, str(write), *self.argv],
                stdin=subprocess.DEVNULL, pass_fds=(write,),
                start_new_session=True, **self.options)
            # The kernel creates this SID before the helper can launch work.
            # It remains known even if the helper dies before its first report.
            self.pid = self.supervisor.pid
        except BaseException:
            os.close(read)
            raise
        finally:
            os.close(write)
        self.reader = read
        os.set_blocking(read, False)
        with selectors.DefaultSelector() as selector:
            selector.register(read, selectors.EVENT_READ)
            while self.producer_pid is None:
                self._receive()
                if self.producer_pid is None:
                    if self.supervisor.poll() is not None:
                        self._receive()
                        raise SDKError('Linux session supervisor exited before its producer report; shutdown is unproven')
                    selector.select(.05)
        return self.pid

    def _receive(self):
        while self.reader is not None:
            try:
                chunk = os.read(self.reader, 4096)
            except BlockingIOError:
                return
            if not chunk:
                os.close(self.reader)
                self.reader = None
                if self.buffer:
                    raise SDKError('truncated Linux session supervisor result')
                return
            self.buffer += chunk
            if len(self.buffer) > 65536:
                raise SDKError('Linux session supervisor result exceeds the size limit')
            while b'\n' in self.buffer:
                line, self.buffer = self.buffer.split(b'\n', 1)
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeError) as exc:
                    raise SDKError('invalid Linux session supervisor result') from exc
                if 'error' in record:
                    self.supervisor.wait()
                    os.close(self.reader)
                    self.reader = None
                    # The helper reports launch errors only before a producer
                    # exists. Its exit therefore proves there are no writers.
                    self.launch_failed = True
                    self.returncode = 0
                    raise OSError(record['errno'], record['error'], record.get('filename'))
                if 'pid' in record:
                    self.producer_pid = record['pid']
                if 'returncode' in record:
                    self.returncode = record['returncode']

    def poll(self):
        self._receive()
        if self.supervisor.poll() is not None:
            self._receive()
            if not self.launch_failed and (self.supervisor.returncode != 0 or self.returncode is None):
                raise SDKError('Linux session supervisor failed; descendant shutdown is unproven')
        return self.returncode

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.poll() is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.argv, timeout)
            time.sleep(.01)
        return self.returncode

    def drained(self):
        self.poll()
        # Exit zero follows waitpid(-1) returning ECHILD in the dedicated
        # supervisor. Its only children are this producer and its descendants.
        return self.supervisor.returncode == 0 or self.launch_failed
