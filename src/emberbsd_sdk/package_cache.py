# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Shared archive cache. Callers hold its directory lock through installation."""

from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import re
import shutil

from .errors import SDKError
from .paths import open_regular, verify_stable


def _hold(path, flags):
    """Hold the kernel lock on the owner file; the kernel drops it with a dead owner."""
    try:
        descriptor = os.open(path / 'held', flags | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    except FileNotFoundError:
        return None
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # A released lock unlinks this file before it closes the descriptor.
        if os.fstat(descriptor).st_ino != os.stat(path / 'held').st_ino:
            raise FileNotFoundError
    except OSError:
        os.close(descriptor)
        return None
    return descriptor


@contextmanager
def directory_lock(path):
    """Directory lock shared with other owners; only a dead owner's own lock is reclaimed.

    A foreign owner (cross-bulk, a source-wave controller) records its identity
    without ``held``; such a lock is never removed here.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    held = None
    for _ in range(2):
        try:
            path.mkdir()
        except FileExistsError:
            held = _hold(path, os.O_RDWR)
            if held is not None or path.exists():
                break
            continue
        held = _hold(path, os.O_RDWR | os.O_CREAT)
        break
    if held is None:
        raise SDKError(f'writer is busy: {path}; a build owns this lock and its controller is recorded there')
    try:
        (path / 'pid').write_text(str(os.getpid()) + '\n')
        yield
    finally:
        _release(path, held)


def _release(path, held):
    """Remove owner files only while holding their kernel lock."""
    if held is None:
        return
    try:
        (path / 'held').unlink(missing_ok=True)
        (path / 'pid').unlink(missing_ok=True)
    finally:
        os.close(held)
    path.rmdir()


def check_space(path, required):
    path = Path(path)
    while not path.exists():
        path = path.parent
    free = shutil.disk_usage(path).free
    if free < required:
        raise SDKError(f'insufficient disk space at {path}: need {required} bytes, available {free}')


def check_archive(path, item):
    stream, before = open_regular(path)
    with stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    verify_stable(path, before)
    if before.st_size != item['size'] or digest != item['sha256']:
        raise SDKError(f'cached package size or SHA256 differs from signed inventory: {path}')


class PackageCache:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, item):
        if not re.fullmatch('[0-9a-f]{64}', item['sha256']):
            raise SDKError('invalid archive cache key')
        return self.root / (item['sha256'] + '.tgz')

    def fetch(self, store, key, item):
        destination = self.path(item)
        if destination.exists() or destination.is_symlink():
            check_archive(destination, item)
            os.utime(destination, None)
            return destination
        partial = destination.with_suffix('.part')
        start = 0
        if partial.exists() or partial.is_symlink():
            stream, before = open_regular(partial)
            stream.close()
            start = before.st_size
            if start > item['size']:
                partial.unlink()
                start = 0
        check_space(self.root, item['size'] - start)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, 'O_NOFOLLOW', 0)
        with os.fdopen(os.open(partial, flags, 0o600), 'ab') as stream:
            if not start:
                try:
                    store.verify(key, item['size'], item['sha256'], sink=stream)
                except SDKError:
                    # HTTPStore wraps transport interruptions in SDKError too.
                    # Retain an incomplete prefix for Range; discard complete
                    # corrupt/oversized bodies rather than retrying bad bytes.
                    stream.flush()
                    if partial.stat().st_size >= item['size']:
                        try:
                            check_archive(partial, item)
                        except SDKError:
                            partial.unlink()
                    raise
            else:
                while start < item['size']:
                    end = min(item['size'], start + 4 * 1024 * 1024) - 1
                    block = store.range(key, start, end, item['size'])
                    if len(block) != end - start + 1:
                        raise SDKError('truncated resumed package')
                    stream.write(block)
                    start = end + 1
            stream.flush()
            os.fsync(stream.fileno())
        try:
            check_archive(partial, item)
        except SDKError:
            partial.unlink()
            raise
        partial.replace(destination)
        return destination

    def trim(self, limit, *, keep=()):
        if type(limit) is not int or limit < 0:
            raise SDKError('cache limit must be a nonnegative byte count')
        entries = []
        for path in self.root.iterdir():
            if re.fullmatch('[0-9a-f]{64}\\.(tgz|part)', path.name) and not path.is_symlink():
                info = path.stat()
                if path.is_file():
                    entries.append((info.st_mtime_ns, path, info.st_size))
        size = sum(row[2] for row in entries)
        removed = 0
        for _, path, length in sorted(entries):
            if size <= limit:
                break
            if path.stem not in keep:
                path.unlink()
                size -= length
                removed += length
        return removed
