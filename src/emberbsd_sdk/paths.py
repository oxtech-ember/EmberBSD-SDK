# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Path identity checks. Generic APIs support spaces and Unicode."""

import os
import re
import stat
from .errors import SDKError


def path_string(value):
    try:
        value = os.fspath(value)
        if not isinstance(value, str) or not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError('expected a nonempty path without control characters')
        value.encode('utf-8', 'strict')
        return os.path.abspath(value)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise SDKError(f'invalid path: {exc}') from exc


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
        return path_string(os.path.realpath(path_string(value), strict=True))
    except OSError as exc:
        raise SDKError(str(exc)) from exc


def canonical_new(value):
    value = path_string(value)
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


def inside(value, root):
    value, root = os.path.normpath(value), os.path.normpath(root)
    try:
        return value != root and os.path.commonpath((value, root)) == root
    except ValueError:
        return False


def reject_overlap(left, right):
    a, b = canonical_new(left), canonical_new(right)
    sa, sb = entry(a), entry(b)
    same_inode = sa is not None and sb is not None and (sa.st_dev, sa.st_ino) == (sb.st_dev, sb.st_ino)
    if a == b or inside(a, b) or inside(b, a) or same_inode:
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
