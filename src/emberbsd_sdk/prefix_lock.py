# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Readers and writers of one package prefix, shared by SDK and Ports.

A build that uses a prefix is a reader of its package database. A process
that changes the prefix is a writer. A writer waits for other writers. Before
it changes a file, it stops when another process tree reads the prefix. A
reader waits while a writer changes the prefix. Child processes inherit the
reader locks and the reader names in READERS, so the writers of the same
build tree do not stop for their own build. Pass the descriptors to each
child process that must keep the prefix unchanged (pass_fds).
"""

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import secrets

from .errors import SDKError

READERS = 'EMBERBSD_PREFIX_READERS'
_NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0)


def _files(database):
    """Return the reader directory and the writer file of DATABASE."""
    database = Path(os.path.realpath(database))
    return database.parent / (database.name + '.readers'), database.parent / (database.name + '.writer')


def _own():
    return set(filter(None, os.environ.get(READERS, '').split(os.pathsep)))


def _open_writer(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    return os.open(path, os.O_RDWR | os.O_CREAT | _NOFOLLOW, 0o644)


def _register(database):
    """Lock a new reader file of DATABASE. Return its descriptor and path."""
    readers, writer = _files(database)
    readers.mkdir(parents=True, exist_ok=True)
    while True:
        name = readers / f'{os.getpid()}-{secrets.token_hex(8)}'
        descriptor = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o644)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        lock = _open_writer(writer)
        try:
            # Wait while a writer changes the prefix. A writer that ran
            # before the lock above can remove the new file as stale.
            fcntl.flock(lock, fcntl.LOCK_SH)
            try:
                if os.fstat(descriptor).st_ino == os.stat(name).st_ino:
                    return descriptor, name
            except FileNotFoundError:
                pass
        finally:
            os.close(lock)
        os.close(descriptor)


def register(databases):
    """Make this process tree a reader of DATABASES until it exits.

    Return the descriptors. They stay open across exec in child processes.
    """
    descriptors, names = [], []
    try:
        for database in databases:
            if not _files(database)[0].parent.is_dir():
                continue  # No prefix yet: nothing to read.
            descriptor, name = _register(database)
            descriptors.append(descriptor)
            names.append(str(name))
    except BaseException:
        release(descriptors, names)
        raise
    for descriptor in descriptors:
        os.set_inheritable(descriptor, True)
    os.environ[READERS] = os.pathsep.join(sorted(_own().union(names)))
    return descriptors, names


def release(descriptors, names):
    """Stop being a reader. A writer removes a file that is left unlocked."""
    for descriptor in descriptors:
        os.close(descriptor)
    for name in names:
        Path(name).unlink(missing_ok=True)
    own = _own().difference(names)
    if own:
        os.environ[READERS] = os.pathsep.join(sorted(own))
    else:
        os.environ.pop(READERS, None)


@contextmanager
def reading(databases):
    """Yield the reader descriptors; give them to child processes as pass_fds."""
    descriptors, names = register(databases)
    try:
        yield tuple(descriptors)
    finally:
        release(descriptors, names)


def acquire_writer(database, *, allow_own_readers=True):
    """Hold the writer lock of DATABASE. Return its descriptor.

    Stop before any change when another process tree reads the prefix.
    """
    readers, writer = _files(database)
    descriptor = _open_writer(writer)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        own = _own() if allow_own_readers else set()
        entries = sorted(readers.iterdir()) if readers.is_dir() else []
        for entry in entries:
            if str(entry) in own:
                continue
            try:
                other = os.open(entry, os.O_RDWR | _NOFOLLOW)
            except FileNotFoundError:
                continue
            try:
                try:
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise SDKError(f'another build uses the prefix of {database} ({entry.name}); '
                                   'change the prefix after that build stops') from None
                # The owner of this file stopped without removing it.
                entry.unlink(missing_ok=True)
            finally:
                os.close(other)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


@contextmanager
def writing(database, *, allow_own_readers=True):
    descriptor = acquire_writer(database, allow_own_readers=allow_own_readers)
    try:
        yield
    finally:
        os.close(descriptor)
