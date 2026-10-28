# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Run owner checks with one selected native prefix and standard tool variables."""

import os
import re
import shlex

from . import compiler_bindings, paths, process
from .errors import SDKError

NATIVE_CHECKS_API = 1
REQUIRED_ROLES = ('cc', 'cxx', 'cpp', 'sh', 'python')
_FLAGS = ('cppflags', 'cflags', 'cxxflags', 'ldflags')
_VARIABLES = dict(CC='cc', CXX='cxx', CPP='cpp', AS='as', LD='ld', AR='ar',
    RANLIB='ranlib', NM='nm', STRIP='strip', OBJCOPY='objcopy', OBJDUMP='objdump',
    MAKE='make', AWK='awk', YACC='yacc', LEX='lex', M4='m4', PKG_CONFIG='pkg-config',
    PERL='perl', RUBY='ruby', PYTHON='python', SHELL='sh', CONFIG_SHELL='sh',
    CSA='clang', CPPCHECK='cppcheck', VALGRIND='valgrind')


def check_environment(native, *, bindings, extra_flags=None, environment=None,
                      native_cxx_runtime=None):
    """Discard ambient compiler/search flags and retain selected native ABI flags.

    Compiler adapters retain exact ABI argument words for existing consumers
    that accept only an executable or ignore standard flag variables.
    """
    extra_flags = {} if extra_flags is None else extra_flags
    if not isinstance(extra_flags, dict) or not set(extra_flags) <= set(_FLAGS):
        raise SDKError('native checks accept only explicit compiler/linker flag arrays')
    for role in REQUIRED_ROLES:
        if role not in native.tools:
            raise SDKError('native check selection is missing role: ' + role)
    result = process.sanitized_environment(environment)
    result['PATH'] = native.path
    for name, role in _VARIABLES.items():
        if role not in native.tools:
            continue
        command = native.command(role)
        if len(command) != 1:
            raise SDKError('native check tool variable requires a single executable: ' + role)
        result[name] = command[0]
    for name in _FLAGS:
        extra = extra_flags.get(name, ())
        if not isinstance(extra, (list, tuple)):
            raise SDKError('native check flags must be argument arrays: ' + name)
        words = [*native.abi_flags[name], *extra]
        if any(not isinstance(word, str) or not word or '\0' in word for word in words):
            raise SDKError('invalid native check flag: ' + name)
        result[name.upper()] = shlex.join(words)
    commands = {role:list(native.command(role)) for role in ('cc', 'cxx', 'cpp')}
    if 'clang' in native.tools:
        commands['csa'] = list(native.command('clang'))
    if native_cxx_runtime is not None:
        if (not isinstance(native_cxx_runtime, dict) or
                not isinstance(native_cxx_runtime.get('targets'), (list, tuple)) or
                native.host.get('os') != 'Darwin' or native.compiler.get('family') != 'clang' or
                native.compiler.get('triplet') not in native_cxx_runtime['targets']):
            raise SDKError('native C++ runtime must include the selected Darwin Clang host triplet')
        cpu = native.host.get('cpu')
        cpus = {'aarch64':r'(?:arm64|aarch64)', 'arm64':r'(?:arm64|aarch64)',
                'x86_64':'x86_64', 'amd64':'x86_64'}.get(cpu)
        if not cpus or any(not isinstance(target, str) or not re.fullmatch(
                cpus + r'-apple-(?:darwin|macosx?)[0-9.]*', target)
                for target in native_cxx_runtime['targets']):
            raise SDKError('native C++ runtime targets differ from the selected Darwin host')
    bindings = paths.canonical_new(bindings)
    aliases = {role:native.command(role)[0] for role in native.tools if role not in commands}
    aliases.update({'c++':bindings + '/cxx', 'python3':native.command('python')[0]})
    bound = compiler_bindings.bind_compilers(bindings, commands=commands,
        flags={name:shlex.split(result[name.upper()]) for name in _FLAGS},
        python=native.command('python')[0], aliases=aliases,
        native_cxx_runtime=native_cxx_runtime)
    result['PATH'] = bindings + os.pathsep + native.path
    for name, role in (('CC','cc'), ('CXX','cxx'), ('CPP','cpp'), ('CSA','csa')):
        if role in bound:
            result[name] = bound[role]
    # Consumers that already honor standard flags must not add the same ABI
    # flags twice. Per-test arguments still reach the adapter unchanged.
    result.update({name.upper():'' for name in _FLAGS})
    return result


def run_check(native, role, arguments, *, cwd, log, bindings, extra_flags=None, environment=None,
              native_cxx_runtime=None):
    """Execute a selected tool role using the shared owned-process backend."""
    if role not in native.tools:
        raise SDKError('native check selection is missing role: ' + role)
    if isinstance(arguments, (str, bytes)):
        raise SDKError('native check arguments must be an argv sequence')
    cwd = paths.canonical_existing(cwd)
    paths.real_directory(cwd)
    env = check_environment(native, bindings=bindings, extra_flags=extra_flags, environment=environment,
                            native_cxx_runtime=native_cxx_runtime)
    compiler = {'cc':'CC', 'cxx':'CXX', 'cpp':'CPP', 'clang':'CSA'}.get(role)
    command = [env[compiler]] if compiler else list(native.command(role))
    return process.run_logged([*command, *arguments], log, cwd=cwd, env=env,
                              empty_snapshot_timeout=120)
