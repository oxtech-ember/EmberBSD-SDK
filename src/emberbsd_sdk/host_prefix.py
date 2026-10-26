# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Select native Ports tools from one installed host prefix."""

from dataclasses import dataclass
import os
import platform
import re
import shlex
from types import MappingProxyType

from . import process
from .errors import SDKError
from .native_toolset import _gcc, _native_image, _require
from .paths import canonical_existing

HOST_PREFIX_API = 1
_NAMES = {'cc': ('gcc16/bin/gcc', 'bin/gcc-16', 'bin/gcc16', 'bin/gcc'),
          'cxx': ('gcc16/bin/g++', 'bin/g++-16', 'bin/g++16', 'bin/g++'),
          'cpp': ('gcc16/bin/cpp', 'bin/cpp-16', 'bin/cpp16', 'bin/cpp'),
          'ar': ('bin/ar', 'bin/llvm-ar'),
          'ranlib': ('bin/ranlib', 'bin/llvm-ranlib'),
          'nm': ('bin/nm', 'bin/llvm-nm'),
          'strip': ('bin/strip', 'bin/llvm-strip'),
          'pkg_info': ('sbin/pkg_info', 'bin/pkg_info'),
          'otool': ('bin/otool', 'bin/llvm-otool'),
          'install': ('bin/bsdinstall', 'bin/install', 'bin/ginstall'),
          'awk': ('bin/awk', 'bin/nawk', 'bin/gawk', 'gnu/bin/awk'),
          'yacc': ('bin/yacc', 'bin/byacc'),
          'make': ('bin/bmake', 'bin/make'),
          'python': ('bin/python3.15', 'bin/python3', 'bin/python'),
          'sh': ('bin/sh', 'bin/mksh', 'bin/dash', 'bin/bash'),
          'pkg-config': ('bin/pkg-config', 'bin/pkgconf')}
_COMPONENTS = ('cc1', 'cc1plus', 'collect2', 'lto1', 'lto-wrapper', 'as', 'ld')


@dataclass(frozen=True)
class HostPrefix:
    """A checked selection. It has no qualification or receipt prerequisite."""

    bundle_root: str
    host: object
    compiler: object
    tools: object
    abi_flags: object
    driver_components: object

    def tool(self, role):
        try:
            return self.tools[role]
        except KeyError as exc:
            raise SDKError(f'native tool role is not selected: {role}') from exc

    def command(self, role):
        return (self.tool(role),)

    @property
    def path(self):
        return os.pathsep.join(dict.fromkeys(
            ([os.path.dirname(self.tool('cc'))] if 'cc' in self.tools else []) +
            [os.path.join(self.bundle_root, name) for name in ('bin', 'gnu/bin')] +
            [os.path.dirname(path) for _, path in sorted(self.tools.items())]))

    def as_dict(self):
        return dict(format='emberbsd-host-prefix', version=HOST_PREFIX_API,
                    bundle_root=self.bundle_root, host=dict(self.host),
                    compiler=dict(self.compiler), tools=dict(self.tools),
                    abi_flags={name: list(flags) for name, flags in self.abi_flags.items()},
                    driver_components=dict(self.driver_components))


def _member(path, root, host, *, binary=False, seen=()):
    """Check both path spellings and every script interpreter before execution."""
    real = canonical_existing(path)
    path = os.path.abspath(path)
    _require(os.path.commonpath((path, root)) == root and path != root and
             os.path.commonpath((real, root)) == root and real != root,
             f'native tool escapes host prefix: {path}')
    _require(os.path.isfile(real) and os.access(path, os.X_OK), f'not executable: {path}')
    _require(real not in seen, f'cyclic native script interpreter: {path}')
    with open(real, 'rb') as stream:
        first = stream.readline(4097)
    if first.startswith(b'#!') and not binary:
        try:
            words = shlex.split(first[2:].decode('utf-8').strip())
        except (ValueError, UnicodeError) as exc:
            raise SDKError(f'invalid native script interpreter: {path}') from exc
        _require(bool(words) and os.path.isabs(words[0]), f'absolute native interpreter required: {path}')
        _member(words[0], root, host, seen=(*seen, real))
        if os.path.basename(words[0]) == 'env':
            args = words[1:]
            if args and args[0] == '-S':
                args = args[1:]
            _require(bool(args) and not args[0].startswith('-') and '=' not in args[0],
                     f'explicit native env interpreter required: {path}')
            interpreter = args[0] if os.path.isabs(args[0]) else os.path.join(root, 'bin', args[0])
            _member(interpreter, root, host, seen=(*seen, real))
    else:
        _native_image(real, host)
    return path


