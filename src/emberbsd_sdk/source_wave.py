# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Own the Ports build lock for a whole source schedule.

The client computes waves. The published lock PID starts the controller and
records that controller's PID. An outer drainer survives the published PID:
if that PID is killed, the drainer stops the recorded controller, including a
session it opened, copies any receipts the controller had already written, and
only then removes the lock. The same parent walk is used on Darwin and Linux.
"""

import json
import os
from pathlib import Path
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


def _parents():
    """Parent of every process, from ``ps``. Darwin and Linux both provide this."""
    result = subprocess.run(
        ['ps', '-ax', '-o', 'pid=,ppid='], capture_output=True, text=True, check=False)
    if result.returncode != 0 and not result.stdout.strip():
        raise SDKError('ps parent walk failed')
    parents = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            parents[int(parts[0])] = int(parts[1])
    return parents


def _descendants(pid):
    if pid <= 1:
        raise SDKError('refusing to walk host init')
    parents = _parents()
    children = {}
    for child, parent in parents.items():
        children.setdefault(parent, []).append(child)
    found = []

    def walk(current):
        for child in children.get(current, ()):
            found.append(child)
            walk(child)

    walk(pid)
    return found


def _alive(pid):
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _kill_tree(roots):
    """Stop roots and every process still parented under them. Return false if any remain."""
    roots = {pid for pid in roots if pid > 1}
    for _ in range(50):
        victims = []
        for root in list(roots):
            if not _alive(root):
                roots.discard(root)
                continue
            victims.extend(_descendants(root))
        if not roots and not victims:
            return True
        for pid in reversed(victims):
            if pid > 1:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        for pid in list(roots):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        time.sleep(0.05)
    return not any(_alive(pid) or _descendants(pid) for pid in roots)


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


def _write_controller(lock, pid, private):
    blob = f'{int(pid)}\n{private}\n'.encode()
    temporary = lock / 'controller.tmp'
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, blob)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, lock / 'controller')


def _clear_controller(lock):
    try:
        (lock / 'controller').unlink()
    except OSError:
        pass
    try:
        (lock / 'controller.tmp').unlink()
    except OSError:
        pass


def _read_record(lock):
    try:
        lines = (lock / 'controller').read_text().splitlines()
    except OSError:
        return None
    if len(lines) < 2:
        return None
    try:
        pid = int(lines[0])
    except ValueError:
        return None
    private = _safe_private(lines[1])
    if private is None or pid < 0:
        return None
    return pid, private


def _recover_receipts(state, private):
    if private is None or not private.is_dir():
        return
    before = {name: _read_text(state / name) for name in (*_RECEIPTS, 'progress')}
    _merge_receipts(state, private, before)
    shutil.rmtree(private, ignore_errors=True)


def _release_lock(lock):
    _clear_controller(lock)
    try:
        (lock / 'pid').unlink(missing_ok=True)
        lock.rmdir()
    except OSError:
        pass


def _run_build(message, real_state):
    ready = message.get('ready')
    if not isinstance(ready, list) or any(not isinstance(root, str) for root in ready):
        raise SDKError('source wave has no schedule')
    before = {name: _read_text(real_state / name) for name in (*_RECEIPTS, 'progress')}
    private = _private_schedule(real_state, ready)
    lock = real_state / 'build.lock'
    # The private directory is recorded before the controller exists so a killed
    # owner can still copy the receipts that controller manages to write.
    _write_controller(lock, 0, private)
    run_error = None
    receipts = {name: [] for name in _RECEIPTS}
    try:
        try:
            if message.get('op') == 'build':
                from .sdk_client import run_source
                config = message['config']
                wave = dict(config, source=dict(config['source'], state=str(private)))
                binaries = [tuple(item) for item in message.get('binaries') or ()]
                run_source(wave, ready, 'build', binaries=binaries, controller_mode='build')
            else:
                argv = message.get('argv')
                if not isinstance(argv, list) or not argv or any(not isinstance(part, str) for part in argv):
                    raise SDKError('source controller argv is invalid')
                env = os.environ.copy()
                env.update(message.get('env') or {})
                command = [str(private) if part == '{state}' else part for part in argv]
                # A new session matches package selection and the Ports stage supervisor.
                # The recorded PID remains the parent of that session for the ps walk.
                process = subprocess.Popen(command, env=env, start_new_session=True)
                _write_controller(lock, process.pid, private)
                status = process.wait()
                if status:
                    raise SDKError(f'Ports build failed ({status}); compatible objects and logs were retained')
        except Exception as exc:
            run_error = exc
        try:
            receipts = _merge_receipts(real_state, private, before)
        except Exception as exc:
            if run_error is None:
                run_error = exc
    finally:
        _clear_controller(lock)
        shutil.rmtree(private, ignore_errors=True)
    if run_error is not None:
        raise run_error
    return receipts


def _stop_orphans(owner_pid, record, snapshot):
    roots = set()
    if record and record[0] > 1:
        roots.add(record[0])
    parents = _parents()
    for pid in snapshot:
        if pid <= 1 or pid == owner_pid:
            continue
        parent = parents.get(pid)
        if parent in (1, owner_pid) or (record and parent == record[0]):
            roots.add(pid)
    return _kill_tree(roots)


def _reap_owner(owner, state):
    """After the published owner dies, stop the controller it recorded."""
    lock = state / 'build.lock'
    record = None
    snapshot = set()
    while True:
        current = _read_record(lock)
        if current is not None:
            record = current
            if current[0] > 1:
                snapshot.update(_descendants(current[0]))
                snapshot.add(current[0])
        try:
            snapshot.update(_descendants(owner))
        except SDKError:
            pass
        ended, status = os.waitpid(owner, os.WNOHANG)
        if ended:
            break
        time.sleep(0.02)
    if not lock.exists():
        return
    # A clean owner removes the lock itself. This path is the killed owner.
    if not _stop_orphans(owner, record, snapshot):
        os._exit(3)
    if record is not None:
        _recover_receipts(state, record[1])
    _release_lock(lock)


def _owner(sock, death_read, state):
    lock = state / 'build.lock'
    owned = False
    dead = threading.Event()

    def watch():
        try:
            while os.read(death_read, 4096):
                pass
        except OSError:
            pass
        dead.set()
        try:
            _kill_tree(_descendants(os.getpid()))
        except SDKError:
            pass

    threading.Thread(target=watch, daemon=True).start()
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        try:
            lock.mkdir()
        except FileExistsError:
            _send(sock, {'op': 'error', 'error': f'writer is busy: {lock}; a build owns this lock and its controller is recorded there'})
            return
        owned = True
        (lock / 'pid').write_text(str(os.getpid()) + '\n')
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
                try:
                    _kill_tree(_descendants(os.getpid()))
                except SDKError:
                    pass
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
        try:
            drained = _kill_tree(_descendants(os.getpid()))
        except SDKError:
            drained = False
        if owned and drained:
            _release_lock(lock)
        elif owned:
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
    owner = os.fork()
    if owner == 0:
        try:
            os.setsid()
        except OSError:
            pass
        try:
            _owner(sock, death_read, state)
        except BaseException:
            os._exit(1)
        os._exit(0)
    os.close(death_read)
    sock.close()
    _reap_owner(owner, state)
