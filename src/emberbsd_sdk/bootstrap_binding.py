# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Recognize fixed Ports-owned bootstrap driver bindings. Never run a tool."""

import os
import re

from .errors import SDKError
from .inventory import sha256_file
from .native_toolset import _absolute, _bytes, _json, _require, _sha, _source
from .paths import canonical_existing, inside

BOOTSTRAP_BINDING_API = 1
DARWIN_LLVM_MC_SEED = 'ports-gcc16-darwin-llvm-mc-seed-v1'
DARWIN_LLVM_MC_LLD_SEED = 'ports-gcc16-darwin-llvm-mc-seed-v2'
NETBSD_CONFIGURED_BASE_SEED = 'netbsd11-configured-base-as-ld-seed-v1'
_NETBSD_BASE_PROGRAMS = {'as': '/usr/bin/as', 'ld': '/usr/bin/ld'}
_NETBSD_LTO_PLUGIN_RELATIVE = ('libexec/gcc/x86_64-unknown-netbsd11.0/16.2.0/'
                               'liblto_plugin.so')
_NETBSD_LTO_PLUGIN_STALE_RPATH = ('/usr/pkg/gcc12/x86_64--netbsd/lib/.',
                                  '/usr/pkg/gcc12/lib/.')
_NETBSD_TEMPLATE_RELATIVE = 'profiles/development-toolchain/bootstrap/netbsd11-configured-base-seed.specs.in'
_NETBSD_TEMPLATE_SHA256 = 'b7a9fa32774695e3504f6b90800ee0ec80889f1053214fe006f8e7fc5d41faa1'
_NETBSD_INSTANTIATED_SHA256 = _NETBSD_TEMPLATE_SHA256
_TEMPLATE_RELATIVE = 'profiles/development-toolchain/bootstrap/darwin-llvm-mc-seed.specs.in'
_TEMPLATE_SHA256 = 'f6cd27cdc2bf3926de2816e6aaf2dc964e744d7cd42da298f2b073b3fb04d929'
_INSTANTIATED_SHA256 = 'c8e99fe7a77bd22b1e91af6f1bffa3d6a0dd2636c0ae9173e2ece2fdb9ced663'
_LLD_TEMPLATE_RELATIVE = 'profiles/development-toolchain/bootstrap/darwin-llvm-mc-lld-seed.specs.in'
_LLD_TEMPLATE_SHA256 = 'ef055eaac77dcb506165e366a984839820edc900c8e39bff5ce1e797945bd15b'
# Each recorded macOS SDK version has its exact v2 instantiation identity.
# Deployment stays 26.0. An unrecorded SDK version fails closed.
_LLD_INSTANTIATED_SHA256 = {
    '26.5': 'bc8bda2a69069af27154c7da4ba3fa711136558a86a6076f012edf82544c64a2',
    '27.0': '3ca395b659b167215ba5dbf5eb6f0ef75641bf1e791d21e9c47b7b9f33b23786',
}


def _sdk_version(value):
    version = value['sdk_version']
    _require(isinstance(version, str) and version in _LLD_INSTANTIATED_SHA256,
             'unsupported fixed seed SDK version')
    return version


def _sdk_metadata(value, descriptor, *, inspect_inputs):
    version = _sdk_version(value)
    reference = value['sdk_metadata']
    _require(isinstance(reference, dict) and set(reference) == {'path', 'sha256'},
             'fixed bootstrap SDK metadata needs an exact file reference')
    path, sha = _absolute(reference['path']), _sha(reference['sha256'])
    matches = [row for row in descriptor['host_abi']['headers']
               if path == os.path.join(_absolute(row['root']), 'SDKSettings.json')]
    _require(len(matches) == 1, 'fixed bootstrap SDK metadata is outside its declared headers')
    inventory = _json(matches[0]['inventory'])
    row = inventory.get('SDKSettings.json', {})
    _require(row.get('kind') == 'file' and row.get('sha256') == sha,
             'fixed bootstrap SDK metadata differs from its pinned ABI inventory')
    if inspect_inputs:
        metadata = _json(reference)
        _require(metadata.get('Version') == value['sdk_version'] and
                 metadata.get('CanonicalName') == 'macosx' + version and
                 metadata.get('DefaultVariant') == 'macos' and
                 metadata.get('SupportedTargets', {}).get('macosx', {}).get('BuildVersionPlatformID') == '1',
                 'fixed bootstrap requires the pinned macOS SDK metadata')


