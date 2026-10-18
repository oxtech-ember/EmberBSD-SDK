# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Read Darwin process birth ticks. Load native APIs only during preflight.

AI-assisted implementation for EmberBSD. The ABI is from Apple's public
libproc.h and sys/resource.h: proc_pid_rusage, RUSAGE_INFO_V0. It uses the
96-byte rusage_info_v0 record. No kernel structure or private flavor is used.
"""

import ctypes
import errno
import os
from .errors import SDKError


class _Usage(ctypes.Structure):
    _fields_ = [('uuid', ctypes.c_ubyte * 16)] + [
        (name, ctypes.c_uint64) for name in (
            'user', 'system', 'idle', 'interrupt', 'pageins', 'wired',
            'resident', 'footprint', 'start', 'exit')]


class DarwinIdentity:
    """Use monotonic Mach birth ticks, not wall-clock start times."""

    def __init__(self):
        if ctypes.sizeof(_Usage) != 96 or _Usage.start.offset != 80:
            raise SDKError('unsupported Darwin rusage_info_v0 ABI')
        try:
            self.library = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
            self.usage = self.library.proc_pid_rusage
            self.usage.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_void_p)
            self.usage.restype = ctypes.c_int
            self.clock = self.library.mach_absolute_time
            self.clock.argtypes = ()
            self.clock.restype = ctypes.c_uint64
        except (OSError, AttributeError) as exc:
            raise SDKError(f'Darwin process identity API is unavailable: {exc}') from exc
        before = self.now()
        first, second = self.birth(os.getpid()), self.birth(os.getpid())
        if first is None or first != second or not 0 < first <= before <= self.now():
            raise SDKError('Darwin process identity API failed its preflight probe')

    def now(self):
        return int(self.clock())

    def birth(self, pid):
        record = _Usage()
        ctypes.set_errno(0)
        if self.usage(pid, 0, ctypes.byref(record)) != 0:
            error = ctypes.get_errno()
            if error == errno.ESRCH:
                return None
            raise SDKError(f'cannot read Darwin birth identity for PID {pid}: {os.strerror(error)}')
        if record.start <= 0:
            raise SDKError(f'invalid Darwin birth identity for PID {pid}')
        return int(record.start)
