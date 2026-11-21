# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Publish generated command trees for the lifetime of their build workspace."""

import errno
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile

from . import jsonio, paths, publication
from .errors import SDKError


_STORE = '.emberbsd-command-trees'


def _private_directory(path):
    info = paths.real_directory(str(path))
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise SDKError(f'expected a private SDK command store directory: {path}')


def _generation(index):
    _private_directory(index.parent)
    target = os.readlink(index)
    if not re.fullmatch(r'g-[a-zA-Z0-9_]+/tree', target):
        raise SDKError(f'invalid SDK command generation: {index}')
    tree = index.parent / target
    _private_directory(tree.parent)
    paths.real_directory(str(tree))
    return tree


def managed_tree(destination):
    """Return the backing tree only for a managed reference."""
    path = Path(destination)
    if not sys.platform.startswith('netbsd') or not path.is_symlink():
        return None
    target = os.readlink(path)
    if not re.fullmatch(re.escape(_STORE) + r'/[0-9a-f]{64}', target):
        return None
    return _generation(path.parent / target)


def binding_path(value):
    if not sys.platform.startswith('netbsd'):
        return paths.canonical_new(value)
    path = Path(paths.path_string(value))
    if path.name in ('', '.', '..', _STORE):
        raise SDKError(f'invalid command directory path: {path}')
    if path.is_symlink() and managed_tree(path) is None:
        raise SDKError(f'unexpected command directory link: {path}')
    # Resolving the leaf here can race a legitimate concurrent publication
    # and embed a private generation path in the adapters.
    return os.path.join(paths.canonical_existing(path.parent), path.name)


def publish(stage, destination):
    if not sys.platform.startswith('netbsd'):
        return publication.publish_directory_noreplace(stage, destination)

    # The release/sysroot API requires a real directory rename. For a command
    # path a symlink to an immutable, co-located tree is sufficient.
    from .inventory import snapshot_installed_tree

    destination = Path(destination)
    if destination.name == _STORE:
        raise SDKError(f'reserved SDK command store path: {destination}')
    if os.path.lexists(destination):
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), str(destination))
    store = destination.parent / _STORE
    store.mkdir(mode=0o700, exist_ok=True)
    _private_directory(store)
    manifest = snapshot_installed_tree(stage)
    key = hashlib.sha256(jsonio.json_bytes(manifest)).hexdigest()
    index = store / key
    if not os.path.lexists(index):
        holder = Path(tempfile.mkdtemp(prefix='g-', dir=store))
        # Only this invocation has created the holder. The externally visible
        # names below are installed exclusively with symlink(2).
        try:
            os.rename(stage, holder / 'tree')
            try:
                os.symlink(holder.name + '/tree', index)
            except FileExistsError:
                # Another producer published the same selection first.
                shutil.rmtree(holder)
        except OSError:
            if holder.exists():
                shutil.rmtree(holder)
            raise
    if snapshot_installed_tree(str(_generation(index))) != manifest:
        raise SDKError(f'saved SDK command generation differs from the selected tools: {index}')
    os.symlink(f'{_STORE}/{key}', destination)
