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
from .native_toolset import _gcc, _native_image, _native_triplet, _require
from .paths import canonical_existing

HOST_PREFIX_API = 2
_NAMES = {'cc': ('gcc16/bin/gcc', 'bin/gcc-16', 'bin/gcc16', 'bin/gcc'),
          'cxx': ('gcc16/bin/g++', 'bin/g++-16', 'bin/g++16', 'bin/g++'),
          'cpp': ('gcc16/bin/cpp', 'bin/cpp-16', 'bin/cpp16', 'bin/cpp'),
          'ar': ('bin/ar', 'bin/llvm-ar'),
          'ranlib': ('bin/ranlib', 'bin/llvm-ranlib'),
          'nm': ('bin/nm', 'bin/llvm-nm'),
          'strip': ('bin/strip', 'bin/llvm-strip'),
          'pkg_info': ('sbin/pkg_info', 'bin/pkg_info', 'gnu/bin/pkg_info'),
          'otool': ('bin/otool', 'bin/llvm-otool'),
          'install': ('bin/bsdinstall', 'bin/install', 'bin/ginstall'),
          'awk': ('bin/awk', 'bin/nawk', 'bin/gawk', 'gnu/bin/awk'),
          'yacc': ('bin/yacc', 'bin/byacc'),
          'hostname': ('bin/hostname', 'gnu/bin/hostname', 'libexec/gettext/hostname'),
          'make': ('bin/bmake', 'bin/make'),
          'python': ('bin/python3.15', 'bin/python3', 'bin/python'),
          'sh': ('bin/sh', 'bin/mksh', 'bin/dash', 'bin/bash'),
          'pkg-config': ('bin/pkg-config', 'bin/pkgconf'),
          'patch': ('bin/patch', 'bin/nbpatch', 'gnu/bin/patch'),
          'tar': ('bin/tar', 'bin/nbtar', 'bin/bsdtar', 'gnu/bin/tar'),
          'gzcat': ('bin/gzcat', 'bin/zcat', 'gnu/bin/gzcat'),
          'readelf': ('bin/readelf', 'bin/llvm-readelf', 'gnu/bin/readelf'),
          'elf-ld': ('bin/ld.lld',),
          'install_name_tool': ('bin/install_name_tool', 'bin/llvm-install-name-tool', 'gnu/bin/install_name_tool'),
          'libtool_darwin': ('bin/libtool_darwin', 'bin/llvm-libtool-darwin', 'gnu/bin/libtool_darwin'),
          'lipo': ('bin/lipo', 'bin/llvm-lipo', 'gnu/bin/lipo'),
          'llvm_readtapi': ('bin/llvm_readtapi', 'bin/llvm-readtapi', 'gnu/bin/llvm_readtapi'),
          'pkg_add': ('bin/pkg_add', 'sbin/pkg_add', 'gnu/bin/pkg_add'),
          'pkg_admin': ('bin/pkg_admin', 'sbin/pkg_admin', 'gnu/bin/pkg_admin'),
          'pkg_create': ('bin/pkg_create', 'sbin/pkg_create', 'gnu/bin/pkg_create'),
          'pkg_delete': ('bin/pkg_delete', 'sbin/pkg_delete', 'gnu/bin/pkg_delete')}
_COMPONENTS = ('cc1', 'cc1plus', 'collect2', 'lto1', 'lto-wrapper', 'as', 'ld')
_CLANG_NAMES = {'cc': ('bin/clang',), 'cxx': ('bin/clang++',),
                'cpp': ('bin/clang-cpp',), 'as': ('bin/clang-as',),
                'otool': ('bin/llvm-otool', 'bin/otool'),
                **{role: ('bin/llvm-' + role,) for role in
                   ('ar', 'ranlib', 'nm', 'strip', 'objcopy', 'objdump', 'readelf')}}


def _compiler(value, host):
    _require(isinstance(value, dict) and set(value) == {'family', 'version', 'triplet'},
             'invalid native compiler identity')
    _require(value['family'] in ('clang', 'gcc'), 'unsupported compiler family')
    if value['family'] == 'gcc':
        _gcc(value, host)
    else:
        _require(isinstance(value['version'], str) and
                 re.fullmatch(r'[1-9][0-9]*\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9_.-]+)?', value['version']),
                 'invalid Clang compiler version')
        _native_triplet(value['triplet'], host)


