# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Explicit OS component dependencies for fixed native program consumers."""

from pathlib import Path
from . import bootstrap_compat_component as component
from .native_toolset import _json, _require

CONSUMERS = {'touch-bootstrap': ('filetime', 'parsedate'), 'ls-bootstrap': ('listing',), 'cp-bootstrap': ('filetime', 'filemeta')}


def selected(plan, descriptor):
    names = CONSUMERS.get(plan.get('stage'), ())
    value = plan['program'].get('components')
    _require(bool(names) and isinstance(value, dict) and set(value) == set(names),
             'program needs its exact named OS component inputs')
    exports = {name: component.validate_input(value[name], descriptor, component=name) for name in names}
    # The two configurations and archives are deliberately distinct. Common
    # source declaration helpers must still have identical accepted bytes.
    common = [row['bootstrap_cdefs.h']['sha256'] for row in exports.values()]
    _require(len(set(common)) == 1, 'OS component common declaration headers differ')
    roots = [str(Path(row['libnbcompat.a']['path']).parent) for row in exports.values()]
    _require(len(set(roots)) == len(roots), 'OS components cannot share an ambiguous export root')
    return exports


def flags(plan, descriptor):
    exports = selected(plan, descriptor)
    cppflags = []
    # Feature requests must precede every native header in either configuration.
    for name in CONSUMERS[plan['stage']]:
        feature = exports[name].get('compat_' + name + '_features.h')
        if feature is not None:
            cppflags += ['-include', feature['path']]
    for name in CONSUMERS[plan['stage']]:
        row = exports[name]
        cppflags += ['-include', row['nbtool_config.h']['path'], '-I',
                     str(Path(row['nbtool_config.h']['path']).parent)]
    libraries = [exports[name]['libnbcompat.a']['path'] for name in CONSUMERS[plan['stage']]]
    return cppflags, libraries


def revalidate(plan):
    if plan.get('stage') in CONSUMERS:
        selected(plan, _json(plan['seed_descriptor']))


def read_inputs(plan):
    """Keep retained component evidence under the existing pilot's read leases."""
    names = CONSUMERS.get(plan.get('stage'), ())
    if not names:
        return [], []
    _require(isinstance(plan['program'].get('components'), dict) and
             set(plan['program']['components']) == set(names), 'program component lease roster differs')
    roots, documents = [], []
    for name in names:
        receipt = _json(plan['program']['components'][name])
        saved = _json(receipt['candidate'])
        original = _json(saved['bootstrap_plan'])
        source = saved['source']
        proof = _json(source['source_proof'])
        roots += [original['work_root'], source['root']]
        documents += [receipt, saved, original, _json(saved['producer_result']),
                      _json(saved['seed_descriptor']), _json(receipt['independent_review']), proof]
    return roots, documents
