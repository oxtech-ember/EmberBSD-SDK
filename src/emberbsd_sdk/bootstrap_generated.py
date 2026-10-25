# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Bounded generated-source evidence for the fixed program runner. No executor."""

import os
from pathlib import Path
import stat

from .inventory import sha256_file
from .native_toolset import _absolute, _bytes, _require
from .paths import entry, regular_file

MAX_GENERATED_BYTES = 2 * 1024 * 1024


def selected_yacc(descriptor, commands):
    """Require one exact selected native generator, without ambient discovery."""
    selected = descriptor['tools'].get('yacc', {})
    _require(selected.get('kind') == 'native' and
             tuple(commands.get('yacc', ())) == (selected.get('path'),) and
             os.path.isabs(selected.get('path', '')) and
             selected['path'] != descriptor['bundle_root'] and
             os.path.commonpath((_absolute(selected['path']), _absolute(descriptor['bundle_root']))) ==
             descriptor['bundle_root'], 'program needs one explicitly selected native yacc invocation')


def contract(plan):
    from .bootstrap_program import program_manifest
    stage = plan.get('stage')
    if stage == 'byacc-bootstrap':
        return program_manifest(plan)['generated'], ('generate', 'regenerate')
    if stage == 'expr-bootstrap':
        return [program_manifest(plan)['generation']['output']], ('generate-expr',)
    if stage == 'compat-parsedate-bootstrap':
        return [program_manifest(plan)['generator']['output']], ('generate-parsedate',)
    return [], ()


def capture(work, names):
    result = {}
    for name in names:
        _require(isinstance(name, str) and Path(name).name == name and name not in ('.', '..'),
                 'generated output must be one fixed work member')
        path = os.path.join(work, name)
        regular_file(path)
        _require(0 < Path(path).stat().st_size <= MAX_GENERATED_BYTES,
                 'generated source has invalid size')
        result[name] = dict(path=path, sha256=sha256_file(path))
    return result


def revalidate(work, names, references):
    _require(isinstance(references, dict) and set(references) == set(names),
             'generated source evidence has an incomplete member roster')
    for name in names:
        reference = references[name]
        _require(reference['path'] == os.path.join(work, name),
                 'generated source evidence escapes its fixed work member')
        regular_file(reference['path'])
        _require(0 < len(_bytes(reference, limit=MAX_GENERATED_BYTES)),
                 'generated source evidence is empty')


def _modes(work, names):
    result = {}
    for name in names:
        info = entry(os.path.join(work, name))
        _require(info is not None and stat.S_ISREG(info.st_mode),
                 'generated mode evidence needs a regular file')
        result[name] = stat.S_IMODE(info.st_mode)
    return result


def _revalidate_modes(work, names, expected):
    _require(isinstance(expected, dict) and set(expected) == set(names) and
             all(type(mode) is int and 0 <= mode <= 0o7777 for mode in expected.values()) and
             expected == _modes(work, names), 'generated source mode changed or is missing')


def before_step(step, plan, completed, *, track_modes=False):
    names, generators = contract(plan)
    if not generators:
        _require(step['phase'] != 'generator', 'unsupported source generator step')
        return
    _require(step in plan['commands'], 'unknown generated-source consumer step')
    _require(len(completed) < len(plan['commands']) and
             [row['id'] for row in completed] == [row['id'] for row in plan['commands'][:len(completed)]] and
             step == plan['commands'][len(completed)], 'generated-source execution order differs')
    previous = [row for row in completed if row['id'] in generators]
    _require([row['id'] for row in previous] == list(generators[:len(previous)]),
             'generated-source step order differs')
    if step['phase'] == 'generator':
        _require(len(previous) < len(generators) and step['id'] == generators[len(previous)],
                 'unknown or repeated source generator step')
        if not previous:
            _require(all(entry(os.path.join(plan['work_root'], name)) is None for name in names),
                     'generated output exists before its first generator')
    elif step['id'] in generators:
        _require(False, 'source generator phase differs')
    if previous:
        revalidate(plan['work_root'], names, previous[-1]['generated'])
        if track_modes:
            _revalidate_modes(plan['work_root'], names, previous[-1].get('generated_modes'))


def after_step(step, plan, actual_exit, completed, *, track_modes=False):
    names, generators = contract(plan)
    if not generators:
        _require(step['phase'] != 'generator', 'unsupported source generator step')
        return {}
    _require(step in plan['commands'], 'unknown generated-source consumer step')
    if step['phase'] == 'generator':
        previous = [row for row in completed if row['id'] in generators]
        _require(type(actual_exit) is int and actual_exit == 0 and
                 [row['id'] for row in previous] == list(generators[:len(previous)]) and
                 len(previous) < len(generators) and step['id'] == generators[len(previous)],
                 'source generator has no successful ordered observation')
        generated = capture(plan['work_root'], names)
        if previous:
            _require(generated == previous[-1]['generated'], 'generation is not byte reproducible')
        result = dict(generated=generated)
        if track_modes:
            result['generated_modes'] = _modes(plan['work_root'], names)
            if previous:
                _require(result['generated_modes'] == previous[-1].get('generated_modes'),
                         'generation has different file modes')
        return result
    previous = [row for row in completed if row['id'] in generators]
    if previous:
        revalidate(plan['work_root'], names, previous[-1]['generated'])
        if track_modes:
            _revalidate_modes(plan['work_root'], names, previous[-1].get('generated_modes'))
    return {}


def verify_history(plan, result, *, track_modes=False):
    names, generators = contract(plan)
    if not generators:
        return
    rows = [row for row in result['commands'] if row['id'] in generators]
    _require([row['id'] for row in rows] == list(generators),
             'historical generated-source observations are incomplete')
    expected = {name: dict(path=os.path.join(plan['work_root'], name),
                           sha256=result['outputs'][name]) for name in names}
    for row in rows:
        _require(row.get('generated') == expected, 'historical generated source differs')
        if track_modes:
            _revalidate_modes(plan['work_root'], names, row.get('generated_modes'))
    revalidate(plan['work_root'], names, expected)
