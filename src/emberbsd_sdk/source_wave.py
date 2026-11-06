# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Own the Ports build lock for a whole source schedule.

The client computes waves. This process is the lock PID and the parent of
the controller. A killed client closes the death pipe; the controller tree
is stopped before the lock is removed. Receipts are copied back from a
private schedule only after that tree is gone, so a second controller cannot
observe a narrowed order or write the same state at the same time.
"""

import ctypes
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

from .errors import SDKError


_RECEIPTS = ('done', 'failed', 'blocked', 'skipped')
_COPIED = ('edges', 'packages', 'progress', *_RECEIPTS)


class SourceWaveLock:
    """Hold ``STATE/build.lock`` in a child that outlives this process."""

    def __init__(self, state):
        self.state = Path(state)
        self._parent, child = socket.socketpair()
        self._death = os.pipe()
        self._pid = os.fork()
        if self._pid == 0:
            try:
                os.close(self._death[1])
                self._parent.close()
                try:
                    os.setsid()
                except OSError:
                    pass
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
        if self._parent is None:
            return
        try:
            self._send({'op': 'close'})
            self._read()
        except (OSError, SDKError):
            pass
        finally:
            self._parent.close()
            self._parent = None
            os.close(self._death[1])
            os.waitpid(self._pid, 0)

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


def _become_subreaper():
    if sys.platform != 'linux':
        return
    try:
        library = ctypes.CDLL(None, use_errno=True)
        library.prctl.argtypes = (ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
                                  ctypes.c_ulong, ctypes.c_ulong)
        library.prctl.restype = ctypes.c_int
        library.prctl(36, 1, 0, 0, 0)  # PR_SET_CHILD_SUBREAPER
    except (OSError, AttributeError):
        pass


def _parents():
    if sys.platform == 'linux' and Path('/proc').is_dir():
        parents = {}
        for entry in os.listdir('/proc'):
            if not entry.isdigit():
                continue
            try:
                text = Path('/proc', entry, 'status').read_text()
            except OSError:
                continue
            for line in text.splitlines():
                if line.startswith('PPid:'):
                    parents[int(entry)] = int(line.split()[1])
                    break
        return parents
    result = subprocess.run(['ps', '-ax', '-o', 'pid=,ppid='], capture_output=True, text=True, check=False)
    parents = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            parents[int(parts[0])] = int(parts[1])
    return parents


def _descendants(pid):
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


def _kill_descendants(pid):
    """Stop every child, including a session the controller opened. Return false if any remain."""
    for _ in range(50):
        descendants = _descendants(pid)
        if not descendants:
            return True
        for child in reversed(descendants):
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            while True:
                os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            pass
    return not _descendants(pid)


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


def _run_build(message, real_state):
    ready = message.get('ready')
    if not isinstance(ready, list) or any(not isinstance(root, str) for root in ready):
        raise SDKError('source wave has no schedule')
    before = {name: _read_text(real_state / name) for name in (*_RECEIPTS, 'progress')}
    private = _private_schedule(real_state, ready)
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
                status = subprocess.call(command, env=env)
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
        shutil.rmtree(private, ignore_errors=True)
    if run_error is not None:
        raise run_error
    return receipts


def _supervise(sock, death_read, state):
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    _become_subreaper()
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
        _kill_descendants(os.getpid())

    threading.Thread(target=watch, daemon=True).start()
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        try:
            lock.mkdir()
        except FileExistsError as exc:
            _send(sock, {'op': 'error', 'error': f'writer is busy: {lock}; remove only after confirming its owner and every build it started have stopped'})
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
            chunk = sock.recv(65536)
            if not chunk:
                dead.set()
                _kill_descendants(os.getpid())
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
                            _send(sock, {'op': 'error', 'error': str(exc)})
                        continue
                    if dead.is_set():
                        break
                    _send(sock, {'op': 'built', 'receipts': receipts})
                else:
                    _send(sock, {'op': 'error', 'error': 'unknown source wave command'})
    finally:
        os.close(death_read)
        drained = _kill_descendants(os.getpid())
        if owned and drained:
            try:
                (lock / 'pid').unlink(missing_ok=True)
                lock.rmdir()
            except OSError:
                pass
        elif owned:
            os._exit(3)
        try:
            sock.close()
        except OSError:
            pass