def _binding(descriptor, *, inspect_inputs):
    value = descriptor.get('driver_binding')
    if value is None:
        return None
    _require(isinstance(value, dict), 'unsupported bootstrap driver binding fields')
    if value.get('kind') == NETBSD_CONFIGURED_BASE_SEED:
        _require(set(value) == {'kind', 'ports_source', 'reported_programs',
                                'configured_route_evidence', 'template_relative',
                                'template', 'instantiated', 'lto_plugin',
                                'lto_plugin_stale_rpath',
                                'lto_plugin_elf_metadata'} and
                 descriptor['host'] == {'os': 'NetBSD', 'cpu': 'amd64'} and
                 descriptor.get('admission') == 'netbsd11-amd64-first-native-v1',
                 'unsupported NetBSD configured-base driver binding')
        _source(value['ports_source'])
        _require(value['ports_source'] == descriptor['sources']['ports'] and
                 value['reported_programs'] == _NETBSD_BASE_PROGRAMS,
                 'NetBSD driver binding must retain the exact configured assembler/linker queries')
        for name in ('template', 'instantiated'):
            ref = value[name]
            _require(isinstance(ref, dict) and set(ref) == {'path', 'sha256'},
                     'NetBSD configured-base binding needs exact specs references')
            _absolute(ref['path'])
            _sha(ref['sha256'])
        root = _absolute(descriptor['bundle_root'])
        specs = _absolute(value['instantiated']['path'])
        _require(value['template_relative'] == _NETBSD_TEMPLATE_RELATIVE and
                 value['template']['sha256'] == _NETBSD_TEMPLATE_SHA256 and
                 value['instantiated']['sha256'] == _NETBSD_INSTANTIATED_SHA256 and
                 os.path.commonpath((root, specs)) == root and specs != root,
                 'NetBSD specs must match the fixed Ports-owned %D template inside the seed')
        evidence = value['configured_route_evidence']
        if inspect_inputs:
            try:
                route_text = _bytes(evidence).decode('utf-8', 'strict')
            except UnicodeDecodeError as exc:
                raise SDKError('NetBSD configured-driver evidence is not UTF-8') from exc
            configure = next((line for line in route_text.splitlines()
                              if line.startswith('Configured with: ')), '')
            template = _bytes(value['template'])
            instantiated = _bytes(value['instantiated'])
            _require('--with-as=/usr/bin/as' in configure and
                     '--with-ld=/usr/bin/ld' in configure and
                     '/usr/bin/as ' in route_text and 'collect2 ' in route_text and
                     b'*link_libgcc:\n%D\n' == template == instantiated and
                     '-static-libstdc++' in route_text and '-static-libgcc' in route_text and
                     ('-specs=' + specs) in route_text and '-rpath ' not in route_text,
                     'NetBSD compiler evidence does not show the pinned isolated runtime route')
            _require(inside(canonical_existing(specs), root),
                     'NetBSD configured specs escape the seed bundle')
        else:
            _require(isinstance(evidence, dict) and set(evidence) == {'path', 'sha256'},
                     'NetBSD configured-driver evidence needs an exact reference')
            _absolute(evidence['path'])
            _sha(evidence['sha256'])
        plugin = value['lto_plugin']
        _require(isinstance(plugin, dict) and set(plugin) == {'path', 'sha256'},
                 'NetBSD driver binding needs the exact retained LTO plugin')
        plugin_path, plugin_sha = _absolute(plugin['path']), _sha(plugin['sha256'])
        prefix = _absolute(descriptor['driver_prefix'])
        _require(plugin_path == os.path.join(prefix, _NETBSD_LTO_PLUGIN_RELATIVE) and
                 inside(plugin_path, root) and
                 value['lto_plugin_stale_rpath'] == list(_NETBSD_LTO_PLUGIN_STALE_RPATH),
                 'NetBSD LTO plugin path or dormant historical RPATH differs from the fixed seed')
        history = descriptor.get('history', {})
        gcc_history = next((row for name, row in history.items()
                            if descriptor.get('providers', {}).get(name, {}).get('origin') ==
                            'retained-local-netbsd-build'), None)
        mappings = gcc_history.get('shared_object_mappings', []) if isinstance(gcc_history, dict) else []
        _require(any(isinstance(row, dict) and row.get('role') == 'lto_plugin' and
                     row.get('legacy_relative') == _NETBSD_LTO_PLUGIN_RELATIVE and
                     row.get('selected_path') == plugin_path and row.get('sha256') == plugin_sha
                     for row in mappings),
                 'NetBSD LTO plugin is not bound to its retained prefix inventory')
        if inspect_inputs:
            _require(sha256_file(plugin_path) == plugin_sha and
                     all(not os.path.lexists(path) for path in _NETBSD_LTO_PLUGIN_STALE_RPATH),
                     'NetBSD LTO plugin bytes or absent historical RPATH directories changed')
            try:
                elf_text = _bytes(value['lto_plugin_elf_metadata']).decode('utf-8', 'strict')
            except UnicodeDecodeError as exc:
                raise SDKError('NetBSD LTO plugin ELF metadata is not UTF-8') from exc
            needed = re.findall(r'\(NEEDED\)\s+Shared library: \[([^]]+)\]', elf_text)
            rpaths = re.findall(r'\(RPATH\)\s+Library rpath: \[([^]]+)\]', elf_text)
            _require(needed == ['libc.so.12'] and
                     rpaths == [':'.join(_NETBSD_LTO_PLUGIN_STALE_RPATH)],
                     'NetBSD LTO plugin ELF metadata differs from its pinned initial runtime inputs')
            try:
                route_text = _bytes(value['configured_route_evidence']).decode('utf-8', 'strict')
            except UnicodeDecodeError as exc:
                raise SDKError('NetBSD configured-driver evidence is not UTF-8') from exc
            plugin_arguments = re.findall(r'(?:^|\s)-plugin\s+(\S+)', route_text, re.MULTILINE)
            _require(plugin_arguments and
                     all(os.path.normpath(argument) == plugin_path and
                         canonical_existing(argument) == plugin_path
                         for argument in plugin_arguments),
                     'NetBSD read-only route does not select the retained LTO plugin')
        tools = descriptor.get('tools', {})
        providers = descriptor.get('providers', {})
        histories = descriptor.get('history', {})
        for role, path in _NETBSD_BASE_PROGRAMS.items():
            record = tools.get(role)
            _require(isinstance(record, dict) and record.get('path') == path,
                     f'NetBSD configured {role} must select its exact base executable')
            provider = record.get('provider')
            _require(provider in providers and providers[provider].get('origin') ==
                     'retained-netbsd-base-tools',
                     f'NetBSD configured {role} requires retained base-tool history')
            history = histories.get(provider, {})
            member = next((row for row in history.get('tool_members', [])
                           if row.get('role') == role), None)
            _require(isinstance(member, dict) and member.get('original_path') == path and
                     member.get('selected_path') == path and
                     member.get('sha256') == record.get('sha256'),
                     f'NetBSD configured {role} is not byte-bound to its base history')
        return value
    lld = value.get('kind') == DARWIN_LLVM_MC_LLD_SEED
    fields = {'kind', 'ports_source', 'template_relative', 'template', 'instantiated',
              'deployment', 'reported_programs'} | ({'sdk_version', 'sdk_metadata'} if lld else set())
    _require(set(value) == fields, 'unsupported bootstrap driver binding fields')
    _require(value['kind'] in (DARWIN_LLVM_MC_SEED, DARWIN_LLVM_MC_LLD_SEED) and
             descriptor['host']['os'] == 'Darwin' and
             descriptor['host']['cpu'] in ('arm64', 'aarch64'),
             'unsupported bootstrap driver binding or native host')
    _source(value['ports_source'])
    _require(value['ports_source'] == descriptor['sources']['ports'],
             'bootstrap binding uses another Ports source')
    for name in ('template', 'instantiated'):
        ref = value[name]
        _require(isinstance(ref, dict) and set(ref) == {'path', 'sha256'},
                 'fixed bootstrap binding requires exact file references')
        _absolute(ref['path'])
        _sha(ref['sha256'])
    if lld:
        _sdk_version(value)
    _require(value['template_relative'] == (_LLD_TEMPLATE_RELATIVE if lld else _TEMPLATE_RELATIVE) and
             value['template']['sha256'] == (_LLD_TEMPLATE_SHA256 if lld else _TEMPLATE_SHA256) and
             value['instantiated']['sha256'] == (_LLD_INSTANTIATED_SHA256[value['sdk_version']]
                                                 if lld else _INSTANTIATED_SHA256) and
             value['deployment'] == '26.0', 'unsupported Ports seed template or deployment')
    if lld:
        _sdk_metadata(value, descriptor, inspect_inputs=inspect_inputs)
    prefix = _absolute(descriptor['driver_prefix'])
    root = _absolute(descriptor['bundle_root'])
    _require(value['reported_programs'] == {'ld': os.path.join(prefix, 'ld')} and
             descriptor['tools']['ld']['path'] == os.path.join(prefix, 'ld64.lld'),
             'bootstrap binding must separate the nominal ld query and Mach-O linker')
    specs = _absolute(value['instantiated']['path'])
    _require(os.path.commonpath((root, specs)) == root and specs != root,
             'bootstrap specs must be an in-bundle input')
    if inspect_inputs:
        template = _bytes(value['template'])
        instantiated = _bytes(value['instantiated'])
        expected = template.replace(b'@DEPLOYMENT@', b'26.0')
        if lld:
            expected = expected.replace(b'@SDK_VERSION@', value['sdk_version'].encode())
        _require(instantiated == expected,
                 'bootstrap specs differ from the fixed Ports instantiation')
        _require(inside(canonical_existing(specs), root) and
                 inside(canonical_existing(value['reported_programs']['ld']), root),
                 'bootstrap specs or nominal driver escapes the seed')
    # Graduation checks identities without retaining the historical seed files.
    return value


