# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Recognize fixed Ports-owned bootstrap driver bindings. Never run a tool."""

import os

from .native_toolset import _absolute, _bytes, _json, _require, _sha, _source
from .paths import canonical_existing, inside

BOOTSTRAP_BINDING_API = 1
DARWIN_LLVM_MC_SEED = 'ports-gcc16-darwin-llvm-mc-seed-v1'
DARWIN_LLVM_MC_LLD_SEED = 'ports-gcc16-darwin-llvm-mc-seed-v2'
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
    extra = () if value is None else ('-specs=' + value['instantiated']['path'],
                                      '-mmacosx-version-min=' + value['deployment'])
    commands = {role: (_absolute(row['path']), '-B' + prefix + os.sep, *extra)
            if role in ('cc', 'cxx', 'cpp') else (_absolute(row['path']),)
            for role, row in descriptor['tools'].items()}
    from .bootstrap_make import bind_make_commands
    return bind_make_commands(descriptor, commands, inspect_inputs=inspect_inputs)


def seed_reported_tools(descriptor, tools):
    """Adapt query expectations only. Never change the selected execution roles."""
    value = _binding(descriptor, inspect_inputs=False)
    result = dict(tools)
    if value is not None:
        result['ld'] = value['reported_programs']['ld']
    return result