def load_host_prefix(prefix, *, required_roles, host=None):
    """Select GCC16 and required tools. Query drivers; do not compile probes.

    The caller builds the prefix from the common Ports recipes. This function
    checks containment, native CPU, script interpreters, and GCC component paths.
    Host ABI headers, libraries, and process services remain OS inputs.
    """
    try:
        root = canonical_existing(os.fspath(prefix))
        _require(os.path.isdir(root), 'host prefix must be a directory')
        _require(root != os.path.sep, 'host prefix must be a dedicated directory')
        for name in ('bin', 'gnu/bin'):
            directory = os.path.join(root, name)
            if os.path.lexists(directory):
                real = canonical_existing(directory)
                _require(os.path.isdir(real) and os.path.commonpath((real, root)) == root,
                         f'build PATH directory escapes host prefix: {directory}')
        selected_host = dict(host or dict(os=platform.system(), cpu=platform.machine()))
        _require(set(selected_host) == {'os', 'cpu'}, 'host requires OS and CPU')
        roles = set(required_roles)
        needs_compiler = bool(roles & {'cc', 'cxx', 'cpp'})
        if needs_compiler:
            roles |= {'cc', 'cxx', 'cpp', 'as', 'ld'}
        tools = {}
        for role in sorted(roles):
            _require(isinstance(role, str) and re.fullmatch(r'[a-z][a-z0-9_-]*', role),
                     'invalid native tool role')
            names = _NAMES.get(role, ('bin/' + role, 'gnu/bin/' + role))
            candidates = [os.path.join(root, name) for name in names]
            selected = next((path for path in candidates if os.path.lexists(path)), None)
            if selected is None and needs_compiler and role in ('as', 'ld'):
                # GCC can use another installed name, such as llvm-mc or
                # ld64.lld on Darwin. Check that actual route below.
                continue
            _require(selected is not None, f'missing native tool in host prefix: {role}')
            tools[role] = _member(selected, root, selected_host)
        flags = {name: () for name in ('cppflags', 'cflags', 'cxxflags', 'ldflags')}
        if not needs_compiler:
            return HostPrefix(root, MappingProxyType(selected_host), MappingProxyType({}),
                              MappingProxyType(tools), MappingProxyType(flags), MappingProxyType({}))
        env = process.sanitized_environment()
        env['PATH'] = os.pathsep.join(dict.fromkeys(
            [os.path.dirname(tools['cc']), os.path.join(root, 'bin'), os.path.join(root, 'gnu/bin')] +
            [os.path.dirname(path) for _, path in sorted(tools.items())]))

        def query(driver, option):
            return process.capture([driver, option], env=env).stdout.decode('utf-8', 'strict').strip()

        compiler = dict(family='gcc', version=query(tools['cc'], '-dumpfullversion'),
                        triplet=query(tools['cc'], '-dumpmachine'))
        _gcc(compiler, selected_host)
        _require(query(tools['cxx'], '-dumpfullversion') == compiler['version'] and
                 query(tools['cxx'], '-dumpmachine') == compiler['triplet'],
                 'native C and C++ compilers differ')
        components = {}
        for component in _COMPONENTS:
            driver = tools['cxx'] if component == 'cc1plus' else tools['cc']
            path = query(driver, '-print-prog-name=' + component)
            if not os.path.isabs(path):
                _require('/' not in path and path == component,
                         f'implicit native driver component: {component}')
                path = next((os.path.join(directory, path) for directory in env['PATH'].split(os.pathsep)
                             if os.path.lexists(os.path.join(directory, path))), '')
            _require(bool(path), f'missing native driver component: {component}')
            components[component] = _member(path, root, selected_host)
        for role in ('as', 'ld'):
            tools.setdefault(role, components[role])
            _require(canonical_existing(components[role]) == canonical_existing(tools[role]),
                     f'compiler selects a different native {role}')
        return HostPrefix(root, MappingProxyType(selected_host), MappingProxyType(compiler),
                          MappingProxyType(tools), MappingProxyType(flags), MappingProxyType(components))
    except (OSError, ValueError, TypeError, UnicodeError) as exc:
        raise SDKError(f'invalid host prefix: {exc}') from exc


def validate_host_prefix(value, *, required_roles):
    """Check saved command consistency. Do not read the old prefix."""
    _require(value.get('format') == 'emberbsd-host-prefix' and value.get('version') == HOST_PREFIX_API,
             'invalid saved host prefix')
    root = value['bundle_root']
    _require(os.path.isabs(root) and root != os.path.sep, 'invalid host prefix')
    roles = set(required_roles)
    if roles & {'cc', 'cxx', 'cpp'} or value['compiler']:
        roles |= {'cc', 'cxx', 'cpp', 'as', 'ld'}
        _gcc(value['compiler'], value['host'])
    _require(roles <= set(value['tools']),
             'missing recorded native roles')
    for path in (*value['tools'].values(), *value['driver_components'].values()):
        _require(os.path.isabs(path) and os.path.normpath(path) == path and
                 os.path.commonpath((path, root)) == root and path != root,
                 'recorded tool escapes host prefix')
    _require(value['abi_flags'] == {name: [] for name in ('cppflags', 'cflags', 'cxxflags', 'ldflags')},
             'unexpected host prefix flags')