def _clang_linker(driver, root, host, env, language, arguments=()):
    """Check a Clang frontend and actual link route without executing either."""
    result = process.capture([driver, *arguments, '-###', '-x', language, os.devnull,
                              '-o', os.devnull], env=env)
    commands = [shlex.split(line) for line in result.stderr.decode('utf-8', 'strict').splitlines()
                if line.lstrip().startswith('"')]
    linker, frontend = None, False
    for command in commands:
        _require(bool(command) and os.path.isabs(command[0]), 'implicit Clang driver component')
        path = _member(command[0], root, host, search_path=env['PATH'])
        if '-cc1' in command or '-cc1as' in command:
            _require(canonical_existing(path) == canonical_existing(driver),
                     'Clang selects a different frontend')
            frontend = True
        else:
            _require(os.path.basename(path) in ('ld.lld', 'ld64.lld', 'lld'),
                     'Clang must select the prefix LLVM linker')
            _require(linker is None, 'multiple Clang linker routes')
            linker = path
    _require(frontend and linker is not None, 'Clang did not report compiler and linker routes')
    return linker


def _clang(tools, root, host, env, query):
    """Inspect the real Clang routes without producing compilation outputs."""
    def identity(driver):
        version = re.search(r'\bclang version ([0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9_.-]+)?)',
                            query(driver, '--version'))
        _require(version is not None, 'selected compiler does not identify Clang')
        return dict(family='clang', version=version[1], triplet=query(driver, '-dumpmachine'))
    compiler = identity(tools['cc'])
    _compiler(compiler, host)
    _require(all(identity(tools[role]) == compiler for role in ('cxx', 'cpp')),
             'native C, C++ and preprocessor compilers differ')
    resource = canonical_existing(query(tools['cc'], '-print-resource-dir'))
    _require(os.path.isdir(resource) and resource != root and
             os.path.commonpath((resource, root)) == root, 'Clang resource directory escapes host prefix')
    components = {'resource-dir': resource}
    for role, language in (('cc', 'c'), ('cxx', 'c++')):
        linker = _clang_linker(tools[role], root, host, env, language)
        if 'ld' in components:
            _require(canonical_existing(linker) == canonical_existing(components['ld']),
                     'native C and C++ linkers differ')
        components['ld'] = linker
    # The Ports clang-as command provides assembler-only driver arguments.
    # Its backend is Clang's integrated assembler, not another assembler.
    components.update(cc1=tools['cc'], cc1as=tools['cc'])
    tools['ld'] = components['ld']
    return compiler, components


def _search_path(root, tools):
    return os.pathsep.join(dict.fromkeys(
        ([os.path.dirname(tools['cc'])] if 'cc' in tools else []) +
        [os.path.join(root, name) for name in ('bin', 'gnu/bin')] +
        [os.path.dirname(path) for _, path in sorted(tools.items())]))


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
    def compiler_family(self):
        """The inspected family, or None when no compiler was requested."""
        return self.compiler.get('family')

    @property
    def path(self):
        return _search_path(self.bundle_root, self.tools)

    def as_dict(self):
        return dict(format='emberbsd-host-prefix', version=HOST_PREFIX_API,
                    bundle_root=self.bundle_root, host=dict(self.host),
                    compiler=dict(self.compiler), tools=dict(self.tools),
                    abi_flags={name: list(flags) for name, flags in self.abi_flags.items()},
                    driver_components=dict(self.driver_components))


def validate_host_executable(prefix, executable, host, *, search_path):
    """Validate a selected host executable and its interpreter inside prefix."""
    _require(os.path.isabs(prefix) and os.path.isabs(executable),
             "native prefix and executable must be absolute")
    root = canonical_existing(prefix)
    return _member(executable, root, host, search_path=search_path)


