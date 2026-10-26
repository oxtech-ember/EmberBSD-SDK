# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Publish the OS native-tool mapping. Do not qualify tools or start builds."""

import hashlib
import os
import re
import shlex
import shutil
import tempfile

from .errors import SDKError
from .native_toolset import NativeToolset
from .host_prefix import HostPrefix
from .paths import (canonical_existing, entry, open_regular, path_string,
                    real_directory, reject_overlap, verify_stable)
from .publication import publish_directory_noreplace

NATIVE_OS_ROUTING_API = 1
_SAFE_PATH = re.compile(r'/[A-Za-z0-9_./+@%=\-]+\Z')
_NAME = re.compile(r'[A-Za-z0-9_+][A-Za-z0-9_+.\-]*\Z')
_BINDINGS = frozenset(('-', 'HOST_AR', 'HOST_CC', 'HOST_CXX', 'HOST_CPP',
                       'HOST_LD', 'HOST_LN', 'HOST_RANLIB', 'HOST_SH', 'HOST_YACC', 'HOST_MV_CP', 'HOST_MV_RM', 'MAKE', 'SED'))


def native_host_flags(selection):
    """Map the selected host ABI arguments only to OS native variables."""
    flags = selection['abi_flags']
    result = ['HOST_' + name.upper() + '=' + shlex.join(flags[name])
              for name in ('cppflags', 'cflags', 'cxxflags', 'ldflags')]
    if selection['format'] != 'emberbsd-host-prefix':
        result.append('HOST_SH=' + selection['tools']['sh'])
    elif selection['version'] >= 2:
        result.append('EMBER_HOST_COMPILER_FAMILY=' + selection['compiler']['family'])
    return result


def native_path(selection):
    directories = [os.path.dirname(path) for _, path in sorted(selection['tools'].items())]
    if selection['format'] == 'emberbsd-host-prefix':
        directories = [os.path.dirname(selection['tools']['cc'])] + [
            os.path.join(selection['bundle_root'], name) for name in ('bin', 'gnu/bin')] + directories
    directories = tuple(dict.fromkeys(directories))
    if any(os.pathsep in directory for directory in directories):
        raise SDKError('native tool directory contains the PATH separator')
    return os.pathsep.join(directories)


def ensure_native_os_routing(toolset, *, contract_path, contract_sha256, destination):
    """Reuse exactly the existing mapping, or publish it without replacement."""
    destination = path_string(destination)
    if entry(destination) is None:
        return publish_native_os_routing(toolset, contract_path=contract_path,
            contract_sha256=contract_sha256, destination=destination)
    real_directory(destination)
    _, rows, _ = read_native_os_contract(contract_path, sha256=contract_sha256)
    read_native_os_contract(os.path.join(destination, 'contract'), sha256=contract_sha256)
    if set(os.listdir(destination)) != {'contract', *(command for command, _, _ in rows)}:
        raise SDKError('saved native routing contains unexpected members')
    for command, role, _ in rows:
        selected = os.path.join(destination, command)
        if not os.path.islink(selected) or os.readlink(selected) != toolset.tool(role):
            raise SDKError('saved native routing differs from host prefix')
    return dict(directory=destination, contract_sha256=contract_sha256,
                members={command:toolset.tool(role) for command,role,_ in rows})


def read_native_os_contract(path, *, sha256):
    """Read pinned OS data without executing tools or emitted shell code."""
    if not isinstance(sha256, str) or re.fullmatch(r'[0-9a-f]{64}', sha256) is None:
        raise SDKError('expected the explicit OS contract SHA256')
    path = path_string(path)
    stream, before = open_regular(path)
    with stream:
        data = stream.read(65537)
    verify_stable(path, before)
    if len(data) > 65536 or hashlib.sha256(data).hexdigest() != sha256:
        raise SDKError('OS native-tool contract changed or is too large')
    try:
        lines = data.decode('ascii').splitlines()
    except UnicodeError as exc:
        raise SDKError('OS native-tool contract must be ASCII') from exc
    if not data.endswith(b'\n') or not lines or lines[0] != 'EmberBSD-native-tools 1':
        raise SDKError('unsupported OS native-tool contract')
    commands, roles, bindings = set(), set(), set()
    rows = []
    for line in lines[1:]:
        fields = line.split(' ')
        if len(fields) != 3:
            raise SDKError('invalid OS native-tool contract row')
        command, role, binding = fields
        if (not _NAME.fullmatch(command) or not _NAME.fullmatch(role)
                or command in commands or binding not in _BINDINGS
                or (binding != '-' and binding in bindings)):
            raise SDKError('invalid or duplicate OS native-tool mapping')
        commands.add(command)
        roles.add(role)
        bindings.add(binding)
        rows.append((command, role, binding))
    if not rows or not {'cmp', 'readlink', 'make', 'sh'} <= commands:
        raise SDKError('OS native-tool control members are missing')
    return data, tuple(rows), frozenset(roles)


def publish_native_os_routing(toolset, *, contract_path, contract_sha256,
                              destination):
    """Publish links to selected spellings under caller-owned input leases.

    The caller selects a host prefix, owns the output parent, and keeps the
    selected tools unchanged until every consumer has drained.
    """
    if not isinstance(toolset, (HostPrefix, NativeToolset)):
        raise SDKError('OS routing requires a selected host prefix or historical NativeToolset')
    data, rows, _ = read_native_os_contract(contract_path, sha256=contract_sha256)
    destination = os.fspath(destination)
    if not isinstance(destination, str) or not _SAFE_PATH.fullmatch(destination):
        raise SDKError('unsupported OS native-tool routing path')
    parent = canonical_existing(os.path.dirname(destination))
    if parent != os.path.dirname(destination):
        raise SDKError('OS routing parent must use its canonical spelling')
    reject_overlap(destination, toolset.bundle_root)
    reject_overlap(destination, path_string(contract_path))
    members = {}
    for command, role, _ in rows:
        selected = toolset.tool(role)
        if not _SAFE_PATH.fullmatch(selected):
            raise SDKError('unsupported selected OS native-tool path')
        # Keep the selected spelling. Do not replace an in-bundle alias with
        # its real path: compiler support lookup can depend on argv[0].
        if not os.access(selected, os.X_OK):
            raise SDKError('selected OS native tool is no longer executable')
        members[command] = selected
    stage = tempfile.mkdtemp(prefix='.native-os-tools-', dir=parent)
    try:
        with open(os.path.join(stage, 'contract'), 'xb') as stream:
            stream.write(data)
        for command, selected in members.items():
            os.symlink(selected, os.path.join(stage, command))
        publish_directory_noreplace(stage, destination)
    finally:
        # Publication moves the stage. On failure remove only our exact stage.
        if os.path.isdir(stage):
            shutil.rmtree(stage)
    result = dict(format='emberbsd-native-os-routing', version=NATIVE_OS_ROUTING_API,
                  directory=destination, contract_sha256=contract_sha256, members=members)
    if isinstance(toolset, NativeToolset):
        result.update(native_toolset_sha256=toolset.descriptor_sha256, production_build_admitted=False)
    return result
