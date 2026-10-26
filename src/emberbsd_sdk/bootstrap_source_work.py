# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Copy admitted source data and bind a finite accepted utility namespace."""
import os
from pathlib import Path
import shutil
import stat
from .inventory import snapshot_tree
from .native_toolset import _absolute, _require
from .paths import entry


def copy_tree(source, destination):
    before = snapshot_tree(str(source))
    _require(all(row['kind'] in ('file', 'directory') for row in before.values()),
             'staged source must contain only regular files and directories')
    mtimes = {name: os.stat(Path(source, name)).st_mtime_ns for name in before}
    shutil.copytree(source, destination, copy_function=shutil.copy2, symlinks=False)
    expected = {}
    for name, row in before.items():
        mode = row['mode'] | (0o700 if row['kind'] == 'directory' else 0o200)
        os.chmod(Path(destination, name), mode)
        expected[name] = dict(row, mode=mode)
    os.chmod(destination, os.stat(destination).st_mode | 0o700)
    _require(snapshot_tree(str(destination)) == expected and
             all(os.stat(Path(destination, name)).st_mtime_ns == value for name, value in mtimes.items()),
             'staged source differs from admitted bytes, modes or mtimes')
    return expected


def tool_namespace(directory, commands, roles):
    os.mkdir(directory)
    for role in sorted(roles):
        _require(len(commands[role]) == 1 and '/' not in role and role not in ('.', '..'),
                 'utility namespace requires one selected native spelling')
        os.symlink(_absolute(commands[role][0]), Path(directory, role))


def validate_namespace(directory, commands, roles):
    info = entry(directory)
    _require(info is not None and stat.S_ISDIR(info.st_mode) and
             set(os.listdir(directory)) == set(roles), 'selected utility namespace roster changed')
    for role in roles:
        path = Path(directory, role)
        info = entry(path)
        _require(len(commands[role]) == 1 and info is not None and stat.S_ISLNK(info.st_mode) and
                 os.readlink(path) == _absolute(commands[role][0]), 'selected utility namespace target changed')
