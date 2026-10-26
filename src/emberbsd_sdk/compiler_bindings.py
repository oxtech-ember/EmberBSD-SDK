# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Bind exact compiler argument vectors for executable-only consumers."""

import os
from pathlib import Path
import re
import shlex
import shutil
import sys
import tempfile

from . import jsonio, paths, publication
from .errors import SDKError

COMPILER_BINDINGS_API = 1
_ROLES = {'cc', 'cxx', 'cpp', 'csa'}
_FLAGS = {'cppflags', 'cflags', 'cxxflags', 'ldflags'}
_COMPILE_ONLY = {'-c', '-S', '-E', '-M', '-MM', '-fsyntax-only', '--analyze', '-emit-ast'}


def _words(value):
    if (not isinstance(value, (list, tuple)) or
            any(not isinstance(word, str) or not word or '\0' in word for word in value)):
        raise SDKError('compiler binding requires argument arrays without empty words or NUL')
    return list(value)


def _operation_words(arguments, seen=()):
    """Inspect POSIX response files for operation switches; preserve caller argv."""
    result = []
    for word in arguments:
        if word.startswith('@'):
            path = paths.canonical_existing(word[1:])
            if path in seen or len(seen) >= 32:
                raise SDKError('compiler response-file cycle or nesting limit')
            with open(path, encoding='utf-8') as stream:
                content = stream.read(1024 * 1024 + 1)
            if len(content) > 1024 * 1024:
                raise SDKError('compiler response file exceeds inspection limit')
            result.extend(_operation_words(shlex.split(content), (*seen, path)))
        else:
            result.append(word)
    return result


def compiler_argv(record, role, arguments):
    """Attach selected native flags without shell evaluation or lost argument words."""
    command = _words(record['commands'][role])
    arguments = _words(arguments)
    operation = _operation_words(arguments)
    terminated = '--' in operation
    if terminated:
        operation = operation[:operation.index('--')]
    if any(word in ('--version', '-dumpversion', '-dumpfullversion', '-dumpmachine', '-dumpspecs') or
           word.startswith('-print-') for word in operation):
        return command + arguments
    flags = record['flags']
    prefix = flags['cppflags'] + (flags['cxxflags'] if role == 'cxx' else
                                 flags['cflags'] if role in ('cc', 'csa') else [])
    link = role in ('cc', 'cxx') and not _COMPILE_ONLY.intersection(operation)
    linker = flags['ldflags'] + record.get('link_flags', {}).get(role, []) if link else []
    if link and terminated and linker:
        # Appending after -- makes flags filenames; moving libraries before
        # its inputs can change archive resolution. Do not silently do either.
        raise SDKError('link option terminator with bound linker flags is unsupported; use explicit input paths')
    return command + prefix + arguments + linker


def bind_compilers(destination, *, commands, flags, python, aliases=None, link_flags=None):
    """Create or reuse caller-owned adapters for a previously selected toolset.

    This publishes command routing, not tool qualification. Callers obtain the
    commands and Python from their selected native prefix before binding them.
    """
    if not isinstance(commands, dict) or not {'cc', 'cxx', 'cpp'} <= set(commands) <= _ROLES:
        raise SDKError('compiler binding requires selected C, C++ and preprocessor commands')
    if not isinstance(flags, dict) or set(flags) != _FLAGS:
        raise SDKError('compiler binding requires all four native flag arrays')
    commands = {role:_words(words) for role, words in commands.items()}
    if any(not words or not os.path.isabs(words[0]) for words in commands.values()):
        raise SDKError('compiler binding requires absolute selected executables')
    flags = {name:_words(words) for name, words in flags.items()}
    if link_flags is not None:
        if not isinstance(link_flags, dict) or not set(link_flags) <= {'cc', 'cxx'}:
            raise SDKError('compiler link flags support only cc and cxx roles')
        link_flags = {role:_words(words) for role, words in link_flags.items()}
    aliases = {} if aliases is None else aliases
    if (not isinstance(aliases, dict) or set(aliases) & (set(commands) | {'commands.json'}) or
            any(not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_+.-]+', name) or
                name in ('.','..') or not isinstance(target, str) or not os.path.isabs(target)
                for name, target in aliases.items())):
        raise SDKError('compiler binding requires distinct tool alias names and absolute selected paths')
    python = paths.canonical_existing(python)
    if any(character.isspace() for character in python):
        raise SDKError('native Python shebang path cannot contain whitespace')
    destination = paths.canonical_new(destination)
    parent = os.path.dirname(destination)
    paths.real_directory(parent)
    record = dict(format='emberbsd-compiler-binding', version=COMPILER_BINDINGS_API,
                  commands=commands, flags=flags, aliases=aliases)
    if link_flags is not None:
        record['link_flags'] = link_flags
    module_root = str(Path(__file__).resolve().parents[1])
    content = {'commands.json':jsonio.json_bytes(record)}
    for role in sorted(commands):
        content[role] = (f'#!{python}\nimport sys\nsys.dont_write_bytecode=True\n'
            f'sys.path.insert(0, {module_root!r})\n'
            'from emberbsd_sdk.compiler_bindings import execute\n'
            f'execute({os.path.join(destination, "commands.json")!r}, {role!r}, sys.argv[1:])\n').encode()
    if os.path.lexists(destination):
        paths.real_directory(destination)
        if set(os.listdir(destination)) != set(content) | set(aliases):
            raise SDKError('compiler binding differs from selected commands')
        for name, data in content.items():
            path = os.path.join(destination, name)
            paths.regular_file(path)
            if Path(path).read_bytes() != data or (name != 'commands.json' and not os.access(path, os.X_OK)):
                raise SDKError('compiler binding differs from selected commands')
        for name, target in aliases.items():
            path = os.path.join(destination, name)
            if not os.path.islink(path) or os.readlink(path) != target:
                raise SDKError('compiler binding differs from selected tool aliases')
    else:
        stage = tempfile.mkdtemp(prefix='.compiler-binding-', dir=parent)
        try:
            for name, data in content.items():
                path = Path(stage, name)
                path.write_bytes(data)
                path.chmod(0o444 if name == 'commands.json' else 0o555)
            for name, target in aliases.items():
                os.symlink(target, os.path.join(stage, name))
            publication.publish_directory_noreplace(stage, destination)
        finally:
            if os.path.isdir(stage):
                shutil.rmtree(stage)
    return {role:os.path.join(destination, role) for role in commands}


def execute(path, role, arguments):
    """Replace the adapter process so compiler exit status and signals propagate."""
    try:
        record = jsonio.read_json(path)
        command = compiler_argv(record, role, arguments)
        os.execv(command[0], command)
    except (SDKError, OSError, ValueError, KeyError, TypeError) as error:
        print('compiler binding: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
