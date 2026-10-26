# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Inspect a selected LLVM cross toolchain; runtime support is a separate input."""

import os
import re

from . import inventory, paths, process
from .host_prefix import _clang_linker, _member, _require


def clang_compiler(prefix, triplet, *, host, env):
    """Record native executables and explicit target argv without compiling."""
    root = paths.canonical_existing(prefix)
    names = {'cc': 'clang', 'cxx': 'clang++', 'cpp': 'clang', 'as': 'clang',
             'ld': 'ld.lld', **{name: 'llvm-' + name for name in
                 ('ar', 'ranlib', 'nm', 'strip', 'objcopy', 'objdump', 'readelf', 'size', 'strings')}}
    binaries = {}
    for role, name in names.items():
        selected = os.path.join(root, 'bin', name)
        _member(selected, root, host, search_path=env['PATH'])
        path = paths.canonical_existing(selected)
        binaries[role] = dict(selected_path=selected, path=path, sha256=inventory.sha256_file(path))
    target = '--target=' + triplet
    linker = binaries['ld']['selected_path']
    commands = {role: [row['selected_path']] for role, row in binaries.items()}
    for role in ('cc', 'cxx'):
        commands[role] += [target, '--ld-path=' + linker]
    commands['cpp'] += ['-E', target]
    commands['as'] += [target, '-c', '-x', 'assembler']
    identity = None
    for role in ('cc', 'cxx'):
        driver = binaries[role]['selected_path']
        def query(option):
            return process.capture([driver, target, option], env=env).stdout.decode('utf-8', 'strict').strip()
        match = re.search(r'\bclang version ([0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9_.-]+)?)', query('--version'))
        _require(match is not None, 'selected target compiler does not identify Clang')
        reported = query('-dumpmachine')
        # Clang prints an empty vendor as "unknown". Keep the selected ABI
        # spelling in command argv while accepting that precise normalization.
        _require(reported in (triplet, triplet.replace('--', '-unknown-', 1)),
                 'target compiler triplet differs from selection')
        resource = paths.canonical_existing(query('-print-resource-dir'))
        _require(os.path.isdir(resource) and paths.inside(resource, root) and resource != root,
                 'Clang resource directory escapes compiler prefix')
        current = (match[1], reported, resource)
        _require(identity is None or current == identity, 'target C and C++ compiler inputs differ')
        identity = current
        actual = _clang_linker(driver, root, host, env, 'c' if role == 'cc' else 'c++',
                               (target, '--ld-path=' + linker))
        _require(paths.canonical_existing(actual) == binaries['ld']['path'],
                 'target driver selects a different LLVM linker')
    return dict(family='clang', version=identity[0], triplet=triplet, reported_triplet=identity[1], resource_dir=identity[2],
                binaries=binaries, commands=commands)
