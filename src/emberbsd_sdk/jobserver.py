# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""A GNU make jobserver on a FIFO for one job budget.

The SDK keeps one job budget from resolve_jobs. Ninja 1.13 and later use a
FIFO jobserver as clients when MAKEFLAGS names it and no -j option is given.
Each client has one implicit job slot; the FIFO holds one token for each
other slot. bmake does not use the GNU protocol, so a stage that runs bmake
takes the free tokens before it starts and gives them back when it stops.
It takes them once: a stage that starts while other jobs hold tokens keeps
the smaller number of slots until it stops.

A job that stops on SIGKILL cannot give its tokens back, and the budget is
smaller for the rest of that run. Each run makes a new FIFO.
"""

from contextlib import contextmanager
import os
from pathlib import Path
import re
import signal
import stat

from .errors import SDKError

TOKEN = b'+'
_AUTH = re.compile(r'(?:^|\s)--jobserver-auth=fifo:(\S+)')


def _write(descriptor, data):
    """Write all of DATA; a pipe can take less than all of it at once."""
    while data:
        data = data[os.write(descriptor, data):]


def _terminate(signum, frame):
    raise SystemExit(128 + signum)


@contextmanager
def serve(path, jobs):
    """Create the FIFO PATH with JOBS - 1 tokens and yield its MAKEFLAGS value."""
    if type(jobs) is not int or jobs < 1:
        raise SDKError('the job budget must be a positive integer')
    path = Path(path)
    if path.is_dir() and not path.is_symlink():
        raise SDKError(f'the jobserver path {path} is a directory')
    path.unlink(missing_ok=True)
    os.mkfifo(path, 0o600)
    # Keep one descriptor open for reading and writing, so that the FIFO
    # never reports end of file while a client releases its tokens.
    descriptor = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    try:
        _write(descriptor, TOKEN * (jobs - 1))
        yield f'-j{jobs} --jobserver-auth=fifo:{path}'
    finally:
        os.close(descriptor)
        path.unlink(missing_ok=True)


def fifo(makeflags):
    """Return the jobserver FIFO that MAKEFLAGS names, or None."""
    match = _AUTH.search(makeflags or '')
    if match is None:
        return None
    path = match.group(1)
    try:
        mode = os.stat(path).st_mode
    except OSError as exc:
        raise SDKError(f'cannot open the jobserver {path}: {exc.strerror}') from exc
    if not stat.S_ISFIFO(mode):
        raise SDKError(f'the jobserver {path} is not a FIFO')
    return path


@contextmanager
def slots(makeflags, wanted):
    """Yield the number of job slots, 1 to WANTED, that this job may use.

    The job already has one slot from its client. Take the free tokens for
    the other slots without waiting, and give them back at the end."""
    path = fifo(makeflags)
    if path is None:
        # Without a jobserver, the job uses its own budget.
        yield max(1, wanted)
        return
    if wanted <= 1:
        yield 1
        return
    descriptor = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    taken = b''
    # The default SIGTERM action skips finally and loses the tokens.
    previous = signal.signal(signal.SIGTERM, _terminate)
    try:
        try:
            taken = os.read(descriptor, wanted - 1)
        except BlockingIOError:
            taken = b''
        yield 1 + len(taken)
    finally:
        if taken:
            _write(descriptor, taken)
        os.close(descriptor)
        signal.signal(signal.SIGTERM, previous)
