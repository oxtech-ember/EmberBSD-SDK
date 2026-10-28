# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Bind exact compiler argument vectors for executable-only consumers."""

import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile

from . import elf, jsonio, paths, publication
from .errors import SDKError

COMPILER_BINDINGS_API = 3
NATIVE_CXX_RUNTIME_BINDING_API = 1
# Keep compiler-only mappings byte-compatible with existing bindings.
_FORMAT_VERSION = 1
_NATIVE_FORMAT_VERSION = 2
_DIRECT_ROLES = {'as', 'ld'}
_ROLES = {'cc', 'cxx', 'cpp', 'csa'} | _DIRECT_ROLES
_FLAGS = {'cppflags', 'cflags', 'cxxflags', 'ldflags'}
_COMPILE_ONLY = {'-c', '-S', '-E', '-M', '-MM', '-fsyntax-only', '--analyze', '-emit-ast'}
# The C++ driver links its standard library unless one of these drops it.
_NO_CXX_LIBRARY = {'-nostdlib', '-nodefaultlibs', '-nostdlib++', '-static'}
_LIBRARY_NAME = re.compile(r'lib[A-Za-z0-9_+-]+')


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


def _link_words(role, arguments):
    """Return (link operation words or None, option terminator seen, query)."""
    operation = _operation_words(arguments)
    terminated = '--' in operation
    if terminated:
        operation = operation[:operation.index('--')]
    if any(word in ('--version', '-dumpversion', '-dumpfullversion', '-dumpmachine', '-dumpspecs') or
           word.startswith('-print-') for word in operation):
        return None, terminated, True
    link = role in ('cc', 'cxx') and not _COMPILE_ONLY.intersection(operation)
    return (operation if link else None), terminated, False


def _predicted_runtime_path(role, operation):
    return role == 'cxx' and not _NO_CXX_LIBRARY.intersection(operation)


def _deployment_version(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){0,2}", value):
        raise SDKError("invalid native macOS deployment version")
    return tuple(int(part) for part in (value.split(".") + ["0", "0"])[:3])


def _native_cxx_options(runtime, arguments):
    """Return selected native header/link defaults without overriding caller options."""
    words = _operation_words(arguments)
    if '--' in words:
        words = words[:words.index('--')]
    stdlib = None
    explicit_headers = []
    deployment = None
    explicit_target = None
    explicit_sysroot = None
    explicit_arches = []
    explicit_abi_overrides = []
    iterator = iter(words)
    for word in iterator:
        if word.startswith('-stdlib='):
            stdlib = word.split('=', 1)[1]
        elif word == '-stdlib++-isystem':
            explicit_headers.append(next(iterator, ''))
        elif word.startswith('-stdlib++-isystem='):
            explicit_headers.append(word.split('=', 1)[1])
        elif word.startswith(('-mmacos-version-min=', '-mmacosx-version-min=')):
            deployment = word.split('=', 1)[1]
        elif re.match(r'-m(?:ios|iphoneos|tvos|watchos|visionos).*version-min=', word):
            raise SDKError('foreign deployment target in native C++ binding')
        elif word in ('--target', '-target'):
            explicit_target = next(iterator, '')
        elif word == '-arch':
            explicit_arches.append(next(iterator, ''))
        elif word in ('-m32', '-m64', '-m16') or word.startswith('-mabi='):
            explicit_abi_overrides.append(word)
        elif word == '-Xclang':
            forwarded = next(iterator, '')
            if forwarded in ('-triple', '-isysroot', '-target-abi') or forwarded.startswith(
                    ('-triple=', '-isysroot=', '-target-abi=')):
                raise SDKError('native C++ binding cannot inspect forwarded target or sysroot overrides')
        elif word.startswith('--target='):
            explicit_target = word.split('=', 1)[1]
        elif word in ('--sysroot', '-isysroot'):
            explicit_sysroot = next(iterator, '')
        elif word.startswith('--sysroot='):
            explicit_sysroot = word.split('=', 1)[1]
        elif word.startswith('-isysroot') and word != '-isysroot':
            explicit_sysroot = word[len('-isysroot'):].removeprefix('=')
        elif word in ('-I', '-isystem', '-iquote', '-idirafter', '-o', '-include',
                      '-imacros', '-L', '-l', '-Xlinker', '-MF', '-MT',
                      '-MQ', '-resource-dir', '--gcc-toolchain', '-B', '-x'):
            next(iterator, None)
    if explicit_target is not None and explicit_target not in runtime['targets']:
        raise SDKError('target differs from selected native C++ runtime')
    native_arches = {target.split('-', 1)[0] for target in runtime['targets']}
    if any(arch not in native_arches for arch in explicit_arches):
        raise SDKError('architecture differs from selected native C++ runtime')
    if any(option != '-m64' or native_arches != {'x86_64'}
           for option in explicit_abi_overrides):
        raise SDKError('unsupported ABI override for selected native C++ runtime')
    if explicit_sysroot is not None and (
            runtime['sysroot'] is None or
            os.path.realpath(explicit_sysroot) != runtime['sysroot']):
        raise SDKError('sysroot differs from selected native C++ runtime')
    minimum = _deployment_version(runtime['deployment'])
    if deployment is not None and _deployment_version(deployment) < minimum:
        raise SDKError('deployment target is older than selected native C++ runtime')
    if explicit_target:
        match = re.search(r'-apple-macosx?([0-9]+(?:\.[0-9]+){0,2})$', explicit_target)
        if match and _deployment_version(match.group(1)) < minimum:
            raise SDKError('target is older than selected native C++ runtime')
    if stdlib not in (None, 'libc++'):
        return [], []
    if any(os.path.realpath(header) != runtime['include_dir'] for header in explicit_headers):
        raise SDKError('C++ standard headers differ from selected native runtime')
    no_headers = bool(explicit_headers) or bool({'-nostdinc', '-nostdinc++', '-nostdlibinc'} & set(words))
    headers = [] if no_headers else [
        '--start-no-unused-arguments', '-stdlib++-isystem', runtime['include_dir'],
        '--end-no-unused-arguments']
    if deployment is None:
        headers = ['-mmacosx-version-min=' + runtime['deployment']] + headers
    libraries = [] if _NO_CXX_LIBRARY.intersection(words) or '-r' in words else [
        '-L' + runtime['library_dir']]
    return headers, libraries


