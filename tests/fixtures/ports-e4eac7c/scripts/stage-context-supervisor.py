#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
# Origin: EmberBSD contributors (AI-assisted).
"""Hold SDK locks over an owned pkgsrc stage process session."""
import argparse
from contextlib import ExitStack
import os
from pathlib import Path
import signal
import sys
import time


def drain_or_hold(group, ownership_error, *, delay=.25):
    """Never return while the SDK cannot prove the owned session drained."""
    reported = False
    while True:
        try:
            group.stop()
            return
        except ownership_error:
            if not reported:
                print('Cannot prove stage session drained; retaining stage locks and retrying.', file=sys.stderr, flush=True)
                reported = True
            time.sleep(delay)


def main():
    if sys.argv[1:2] == ['--entry']:
        command = sys.argv[2:]
        if command[:1] == ['--']:
            command = command[1:]
        if len(command) != 5 or command[2] not in ('build', 'package', 'package-install', 'patch-refresh', 'buildlink-refresh'):
            raise ValueError('invalid supervised cross-bulk entry')
        os.environ['CROSS_BULK_SUPERVISED'] = '|'.join([str(os.getppid()), *command[2:]])
        os.execv(command[0], command)
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--sdk-source', required=True, type=Path)
    p.add_argument('--locks', required=True, type=Path)
    p.add_argument('--bulk', action='store_true')
    p.add_argument('--manifest', type=Path)
    p.add_argument('command', nargs=argparse.REMAINDER)
    a = p.parse_args()
    if a.command[:1] == ['--']:
        a.command = a.command[1:]
    if not a.command or not os.path.isabs(a.command[0]):
        p.error('an absolute selected command is required')
    sys.path.insert(0, str(a.sdk_source / 'src'))
    from emberbsd_sdk import jsonio, paths, process
    from emberbsd_sdk.errors import ProcessOwnershipError

    spec = jsonio.read_json(str(a.locks))
    if not isinstance(spec, list) or not spec:
        raise ValueError('stage lock list is empty or invalid')
    groups = {}
    work_roots = {}
    for entry in spec:
        if not isinstance(entry, dict) or set(entry) != {'root', 'work'}:
            raise ValueError('invalid stage lock entry')
        declared_root = paths.canonical_new(entry['root'])
        value = entry['work']
        work = paths.canonical_new(value)
        if not Path(work).is_relative_to(Path(declared_root)) or work == declared_root:
            raise ValueError('stage path is outside its lock root')
        parent = Path(work).parent
        parent.mkdir(parents=True, exist_ok=True)
        root = paths.canonical_existing(str(parent))
        paths.real_directory(root)
        work = paths.canonical_new(value)
        if os.path.lexists(value):
            paths.real_directory(value)
        if Path(work).parent != Path(root) or not Path(work).is_relative_to(Path(declared_root)):
            raise ValueError('stage path changed while resolving its lock root')
        previous_root = work_roots.setdefault(work, root)
        if previous_root != root:
            raise ValueError('same stage has different lock roots')
        groups.setdefault(root, set()).add(work)

    interrupted = []
    def on_signal(number, _frame):
        interrupted.append(number)
    for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(number, on_signal)
    with ExitStack() as stack:
        for root in sorted(groups):
            stack.enter_context(process.ContextLocks(sorted(groups[root]), root))
        env = os.environ.copy()
        if a.bulk:
            if a.manifest is None:
                raise ValueError('supervised cross-bulk requires a stage manifest')
            values = [entry['work'] for entry in spec]
            if any(not isinstance(value, str) or '\n' in value or '\r' in value for value in values):
                raise ValueError('invalid stage manifest path')
            fd = os.open(a.manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            with os.fdopen(fd, 'w') as manifest:
                manifest.write(''.join(value + '\n' for value in values))
                manifest.flush()
                os.fsync(manifest.fileno())
            env['CROSS_BULK_STAGE_MANIFEST'] = str(a.manifest)
        if a.bulk and len(a.command) == 5 and a.command[2] in ('patch-refresh', 'buildlink-refresh'):
            if len(spec) != 1:
                raise ValueError('stage refresh requires exactly one stage lock')
            env['CROSS_BULK_LOCKED_WORK'] = str(next(iter(next(iter(groups.values())))))
        command = a.command
        if a.bulk:
            command = [sys.executable, '-B', str(Path(__file__).resolve()), '--entry', '--', *command]
        group = process.OwnedProcessGroup(command, cwd=os.getcwd(), env=env,
                                          stdin=None, stdout=None, stderr=None)
        group.start()
        try:
            while group.poll() is None and not interrupted:
                time.sleep(.05)
            result = group.poll()
            # A dead stage shell can leave bmake/compiler descendants writing.
            while not interrupted and group.inspector.members(group.pid):
                time.sleep(.05)
        finally:
            # An inspection failure must not silently release active stage locks.
            drain_or_hold(group, ProcessOwnershipError)
            if a.bulk:
                a.manifest.unlink()
        if interrupted:
            return 128 + interrupted[0]
        return 128 - result if result is not None and result < 0 else result if result is not None else 1


if __name__ == '__main__':
    raise SystemExit(main())
