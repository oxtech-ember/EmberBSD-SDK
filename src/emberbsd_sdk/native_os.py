# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Run OS producers with their declared native roles and separate target argv."""

import os
import re
from pathlib import Path

from . import inventory, native_os_routing, paths, process
from .errors import SDKError
from .host_prefix import load_host_prefix

NATIVE_OS_API = 1


def _contract(source):
    source = paths.canonical_existing(source)
    paths.real_directory(source)
    contract = os.path.join(source, 'tools/native-tools/contract')
    digest = inventory.sha256_file(contract)
    _, _, roles = native_os_routing.read_native_os_contract(contract, sha256=digest)
    return source, contract, digest, roles


def select_native_os_tools(host_prefix, source, *, compiler_family='clang', gnu_target=False):
    """Load all roles in the selected OS contract, plus the shared interpreter."""
    _, _, _, roles = _contract(source)
    extra = ({'gmake'} | {role for _, role in native_os_routing.GETTEXT_TARGET_ROLES}) if gnu_target else set()
    return load_host_prefix(host_prefix, compiler_family=compiler_family,
                            required_roles=roles | {'python'} | extra)


def native_os_environment(native, routing, *, environment=None):
    """Expose selected host tools and ABI flags, separate from target variables."""
    result = process.sanitized_environment(environment)
    result.update(PATH=os.fspath(routing) + os.pathsep + native.path,
                  EMBER_NATIVE_TOOLS=os.fspath(routing), HOST_SH=native.tool('sh'),
                  PYTHONDONTWRITEBYTECODE='1')
    for flag in native_os_routing.native_host_flags(native.as_dict()):
        name, value = flag.split('=', 1)
        result[name] = value
    return result


def _build_sh_options(arguments):
    # Match build.sh's POSIX getopts prefix, including clustered short options.
    requires = set('aBCcDjMmNORTSVwXZ')
    allowed = requires | set('Ehn oPruUx'.replace(' ', ''))
    result = []
    index = 0
    while index < len(arguments):
        word = arguments[index]
        if word == '--' or word == '-' or not word.startswith('-'):
            break
        pos = 1
        while pos < len(word):
            option = word[pos]
            if option not in allowed:
                raise SDKError('unsupported build.sh option: -' + option)
            if option in requires:
                value = word[pos + 1:]
                if not value:
                    index += 1
                    if index >= len(arguments):
                        raise SDKError('missing build.sh option value: -' + option)
                    value = arguments[index]
                result.append((option, value))
                break
            result.append((option, None))
            pos += 1
        index += 1
    return result


def _check_build_arguments(arguments, *, gnu_target=False):
    options = _build_sh_options(arguments)
    for option, value in options:
        if option not in ('V', 'Z'):
            continue
        name = value.split('=', 1)[0]
        if (name.startswith('HOST_') or name.startswith('EMBER_HOST_ABI_') or
                name in ('EMBER_NATIVE_TOOLS', 'EMBER_HOST_COMPILER_FAMILY',
                         'EMBER_HOST_PREFIX', 'PATH') or
                (gnu_target and name in ('TOOLDIR', 'GNU_TARGET_LIBTOOL', 'EMBER_TARGET_TOOL_DIR', 'EMBER_HOST_GNU_TRIPLE'))):
            raise SDKError('build.sh arguments cannot override the selected native tools or ABI')
    return options


def _safe_gnu_path(value):
    return (isinstance(value, str) and
            re.fullmatch(r'/[A-Za-z0-9_./+@%=-]*', value) is not None)


