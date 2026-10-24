# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Recognize one Ports-owned bootstrap driver binding. Never run a tool."""

import os

from .native_toolset import _absolute, _bytes, _require, _sha, _source
from .paths import canonical_existing, inside

BOOTSTRAP_BINDING_API = 1
DARWIN_LLVM_MC_SEED = 'ports-gcc16-darwin-llvm-mc-seed-v1'
_TEMPLATE_RELATIVE = 'profiles/development-toolchain/bootstrap/darwin-llvm-mc-seed.specs.in'
_TEMPLATE_SHA256 = 'f6cd27cdc2bf3926de2816e6aaf2dc964e744d7cd42da298f2b073b3fb04d929'
_INSTANTIATED_SHA256 = 'c8e99fe7a77bd22b1e91af6f1bffa3d6a0dd2636c0ae9173e2ece2fdb9ced663'


def _binding(descriptor, *, inspect_inputs):
    value = descriptor.get('driver_binding')
    if value is None:
        return None
    _require(isinstance(value, dict) and set(value) ==
             {'kind', 'ports_source', 'template_relative', 'template', 'instantiated',
              'deployment', 'reported_programs'}, 'unsupported bootstrap driver binding fields')
    _require(value['kind'] == DARWIN_LLVM_MC_SEED and
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
    _require(value['template_relative'] == _TEMPLATE_RELATIVE and
             value['template'].get('sha256') == _TEMPLATE_SHA256 and
             value['instantiated']['sha256'] == _INSTANTIATED_SHA256 and
             value['deployment'] == '26.0', 'unsupported Ports seed template or deployment')
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
        _require(instantiated == template.replace(b'@DEPLOYMENT@', b'26.0'),
                 'bootstrap specs differ from the fixed Ports instantiation')
        _require(inside(canonical_existing(specs), root) and
                 inside(canonical_existing(value['reported_programs']['ld']), root),
                 'bootstrap specs or nominal driver escapes the seed')
    # Graduation checks identities without retaining the historical seed files.
    return value


def seed_commands(descriptor, *, inspect_inputs=True):
    """Return only the default -B tuple or the recognized fixed-source tuple."""
    value = _binding(descriptor, inspect_inputs=inspect_inputs)
    prefix = _absolute(descriptor['driver_prefix'])
    extra = () if value is None else ('-specs=' + value['instantiated']['path'],
                                      '-mmacosx-version-min=' + value['deployment'])
    return {role: (_absolute(row['path']), '-B' + prefix + os.sep, *extra)
            if role in ('cc', 'cxx', 'cpp') else (_absolute(row['path']),)
            for role, row in descriptor['tools'].items()}


def seed_reported_tools(descriptor, tools):
    """Adapt query expectations only. Never change the selected execution roles."""
    value = _binding(descriptor, inspect_inputs=False)
    result = dict(tools)
    if value is not None:
        result['ld'] = value['reported_programs']['ld']
    return result