def seed_commands(descriptor, *, inspect_inputs=True):
    """Return only default -B or recognized fixed source-owned tuples."""
    value = _binding(descriptor, inspect_inputs=inspect_inputs)
    prefix = _absolute(descriptor['driver_prefix'])
    commands = {}
    for role, row in descriptor['tools'].items():
        command = (_absolute(row['path']),)
        if role in ('cc', 'cxx', 'cpp'):
            command += ('-B' + prefix + os.sep,)
            if value is not None and value['kind'] == NETBSD_CONFIGURED_BASE_SEED:
                command += ('-specs=' + value['instantiated']['path'],)
                if role in ('cc', 'cxx'):
                    command += ('-static-libstdc++', '-static-libgcc')
            elif value is not None:
                command += ('-specs=' + value['instantiated']['path'],
                            '-mmacosx-version-min=' + value['deployment'])
        commands[role] = command
    from .bootstrap_make import bind_make_commands
    return bind_make_commands(descriptor, commands, inspect_inputs=inspect_inputs)


def seed_external_tools(descriptor, *, inspect_inputs=True):
    """Return only external tools from a recognized bootstrap driver binding."""
    value = _binding(descriptor, inspect_inputs=inspect_inputs)
    if value is not None and value['kind'] == NETBSD_CONFIGURED_BASE_SEED:
        return dict(_NETBSD_BASE_PROGRAMS)
    return {}


def seed_reported_tools(descriptor, tools):
    """Adapt query expectations only. Never change the selected execution roles."""
    value = _binding(descriptor, inspect_inputs=False)
    result = dict(tools)
    if value is not None:
        result['ld'] = value['reported_programs']['ld']
    return result
