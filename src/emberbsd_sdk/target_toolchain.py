# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Inspect a selected LLVM cross toolchain; runtime support is a separate input."""

import os
import re

from . import inventory, paths, process
from .host_prefix import _clang_linker, _member, _require

CLANG_BINARIES = ('cc', 'cxx', 'cpp', 'as', 'ld', 'ar', 'ranlib', 'nm', 'strip',
                  'objcopy', 'objdump', 'readelf', 'size', 'strings')


def validate_clang_identity(record, triplet):
    """Validate a saved compiler route without consulting mutable executables."""
    _require(isinstance(record, dict) and set(record) == {'family', 'version', 'triplet',
        'reported_triplet', 'resource_dir', 'binaries', 'commands'}, 'invalid Clang compiler identity')
    _require(record['family'] == 'clang' and isinstance(record['version'], str) and
             re.fullmatch(r'[1-9][0-9]*\.[0-9]+\.[0-9]+', record['version']) and
             record['triplet'] == triplet and record['reported_triplet'] in
             (triplet, triplet.replace('--', '-unknown-', 1)), 'mixed Clang compiler identity')
    binaries, commands = record['binaries'], record['commands']
    _require(isinstance(binaries, dict) and set(binaries) == set(CLANG_BINARIES) and
             isinstance(commands, dict) and set(commands) == set(CLANG_BINARIES),
             'missing Clang compiler routes')
    for role, row in binaries.items():
        _require(isinstance(row, dict) and set(row) == {'selected_path', 'path', 'sha256'} and
                 all(isinstance(row[name], str) and os.path.isabs(row[name]) for name in ('selected_path', 'path')) and
                 isinstance(row['sha256'], str) and re.fullmatch('[0-9a-f]{64}', row['sha256']),
                 'invalid Clang compiler executable identity')
        expected = [row['selected_path']]
        if role in ('cc', 'cxx'):
            expected += ['--target=' + triplet, '--ld-path=' + binaries['ld']['selected_path']]
        elif role == 'cpp':
            expected += ['-E', '--target=' + triplet]
        elif role == 'as':
            expected += ['--target=' + triplet, '-c', '-x', 'assembler']
        _require(commands[role] == expected, 'mixed Clang compiler command')
    root = os.path.dirname(os.path.dirname(binaries['cc']['selected_path']))
    def contained(value):
        return (isinstance(value, str) and os.path.isabs(value) and os.path.normpath(value) == value and
                value.startswith(root + '/'))
    _require(all(contained(row[name]) for row in binaries.values() for name in ('selected_path', 'path')) and
             contained(record['resource_dir']),
             'Clang compiler route escapes its prefix')
    return record


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
