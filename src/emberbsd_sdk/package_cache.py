# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Shared archive cache. Callers hold its directory lock through installation."""

from contextlib import ExitStack, contextmanager
import fcntl
import hashlib
import os
import stat
from pathlib import Path
import re
import shutil

from .errors import SDKError
from .paths import open_regular, verify_stable
from .release_s3 import remaining_ranges


def _hold(directory, flags, private):
    """Hold the kernel lock on the owner file; the kernel drops it with a dead owner."""
    try:
        descriptor = os.open('held', flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(descriptor)
        if private and (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1):
            raise SDKError('invalid artifact writer lock file')
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # A released lock unlinks this file before it closes the descriptor.
        if not os.path.samestat(info, os.stat('held', dir_fd=directory, follow_symlinks=False)):
            raise FileNotFoundError
    except OSError:
        os.close(descriptor)
        return None
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


@contextmanager
def directory_lock(path, *, parent_fd=None):
    """Directory lock shared with other owners; only a dead owner's own lock is reclaimed.

    A foreign owner (cross-bulk, a source-wave controller) records its identity
    without ``held``; such a lock is never removed here. For artifact outputs,
    lock a single entry of the already open destination directory.
    """
    path = Path(path)
    if parent_fd is None:
        path.parent.mkdir(parents=True, exist_ok=True)
    elif path.name != str(path):
        raise SDKError('an anchored lock needs a single name')
    with ExitStack() as stack:
        for _ in range(2):
            try:
                os.mkdir(path, 0o700 if parent_fd is not None else 0o777, dir_fd=parent_fd)
                flags = os.O_RDWR | os.O_CREAT
            except FileExistsError:
                flags = os.O_RDWR
            try:
                directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
            except FileNotFoundError:
                continue
            stack.callback(os.close, directory)
            info = os.fstat(directory)
            if parent_fd is not None and (info.st_uid != os.geteuid() or info.st_mode & 0o022):
                raise SDKError('untrusted artifact writer lock directory')
            held = _hold(directory, flags, parent_fd is not None)
            if held is not None:
                break
            raise SDKError(f'writer is busy: {path}; a build owns this lock and its controller is recorded there')
        else:
            raise SDKError(f'writer is busy: {path}')
        stack.callback(os.close, held)
        try:
            # Replace a stale PID entry without following or writing its old inode.
            try:
                os.unlink('pid', dir_fd=directory)
            except FileNotFoundError:
                pass
            descriptor = os.open('pid', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644, dir_fd=directory)
            with os.fdopen(descriptor, 'w') as stream:
                stream.write(str(os.getpid()) + '\n')
            yield
        finally:
            for name in ('pid', 'held'):
                try:
                    os.unlink(name, dir_fd=directory)
                except FileNotFoundError:
                    pass
            os.rmdir(path, dir_fd=parent_fd)


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
                for block in remaining_ranges(store, key, start, item['size']):
                    stream.write(block)
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
