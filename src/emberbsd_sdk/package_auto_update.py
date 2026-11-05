# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Repeat the existing installed-package update without installing a service."""

import math
import os
import select
import signal
import sys
import threading

from .errors import SDKError


def watch_updates(update, interval):
    """Run now, then wait after each attempt. Request stop between attempts."""
    if not math.isfinite(interval) or interval <= 0 or interval > threading.TIMEOUT_MAX:
        raise SDKError('automatic update interval must be positive and within the platform wait limit')
    stopping = False
    reader, writer = os.pipe()
    previous = {}

    def request_stop(_number, _frame):
        nonlocal stopping
        stopping = True
        try:
            os.write(writer, b'\0')
        except BlockingIOError:
            pass

    try:
        os.set_blocking(writer, False)
        for name in ('SIGINT', 'SIGTERM', 'SIGHUP'):
            number = getattr(signal, name, None)
            if number is not None:
                previous[number] = signal.signal(number, request_stop)
        print(f'Automatic package updates: every {interval:g} seconds after each attempt.', file=sys.stderr)
        while not stopping:
            if update():
                print('Automatic update failed; the next attempt will reread pkgdb and retry.', file=sys.stderr)
            if stopping:
                break
            select.select([reader], [], [], interval)
        return 0
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
        os.close(writer)
        os.close(reader)
