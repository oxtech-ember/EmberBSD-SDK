# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Fixed bound-make commands for the complete first-native GCC16 recipe plan."""

import os
from pathlib import Path
import re
import shutil
import shlex
import sys

from .bootstrap_binding import DARWIN_LLVM_MC_LLD_SEED
from .bootstrap_make import _safe_path
from .bootstrap_producer import (metadata_environment, metadata_paths,
                                 validate_metadata_inputs)
from .inventory import sha256_file
from .native_toolset import _file, _require, _sha
from .paths import canonical_new, reject_overlap

BOOTSTRAP_FORMAT = 'emberbsd-native-gcc16-bootstrap-plan'


def recipe_linker_launcher(plan):
    """Construct only the pinned SDK control-plane resource service prefix."""
    value, resources = plan['link_control'], plan['resources']
    return shlex.join([_safe_path(value['python']['path']), _safe_path(value['launcher']['path']),
                       '--lock-root', _safe_path(resources['lock_root']),
                       '--context', _safe_path(resources['heavy_link_context']), '--'])


def validate_link_control(value, *, inspect_inputs):
    _require(isinstance(value, dict) and set(value) == {'python', 'launcher'},
             'link control needs only its exact Python and SDK launcher references')
    for reference in value.values():
        _require(isinstance(reference, dict) and set(reference) == {'path', 'sha256'},
                 'link control requires exact file references')
        _safe_path(reference['path'])
        _sha(reference['sha256'])
    _require(value['launcher']['path'].endswith('/scripts/link-launcher.py'),
             'link control must use the SDK resource launcher')
    if inspect_inputs:
        expected = str(Path(__file__).resolve().parents[2] / 'scripts/link-launcher.py')
        _require(value['python']['path'] == sys.executable and value['launcher']['path'] == expected,
                 'link control must use this same SDK control-plane Python and source')
        for reference in value.values():
            _file(reference)


def recipe_order(plan):
    """Return a deterministic dependency-first order. Do not resolve recipes."""
    recipes = plan['recipes']
    _require(isinstance(recipes, dict) and 1 <= len(recipes) <= 256 and
             'lang/gcc16' in recipes, 'full bootstrap needs a finite GCC16 closure')
    result, visited = [], set()

    def visit(name, active):
        _require(name in recipes and name not in active and
                 isinstance(name, str) and re.fullmatch('[a-z0-9_+-]+/[a-z0-9_+.-]+', name),
                 'invalid bound recipe dependency graph')
        if name in visited:
            return
        dependencies = recipes[name]['dependencies']
        _require(isinstance(dependencies, list) and all(isinstance(item, str) for item in dependencies) and
                 len(dependencies) == len(set(dependencies)), 'invalid bound recipe dependencies')
        for dependency in sorted(dependencies):
            visit(dependency, active | {name})
        visited.add(name)
        result.append(name)

    visit('lang/gcc16', set())
    _require(visited == set(recipes), 'bound bootstrap contains unrelated recipes')
    return result


def recipe_steps(plan, descriptor, commands, flags):
    """Construct complete selected tuples and fixed targets. No caller tail."""
    work = _safe_path(plan['work_root'])
    export = _safe_path(plan['ports_export']['root'])
    env = metadata_environment(plan, commands)
    jobs = '-j' + str(plan['resources']['cpu_jobs'])
    result = []
    for number, recipe in enumerate(recipe_order(plan)):
        for phase in ('build', 'install'):
            if recipe == 'lang/gcc16' and phase == 'install':
                result.append(dict(id='gcc16-compare', recipe=recipe, phase='compare', role='gmake',
                    cwd=work, env=dict(env), argv=[*commands['gmake'], '--no-print-directory', jobs,
                        '-C', _safe_path(plan['recipes'][recipe]['object_directory']), 'compare']))
            result.append(dict(id=f'r{number:03d}-' + phase, recipe=recipe, phase=phase, role='make',
                cwd=work, env=dict(env), argv=[*commands['make'], 'EMBERBSD_DARWIN_BOOTSTRAP=yes',
                    jobs, '-C', os.path.join(export, recipe), phase]))
    return result


