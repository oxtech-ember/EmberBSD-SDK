# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Read exact process arguments, executable path and native birth identity.

AI-assisted implementation from Linux proc_pid_{stat,cmdline,exe}(5) and
Apple's public sysctl.h KERN_PROCARGS2, libproc.h proc_pidpath and
proc_pid_rusage interfaces. This module does not send signals.
Sources: https://github.com/apple-oss-distributions/xnu/blob/main/bsd/sys/sysctl.h
https://github.com/apple-oss-distributions/Libc/blob/main/include/libproc.h
https://man7.org/linux/man-pages/man5/proc_pid_cmdline.5.html
https://man7.org/linux/man-pages/man5/proc_pid_stat.5.html
https://man7.org/linux/man-pages/man5/proc_pid_exe.5.html
"""
import ctypes
from dataclasses import dataclass
import errno
import os
import struct
import sys
from .errors import SDKError

PROCESS_IDENTITY_API = 1
_LIMIT = 8 * 1024 * 1024


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    birth: int
    executable: str
    argv: tuple[str, ...]
    backend: str


def _darwin_arguments(data):
    """Decode argc, executable, alignment NULs, then exactly argc strings."""
    if len(data) < 5:
        raise SDKError('truncated Darwin process arguments')
    argc = struct.unpack_from('=i', data)[0]
    if not 1 <= argc <= _LIMIT // 2:
        raise SDKError('invalid Darwin process argc')
    end = data.find(b'\0', 4)
    if end < 5:
        raise SDKError('missing Darwin executable path')
    position = end + 1
    while position < len(data) and data[position] == 0:
        position += 1
    result = []
    for _ in range(argc):
        end = data.find(b'\0', position)
        if end < position:
            raise SDKError('truncated Darwin process argv')
        result.append(os.fsdecode(data[position:end]))
        position = end + 1
    return tuple(result)


class _Darwin:
    def __init__(self):
        from ._darwin_process import DarwinIdentity
        self.identity = DarwinIdentity()
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.sysctl = self.libc.sysctl
        self.sysctl.argtypes = (ctypes.POINTER(ctypes.c_int), ctypes.c_uint,
                               ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
                               ctypes.c_void_p, ctypes.c_size_t)
        self.sysctl.restype = ctypes.c_int
        self.path = self.identity.library.proc_pidpath
        self.path.argtypes = (ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32)
        self.path.restype = ctypes.c_int

    def executable(self, pid):
        buffer = ctypes.create_string_buffer(4096)  # PROC_PIDPATHINFO_MAXSIZE
        if self.path(pid, buffer, len(buffer)) <= 0:
            error = ctypes.get_errno()
            if error == errno.ESRCH:
                return None
            raise SDKError(f'cannot read Darwin executable for PID {pid}: {os.strerror(error)}')
        return os.fsdecode(buffer.value)

    def arguments(self, pid):
        mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
        size = ctypes.c_size_t()
        if self.sysctl(mib, 3, None, ctypes.byref(size), None, 0):
            error = ctypes.get_errno()
            if error == errno.ESRCH:
                return None
            raise SDKError(f'cannot size Darwin argv for PID {pid}: {os.strerror(error)}')
        if not 0 < size.value <= _LIMIT:
            raise SDKError('Darwin process argv exceeds the size limit')
        buffer = ctypes.create_string_buffer(size.value)
        if self.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0):
            error = ctypes.get_errno()
            if error == errno.ESRCH:
                return None
            raise SDKError(f'cannot read Darwin argv for PID {pid}: {os.strerror(error)}')
        return _darwin_arguments(buffer.raw[:size.value])

    def snapshot(self, pid):
        before = self.identity.lifetime(pid)
        if before is None:
            return None
        birth, exited = before
        if exited:
            return None
        try:
            first = self.executable(pid)
            argv = self.arguments(pid)
            repeated = self.arguments(pid)
            second = self.executable(pid)
        except SDKError:
            # Darwin may deny argv access after exit, before the parent reaps
            # its child. Permission errors for a live or unproven PID remain
            # failures. A recycled PID cannot prove the earlier process exited.
            after = self.identity.lifetime(pid)
            if after is not None:
                if birth != after[0]:
                    raise SDKError('process identity changed during the Darwin snapshot')
                if after[1]:
                    return None
            raise
        after = self.identity.lifetime(pid)
        if after is None:
            return None
        if birth != after[0]:
            raise SDKError('process identity changed during the Darwin snapshot')
        if after[1]:
            return None
        if first is None or first != second or argv is None or argv != repeated:
            raise SDKError('process identity changed during the Darwin snapshot')
        return ProcessIdentity(pid, birth, second, argv, 'darwin-rusage-procargs-v1')


def _linux_snapshot(pid):
    from ._linux_process import parse_stat
    try:
        descriptor = os.open(f'/proc/{pid}', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    try:
        def read(name):
            fd = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=descriptor)
            with os.fdopen(fd, 'rb') as handle:
                data = handle.read(_LIMIT + 1)
            if len(data) > _LIMIT:
                raise SDKError('Linux process record exceeds the size limit')
            return data
        try:
            before = parse_stat(read('stat'), pid, 0)
            if before.state in (b'Z', b'X', b'x'):
                return None
            first = os.readlink('exe', dir_fd=descriptor)
            raw = read('cmdline')
            repeated = read('cmdline')
            second = os.readlink('exe', dir_fd=descriptor)
            after = parse_stat(read('stat'), pid, 0)
        except (FileNotFoundError, ProcessLookupError):
            return None
        if before.birth != after.birth or first != second or raw != repeated or not raw.endswith(b'\0'):
            raise SDKError('process identity changed during the Linux snapshot')
        return ProcessIdentity(pid, after.birth, second,
                               tuple(os.fsdecode(arg) for arg in raw[:-1].split(b'\0')),
                               'linux-proc-identity-v1')
    finally:
        os.close(descriptor)


def process_identity(pid):
    """Return a coherent read-only identity, or None for an absent/exited PID.

    Arguments and the executable path come from native interfaces, not a
    whitespace-split ps command. Darwin requires conventional nonempty argv[0]:
    KERN_PROCARGS2 cannot distinguish an empty argv[0] from padding. Later
    empty arguments are preserved. The kernel interfaces do not provide an
    atomic whole-process snapshot. Repeated executable/argv/birth observations
    must agree, but callers must recheck the full identity before an operation.
    This read-only snapshot does not provide a process-bound signal handle.
    """
    if type(pid) is not int or not 1 <= pid <= 2147483647:
        raise SDKError('process PID must be an integer from 1 to 2147483647')
    try:
        if sys.platform == 'darwin':
            return _Darwin().snapshot(pid)
        if sys.platform == 'linux':
            return _linux_snapshot(pid)
        raise SDKError('process identity is qualified only on Darwin and Linux')
    except OSError as exc:
        raise SDKError(f'cannot read process identity for PID {pid}: {exc}') from exc