def run_native_os(native, *, source, routing, cwd, arguments, log=None,
                  capture=False, append=False, build_sh=False, shell=False,
                  environment=None, target_tools=None, target_libtool=None):
    """Run one producer or metadata query using existing process backends.

    build_sh receives only build.sh arguments and records native ABI values in
    its generated make wrapper. Other commands must use an explicit path;
    shell selects the native shell for OS scripts and generated make wrappers.
    """
    gnu_target = target_tools is not None or target_libtool is not None
    if gnu_target and (not build_sh or target_tools is None or target_libtool is None):
        raise SDKError('GNU target tools and libtool must be selected together for build.sh')
    if capture and build_sh:
        raise SDKError('build.sh producers require a log; capture cannot drain the owned process')
    if (capture == (log is not None) or (append and log is None) or (build_sh and shell)):
        raise SDKError('select capture or log, and only one OS command mode')
    if isinstance(arguments, (str, bytes)):
        raise SDKError('OS command arguments must be an argv sequence')
    arguments = list(arguments)
    if any(not isinstance(word, str) or '\0' in word for word in arguments):
        raise SDKError('invalid OS command argument')
    source, contract, digest, roles = _contract(source)
    for role in roles | {'python'}:
        native.tool(role)
    cwd = paths.canonical_existing(cwd)
    paths.real_directory(cwd)
    routing = paths.path_string(routing)
    native_os_routing.ensure_native_os_routing(native, contract_path=contract,
        contract_sha256=digest, destination=routing)
    env = native_os_environment(native, routing, environment=environment)
    if build_sh:
        options = _check_build_arguments(arguments, gnu_target=gnu_target)
        script = os.path.join(source, 'build.sh')
        paths.regular_file(script)
        selection = native.as_dict()
        flags = native_os_routing.native_host_flags(selection)
        if gnu_target:
            for role in {'gmake'} | {role for _, role in native_os_routing.GETTEXT_TARGET_ROLES}:
                native.tool(role)
            dirs = [value for option, value in options if option == 'T']
            if len(dirs) != 1:
                raise SDKError('GNU target tools require one explicit OS TOOLDIR (-T)')
            if not all(_safe_gnu_path(path) for path in (dirs[0], target_tools, target_libtool)):
                raise SDKError('GNU target tools require safe absolute paths')
            tooldir = Path(paths.canonical_existing(dirs[0]))
            selected_tools = Path(paths.canonical_existing(target_tools))
            selected_libtool = Path(paths.canonical_existing(target_libtool))
            if (not selected_tools.is_relative_to(tooldir) or not selected_libtool.is_relative_to(tooldir) or
                    not (selected_tools / 'bin').is_dir() or not selected_libtool.is_file() or
                    not os.access(selected_libtool, os.X_OK) or
                    (selected_tools / 'bin/target-libtool').resolve() != selected_libtool):
                raise SDKError('GNU target tools must belong to the selected OS TOOLDIR')
            host_flags = (native_os_routing.gnu_target_host_flags(selection) +
                          native_os_routing.gettext_target_host_flags(selection))
            for flag in host_flags:
                name, value = flag.split('=', 1)
                if ((name == 'EMBER_HOST_GNU_TRIPLE' and re.fullmatch(r'[A-Za-z0-9_.+-]+', value) is None) or
                        (name != 'EMBER_HOST_GNU_TRIPLE' and not _safe_gnu_path(value))):
                    raise SDKError('GNU target tools require safe selected host paths')
            flags += host_flags
            flags += ['GNU_TARGET_LIBTOOL=' + str(target_libtool),
                      'EMBER_TARGET_TOOL_DIR=' + str(target_tools)]
        argv = [native.tool('sh'), script, *(word for flag in flags for word in ('-V', flag)), *arguments]
    else:
        if not arguments or not os.path.isabs(arguments[0]):
            raise SDKError('OS command requires an explicit absolute path')
        paths.regular_file(paths.canonical_existing(arguments[0]))
        argv = ([native.tool('sh')] if shell else []) + arguments
    if capture:
        return process.capture(argv, cwd=cwd, env=env)
    return process.run_logged(argv, log, cwd=cwd, env=env, append=append,
                              empty_snapshot_timeout=120)