def recipe_distfiles(plan):
    """Map explicit DIST_SUBDIR-relative destinations to pinned original files."""
    files = {}
    for recipe in recipe_order(plan):
        rows = plan['recipes'][recipe]['distfiles']
        _require(isinstance(rows, list) and 1 <= len(rows) <= 128, 'invalid bound recipe distfiles')
        for row in rows:
            _require(isinstance(row, dict) and set(row) == {'url', 'file', 'relative'},
                     'bound distfiles need explicit export-relative destinations')
            relative = row['relative']
            _require(isinstance(relative, str) and len(relative) <= 4096 and
                     re.fullmatch(r'[A-Za-z0-9_+./@%=-]+', relative) and
                     not os.path.isabs(relative) and
                     all(part not in ('', '.', '..') for part in relative.split('/')),
                     'invalid bound distfile destination')
            previous = files.get(relative)
            _require(previous is None or previous['sha256'] == row['file']['sha256'],
                     'bound distfile destination has conflicting content')
            files[relative] = row['file']
    _require(not any('/'.join(name.split('/')[:length]) in files
                     for name in files for length in range(1, len(name.split('/')))),
             'bound distfile destinations overlap')
    return files


def validate_bound_recipe_plan(plan, descriptor, commands, flags, *, inspect_inputs=True):
    """Extend the existing full plan. This function issues no qualification."""
    _require(set(plan) == {'format', 'version', 'scope', 'purpose', 'seed_descriptor', 'host', 'sources',
        'output_compiler', 'ports_export', 'recipes', 'work_root', 'output_bundle_root', 'required_roles',
        'commands', 'comparison_command', 'resources', 'metadata'} |
        ({'link_control'} if 'link_control' in plan else set()), 'unsupported bound full-plan fields')
    _require(descriptor['host']['os'] == 'Darwin' and descriptor['host']['cpu'] in ('arm64', 'aarch64') and
             descriptor['make_binding']['kind'] == 'ports-bootstrap-make-v1' and
             descriptor.get('driver_binding', {}).get('kind') == DARWIN_LLVM_MC_LLD_SEED,
             'bound full plans need the recognized Darwin make and v2 driver bindings')
    from .bootstrap_pilot import _references, _validate_resources
    parallel = 'link_control' in plan
    if parallel:
        validate_link_control(plan['link_control'], inspect_inputs=inspect_inputs)
    _validate_resources(plan['resources'], recipe=True, parallel=parallel, inspect_inputs=inspect_inputs)
    validate_metadata_inputs(plan, descriptor, commands, flags,
                             inspect_inputs=inspect_inputs, recipe_limit=256)
    _require(plan['metadata']['recipes'] == sorted(plan['recipes']) and
             plan['ports_export'] == dict(source=descriptor['sources']['ports'], **plan['metadata']['export']),
             'bound recipe metadata and export must describe the complete same closure')
    work = _safe_path(plan['work_root'])
    paths = metadata_paths(work)
    _require(plan['output_bundle_root'] == paths['LOCALBASE'],
             'bound output bundle must be the owned work prefix')
    obj = _safe_path(plan['recipes']['lang/gcc16']['object_directory'])
    _require(obj != paths['WRKOBJDIR'] and os.path.commonpath((obj, paths['WRKOBJDIR'])) == paths['WRKOBJDIR'],
             'GCC comparison must use an explicit metadata-derived object directory')
    roots = [descriptor['bundle_root'], plan['ports_export']['root']]
    roots += [row['root'] for kind in ('headers', 'libraries') for row in descriptor['host_abi'][kind]]
    if inspect_inputs:
        _require(canonical_new(work) == work and canonical_new(obj) == obj,
                 'bound recipe work or object directory has aliased ancestors')
    for path in [*roots, *list(_references(plan)), *list(_references(descriptor))]:
        if inspect_inputs:
            reject_overlap(work, path)
        else:
            _require(os.path.commonpath((work, path)) not in (work, path),
                     'historical bound work overlaps a recorded input')
    recipe_distfiles(plan)
    _require(plan['comparison_command'] == 'gcc16-compare' and
             plan['commands'] == recipe_steps(plan, descriptor, commands, flags),
             'bound recipe command differs from its complete source-owned route')
    return plan


def prepare_recipe_work(plan):
    """Stage only pinned distfiles in fresh, exclusively leased work."""
    paths = metadata_paths(plan['work_root'])
    for path in paths.values():
        os.mkdir(path)
    for relative, reference in recipe_distfiles(plan).items():
        destination = Path(paths['DISTDIR'], relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with open(reference['path'], 'rb') as source, destination.open('xb') as output:
            shutil.copyfileobj(source, output, length=1024 * 1024)
        _require(sha256_file(str(destination)) == reference['sha256'],
                 'staged bound distfile differs from its accepted input')
