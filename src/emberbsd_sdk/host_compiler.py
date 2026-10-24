# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD; AI-assisted OS and Ports build orchestration.
"""Explicit native GCC16 selection for host code. No query occurs on import."""

import os
import re

from . import inventory, paths
from .errors import SDKError

MAJOR = 16
LANGUAGES = {'cc': ('host-cc', 'c', 'HOST_CC'), 'c++': ('host-cxx', 'c++', 'HOST_CXX')}
# Ambient providers that the tool-selection policy forbids.
FORBIDDEN_PREFIXES = ('/usr/bin/', '/usr/local/', '/opt/homebrew/', '/opt/local/', '/Library/Developer/',
                      '/Applications/Xcode', '/home/linuxbrew/')


def forbidden_location(path):
    return next((prefix for prefix in FORBIDDEN_PREFIXES if path.startswith(prefix)), None)


def select(name, value, capture):
    """Validate one explicit native GCC16 driver and return its record.

    capture(argv) must return (stdout, stderr) and raise SDKError on failure.
    """
    option, language, variable = LANGUAGES[name]
    if not isinstance(value, str) or not value:
        raise SDKError(f'--{option} is required: select the SDK native GCC {MAJOR} driver; '
                       f'there is no PATH, cc or Clang fallback ({variable})')
    if not os.path.isabs(value):
        raise SDKError(f'--{option} must be an absolute path: {value}')
    if not os.path.lexists(value):
        raise SDKError(f'--{option} does not exist: {value}')
    path = paths.canonical_existing(value)
    for spelling in (value, path):
        prefix = forbidden_location(spelling)
        if prefix is not None:
            raise SDKError(f'--{option} selects an ambient system or package-manager compiler ({prefix}): {spelling}')
    paths.regular_file(path)
    if not os.access(path, os.X_OK):
        raise SDKError(f'--{option} is not executable: {path}')
    version = ''.join(capture([path, '--version'])).strip()
    if re.search(r'clang|llvm|apple', version, re.IGNORECASE):
        raise SDKError(f'--{option} is Clang, not native GCC {MAJOR}: {path}: {version.splitlines()[0] if version else ""}')
    macros = capture([path, '-x', language, '-dM', '-E', os.devnull])[0]
    defined = dict(m.groups() for m in re.finditer(r'^#define (\S+) ?(.*)$', macros, re.MULTILINE))
    if '__clang__' in defined or '__apple_build_version__' in defined:
        raise SDKError(f'--{option} is Clang, not native GCC {MAJOR}: {path}')
    if defined.get('__GNUC__') != str(MAJOR):
        raise SDKError(f'--{option} is not GCC {MAJOR}: __GNUC__={defined.get("__GNUC__")}: {path}')
    full = capture([path, '-dumpfullversion'])[0].strip()
    if re.fullmatch(rf'{MAJOR}\.\d+\.\d+', full) is None:
        raise SDKError(f'--{option} reports an unexpected GCC version {full!r}: {path}')
    machine = capture([path, '-dumpmachine'])[0].strip()
    programs = {tool: capture([path, f'-print-prog-name={tool}'])[0].strip() for tool in ('as', 'ld')}
    return dict(path=path, selected=value, sha256=inventory.sha256_file(path), version=version,
                gcc_version=full, machine=machine, programs=programs)


def select_pair(options, capture):
    records = {name: select(name, options.get(LANGUAGES[name][0]), capture) for name in LANGUAGES}
    if records['cc']['gcc_version'] != records['c++']['gcc_version'] or records['cc']['machine'] != records['c++']['machine']:
        raise SDKError('--host-cc and --host-cxx belong to different GCC installations')
    return records


def check_record(records):
    """Refuse a recorded host tool set that this selector would not accept."""
    if not isinstance(records, dict) or set(records) != set(LANGUAGES):
        raise SDKError('recorded host tools are not an explicit native GCC16 pair')
    for name, record in records.items():
        option = LANGUAGES[name][0]
        if not isinstance(record, dict) or not isinstance(record.get('path'), str) or not isinstance(record.get('gcc_version'), str):
            raise SDKError(f'recorded {option} was not selected as native GCC {MAJOR}; rebuild the sysroot with --{option}')
        if forbidden_location(record['path']) or forbidden_location(str(record.get('selected', ''))) \
                or re.search(r'clang|llvm|apple', str(record.get('version', '')), re.IGNORECASE) \
                or re.fullmatch(rf'{MAJOR}\.\d+\.\d+', record['gcc_version']) is None:
            raise SDKError(f'recorded {option} is not native GCC {MAJOR}: {record["path"]}')
