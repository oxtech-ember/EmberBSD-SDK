# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD; AI-assisted shared linker resource control.
"""Serialize foreground linker groups without leaving the build's session."""

import argparse
import os
import signal
import subprocess
import sys
import time

from .errors import SDKError
from .paths import canonical_existing, canonical_new, real_directory
from .process import ContextLocks, SessionInspector, _argv

LINK_LAUNCHER_API = 1
_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


class LinkGroup:
    """Control one foreground PGID in the caller's inherited Darwin SID.

    Commands must retain their UID, SID and PGID. A surrounding SDK producer
    still owns the complete build session, including this group.
    """

    def __init__(self):
        if sys.platform != 'darwin':
            raise SDKError('link launcher currently requires the qualified Darwin backend')
        self.inspector = SessionInspector()
        self.sid = os.getsid(0)
        self.child = None

    def start(self, argv):
        self.child = subprocess.Popen(_argv(argv), process_group=0)

    def members(self):
        deadline = time.monotonic() + self.inspector.empty_snapshot_timeout
        while True:
            rows, uncertain = self.inspector._scan(self.sid)
            found = set()
            for pid in rows:
                try:
                    before = self.inspector.identity.birth(pid)
                    pgid = os.getpgid(pid)
                    after = self.inspector.identity.birth(pid)
                except (ProcessLookupError, SDKError):
                    uncertain = True
                    continue
                if pgid == self.child.pid:
                    found.add(pid)
                elif before != after:
                    uncertain = True
            if found or not uncertain:
                return found
            if time.monotonic() >= deadline:
                raise SDKError('cannot prove that the linker process group is empty')
            time.sleep(.02)

    def stop(self):
        if self.child is None:
            return
        for number, delay in ((signal.SIGTERM, 2), (signal.SIGKILL, 10)):
            deadline = time.monotonic() + delay
            while True:
                self.child.poll()
                members = self.members()
                if not members:
                    self.child.wait()
                    return
                for pid in sorted(members):
                    try:
                        if os.getsid(pid) == self.sid and os.getpgid(pid) == self.child.pid:
                            os.kill(pid, number)
                    except ProcessLookupError:
                        pass
                if time.monotonic() >= deadline:
                    break
                time.sleep(.05)
        raise SDKError('linker process group did not drain')


def run(argv, *, context, lock_root, interrupted=lambda: None):
    """Inherit argv, cwd, environment and stdio; return the command status."""
    argv = _argv(argv)
    root = canonical_existing(lock_root)
    real_directory(root)
    context = canonical_new(context)
    group = LinkGroup()  # Refuse an unsupported host before lock-file writes.
    lock = ContextLocks([context], root)
    def check_interrupt():
        number = interrupted()
        if number is not None:
            raise KeyboardInterrupt(number)

    while True:
        check_interrupt()
        try:
            lock.acquire()
            break
        except SDKError as exc:
            if not isinstance(exc.__cause__, BlockingIOError):
                raise
            time.sleep(.1)
    try:
        try:
            check_interrupt()
            group.start(argv)
            while (code := group.child.poll()) is None:
                check_interrupt()
                time.sleep(.05)
            check_interrupt()
            if group.members():
                raise SDKError('linker left live processes in its group')
            return code
        finally:
            try:
                group.stop()
            except (SDKError, OSError) as exc:
                # Keep the resource fence. The outer producer must reject
                # this run and inspect its owned session before reuse.
                print(f'link launcher HOLD: {exc}; resource lock retained', file=sys.stderr, flush=True)
                while True:
                    time.sleep(30)
    finally:
        lock.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lock-root', required=True)
    parser.add_argument('--context', required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    pending = []
    def record_interrupt(number, frame):
        pending.append(number)

    # A handler must not raise between child creation and assignment, or while
    # entering drainage. Check pending signals only at explicit safe points.
    previous = {number: signal.signal(number, record_interrupt) for number in _SIGNALS}
    try:
        code = run(command, context=args.context, lock_root=args.lock_root,
                   interrupted=lambda: pending[0] if pending else None)
        if pending:
            return 128 + pending[0]
        return 128 - code if code < 0 else code
    except KeyboardInterrupt as exc:
        return 128 + (exc.args[0] if exc.args else signal.SIGINT)
    except (SDKError, OSError) as exc:
        print(f'link launcher: {exc}', file=sys.stderr)
        return 125
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
