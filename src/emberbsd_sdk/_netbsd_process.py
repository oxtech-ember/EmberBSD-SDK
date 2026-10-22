# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""NetBSD 11 process census and pre-exec kernel fork/exit tracking.

ABI source: NetBSD netbsd-11 4decc303f104deb1589104682b60bfb13cfb6a92,
sys/sys/sysctl.h and event.h, sys/kern/kern_event.c. Kernel creation/exit
notes identify owned lifetimes. p_ustart is wall clock and is not used.
Signals retain the existing Darwin check-then-kill contract, not pidfd atomicity.
"""
import ctypes
from dataclasses import dataclass
import errno
import os
import platform
import select
import signal
import subprocess
import sys
import time

from .errors import SDKError

PROC = 4
TRACK, TRACKERR, CHILD = 1, 2, 4
FORK, EXIT = 0x40000000, 0x80000000
EV_ERROR, EV_EOF = 0x4000, 0x8000
LIMIT = 8 * 1024 * 1024


class ProcPrefix(ctypes.Structure):
    """Caller-sized KERN_PROC2 prefix, through p_stat; no argv or names."""
    _fields_ = [
        ('addresses', ctypes.c_uint64 * 13),
        ('flags', ctypes.c_int32 * 3),
        ('pid', ctypes.c_int32), ('ppid', ctypes.c_int32),
        ('sid', ctypes.c_int32), ('pgid', ctypes.c_int32),
        ('tpgid', ctypes.c_int32), ('uid', ctypes.c_uint32),
        ('other_ids', ctypes.c_uint32 * 3),
        ('groups', ctypes.c_uint32 * 16),
        ('ngroups', ctypes.c_int16), ('jobc', ctypes.c_int16),
        ('tdev', ctypes.c_uint32), ('scheduler', ctypes.c_uint32 * 8),
        ('ticks', ctypes.c_uint64 * 3), ('trace', ctypes.c_uint64),
        ('traceflag', ctypes.c_int32), ('holdcnt', ctypes.c_int32),
        ('signals', ctypes.c_uint32 * 16), ('state', ctypes.c_int8)]


@dataclass(frozen=True)
class ProcessRow:
    pid: int
    ppid: int
    sid: int
    uid: int
    state: int


def decode_rows(data):
    stride = ctypes.sizeof(ProcPrefix)
    if len(data) > LIMIT or len(data) % stride:
        raise SDKError('invalid NetBSD KERN_PROC2 record length')
    rows = {}
    for offset in range(0, len(data), stride):
        p = ProcPrefix.from_buffer_copy(data, offset)
        if (p.pid < 0 or p.pid in rows or p.ppid < 0 or p.sid < 0 or
                not 1 <= p.state <= 8):
            raise SDKError('invalid or duplicate NetBSD process identity')
        rows[p.pid] = ProcessRow(p.pid, p.ppid, p.sid, p.uid, p.state)
    return rows


class TrackedTree:
    """Birth tokens come from kernel attachment, never sampled wall clock."""
    def __init__(self, root):
        self.root = root
        self.active = {root: 1}
        self.history = {root}
        self.serial = 1
        self.failure = None

    def accept(self, events):
        if self.failure:
            raise SDKError(self.failure)
        for event in events:
            flags, pid = event.fflags, event.ident
            if (event.filter != PROC or not 0 < pid <= 2147483647 or
                    event.flags & EV_ERROR or flags & TRACKERR or
                    flags & ~(CHILD | FORK | EXIT) or
                    (event.flags & EV_EOF and not flags & EXIT)):
                self.failure = 'invalid or lost NetBSD process tracking event'
            elif flags & CHILD:
                if flags != CHILD or event.data not in self.history or pid in self.active:
                    self.failure = 'unknown or duplicate NetBSD process birth'
                else:
                    self.serial += 1
                    self.active[pid] = self.serial
                    self.history.add(pid)
            elif pid not in self.active or not flags:
                self.failure = 'unknown NetBSD process lifetime event'
            elif flags & EXIT:
                del self.active[pid]
            if self.failure:
                raise SDKError(self.failure)


class NetBSDSession:
    def __init__(self):
        if sys.platform != 'netbsd11' or platform.machine() not in ('amd64', 'x86_64'):
            raise SDKError('NetBSD process ABI requires qualified NetBSD 11 amd64')
        required = ('kqueue', 'kevent', 'KQ_NOTE_TRACK', 'KQ_NOTE_TRACKERR',
                    'KQ_NOTE_CHILD', 'KQ_NOTE_FORK', 'KQ_NOTE_EXIT')
        if any(not hasattr(select, name) for name in required) or any(
                not hasattr(os, name) for name in ('waitid', 'P_PID', 'WEXITED', 'WNOWAIT', 'WNOHANG')):
            raise SDKError('NetBSD process tracking APIs are unavailable')
        if (ctypes.sizeof(ProcPrefix), ProcPrefix.pid.offset, ProcPrefix.uid.offset,
                ProcPrefix.state.offset) != (368, 116, 136, 360):
            raise SDKError('unsupported NetBSD KERN_PROC2 prefix ABI')
        self.uid = os.geteuid()
        self.supervised = {}
        try:
            self.library = ctypes.CDLL(None, use_errno=True)
            self.sysctl = self.library.sysctl
            self.sysctl.argtypes = (ctypes.POINTER(ctypes.c_int), ctypes.c_uint,
                                   ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
                                   ctypes.c_void_p, ctypes.c_size_t)
            self.sysctl.restype = ctypes.c_int
            byname = self.library.sysctlbyname
            byname.argtypes = (ctypes.c_char_p, ctypes.c_void_p,
                               ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t)
            byname.restype = ctypes.c_int
            curtain, size = ctypes.c_int(), ctypes.c_size_t(ctypes.sizeof(ctypes.c_int))
            if byname(b'security.curtain', ctypes.byref(curtain), ctypes.byref(size), None, 0) or size.value != 4 or curtain.value != 0:
                raise SDKError('NetBSD process enumeration requires security.curtain=0')
            queue = select.kqueue()
            try:
                event = select.kevent(os.getpid(), filter=PROC,
                    flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE, fflags=EXIT)
                queue.control([event], 0, 0)
            finally:
                queue.close()
        except (AttributeError, OSError) as exc:
            raise SDKError(f'NetBSD native process preflight failed: {exc}') from exc
        own = self.rows().get(os.getpid())
        if own is None or (own.ppid, own.sid, own.uid) != (os.getppid(), os.getsid(0), self.uid):
            raise SDKError('NetBSD process ABI does not match the controller')

    def describe(self):
        return dict(backend='netbsd-kqueue-session-v1', uid=self.uid,
                    scope='all process groups of the owned session',
                    credentials='producer must retain its effective UID and session',
                    birth='kernel NOTE_TRACK creation and NOTE_EXIT lifetime',
                    signals='session and tracked-lifetime checks before kill; not pidfd atomicity')

    def rows(self):
        stride = ctypes.sizeof(ProcPrefix)
        for _ in range(10):
            mib = (ctypes.c_int * 6)(1, 47, 0, 0, stride, 0)
            size = ctypes.c_size_t()
            if self.sysctl(mib, 6, None, ctypes.byref(size), None, 0):
                raise SDKError('cannot size NetBSD process census')
            capacity = (size.value // stride + 128) * stride
            if capacity > LIMIT:
                raise SDKError('NetBSD process census exceeds the size limit')
            mib[5] = capacity // stride
            data = ctypes.create_string_buffer(capacity)
            size.value = capacity
            if self.sysctl(mib, 6, data, ctypes.byref(size), None, 0):
                if ctypes.get_errno() == errno.ENOMEM:
                    continue
                raise SDKError('cannot enumerate NetBSD process census')
            if size.value > capacity:
                raise SDKError('NetBSD process census returned an invalid size')
            result = decode_rows(data.raw[:size.value])
            if os.getpid() not in result:
                raise SDKError('NetBSD census does not include the controller')
            return result
        raise SDKError('NetBSD process census did not stabilize')

    def child(self, argv, options):
        return NetBSDChild(self, argv, options)

    def scan(self, sid, read_rows):
        owner = self.supervised.get(sid)
        rows = read_rows()
        if sid < 0:
            return set(), False
        if owner is None:
            return {p.pid for p in rows.values() if p.sid == sid}, False
        owner.pump()
        for p in rows.values():
            if p.sid == sid:
                if p.uid != self.uid:
                    raise SDKError('owned producer changed its effective UID')
                if p.pid not in owner.tree.history:
                    raise SDKError('NetBSD census found an untracked owned process')
        # All process creation was tracked before release of the exec gate.
        # Unrelated process churn cannot turn this registry into false emptiness.
        members = set(owner.tree.active)
        for pid in members:
            p = rows.get(pid)
            if p is not None and (p.uid != self.uid or p.sid != sid):
                raise SDKError('owned producer changed its effective UID or session')
        if not members:
            owner.reap()
        return members, False

    def signal(self, pid, sid, number):
        owner = self.supervised.get(sid)
        if owner is None:
            raise SDKError('NetBSD signal has no owned process registry')
        owner.pump()
        birth = owner.tree.active.get(pid)
        if birth is None:
            return
        row = self.rows().get(pid)
        owner.pump()
        if owner.tree.active.get(pid) != birth:
            return
        if row is None:
            # NOTE_CHILD precedes SACTIVE; KERN_PROC2 omits SIDL embryos.
            # Keep the kernel lifetime active. The drain retries its census
            # within the existing deadline, without signalling an unknown row.
            return
        if (row.uid, row.sid) != (self.uid, sid):
            raise SDKError('NetBSD signal identity is foreign')
        try:
            if os.getsid(pid) == sid:
                os.kill(pid, number)
        except ProcessLookupError:
            pass
        except OSError as exc:
            raise SDKError(f'cannot signal NetBSD owned process: {exc}') from exc


class NetBSDChild:
    """Keep the direct child unreaped until its whole tracked tree exits."""
    def __init__(self, native, argv, options):
        self.native, self.argv, self.options = native, argv, options
        self.pid = None
        self.process = None
        self.queue = None
        self.tree = None
        self.returncode = None
        self.reaped = False
        self.released = False

    def start(self):
        read, write = os.pipe()
        try:
            helper = os.path.join(os.path.dirname(__file__), '_netbsd_launcher.py')
            self.process = subprocess.Popen([sys.executable, '-B', '-I', '-S', helper,
                str(read), *self.argv], pass_fds=(read,), start_new_session=True,
                **{'stdin': subprocess.DEVNULL, **self.options})
            self.pid = self.process.pid
            self.native.supervised[self.pid] = self
            self.tree = TrackedTree(self.pid)
            self.queue = select.kqueue()
            event = select.kevent(self.pid, filter=PROC,
                flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE,
                fflags=TRACK | FORK | EXIT)
            self.queue.control([event], 0, 0)
            row = self.native.rows().get(self.pid)
            if row is None or (row.sid, row.uid) != (self.pid, self.native.uid):
                raise SDKError('NetBSD gated child identity mismatch')
            # Mark possible release before write: cancellation may arrive
            # after the kernel accepts the byte but before Python returns.
            self.released = True
            os.write(write, b'G')
        except BaseException as exc:
            # An unreleased launcher cannot fork or exec the producer. Its direct
            # child PID remains reserved until this exact wait reaps it.
            os.close(write)
            write = None
            if self.process is not None and not self.released:
                self.process.wait(timeout=5)
                self.returncode = self.process.returncode
                self.reaped = True
                if self.tree is not None:
                    self.tree.active.clear()
                if self.queue is not None:
                    self.queue.close()
                    self.queue = None
            if isinstance(exc, (SDKError, KeyboardInterrupt, SystemExit)):
                raise
            raise SDKError(f'NetBSD gated producer launch failed: {exc}') from exc
        finally:
            os.close(read)
            if write is not None:
                os.close(write)
        return self.pid

    def pump(self):
        if self.reaped:
            if self.tree is not None:
                self.tree.accept([])
            return
        if self.queue is None:
            raise SDKError('NetBSD process tracking was not established')
        # A full batch does not establish that all pending birth notes were read.
        for _ in range(1024):
            try:
                events = self.queue.control(None, 256, 0)
            except OSError as exc:
                raise SDKError(f'NetBSD process tracking failed: {exc}') from exc
            self.tree.accept(events)
            if len(events) < 256:
                return
        raise SDKError('NetBSD process tracking did not reach a complete batch')

    def poll(self):
        if self.pid is None or self.reaped:
            return self.returncode
        try:
            status = os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except OSError as exc:
            raise SDKError(f'NetBSD root anchor was lost: {exc}') from exc
        if status is not None:
            self.returncode = status.si_status if status.si_code == os.CLD_EXITED else -status.si_status
        return self.returncode

    def reap(self):
        self.pump()
        if self.tree.active:
            raise SDKError('NetBSD root cannot be reaped with live tracked descendants')
        if self.poll() is None:
            raise SDKError('NetBSD root exit was not waitable')
        if not self.reaped:
            self.process.wait()
            self.reaped = True
            self.queue.close()
            self.queue = None
