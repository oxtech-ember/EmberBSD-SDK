# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Own the Ports build lock for a whole source schedule.

The client computes waves. The published lock PID starts the controller.
It records that controller's PID and creation time before the controller may
exec. An outer drainer survives the published PID. It cleans a lock only when
the lock's token is the one this drainer's owner wrote and that owner was
killed. It then rereads the controller record, stops that same process and
its current children, copies receipts, and only then removes the lock.
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
import subprocess
import sys
import tempfile
import threading
import time

from .errors import SDKError


_RECEIPTS = ('done', 'failed', 'blocked', 'skipped')
_COPIED = ('edges', 'packages', 'progress', *_RECEIPTS)
_CONTROLLER_READY = contextvars.ContextVar('ember_controller_ready', default=None)


def current_controller_ready():
    """Hook installed around the production ``run_source`` call, if any."""
    return _CONTROLLER_READY.get()


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


def _signal_proven(pid, created):
    """Signal one PID only when a fresh ``ps`` row still has this creation stamp.

    A reused PID has a different stamp and is left alone, including when its
    parent is now init or launchd.
    """
    if pid <= 1 or not created:
        return False
    row = _process_table().get(pid)
    if row is None or row[1].startswith(('Z', 'z')):
        return True
    if row[2] != created:
        return False
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    return True


def _signal_descendants(rows, root):
    """Signal one fresh snapshot from the leaves up. A changed creation stamp stops the drain."""
    for child, _parent, stamp in reversed(_live_descendants(rows, root)):
        if not _signal_proven(child, stamp):
            return False
    return True


def _stop_recorded(pid, created):
    """Stop one recorded controller and children still parented under it.

    The controller is proven by the creation stamp captured before it could exec.
    A child is signaled only when its current parent is still that tree. A PID
    whose parent is now init or launchd, or whose creation stamp changed, is not
    signaled. False means the lock must stay.
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
        confirmed = True
        if not _signal_descendants(rows, pid):
            return False
        if not _signal_proven(pid, created):
            return False
        time.sleep(0.05)
    rows = _process_table()
    row = rows.get(pid)
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


def _write_controller(lock, pid, created, private):
    if pid <= 1 or not created or '\n' in created:
        raise SDKError('controller identity is incomplete')
    _durable_write(lock, 'controller', f'{int(pid)}\n{created}\n{private}\n'.encode())


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
    if len(lines) != 3:
        return None
    try:
        pid = int(lines[0])
    except ValueError:
        return None
    private = _safe_private(lines[2])
    if private is None or pid <= 1 or not lines[1]:
        return None
    return pid, lines[1], private


def _recover_receipts(state, private):
    if private is None or not private.is_dir():
        return
    before = {name: _read_text(state / name) for name in (*_RECEIPTS, 'progress')}
    _merge_receipts(state, private, before)
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
        for name in ('owner.tmp', 'pid.tmp', 'controller.tmp'):
            (lock / name).unlink(missing_ok=True)
        lock.rmdir()
    except OSError:
        return False
    return True


def _recover_killed_owner(state, token, owner_pid):
    """After the published owner was killed, drain only the controller it recorded.

    No record, a reused PID, a child now parented to init, or a failed receipt
    copy leaves the lock in place.
    """
    lock = state / 'build.lock'
    if not _stamp_matches(lock, token, owner_pid):
        return False
    record = _read_controller(lock)
    if record is None or not _stop_recorded(record[0], record[1]):
        return False
    try:
        _recover_receipts(state, record[2])
    except Exception:
        return False
    if not _stamp_matches(lock, token, owner_pid):
        return False
    rows = _process_table()
    row = rows.get(record[0])
    if row is not None and not row[1].startswith(('Z', 'z')) and row[2] == record[1]:
        return False
    if _live_descendants(rows, record[0]):
        return False
    return _release_if_ours(lock, token, owner_pid)


def _remember_controller(lock, private):
    def record(pid):
        created = _creation(pid)
        if not created:
            raise SDKError('controller identity was not visible before it could run')
        _write_controller(lock, pid, created, private)
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
        except Exception as exc:
            keep = True
            if run_error is None:
                run_error = exc
    finally:
        if not keep:
            _clear_controller(lock)
            shutil.rmtree(private, ignore_errors=True)
    if run_error is not None:
        raise run_error
    return receipts


def _reap_owner(owner, state, token):
    """Wait for the published owner. A normal exit is that owner's own cleanup."""
    while True:
        ended, status = os.waitpid(owner, os.WNOHANG)
        if ended:
            break
        time.sleep(0.02)
    if os.WIFSIGNALED(status):
        _recover_killed_owner(state, token, owner)


def _owner(sock, death_read, state, token):
    lock = state / 'build.lock'
    owned = False
    retain = False
    dead = threading.Event()

    def watch():
        try:
            while os.read(death_read, 4096):
                pass
        except OSError:
            pass
        dead.set()
        _stop_our_children()

    threading.Thread(target=watch, daemon=True).start()
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
        while not dead.is_set():
            readable, _, _ = select.select([sock], [], [], 0.2)
            if dead.is_set():
                break
            if not readable:
                continue
            try:
                chunk = sock.recv(65536)
            except OSError:
                dead.set()
                break
            if not chunk:
                dead.set()
                _stop_our_children()
                break
            buffer += chunk
            while b'\n' in buffer and not dead.is_set():
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
                        if not dead.is_set():
                            try:
                                _send(sock, {'op': 'error', 'error': str(exc)})
                            except OSError:
                                dead.set()
                        continue
                    if dead.is_set():
                        break
                    try:
                        _send(sock, {'op': 'built', 'receipts': receipts})
                    except OSError:
                        dead.set()
                        break
                else:
                    _send(sock, {'op': 'error', 'error': 'unknown source wave command'})
    finally:
        try:
            os.close(death_read)
        except OSError:
            pass
        drained = _stop_our_children()
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