def _member(path, root, host, *, search_path, binary=False, seen=()):
    """Check both path spellings and every script interpreter before execution."""
    real = canonical_existing(path)
    path = os.path.abspath(path)
    roots = (root,) if isinstance(root, str) else root
    _require(all(any(os.path.commonpath((value, item)) == item and value != item
                     for item in roots) for value in (path, real)),
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
        _member(words[0], root, host, search_path=search_path, seen=(*seen, real))
        if os.path.basename(words[0]) == 'env':
            args = words[1:]
            if args and args[0] == '-S':
                args = args[1:]
            _require(bool(args) and not args[0].startswith('-') and '=' not in args[0],
                     f'explicit native env interpreter required: {path}')
            interpreter = args[0]
            if not os.path.isabs(interpreter):
                _require('/' not in interpreter, f'bare native env interpreter required: {path}')
                candidates = (os.path.join(directory, interpreter)
                              for directory in search_path.split(os.pathsep))
                interpreter = next((candidate for candidate in candidates
                                    if os.path.isfile(candidate) and os.access(candidate, os.X_OK)), None)
                _require(interpreter is not None, f'missing native env interpreter: {path}')
            _member(interpreter, root, host, search_path=search_path, seen=(*seen, real))
    else:
        _native_image(real, host)
    return path


def load_host_prefix(prefix, *, required_roles, host=None, compiler_family='clang'):
    """Select LLVM or explicit GCC tools. Query drivers; do not compile probes.

    The caller builds the prefix from the common Ports recipes. This function
    checks containment, native CPU, script interpreters, and compiler routes.
    Host ABI headers, libraries, and process services remain OS inputs.
    """
    try:
        _require(compiler_family in ('clang', 'gcc'), 'unsupported compiler family')
        root = canonical_existing(os.fspath(prefix))
        _require(os.path.isdir(root), 'host prefix must be a directory')
        _require(root != os.path.sep, 'host prefix must be a dedicated directory')
        for name in ('bin', 'gnu/bin', 'sbin'):
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
            if needs_compiler and compiler_family == 'clang' and role == 'ld':
                continue
            names = (_CLANG_NAMES.get(role) if compiler_family == 'clang' else None)
            if compiler_family == 'clang' and role == 'ld':
                names = ('bin/ld64.lld',) if selected_host['os'] == 'Darwin' else ('bin/ld.lld',)
            names = names or _NAMES.get(role, ('bin/' + role, 'gnu/bin/' + role))
            candidates = [os.path.join(root, name) for name in names]
            selected = next((path for path in candidates if os.path.lexists(path)), None)
            if selected is None and needs_compiler and compiler_family == 'gcc' and role in ('as', 'ld'):
                # GCC can use another installed name, such as llvm-mc or
                # ld64.lld on Darwin. Check that actual route below.
                continue
            _require(selected is not None, f'missing native tool in host prefix: {role}')
            tools[role] = selected
        search_path = _search_path(root, tools)
        for selected in tools.values():
            _member(selected, root, selected_host, search_path=search_path)
        flags = {name: () for name in ('cppflags', 'cflags', 'cxxflags', 'ldflags')}
        if not needs_compiler:
            return HostPrefix(root, MappingProxyType(selected_host), MappingProxyType({}),
                              MappingProxyType(tools), MappingProxyType(flags), MappingProxyType({}))
        env = process.sanitized_environment()
        env['PATH'] = search_path

        def query(driver, option):
            return process.capture([driver, option], env=env).stdout.decode('utf-8', 'strict').strip()

        if compiler_family == 'clang':
            compiler, components = _clang(tools, root, selected_host, env, query)
            return HostPrefix(root, MappingProxyType(selected_host), MappingProxyType(compiler),
                              MappingProxyType(tools), MappingProxyType(flags), MappingProxyType(components))

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
            components[component] = _member(path, root, selected_host, search_path=search_path)
        for role in ('as', 'ld'):
            tools.setdefault(role, components[role])
            _require(canonical_existing(components[role]) == canonical_existing(tools[role]),
                     f'compiler selects a different native {role}')
        final_path = _search_path(root, tools)
        if final_path != search_path:
            # Driver-discovered tool directories can change env lookup order.
            for selected in dict.fromkeys((*tools.values(), *components.values())):
                _member(selected, root, selected_host, search_path=final_path)
        return HostPrefix(root, MappingProxyType(selected_host), MappingProxyType(compiler),
                          MappingProxyType(tools), MappingProxyType(flags), MappingProxyType(components))
    except (OSError, ValueError, TypeError, UnicodeError) as exc:
        raise SDKError(f'invalid host prefix: {exc}') from exc


def validate_host_prefix(value, *, required_roles):
    """Check saved command consistency. Do not read the old prefix."""
    _require(value.get('format') == 'emberbsd-host-prefix' and value.get('version') in (1, HOST_PREFIX_API),
             'invalid saved host prefix')
    root = value['bundle_root']
    _require(os.path.isabs(root) and root != os.path.sep, 'invalid host prefix')
    roles = set(required_roles)
    if roles & {'cc', 'cxx', 'cpp'} or value['compiler']:
        roles |= {'cc', 'cxx', 'cpp', 'as', 'ld'}
        _compiler(value['compiler'], value['host'])
        if value['version'] == 1:
            _require(value['compiler']['family'] == 'gcc', 'historical host prefix requires GCC')
    _require(roles <= set(value['tools']),
             'missing recorded native roles')
    for path in (*value['tools'].values(), *value['driver_components'].values()):
        _require(os.path.isabs(path) and os.path.normpath(path) == path and
                 os.path.commonpath((path, root)) == root and path != root,
                 'recorded tool escapes host prefix')
    _require(value['abi_flags'] == {name: [] for name in ('cppflags', 'cflags', 'cxxflags', 'ldflags')},
             'unexpected host prefix flags')
