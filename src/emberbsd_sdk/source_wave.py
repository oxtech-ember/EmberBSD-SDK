# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Own the Ports build lock for a whole source schedule.

The client computes waves. The published lock PID starts the controller and
records its identity before the controller may exec. An outer drainer survives
that PID and removes the lock only after its own owner was killed, the token
still matches, the recorded controller is gone, and no observed descendant
identity is still alive. Logs are copied to ``STATE/logs`` before the private
schedule is removed. ``ps`` creation time is one-second resolution and is not
absolute proof; a process reparented to init or launchd is not signaled from
that stamp alone.
"""

import contextvars
import json
import os
from pathlib import Path
import secrets
import select
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time

from .errors import SDKError


_RECEIPTS = ('done', 'failed', 'blocked', 'skipped')
_COPIED = ('edges', 'packages', 'progress', *_RECEIPTS)
_CONTROLLER_READY = contextvars.ContextVar('ember_controller_ready', default=None)
_CONTROLLER_PULSE = contextvars.ContextVar('ember_controller_pulse', default=None)
_OWNER_SEEN = {}


def current_controller_ready():
    """Hook installed around the production ``run_source`` call, if any."""
    return _CONTROLLER_READY.get()


def current_controller_pulse():
    """Single-threaded poll installed by the lock owner, if any."""
    return _CONTROLLER_PULSE.get()


class SourceWaveLock:
    """Hold ``STATE/build.lock`` until this process ends or asks to stop."""

    def __init__(self, state):
        self.state = Path(state)
        self._parent, child = socket.socketpair()
        self._death = os.pipe()
        self._pid = os.fork()
        if self._pid == 0:
            try:
                os.close(self._death[1])
                self._parent.close()
                _supervise(child, self._death[0], self.state)
            except BaseException as exc:
                try:
                    _send(child, {'op': 'error', 'error': str(exc)})
                except OSError:
                    pass
                os._exit(1)
            os._exit(0)
        os.close(self._death[0])
        child.close()
        self._buffer = b''
        ready = self._read()
        if ready.get('op') == 'error':
            self.close()
            raise SDKError(ready.get('error') or 'source wave lock failed')
        if ready.get('op') != 'ready':
            self.close()
            raise SDKError('source wave lock did not start')
        self.pid = ready['pid']

    def build(self, config, ready, binaries):
        self._send({'op': 'build', 'config': config, 'ready': list(ready),
                    'binaries': [list(item) for item in binaries]})
        return self._result()

    def exec_controller(self, argv, env, ready):
        """Run one absolute controller against a private copy of the schedule."""
        self._send({'op': 'exec', 'argv': list(argv), 'env': dict(env), 'ready': list(ready)})
        return self._result()

    def _result(self):
        reply = self._read()
        if reply.get('op') == 'error':
            raise SDKError(reply.get('error') or 'source wave failed')
        if reply.get('op') != 'built':
            raise SDKError('source wave lock returned no build result')
        return reply['receipts']

    def close(self):
        """Stop the controller, if one is running, and wait until the lock is released.

        Closing the death pipe is the stop request. The published owner drains
        the controller it started; this wait does not depend on that controller
        noticing a terminal signal.
        """
        if self._parent is None:
            return
        death = self._death[1]
        self._death = (None, None)
        try:
            os.close(death)
        except OSError:
            pass
        try:
            self._parent.close()
        except OSError:
            pass
        self._parent = None
        while True:
            try:
                os.waitpid(self._pid, 0)
                break
            except InterruptedError:
                continue
            except ChildProcessError:
                break

    def _send(self, message):
        data = json.dumps(message).encode('utf-8') + b'\n'
        view = memoryview(data)
        while view:
            sent = self._parent.send(view)
            view = view[sent:]

    def _read(self):
        while b'\n' not in self._buffer:
            chunk = self._parent.recv(65536)
            if not chunk:
                raise SDKError('source wave lock closed')
            self._buffer += chunk
        line, self._buffer = self._buffer.split(b'\n', 1)
        try:
            message = json.loads(line.decode('utf-8'))
        except (UnicodeError, ValueError) as exc:
            raise SDKError('source wave lock sent an invalid result') from exc
        if not isinstance(message, dict):
            raise SDKError('source wave lock sent an invalid result')
        return message


def _send(sock, message):
    data = json.dumps(message).encode('utf-8') + b'\n'
    view = memoryview(data)
    while view:
        sent = sock.send(view)
        view = view[sent:]


def _ignore_terminal_signals():
    for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGPIPE):
        try:
            signal.signal(number, signal.SIG_IGN)
        except OSError:
            pass


def _process_table():
    """pid -> (ppid, state, creation). The ``ps`` helper itself is not a build process."""
    last = ''
    for columns in ('pid=,ppid=,stat=,lstart=,command=', 'pid=,ppid=,state=,lstart=,command='):
        result = subprocess.run(
            ['ps', '-ax', '-o', columns], capture_output=True, text=True, check=False)
        last = result.stderr.strip()
        if result.returncode != 0 or not result.stdout.strip():
            continue
        rows = {}
        for line in result.stdout.splitlines():
            parts = line.strip().split()
            # lstart is five words: "Fri Nov  6 22:51:34 2026". Darwin and Linux both print it.
            if len(parts) < 8 or not parts[0].isdigit() or not parts[1].isdigit():
                continue
            command = ' '.join(parts[8:])
            tool = command.split()[0] if command else ''
            if tool == 'ps' or tool.endswith('/ps'):
                continue
            rows[int(parts[0])] = (int(parts[1]), parts[2], ' '.join(parts[3:8]))
        if rows:
            return rows
    raise SDKError('ps parent walk failed' + (f': {last}' if last else ''))


def _parents():
    """Parent of every process, from ``ps``. Darwin and Linux both provide this."""
    return {pid: row[0] for pid, row in _process_table().items()}


def _descendants(pid):
    if pid <= 1:
        raise SDKError('refusing to walk host init')
    return [row[0] for row in _live_descendants(_process_table(), pid)]


def _live_descendants(rows, pid):
    """Current non-zombie descendants as ``(pid, ppid, creation)`` in parent-before-child order."""
    children = {}
    for child, (parent, _state, _created) in rows.items():
        children.setdefault(parent, []).append(child)
    found = []

    def walk(current):
        for child in children.get(current, ()):
            parent, state, created = rows[child]
            if state.startswith(('Z', 'z')):
                continue
            found.append((child, parent, created))
            walk(child)

    walk(pid)
    return found


def _creation(pid):
    """Creation stamp from ``ps``, or None when that PID is not a live process."""
    if pid <= 1:
        return None
    for _ in range(50):
        try:
            row = _process_table().get(pid)
        except SDKError:
            return None
        if row is not None and not row[1].startswith(('Z', 'z')):
            return row[2]
        time.sleep(0.02)
    return None


def _linux_start(pid):
    """Kernel start time from ``/proc``, or None when this host has no such stamp.

    ``ps`` ``lstart`` is one second. This tick count is the stronger identity
    where ``/proc`` exists. Darwin does not provide it, and ``lstart`` alone is
    not treated as proof that a reparented PID is still the same process.
    """
    if pid <= 1:
        return None
    try:
        stat_text = Path(f'/proc/{pid}/stat').read_text()
    except OSError:
        return None
    end = stat_text.rfind(')')
    if end < 0:
        return None
    fields = stat_text[end + 2:].split()
    # starttime is proc field 22, the 20th field after comm.
    if len(fields) < 20 or not fields[19].isdigit():
        return None
    return fields[19]


def _in_tree(rows, root, pid):
    return any(child == pid for child, _parent, _stamp in _live_descendants(rows, root))


def _same_identity(rows, pid, created, starttime):
    """True when this PID still matches an identity observed in the controller tree.

    A different ``lstart``, or a different kernel start time when both sides
    have one, means the PID was reused. A matching ``lstart`` without a kernel
    start time does not prove identity, but it is enough to keep the lock.
    """
    row = rows.get(pid)
    if row is None or row[1].startswith(('Z', 'z')) or row[2] != created:
        return False
    if not starttime:
        return True
    fresh = _linux_start(pid)
    if fresh is None:
        return True
    return fresh == starttime


def _row_gone(pid):
    """True when a fresh ``ps`` row is missing or already a zombie."""
    try:
        row = _process_table().get(pid)
    except SDKError:
        return False
    return row is None or row[1].startswith(('Z', 'z'))


def _signal_proven(pid, created, root=None, starttime=None):
    """Signal one PID only after a fresh identity check.

    True means the process was already gone or this call signaled it. None
    means it is no longer inside ``root`` (its parent may now be init or
    launchd) and was not signaled. False means a live process did not match
    and nothing was signaled. A process that exits between the two reads is
    gone, not a mismatched identity. A remembered PID is never signaled once
    it has left the tree, and one-second ``lstart`` is never enough to signal
    a process init or launchd has inherited.
    """
    if pid <= 1 or not created:
        return False
    try:
        rows = _process_table()
    except SDKError:
        return False
    row = rows.get(pid)
    if row is None or row[1].startswith(('Z', 'z')):
        return True
    if row[2] != created:
        return False
    if root is not None and (row[0] <= 1 or not _in_tree(rows, root, pid)):
        return None
    if root is None and row[0] <= 1 and not starttime:
        return False
    current = _linux_start(pid)
    if starttime and current != starttime:
        if current is None and _row_gone(pid):
            return True
        return False
    try:
        rows = _process_table()
    except SDKError:
        return False
    row = rows.get(pid)
    if row is None or row[1].startswith(('Z', 'z')):
        return True
    if row[2] != created:
        return False
    if root is not None and (row[0] <= 1 or not _in_tree(rows, root, pid)):
        return None
    if root is None and row[0] <= 1 and not starttime:
        return False
    fresh = _linux_start(pid)
    if current is not None and fresh != current:
        if fresh is None and _row_gone(pid):
            return True
        return False
    if starttime and fresh != starttime:
        if fresh is None and _row_gone(pid):
            return True
        return False
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    return True


def _signal_descendants(rows, root):
    """Signal one fresh snapshot from the leaves up.

    A child that has left the tree is skipped. A changed creation stamp or
    kernel start time stops the drain and does not signal that PID.
    """
    for child, _parent, stamp in reversed(_live_descendants(rows, root)):
        if _signal_proven(child, stamp, root=root) is False:
            return False
    return True


def _stop_recorded(pid, created, starttime=None):
    """Stop one recorded controller and children still parented under it.

    The controller is proven by the identity captured before it could exec.
    A child is signaled only while a fresh row still places it in that tree.
    A PID whose parent is now init or launchd is not signaled. Without a
    stored kernel start time, the controller itself is not signaled once init
    or launchd has inherited it: one-second ``lstart`` is not proof. False
    means the lock must stay.
    """
    if pid <= 1 or not created:
        return False
    confirmed = False
    for _ in range(50):
        rows = _process_table()
        row = rows.get(pid)
        if row is None or row[1].startswith(('Z', 'z')):
            return confirmed and not _live_descendants(rows, pid)
        if row[2] != created:
            return False
        if starttime and _linux_start(pid) != starttime:
            if _linux_start(pid) is None and _row_gone(pid):
                return confirmed and not _live_descendants(_process_table(), pid)
            return False
        confirmed = True
        if not _signal_descendants(rows, pid):
            return False
        if _signal_proven(pid, created, starttime=starttime) is not True:
            return False
        time.sleep(0.05)
    rows = _process_table()
    row = rows.get(pid)
    if starttime and row is not None and not row[1].startswith(('Z', 'z')) and _linux_start(pid) == starttime:
        return False
    return (row is None or row[1].startswith(('Z', 'z')) or row[2] != created) and not _live_descendants(rows, pid)


def _stop_our_children():
    """Stop live descendants of this process. Unproven rows leave the lock held."""
    me = os.getpid()
    for _ in range(50):
        rows = _process_table()
        descendants = _live_descendants(rows, me)
        if not descendants:
            return True
        if not _signal_descendants(rows, me):
            return False
        time.sleep(0.05)
    return not _live_descendants(_process_table(), me)


def _read_text(path):
    if not path.is_file() or path.is_symlink():
        return ''
    return path.read_text()


def _new_lines(before, current):
    previous = set(before.splitlines())
    return [line for line in current.splitlines() if line.strip() and line not in previous]


def _private_schedule(real_state, ready):
    private = Path(tempfile.mkdtemp(prefix='ember-wave-'))
    for name in _COPIED:
        source = real_state / name
        target = private / name
        if source.is_file() and not source.is_symlink():
            target.write_bytes(source.read_bytes())
        else:
            target.write_text('')
    (private / 'logs').mkdir()
    (private / 'order').write_text(''.join(root + '\n' for root in ready))
    return private


def _merge_receipts(real_state, private, before):
    receipts = {}
    for name in (*_RECEIPTS, 'progress'):
        lines = _new_lines(before.get(name, ''), _read_text(private / name))
        if lines:
            path = real_state / name
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise SDKError(f'Ports receipt is not a regular file: {path}')
            with path.open('a') as stream:
                stream.write(''.join(line + '\n' for line in lines))
        if name in _RECEIPTS:
            receipts[name] = [line.split() for line in lines]
    return receipts


def _safe_log_name(name):
    if not name or name in {'.', '..'} or len(name) > 200:
        return False
    return all(character.isalnum() or character in '._+-' for character in name)


def _require_directory(path, label, *, create):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        if not create:
            raise SDKError(f'{label} is not available: {path}')
        path.mkdir()
        info = os.lstat(path)
    except OSError as exc:
        raise SDKError(f'{label} is not available: {path}') from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise SDKError(f'{label} is not a directory: {path}')


def _copy_regular(source, directory, name):
    temporary = directory / f'{name}.tmp'
    nofollow = getattr(os, 'O_NOFOLLOW', 0)
    src = os.open(source, os.O_RDONLY | nofollow)
    try:
        dst = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | nofollow, 0o644)
        try:
            while True:
                block = os.read(src, 1024 * 1024)
                if not block:
                    break
                view = memoryview(block)
                while view:
                    written = os.write(dst, view)
                    view = view[written:]
            os.fsync(dst)
        finally:
            os.close(dst)
    finally:
        os.close(src)
    os.replace(temporary, directory / name)
    directory_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _preserve_logs(real_state, private):
    """Copy regular files from the private ``logs`` directory into ``STATE/logs``.

    A symlink, an unexpected name, or a failed copy raises. The caller keeps
    the private schedule and the lock.
    """
    source = private / 'logs'
    _require_directory(source, 'Ports log directory', create=False)
    destination = real_state / 'logs'
    _require_directory(destination, 'Ports log directory', create=True)
    for entry in os.scandir(source):
        name = entry.name
        if not _safe_log_name(name):
            raise SDKError(f'Ports log name is not safe: {name}')
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError as exc:
            raise SDKError(f'Ports log is not a regular file: {source / name}') from exc
        if not stat.S_ISREG(info.st_mode):
            raise SDKError(f'Ports log is not a regular file: {source / name}')
        target = destination / name
        try:
            target_info = os.lstat(target)
        except FileNotFoundError:
            target_info = None
        except OSError as exc:
            raise SDKError(f'Ports log destination is not a regular file: {target}') from exc
        if target_info is not None and (stat.S_ISLNK(target_info.st_mode) or not stat.S_ISREG(target_info.st_mode)):
            raise SDKError(f'Ports log destination is not a regular file: {target}')
        try:
            _copy_regular(source / name, destination, name)
        except OSError as exc:
            raise SDKError(f'Ports log could not be preserved: {target}') from exc


def _safe_private(text):
    if not text or text != text.strip() or '\x00' in text:
        return None
    path = Path(text)
    if not path.is_absolute() or not path.name.startswith('ember-wave-'):
        return None
    return path


def _durable_write(directory, name, data):
    temporary = directory / f'{name}.tmp'
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, directory / name)
    directory_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _write_stamp(lock, token, pid):
    _durable_write(lock, 'owner', f'{token}\n{int(pid)}\n'.encode())
    _durable_write(lock, 'pid', f'{int(pid)}\n'.encode())


def _write_controller(lock, pid, created, private, starttime=None):
    if pid <= 1 or not created or '\n' in created or '\t' in created:
        raise SDKError('controller identity is incomplete')
    stamp = starttime if isinstance(starttime, str) and starttime.isdigit() else '-'
    _durable_write(lock, 'controller', f'{int(pid)}\n{created}\n{private}\n{stamp}\n'.encode())


def _clear_controller(lock):
    for name in ('controller', 'controller.tmp'):
        try:
            (lock / name).unlink()
        except OSError:
            pass


def _read_stamp(lock):
    try:
        lines = (lock / 'owner').read_text().splitlines()
    except OSError:
        return None
    if len(lines) != 2 or not lines[0] or any(character not in '0123456789abcdef' for character in lines[0]):
        return None
    try:
        pid = int(lines[1])
    except ValueError:
        return None
    if pid <= 1:
        return None
    return lines[0], pid


def _stamp_matches(lock, token, pid):
    return _read_stamp(lock) == (token, pid)


def _read_controller(lock):
    try:
        lines = (lock / 'controller').read_text().splitlines()
    except OSError:
        return None
    if len(lines) not in (3, 4):
        return None
    try:
        pid = int(lines[0])
    except ValueError:
        return None
    private = _safe_private(lines[2])
    if private is None or pid <= 1 or not lines[1] or '\t' in lines[1]:
        return None
    starttime = None
    if len(lines) == 4:
        if lines[3] == '-':
            starttime = None
        elif lines[3].isdigit():
            starttime = lines[3]
        else:
            return None
    return pid, lines[1], private, starttime


def _write_seen(lock, seen):
    if not lock.is_dir():
        return
    lines = []
    for pid in sorted(seen):
        stamp, start = seen[pid]
        if pid <= 1 or not stamp or '\t' in stamp or '\n' in stamp:
            continue
        start_text = start if isinstance(start, str) and start.isdigit() else '-'
        lines.append(f'{pid}\t{start_text}\t{stamp}\n')
    try:
        _durable_write(lock, 'seen', ''.join(lines).encode())
    except OSError:
        return


def _read_seen(lock):
    seen = {}
    try:
        text = (lock / 'seen').read_text()
    except OSError:
        return seen
    for line in text.splitlines():
        parts = line.split('\t')
        if len(parts) != 3 or not parts[0].isdigit() or not parts[2]:
            continue
        pid = int(parts[0])
        if pid <= 1:
            continue
        start = None if parts[1] == '-' else parts[1]
        if start is not None and not start.isdigit():
            continue
        seen[pid] = (parts[2], start)
    return seen


def _merge_seen(into, extra):
    for pid, (stamp, start) in extra.items():
        old = into.get(pid)
        if old is None:
            into[pid] = (stamp, start)
        elif old[0] == stamp and not old[1] and start:
            into[pid] = (stamp, start)


def _observe(lock, seen, *, persist=False):
    """Remember descendants actually parented under the verified controller.

    Dead identities are dropped. A PID that has left the tree stays remembered
    until its creation identity is gone, so the lock is not removed while it
    can still be that process.
    """
    record = _read_controller(lock) if lock.is_dir() else None
    try:
        rows = _process_table()
    except SDKError:
        if persist:
            _write_seen(lock, seen)
        return
    if record is not None:
        pid, created, _private, starttime = record
        row = rows.get(pid)
        if (row is not None and row[2] == created and not row[1].startswith(('Z', 'z'))
                and _same_identity(rows, pid, created, starttime)):
            for child, _parent, stamp in _live_descendants(rows, pid):
                start = _linux_start(child)
                old = seen.get(child)
                if old is None or old[0] != stamp or (start and old[1] != start):
                    seen[child] = (stamp, start)
    for child, (stamp, start) in list(seen.items()):
        if not _same_identity(rows, child, stamp, start):
            del seen[child]
    if persist:
        _write_seen(lock, seen)


def _any_alive(seen):
    if not seen:
        return False
    try:
        rows = _process_table()
    except SDKError:
        return True
    return any(_same_identity(rows, pid, created, start) for pid, (created, start) in seen.items())


def _recover_receipts(state, private):
    if private is None or not private.is_dir():
        return
    before = {name: _read_text(state / name) for name in (*_RECEIPTS, 'progress')}
    _merge_receipts(state, private, before)
    _preserve_logs(state, private)
    shutil.rmtree(private, ignore_errors=True)


def _release_if_ours(lock, token, pid):
    """Remove the lock directory only when its token still names this owner."""
    if not _stamp_matches(lock, token, pid):
        return False
    _clear_controller(lock)
    if not _stamp_matches(lock, token, pid):
        return False
    try:
        (lock / 'pid').unlink(missing_ok=True)
        (lock / 'owner').unlink(missing_ok=True)
        (lock / 'seen').unlink(missing_ok=True)
        for name in ('owner.tmp', 'pid.tmp', 'controller.tmp', 'seen.tmp'):
            (lock / name).unlink(missing_ok=True)
        lock.rmdir()
    except OSError:
        return False
    return True


def _recover_killed_owner(state, token, owner_pid, seen=None):
    """After the published owner was killed, drain only the controller it recorded.

    No record, a reused PID, a remembered identity that is still alive, or a
    failed receipt or log copy leaves the lock in place. Remembered processes
    outside the tree are not signaled.
    """
    lock = state / 'build.lock'
    if not _stamp_matches(lock, token, owner_pid):
        return False
    remembered = {} if seen is None else seen
    _merge_seen(remembered, _read_seen(lock))
    record = _read_controller(lock)
    if record is None:
        return False
    pid, created, private, starttime = record
    _observe(lock, remembered, persist=False)
    if not _stop_recorded(pid, created, starttime):
        return False
    if _any_alive(remembered):
        return False
    try:
        _recover_receipts(state, private)
    except Exception:
        return False
    if not _stamp_matches(lock, token, owner_pid):
        return False
    rows = _process_table()
    row = rows.get(pid)
    if row is not None and not row[1].startswith(('Z', 'z')) and row[2] == created:
        if not starttime or _linux_start(pid) in (None, starttime):
            return False
    if _live_descendants(rows, pid):
        return False
    if _any_alive(remembered):
        return False
    return _release_if_ours(lock, token, owner_pid)


def _remember_controller(lock, private):
    def record(pid):
        created = _creation(pid)
        starttime = _linux_start(pid)
        if not created or (os.path.isdir('/proc') and not starttime):
            raise SDKError('controller identity was not visible before it could run')
        _write_controller(lock, pid, created, private, starttime)
    return record


def _run_build(message, real_state):
    ready = message.get('ready')
    if not isinstance(ready, list) or any(not isinstance(root, str) for root in ready):
        raise SDKError('source wave has no schedule')
    before = {name: _read_text(real_state / name) for name in (*_RECEIPTS, 'progress')}
    private = _private_schedule(real_state, ready)
    lock = real_state / 'build.lock'
    run_error = None
    keep = False
    receipts = {name: [] for name in _RECEIPTS}
    try:
        try:
            if message.get('op') == 'build':
                from .sdk_client import run_source
                config = message['config']
                wave = dict(config, source=dict(config['source'], state=str(private)))
                binaries = [tuple(item) for item in message.get('binaries') or ()]
                ready_hook = _CONTROLLER_READY.set(_remember_controller(lock, private))
                try:
                    run_source(wave, ready, 'build', binaries=binaries, controller_mode='build')
                finally:
                    _CONTROLLER_READY.reset(ready_hook)
            else:
                from .package_selection import launch_recorded
                argv = message.get('argv')
                if not isinstance(argv, list) or not argv or any(not isinstance(part, str) for part in argv):
                    raise SDKError('source controller argv is invalid')
                env = os.environ.copy()
                env.update(message.get('env') or {})
                command = [str(private) if part == '{state}' else part for part in argv]
                process = launch_recorded(command, env, _remember_controller(lock, private))
                status = process.wait()
                if status:
                    raise SDKError(f'Ports build failed ({status}); compatible objects and logs were retained')
        except Exception as exc:
            run_error = exc
        try:
            receipts = _merge_receipts(real_state, private, before)
            try:
                _preserve_logs(real_state, private)
            except Exception as exc:
                keep = True
                run_error = exc
        except Exception as exc:
            keep = True
            if run_error is None:
                run_error = exc
    finally:
        try:
            _observe(lock, _OWNER_SEEN, persist=True)
        except Exception as exc:
            keep = True
            if run_error is None:
                run_error = exc if isinstance(exc, SDKError) else SDKError('controller identity could not be reread')
        if not keep and _any_alive(_OWNER_SEEN):
            keep = True
            if run_error is None:
                run_error = SDKError('a recorded build process is still alive; the lock and private schedule were retained')
        if not keep:
            _clear_controller(lock)
            shutil.rmtree(private, ignore_errors=True)
    if run_error is not None:
        raise run_error
    return receipts


def _death_requested(death_read):
    try:
        data = os.read(death_read, 4096)
    except BlockingIOError:
        return False
    except OSError:
        return True
    return data == b''


def _reap_owner(owner, state, token):
    """Wait for the published owner. A normal exit is that owner's own cleanup."""
    seen = {}
    lock = state / 'build.lock'
    while True:
        try:
            _observe(lock, seen, persist=False)
            _merge_seen(seen, _read_seen(lock))
        except Exception:
            pass
        try:
            ended, status = os.waitpid(owner, os.WNOHANG)
        except ChildProcessError:
            return
        if ended:
            break
        time.sleep(0.02)
    if os.WIFSIGNALED(status):
        try:
            _observe(lock, seen, persist=False)
            _merge_seen(seen, _read_seen(lock))
        except Exception:
            pass
        _recover_killed_owner(state, token, owner, seen)