def compiler_argv(record, role, arguments, *, runtime_path=None):
    """Attach selected native flags without shell evaluation or lost argument words.

    runtime_path selects whether a link gets the bound runtime library search
    path; None selects the language prediction that execute() verifies.
    """
    if record.get('format') != 'emberbsd-compiler-binding':
        raise SDKError('invalid compiler binding format')
    version = record.get('version')
    if type(version) is not int or version not in (_FORMAT_VERSION, _NATIVE_FORMAT_VERSION):
        raise SDKError('unsupported compiler binding version')
    specification = record['commands'][role]
    if version == _NATIVE_FORMAT_VERSION:
        if (not isinstance(specification, dict) or set(specification) != {'argv'} or
                'native_cxx_runtime' not in record):
            raise SDKError('invalid native compiler binding record')
        specification = specification['argv']
    elif 'native_cxx_runtime' in record:
        raise SDKError('native C++ runtime requires compiler binding version 2')
    command = _words(specification)
    arguments = _words(arguments)
    if role in _DIRECT_ROLES:
        # Assembler and linker arguments have their own syntax and response
        # files. Their selected argv already contains the target policy.
        return command + arguments
    operation, terminated, query = _link_words(role, arguments)
    if query:
        return command + arguments
    flags = record['flags']
    prefix = flags['cppflags'] + (flags['cxxflags'] if role == 'cxx' else
                                 flags['cflags'] if role in ('cc', 'csa') else [])
    link = operation is not None
    linker = flags['ldflags'] + record.get('link_flags', {}).get(role, []) if link else []
    if role == 'cxx' and 'native_cxx_runtime' in record:
        native_headers, native_libraries = _native_cxx_options(
            record['native_cxx_runtime'], command[1:] + prefix + arguments + linker)
        prefix = prefix + native_headers
        if link:
            linker = linker + native_libraries
    if link and 'runtime_rpath' in record:
        if runtime_path is None:
            runtime_path = _predicted_runtime_path(role, operation)
        if runtime_path:
            linker = linker + record['runtime_rpath']['flags']
    if link and terminated and linker:
        # Appending after -- makes flags filenames; moving libraries before
        # its inputs can change archive resolution. Do not silently do either.
        raise SDKError('link option terminator with bound linker flags is unsupported; use explicit input paths')
    return command + prefix + arguments + linker


