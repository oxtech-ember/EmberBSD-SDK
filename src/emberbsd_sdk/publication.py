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


def _require_emberbsd_kernel(library, required_flag=1, operation='publication'):
    # Calling a syscall missing from a NetBSD kernel can deliver SIGSYS.
    # Check the running kernel independently of the selected userland.
    query = library.sysctlbyname
    query.argtypes = (ctypes.c_char_p, ctypes.c_void_p,
                      ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t)
    query.restype = ctypes.c_int
    flags = ctypes.c_int()
    size = ctypes.c_size_t(ctypes.sizeof(flags))
    result = query(b'vfs.generic.renameat2_flags', ctypes.byref(flags),
                   ctypes.byref(size), None, 0)
    if result != 0 or size.value != ctypes.sizeof(flags) or flags.value & required_flag != required_flag:
        raise SDKError(f'atomic directory {operation} is unavailable in the running EmberBSD kernel')


def _native_rename(required_flag=1, operation='publication'):
    if sys.platform == 'darwin':
        symbol = 'renamex_np'
        arguments = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        relative = False
    elif sys.platform.startswith(('linux', 'netbsd')):
        symbol = 'renameat2'
        arguments = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                     ctypes.c_char_p, ctypes.c_uint)
        relative = True
    else:
        raise SDKError('atomic directory publication requires Darwin, Linux, or EmberBSD')
    try:
        library = ctypes.CDLL(None, use_errno=True)
        function = getattr(library, symbol)
        if sys.platform.startswith('netbsd'):
            _require_emberbsd_kernel(library, required_flag, operation)
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


def _native_exchange():
    return _native_rename(2, 'exchange')


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
        # Linux and EmberBSD share these AT_FDCWD and RENAME_NOREPLACE values.
        result = function(-100, os.fsencode(source), -100,
                          os.fsencode(destination), 1)
    else:
        # Darwin RENAME_EXCL from sys/stdio.h.
        result = function(os.fsencode(source), os.fsencode(destination), 0x00000004)
    if result != 0:
        number = ctypes.get_errno() or errno.EIO
        raise OSError(number, os.strerror(number), destination)


def require_directory_exchange():
    """Check native atomic directory-exchange support without filesystem writes."""
    _native_exchange()


def exchange_directories(first, second):
    """Atomically exchange two existing real directories on one filesystem.

    Callers own both entries and their parent directories for the whole
    operation. This does not provide a writer lock or durability protocol.
    """
    function, relative = _native_exchange()
    first, second = _entry_path(first), _entry_path(second)
    first_info, second_info = paths.real_directory(first), paths.real_directory(second)
    if (first_info.st_dev, first_info.st_ino) == (second_info.st_dev, second_info.st_ino):
        raise SDKError('directory exchange entries name the same directory')
    if paths.relative_inside(first, second) is not None or paths.relative_inside(second, first) is not None:
        raise SDKError('directory exchange entries must not contain one another')
    ctypes.set_errno(0)
    if relative:
        result = function(-100, os.fsencode(first), -100, os.fsencode(second), 2)
    else:
        # Darwin RENAME_SWAP from sys/stdio.h.
        result = function(os.fsencode(first), os.fsencode(second), 0x00000002)
    if result != 0:
        number = ctypes.get_errno() or errno.EIO
        raise OSError(number, os.strerror(number), second)
