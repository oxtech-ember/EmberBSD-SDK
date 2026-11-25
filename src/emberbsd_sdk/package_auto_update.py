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
    first_retry = min(30, interval)
    retry_delay = first_retry

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
        print(f'Automatic package updates run every {interval:g} seconds after a successful attempt.', file=sys.stderr)
        while not stopping:
            failed = update()
            if stopping:
                break
            if failed:
                delay = retry_delay
                retry_delay = min(interval, retry_delay * 2)
                print(f'Automatic update failed. Retrying in {delay:g} seconds.', file=sys.stderr)
            else:
                delay = interval
                retry_delay = first_retry
            select.select([reader], [], [], delay)
        return 0
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
        os.close(writer)
        os.close(reader)
