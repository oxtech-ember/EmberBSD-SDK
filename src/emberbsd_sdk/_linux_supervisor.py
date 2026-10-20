# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Private Linux producer supervisor. Execute only through LinuxChild."""

import ctypes
import json
import os
import signal
import subprocess
import sys


def main():
    channel = int(sys.argv[1])

    def send(record):
        data = json.dumps(record).encode('ascii') + b'\n'
        while data:
            data = data[os.write(channel, data):]

    def child_signals():
        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(number, signal.SIG_DFL)

    # The controller owns shutdown policy. Its terminal signals must not kill
    # the reaper while a descendant still owns writable build files.
    for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(number, signal.SIG_IGN)
    try:
        library = ctypes.CDLL(None, use_errno=True)
        library.prctl.argtypes = (
            ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
            ctypes.c_ulong, ctypes.c_ulong)
        library.prctl.restype = ctypes.c_int
        if library.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        child = subprocess.Popen(sys.argv[2:], preexec_fn=child_signals)
    except OSError as exc:
        send(dict(error=str(exc), errno=exc.errno, filename=exc.filename))
        return 1
    send(dict(pid=child.pid))
    send(dict(returncode=child.wait()))
    while True:
        try:
            os.waitpid(-1, 0)
        except ChildProcessError:
            return 0


if __name__ == '__main__':
    raise SystemExit(main())
