# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Route a bounded set of host OS services beside selected prefix tools."""

import os
import stat

from .errors import SDKError

HOST_SERVICES_API = 1

# These are host OS inspection and filesystem services used by configure
# scripts. Build tools, compilers, interpreters, and generators belong to the
# selected native prefix and must never be exposed through this directory.
SERVICE_COMMANDS = (
    'basename', 'cat', 'cut', 'dirname', 'expr', 'grep', 'head', 'mkdir',
    'pwd', 'rm', 'sort', 'tr', 'uname',
)
SELECTED_PREFIX_ROLES = ('awk', 'sed', 'sh')
PREFIX_COMMANDS = {'awk': 'awk', 'sed': 'sed', 'sh': 'sh'}


def _service_path(name):
    """Find one executable spelling using only the OS's declared default path."""
    for directory in os.defpath.split(os.pathsep):
        if not directory:
            continue
        candidate = os.path.join(directory, name)
        try:
            mode = os.stat(candidate).st_mode
        except OSError:
            continue
        if stat.S_ISREG(mode) and os.access(candidate, os.X_OK):
            return candidate
    raise SDKError('required host OS service is unavailable in os.defpath: ' + name)


def _directory(work):
    if not isinstance(work, (str, os.PathLike)) or not os.path.isabs(work):
        raise SDKError('host services require an absolute producer work directory')
    work = os.fspath(work)
    try:
        work_stat = os.lstat(work)
    except OSError as exc:
        raise SDKError('producer work directory must already exist') from exc
    if not stat.S_ISDIR(work_stat.st_mode):
        raise SDKError('producer work path must be a real directory')
    native_dir = os.path.join(work, '.native')
    shim = os.path.join(native_dir, 'toolshim')
    for path in (native_dir, shim):
        try:
            mode = os.lstat(path).st_mode
        except FileNotFoundError:
            os.mkdir(path, 0o700)
            mode = os.lstat(path).st_mode
        except OSError as exc:
            raise SDKError('cannot inspect host service directory') from exc
        if not stat.S_ISDIR(mode):
            raise SDKError('host service path must be a real directory')
    return shim


def build_path(native, work):
    """Return a work-local bounded tool shim before selected prefix tools."""
    for role in SELECTED_PREFIX_ROLES:
        try:
            selected = native.tool(role)
        except (AttributeError, KeyError) as exc:
            raise SDKError('native prefix is missing selected role: ' + role) from exc
        root = os.path.realpath(native.bundle_root)
        resolved = os.path.realpath(selected)
        if os.path.commonpath((root, resolved)) != root:
            raise SDKError('selected native role escapes host prefix: ' + role)

    shim = _directory(work)
    expected = {name: _service_path(name) for name in SERVICE_COMMANDS}
    expected.update({command: native.tool(role) for role, command in PREFIX_COMMANDS.items()})
    try:
        existing = set(os.listdir(shim))
    except OSError as exc:
        raise SDKError('cannot inspect host service directory') from exc
    if existing - set(expected):
        raise SDKError('host service directory contains an undeclared entry')
    for name, target in expected.items():
        link = os.path.join(shim, name)
        try:
            current = os.readlink(link)
        except FileNotFoundError:
            try:
                os.symlink(target, link)
            except FileExistsError:
                current = os.readlink(link)
            else:
                continue
        except OSError as exc:
            raise SDKError('host service entry is not a reusable symlink: ' + name) from exc
        if current != target:
            raise SDKError('host service symlink differs from selected OS service: ' + name)
    return shim + os.pathsep + native.path