def _owner(sock, death_read, state, token):
    lock = state / 'build.lock'
    owned = False
    retain = False
    dead = [False]
    _OWNER_SEEN.clear()
    os.set_blocking(death_read, False)

    def pulse():
        if dead[0]:
            return
        _observe(lock, _OWNER_SEEN, persist=True)
        if _death_requested(death_read):
            dead[0] = True
            _stop_our_children()

    _CONTROLLER_PULSE.set(pulse)
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        try:
            lock.mkdir()
        except FileExistsError:
            _send(sock, {'op': 'error', 'error': f'writer is busy: {lock}; a build owns this lock and its controller is recorded there'})
            return
        owned = True
        _write_stamp(lock, token, os.getpid())
        _send(sock, {'op': 'ready', 'pid': os.getpid()})
        buffer = b''
        while not dead[0]:
            pulse()
            if dead[0]:
                break
            readable, _, _ = select.select([sock, death_read], [], [], 0.2)
            if dead[0] or death_read in readable:
                pulse()
                if dead[0]:
                    break
            if sock not in readable:
                continue
            try:
                chunk = sock.recv(65536)
            except OSError:
                dead[0] = True
                break
            if not chunk:
                dead[0] = True
                _stop_our_children()
                break
            buffer += chunk
            while b'\n' in buffer and not dead[0]:
                line, buffer = buffer.split(b'\n', 1)
                message = json.loads(line.decode('utf-8'))
                op = message.get('op')
                if op == 'close':
                    _send(sock, {'op': 'closed'})
                    return
                if op in ('build', 'exec'):
                    try:
                        receipts = _run_build(message, state)
                    except Exception as exc:
                        if isinstance(exc, SDKError) and 'not a regular file' in str(exc):
                            retain = True
                        elif (lock / 'controller').is_file():
                            retain = True
                        if _any_alive(_OWNER_SEEN):
                            retain = True
                            dead[0] = True
                        if not dead[0]:
                            try:
                                _send(sock, {'op': 'error', 'error': str(exc)})
                            except OSError:
                                dead[0] = True
                        if dead[0] and retain:
                            break
                        continue
                    if dead[0]:
                        break
                    try:
                        _send(sock, {'op': 'built', 'receipts': receipts})
                    except OSError:
                        dead[0] = True
                        break
                else:
                    _send(sock, {'op': 'error', 'error': 'unknown source wave command'})
    finally:
        try:
            os.close(death_read)
        except OSError:
            pass
        try:
            drained = _stop_our_children()
        except SDKError:
            drained = False
        if _any_alive(_OWNER_SEEN):
            drained = False
        if owned and drained and not retain:
            if not _release_if_ours(lock, token, os.getpid()):
                os._exit(3)
        elif owned and not drained:
            os._exit(3)
        try:
            sock.close()
        except OSError:
            pass


def _supervise(sock, death_read, state):
    """Drainer. The child is the published lock PID and can be killed alone."""
    _ignore_terminal_signals()
    try:
        os.setsid()
    except OSError:
        pass
    token = secrets.token_hex(16)
    owner = os.fork()
    if owner == 0:
        try:
            os.setsid()
        except OSError:
            pass
        try:
            _owner(sock, death_read, state, token)
        except BaseException:
            os._exit(1)
        os._exit(0)
    os.close(death_read)
    sock.close()
    _reap_owner(owner, state, token)


