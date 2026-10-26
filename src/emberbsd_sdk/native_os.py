# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Run OS producers with their declared native roles and separate target argv."""

import os

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


def select_native_os_tools(host_prefix, source, *, compiler_family='clang'):
    """Load all roles in the selected OS contract, plus the shared interpreter."""
    _, _, _, roles = _contract(source)
    return load_host_prefix(host_prefix, compiler_family=compiler_family,
                            required_roles=roles | {'python'})


def native_os_environment(native, routing, *, environment=None):
    """Expose HOST_* only; target compiler variables belong to the OS caller."""
    result = process.sanitized_environment(environment)
    result.update(PATH=os.fspath(routing) + os.pathsep + native.path,
                  EMBER_NATIVE_TOOLS=os.fspath(routing), HOST_SH=native.tool('sh'),
                  PYTHONDONTWRITEBYTECODE='1')
    for flag in native_os_routing.native_host_flags(native.as_dict()):
        name, value = flag.split('=', 1)
        result[name] = value
    return result


def _check_build_arguments(arguments):
    for index, word in enumerate(arguments):
        value = (arguments[index+1] if index+1 < len(arguments) else '') if word == '-V' else word[2:] if word.startswith('-V') else ''
        name = value.split('=', 1)[0]
        if name.startswith('HOST_') or name in ('EMBER_NATIVE_TOOLS', 'EMBER_HOST_COMPILER_FAMILY', 'PATH'):
            raise SDKError('build.sh arguments cannot override the selected native tools or ABI')


def run_native_os(native, *, source, routing, cwd, arguments, log=None,
                  capture=False, append=False, build_sh=False, shell=False,
                  environment=None):
    """Run one producer or metadata query using existing process backends.

    build_sh receives only build.sh arguments and records native ABI values in
    its generated make wrapper. Other commands must use an explicit path;
    shell selects the native shell for OS scripts and generated make wrappers.
    """
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
        _check_build_arguments(arguments)
        script = os.path.join(source, 'build.sh')
        paths.regular_file(script)
        flags = native_os_routing.native_host_flags(native.as_dict())
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
