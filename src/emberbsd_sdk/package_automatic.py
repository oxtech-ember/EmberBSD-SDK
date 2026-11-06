# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Preserve pkg_tools automatic state across interrupted replacements.

Callers hold the repository pkgdb lock throughout every operation.
"""
import os
from pathlib import Path
import re
import stat
import subprocess

from .errors import SDKError
from .jsonio import read_json, write_json
from .package_install import install_paths
from .paths import real_directory
from .release_signing import read_bytes

_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9+_.-]*-[0-9][A-Za-z0-9+_.-]*\Z')
_VALUES = ('yes', 'no', None)


def _name(value):
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise SDKError('invalid package name for automatic state')
    return value


def _state(value):
    if value not in _VALUES or (value is not None and not isinstance(value, str)):
        raise SDKError('invalid automatic state')
    return value


def _intent_directory(config, *, create=False):
    _, database = install_paths(config)
    directory = database.parent / (database.name + '.sdk-automatic')
    if create:
        directory.mkdir(mode=0o700, exist_ok=True)
    if directory.exists() or directory.is_symlink():
        real_directory(directory)
    return database, directory


def _sync(directory):
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_intent(path, name):
    value = read_json(path, max_bytes=4096)
    if not isinstance(value, dict) or set(value) != {'name', 'automatic'} or value['name'] != name:
        raise SDKError('invalid automatic state intent')
    return _state(value['automatic'])


def plan(config, packages, roots):
    """Select exact states for pending packages before any package mutation."""
    database, directory = _intent_directory(config)
    requested = set(roots)
    result = {}
    for package in packages:
        name = _name(package['pkgname'])
        if name in result:
            raise SDKError('duplicate package automatic state')
        replacements = package.get('replaces', [])
        if not isinstance(replacements, list) or len(replacements) > 1:
            raise SDKError('invalid automatic state replacement')
        if replacements:
            old = _name(replacements[0])
            info = database / old / '+INSTALLED_INFO'
            if info.exists() or info.is_symlink():
                lines = read_bytes(info, 1024 * 1024).decode('utf-8').splitlines()
                values = [line.split('=', 1)[1] for line in lines
                          if line.lower().startswith('automatic=')]
                if len(values) > 1:
                    raise SDKError('duplicate automatic value')
                value = values[0].lower() if values else None
                _state(value)
            else:
                value = None
        else:
            value = None if package['root'] in requested else 'yes'
        result[name] = value
    # Check every pending package before the caller starts any installation.
    for name, value in result.items():
        path = directory / name
        if (path.exists() or path.is_symlink()) and _read_intent(path, name) != value:
            raise SDKError('conflicting automatic state intent')
    return result


def prepare(config, name, value):
    """Durably record an idempotent intent before pkg_add."""
    name, value = _name(name), _state(value)
    _, directory = _intent_directory(config, create=True)
    path = directory / name
    if path.exists() or path.is_symlink():
        if _read_intent(path, name) != value:
            raise SDKError('conflicting automatic state intent')
        return
    write_json(path, {'name': name, 'automatic': value}, exclusive=False, mode=0o600)
    _sync(directory)


def finish(crypto, config, name):
    """Apply and remove one intent only after pkg_admin succeeds."""
    name = _name(name)
    database, directory = _intent_directory(config)
    path = directory / name
    value = _read_intent(path, name)
    operation = ['unset', 'automatic'] if value is None else ['set', 'automatic=' + value]
    command = [crypto.prefix.tool('pkg_admin'), '-K', str(database), *operation, name]
    result = subprocess.run(command, env={'PATH': crypto.prefix.path, 'LC_ALL': 'C'},
                            capture_output=True)
    if result.returncode:
        raise SDKError('cannot restore automatic state: ' +
                       result.stderr[-4096:].decode('utf-8', 'replace'))
    path.unlink()
    _sync(directory)


def recover(crypto, config, installed_names):
    """Complete intents for packages present in pkgdb; preserve pending ones."""
    _, directory = _intent_directory(config)
    if not directory.exists():
        return
    installed = {_name(name) for name in installed_names}
    for path in sorted(directory.iterdir()):
        if path.name.startswith('.'):
            match = re.fullmatch(r'\.(.+)\.([0-9a-f]{32})', path.name)
            if match:
                _name(match.group(1))
                if not stat.S_ISREG(path.lstat().st_mode):
                    raise SDKError('invalid automatic state staging file')
                # Unpublished write_json staging file; it is not an intent.
                continue
        name = _name(path.name)
        _read_intent(path, name)
        if name in installed:
            finish(crypto, config, name)
