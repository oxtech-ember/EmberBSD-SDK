# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Publish the OS native-tool mapping. Do not qualify tools or start builds."""

import hashlib
import os
import re
import shutil
import tempfile

from .errors import SDKError
from .native_toolset import NativeToolset
from .paths import (canonical_existing, open_regular, path_string,
                    reject_overlap, verify_stable)
from .publication import publish_directory_noreplace

NATIVE_OS_ROUTING_API = 1
_SAFE_PATH = re.compile(r'/[A-Za-z0-9_./+@%=\-]+\Z')
_NAME = re.compile(r'[A-Za-z0-9_+][A-Za-z0-9_+.\-]*\Z')
_BINDINGS = frozenset(('-', 'HOST_AR', 'HOST_CC', 'HOST_CXX', 'HOST_CPP',
                       'HOST_LD', 'HOST_LN', 'HOST_RANLIB', 'HOST_SH', 'HOST_YACC', 'MAKE', 'SED'))


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

    The caller must load the NativeToolset through its accepted evidence gate.
    It must pin the OS source, own the output parent, and hold all source, ABI,
    toolset and output leases until every consumer has drained. The mapping is
    not a provider, an acceptance receipt, or a production execution override.
    """
    if not isinstance(toolset, NativeToolset):
        raise SDKError('OS routing requires an accepted ordinary NativeToolset')
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
    return dict(format='emberbsd-native-os-routing', version=NATIVE_OS_ROUTING_API,
                directory=destination, contract_sha256=contract_sha256,
                native_toolset_sha256=toolset.descriptor_sha256, members=members,
                production_build_admitted=False)
