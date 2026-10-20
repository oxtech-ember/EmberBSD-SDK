# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD Ports bundle-gcc16.py, 2313b5f8d9b124369310f2b9f4f9a94c4852861f.
# AI-assisted adaptation of the BSD native no-replace wrapper.
"""Publish an owned directory without replacing a destination entry.

Callers own the source and both parent directories for the whole operation.
The native rename excludes a concurrent destination entry. This is not a lock
against another writer changing the source or its parents, nor a durability
or package-validation protocol. The module does not start or stop producers.
"""

import ctypes
import errno
import os
import sys

from .errors import SDKError
from . import paths

DIRECTORY_PUBLICATION_API = 1


def _native_rename():
    if sys.platform == 'darwin':
        symbol = 'renamex_np'
        arguments = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        relative = False
    elif sys.platform.startswith('linux'):
        symbol = 'renameat2'
        arguments = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                     ctypes.c_char_p, ctypes.c_uint)
        relative = True
    else:
        raise SDKError('atomic directory publication requires Darwin or Linux')
    try:
        library = ctypes.CDLL(None, use_errno=True)
        function = getattr(library, symbol)
        function.argtypes = arguments
        function.restype = ctypes.c_int
    except (OSError, AttributeError) as exc:
        raise SDKError(f'atomic directory publication is unavailable: {exc}') from exc
    return function, relative


def require_directory_publication():
    """Check host and native symbol availability without filesystem writes.

    A filesystem can still reject the actual rename. There is no fallback.
    """
    _native_rename()


def _entry_path(value):
    value = paths.path_string(value)
    parent, name = os.path.split(value)
    if name in ('', '.', '..'):
        raise SDKError('publication requires a named directory entry')
    parent = paths.canonical_existing(parent)
    paths.real_directory(parent)
    return os.path.join(parent, name)


def publish_directory_noreplace(source, destination):
    """Rename a real directory atomically; never replace an existing entry.

    Parent aliases are resolved. The source leaf must not be a symlink. The
    destination leaf is passed to the native no-replace operation unchanged.
    Errors preserve the native errno; EXDEV and unsupported filesystems do not
    trigger copying, ordinary rename, removal, or cleanup.
    """
    function, relative = _native_rename()
    source, destination = _entry_path(source), _entry_path(destination)
    source_info = paths.real_directory(source)
    destination_info = paths.entry(destination)
    if destination_info is not None and (
            source_info.st_dev, source_info.st_ino) == (
            destination_info.st_dev, destination_info.st_ino):
        raise SDKError('publication source and destination are the same directory')
    if paths.relative_inside(os.path.dirname(destination), source) is not None:
        raise SDKError('publication destination is inside the source directory')
    ctypes.set_errno(0)
    if relative:
        # Linux AT_FDCWD and RENAME_NOREPLACE; libc declares unsigned flags.
        result = function(-100, os.fsencode(source), -100,
                          os.fsencode(destination), 1)
    else:
        # Darwin RENAME_EXCL from sys/stdio.h.
        result = function(os.fsencode(source), os.fsencode(destination), 0x00000004)
    if result != 0:
        number = ctypes.get_errno() or errno.EIO
        raise OSError(number, os.strerror(number), destination)
