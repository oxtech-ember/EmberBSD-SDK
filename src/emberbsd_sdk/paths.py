# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Path identity checks. Generic APIs support spaces and Unicode."""

import os
import re
import stat
from .errors import SDKError


def _raw_absolute(value):
    try:
        value = os.fspath(value)
        if not isinstance(value, str) or not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError('expected a nonempty path without control characters')
        value.encode('utf-8', 'strict')
        return value if os.path.isabs(value) else os.path.join(os.getcwd(), value)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise SDKError(f'invalid path: {exc}') from exc


def path_string(value):
    return os.path.normpath(_raw_absolute(value))


def entry(value):
    try:
        return os.lstat(value)
    except FileNotFoundError:
        return None


def real_directory(value):
    info = entry(value)
    if info is None or not stat.S_ISDIR(info.st_mode):
        raise SDKError(f'expected a real directory: {value}')
    return info


def regular_file(value):
    info = entry(value)
    if info is None or not stat.S_ISREG(info.st_mode):
        raise SDKError(f'expected a regular file: {value}')
    return info


def canonical_existing(value):
    try:
        return path_string(os.path.realpath(_raw_absolute(value), strict=True))
    except OSError as exc:
        raise SDKError(str(exc)) from exc


def canonical_new(value):
    value = _raw_absolute(value)
    tail = []
    current = value
    while entry(current) is None:
        parent, name = os.path.split(current)
        if parent == current:
            raise SDKError(f'no existing ancestor: {value}')
        tail.append(name)
        current = parent
    base = canonical_existing(current)
    if tail:
        real_directory(base)
    return os.path.join(base, *reversed(tail))


def relative_inside(value, root):
    """Return a relative path within root, or None, using existing inode identity.

    Empty means root itself. Existing ancestor identity also detects aliases on
    case-insensitive filesystems without changing names on case-sensitive ones.
    """
    value, root = canonical_new(value), canonical_new(root)
    root_info = entry(root)
    current, tail = value, []
    while True:
        info = entry(current)
        same_inode = root_info is not None and info is not None and (root_info.st_dev, root_info.st_ino) == (info.st_dev, info.st_ino)
        if current == root or same_inode:
            return os.path.join(*reversed(tail)) if tail else ''
        parent, name = os.path.split(current)
        if parent == current:
            return None
        tail.append(name)
        current = parent


def inside(value, root):
    relative = relative_inside(value, root)
    return relative is not None and relative != ''


def reject_overlap(left, right):
    if relative_inside(left, right) is not None or relative_inside(right, left) is not None:
        raise SDKError(f'overlapping input/output paths: {left} and {right}')


def build_path(value):
    value = path_string(value)
    if re.fullmatch(r'[/A-Za-z0-9_.:+-]+', value) is None:
        raise SDKError(f'unsupported character in POSIX build path: {value}')
    return value


def stable_identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def open_regular(path):
    """Open a real file and verify the lstat/open identity without following links."""
    before = regular_file(path)
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    fd = os.open(path, flags)
    try:
        current = os.fstat(fd)
        if not stat.S_ISREG(current.st_mode) or stable_identity(current) != stable_identity(before):
            raise SDKError(f'file changed during inspection: {path}')
        return os.fdopen(fd, 'rb'), before
    except BaseException:
        os.close(fd)
        raise


def verify_stable(path, before):
    after = entry(path)
    if after is None or stable_identity(after) != stable_identity(before):
        raise SDKError(f'file changed during inspection: {path}')