def _link_output(operation):
    """Return the output path of a driver link, or None when it writes no file."""
    if '-###' in operation or '-r' in operation:
        return None
    output = 'a.out'
    words = iter(operation)
    for word in words:
        if word == '-o':
            output = next(words, None)
        elif word.startswith('-o') and not word.startswith(('-objc', '-object')):
            output = word[2:]
    return None if output in (None, '', '-') else output


def _needs_runtime(record, output):
    """True if the linked output needs a bound runtime library, None if no ELF was written."""
    try:
        with open(output, 'rb') as stream:
            if stream.read(4) != b'\x7fELF':
                return None
    except OSError:
        return None
    try:
        needed = elf.read_elf_dynamic(output)['needed']
    except SDKError:
        # Keep the search path when the output cannot be inspected.
        return True
    return any(re.fullmatch(re.escape(name) + r'\.so\.[0-9]+(?:\.[0-9]+)*', entry)
               for name in record['runtime_rpath']['libraries'] for entry in needed)


def bind_compilers(destination, *, commands, flags, python, aliases=None, link_flags=None,
                   runtime_rpath=None, native_cxx_runtime=None):
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
    if runtime_rpath is not None:
        # Linker search path for runtime libraries that a link may or may not
        # need. execute() keeps it only in outputs that need such a library.
        if (not isinstance(runtime_rpath, dict) or set(runtime_rpath) != {'flags', 'libraries'} or
                not _words(runtime_rpath['flags']) or not _words(runtime_rpath['libraries']) or
                any(not _LIBRARY_NAME.fullmatch(name) for name in runtime_rpath['libraries'])):
            raise SDKError('compiler runtime search path requires flags and library names')
        runtime_rpath = dict(flags=list(runtime_rpath['flags']), libraries=list(runtime_rpath['libraries']))
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
    record = dict(format='emberbsd-compiler-binding', version=_FORMAT_VERSION,
                  commands=commands, flags=flags, aliases=aliases)
    if native_cxx_runtime is not None:
        if (not isinstance(native_cxx_runtime, dict) or
                set(native_cxx_runtime) != {'include_dir', 'library_dir', 'sysroot', 'targets', 'deployment'}):
            raise SDKError('native C++ runtime requires selected include, library, sysroot and targets')
        runtime = dict(native_cxx_runtime)
        for key in ('include_dir', 'library_dir'):
            runtime[key] = paths.canonical_existing(runtime[key])
            paths.real_directory(runtime[key])
        if runtime['sysroot'] is not None:
            runtime['sysroot'] = paths.canonical_existing(runtime['sysroot'])
            paths.real_directory(runtime['sysroot'])
        runtime['targets'] = _words(runtime['targets'])
        _deployment_version(runtime['deployment'])
        # Version 1 readers do not inspect the version field. Their _words()
        # rejects each object below instead of silently omitting this runtime.
        record['version'] = _NATIVE_FORMAT_VERSION
        record['commands'] = {role:{'argv':words} for role,words in commands.items()}
        record['native_cxx_runtime'] = runtime
    if link_flags is not None:
        record['link_flags'] = link_flags
    if runtime_rpath is not None:
        record['runtime_rpath'] = runtime_rpath
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


def _run(command):
    status = subprocess.run(command).returncode
    if status < 0:
        signal.signal(-status, signal.SIG_DFL)
        os.kill(os.getpid(), -status)
        status = 128 - status
    return status


def execute(path, role, arguments):
    """Run the selected compiler so its exit status and signals propagate.

    Other operations replace the adapter process. A link with a bound runtime
    search path runs as a child: if the written ELF output needs a bound
    runtime library and the predicted link lacks the path, or has it without
    such a need, the same link runs once more with the other choice.
    """
    try:
        record = jsonio.read_json(path)
        command = compiler_argv(record, role, arguments)
        operation = None
        if 'runtime_rpath' in record and role not in _DIRECT_ROLES:
            operation = _link_words(role, _words(arguments))[0]
        output = None if operation is None else _link_output(operation)
        if output is None:
            os.execv(command[0], command)
        predicted = _predicted_runtime_path(role, operation)
        status = _run(command)
        if status == 0:
            needed = _needs_runtime(record, output)
            if needed is not None and needed != predicted:
                status = _run(compiler_argv(record, role, arguments, runtime_path=needed))
    except (SDKError, OSError, ValueError, KeyError, TypeError) as error:
        print('compiler binding: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
    raise SystemExit(status)
