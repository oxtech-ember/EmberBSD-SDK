# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Fixed bound-make commands for the complete first-native GCC16 recipe plan."""

import os
from pathlib import Path
import re
import shutil
import shlex
import stat
import sys

from .bootstrap_binding import DARWIN_LLVM_MC_LLD_SEED
from .bootstrap_make import _safe_path
from .bootstrap_producer import (RECIPE_LIMIT, _recipe_name, darwin_tool_phase, metadata_environment,
                                 metadata_paths, metadata_values, validate_metadata_inputs)
from .inventory import sha256_file
from .native_toolset import _file, _json, _native_image, _require, _sha
from .paths import canonical_existing, canonical_new, reject_overlap

BOOTSTRAP_FORMAT = 'emberbsd-native-gcc16-bootstrap-plan'
# The Ports Darwin profile's make and shell handoff edge. See recipe_make().
MAKE_PROVIDER = 'devel/bmake'
INITIAL_DARWIN_PURPOSE = 'initial-darwin-tools'
INITIAL_DARWIN_TARGETS = ('llvm-libtool-darwin', 'llvm-install-name-tool', 'llvm-readtapi', 'llvm-lipo')


def initial_darwin_plan(plan):
    """Bind the initial purpose to its fixed operations, never arbitrary targets."""
    initial = plan['purpose'] == INITIAL_DARWIN_PURPOSE
    phase = darwin_tool_phase(plan)
    _require(not initial or phase == 'initial', 'initial tool purpose requires the initial phase')
    _require(phase != 'initial' or initial, 'initial phase requires the initial tool purpose')
    return initial


def initial_execution_order(plan):
    """Keep the full graph; execute exactly the ordered LLVM dependency closure."""
    _require(initial_darwin_plan(plan), 'initial execution needs its explicit purpose')
    order = recipe_order(plan)
    recipes = plan['recipes']
    _require({'devel/cmake', 'lang/llvm'} <= set(recipes), 'initial tools require CMake and LLVM')
    reached = set()

    def visit(name):
        for child in recipes[name]['dependencies']:
            if child not in reached:
                reached.add(child)
                visit(child)

    visit('lang/llvm')
    _require('devel/cmake' in reached, 'CMake must be an LLVM dependency')
    reached.add('lang/llvm')
    return [name for name in order if name in reached]


def initial_llvm_directory(plan):
    """Validate the metadata-derived LLVM build directory before any child."""
    initial_execution_order(plan)
    row = plan['recipes']['lang/llvm']
    _require(isinstance(row.get('object_directory'), str), 'initial LLVM object directory is required')
    obj = _safe_path(row['object_directory'])
    root = metadata_paths(_safe_path(plan['work_root']))['WRKOBJDIR']
    _require(obj != root and os.path.commonpath((obj, root)) == root,
             'initial LLVM object directory must stay in owned work')
    return obj


def initial_tool_outputs(plan):
    """Bind real initial outputs separately from installed dependency packages."""
    obj = initial_llvm_directory(plan)
    result = {}
    for name in INITIAL_DARWIN_TARGETS:
        path = os.path.join(obj, 'bin', name)
        resolved = canonical_existing(path)
        _require(os.path.commonpath((resolved, obj)) == obj,
                 'initial LLVM tool escapes its object directory')
        _native_image(resolved, plan['host'])
        mode = stat.S_IMODE(os.stat(resolved).st_mode)
        _require(mode & 0o111, 'initial LLVM tool is not executable')
        result[name] = dict(path=path, resolved_path=resolved,
                            sha256=sha256_file(resolved), mode=mode)
    # The upstream recipe declares this spelling as an llvm-objcopy alias.
    objcopy = canonical_existing(os.path.join(obj, 'bin', 'llvm-objcopy'))
    _require(os.path.commonpath((objcopy, obj)) == obj and
             sha256_file(objcopy) == result['llvm-install-name-tool']['sha256'],
             'initial install-name tool differs from its owner-declared alias')
    result['llvm-install-name-tool']['alias_of'] = 'llvm-objcopy'
    return result


def _recipe_linker_words(plan):
    value, resources = plan['link_control'], plan['resources']
    return [_safe_path(value['python']['path']), _safe_path(value['launcher']['path']),
            '--lock-root', _safe_path(resources['lock_root']),
            '--context', _safe_path(resources['heavy_link_context']), '--']


def recipe_linker_launcher(plan):
    """Construct only the pinned SDK control-plane resource service prefix."""
    return shlex.join(_recipe_linker_words(plan))


def recipe_cmake_linker_launcher(plan):
    """Use the same service as CMake's native command-list representation."""
    return ';'.join(_recipe_linker_words(plan))


def validate_link_control(value, *, inspect_inputs):
    """Return the live canonical Python target, or None for offline validation."""
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
        # Keep the invocation spelling, including virtual-environment semantics.
        # Only this control-plane reference may resolve to a regular executable.
        invocation = value['python']['path']
        target = canonical_existing(invocation)
        _require(os.access(target, os.X_OK), 'control-plane Python target is not executable')
        _file(dict(path=target, sha256=value['python']['sha256']))
        _require(canonical_existing(invocation) == target,
                 'control-plane Python target changed during inspection')
        _file(value['launcher'])
        return target


def recipe_order(plan):
    """Return a deterministic dependency-first order. Do not resolve recipes."""
    recipes = plan['recipes']
    _require(isinstance(recipes, dict) and 1 <= len(recipes) <= RECIPE_LIMIT and
             all(_recipe_name(name) for name in recipes) and
             len(recipes) == len({name.casefold() for name in recipes}) and
             'lang/gcc16' in recipes, 'full bootstrap needs a finite GCC16 closure')
    result, visited = [], set()

    def visit(name, active):
        _require(name in recipes and name not in active and _recipe_name(name),
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


def _ports_prefix(plan, recipe):
    """Return the Ports prefix when the recipe has the handoff edge, else None."""
    if MAKE_PROVIDER not in plan['recipes'][recipe]['dependencies']:
        return None
    return _safe_path(plan['output_bundle_root'])


def recipe_make(plan, commands, recipe):
    """Select the make and shell for one recipe from its bound dependency edge.

    The Ports profile owns the handoff. Every recipe outside the eight-recipe
    devel/bmake closure gets a direct devel/bmake edge. That closure includes
    shells/mksh. Other recipes use the installed Ports bmake, its own share/mk
    and the installed Ports mksh. The initial tuple retains its -m tree and
    shell only for the make closure, because MAKEFLAGS carries -m and a
    command-line SHELL overrides the profile. Gzip's extra prerequisites use
    the installed Darwin mk files without a reverse dependency on gzip.
    """
    initial = tuple(commands['make'])
    _require(len(initial) == 5 and initial[1] == '-m',
             'bound recipe steps need the bound initial make tuple')
    prefix = _ports_prefix(plan, recipe)
    if prefix is None:
        return initial
    return (os.path.join(prefix, 'bin/bmake'), 'SHELL=' + os.path.join(prefix, 'bin/mksh'),
            *initial[4:])


def recipe_gmake(plan, commands, recipe):
    """Keep the bound gmake and give it the shell recipe_make() selects."""
    initial = tuple(commands['gmake'])
    _require(len(initial) == 3 and initial[1].startswith('SHELL='),
             'bound recipe steps need the bound initial gmake tuple')
    prefix = _ports_prefix(plan, recipe)
    if prefix is None:
        return initial
    return (initial[0], 'SHELL=' + os.path.join(prefix, 'bin/mksh'), initial[2])


def recipe_environment(plan, commands, recipe):
    """Give the recipe environment the shell recipe_make() selects."""
    env = metadata_environment(plan, commands)
    prefix = _ports_prefix(plan, recipe)
    if prefix is not None:
        shell = os.path.join(prefix, 'bin/mksh')
        env.update(SHELL=shell, CONFIG_SHELL=shell)
    return env


def recipe_steps(plan, descriptor, commands, flags):
    """Construct complete selected tuples and fixed targets. No caller tail."""
    initial = initial_darwin_plan(plan)
    work = _safe_path(plan['work_root'])
    export = _safe_path(plan['ports_export']['root'])
    jobs = '-j' + str(plan['resources']['cpu_jobs'])
    result = []
    for number, recipe in enumerate(initial_execution_order(plan) if initial else recipe_order(plan)):
        make = recipe_make(plan, commands, recipe)
        env = recipe_environment(plan, commands, recipe)
        if initial and recipe == 'lang/llvm':
            result.append(dict(id=f'r{number:03d}-build-tools', recipe=recipe, phase='build-tools', role='make',
                cwd=work, env=dict(env), argv=[*make, 'EMBERBSD_DARWIN_BOOTSTRAP=yes',
                    '-C', os.path.join(export, recipe),
                    'BUILD_TARGET=' + ' '.join(INITIAL_DARWIN_TARGETS), 'build']))
            break
        for phase in ('build', 'install'):
            if recipe == 'lang/gcc16' and phase == 'install':
                result.append(dict(id='gcc16-compare', recipe=recipe, phase='compare', role='gmake',
                    cwd=work, env=dict(env), argv=[*recipe_gmake(plan, commands, recipe), '--no-print-directory', jobs,
                        '-C', _safe_path(plan['recipes'][recipe]['object_directory']),
                        '--eval=.PHONY: compare', 'compare']))
            result.append(dict(id=f'r{number:03d}-' + phase, recipe=recipe, phase=phase, role='make',
                cwd=work, env=dict(env), argv=[*make, 'EMBERBSD_DARWIN_BOOTSTRAP=yes',
                    '-C', os.path.join(export, recipe), phase]))
    return result


_LOCAL_SOURCE_FILES = {
    'lang/nawk': ('Makefile', 'files/Makefile', 'files/awk.h', 'files/awkgram.tab.c',
                   'files/awkgram.tab.h', 'files/main.c', 'files/proctab.c', 'files/LICENSE'),
    'devel/bmake': ('Makefile', 'files/configure', 'files/make-bootstrap.sh.in',
                    'files/main.c', 'files/job.c'),
    'pkgtools/bootstrap-mk-files': ('Makefile', 'files/sys.mk', 'files/bsd.prog.mk',
                                   'files/mods/bsd.own.mk.in'),
    'pkgtools/pkg_install': ('Makefile', 'files/configure', 'files/Makefile.in',
                            'files/lib/version.h', 'files/add/main.c', 'files/create/main.c',
                            'files/admin/main.c', 'files/info/main.c'),
    'archivers/bzip2': ('Makefile', 'files/Makefile', 'files/bzip2.c', 'files/bzlib.c'),
    'archivers/libarchive': ('Makefile', 'Makefile.common', 'files/configure', 'files/Makefile.in',
                           'files/libarchive/archive_read.c', 'files/libarchive/archive.h'),
    'archivers/pax': ('Makefile', 'files/configure', 'files/Makefile.in', 'files/pax.c'),
    'devel/nbpatch': ('Makefile', 'files/Makefile', 'files/patch.c', 'files/pch.c'),
    'net/fetch': ('Makefile', 'files/configure', 'files/Makefile.in', 'files/fetch.c'),
    'net/libfetch': ('Makefile', 'files/Makefile', 'files/common.c', 'files/fetch.c', 'files/fetch.h'),
    'pkgtools/cwrappers': ('Makefile', 'files/bin/Makefile', 'files/bin/cc-wrapper.c'),
    'pkgtools/digest': ('Makefile', 'files/configure', 'files/Makefile.in', 'files/digest.c'),
    'pkgtools/libnbcompat': ('Makefile', 'files/configure', 'files/Makefile.in', 'files/asprintf.c'),
    'pkgtools/mktools': ('Makefile', 'files/Makefile', 'files/mk-buildlink-symlinks.c', 'files/mkpath.c'),
    'security/mozilla-rootcerts-openssl': ('Makefile', 'DESCR', 'PLIST'),
    'shells/mksh': ('Makefile', 'files/Build.sh', 'files/main.c', 'files/sh.h'),
    'sysutils/bsdinstall': ('Makefile', 'DESCR', 'PLIST', 'files/Makefile',
                          'files/bsdinstall.c', 'files/setmode.c', 'files/bsdinstall.1'),
}


def _local_source_distfiles(recipe, row, inventory=None):
    """Check the fixed local layouts. The caller also binds the complete subtree."""
    _require(recipe in _LOCAL_SOURCE_FILES and row['distfiles'] == [] and
             'distfile_metadata' in row, 'empty distfiles need recognized local-source observations')
    inventory = _json(row['inventory']) if inventory is None else inventory
    _require(isinstance(inventory, dict) and
             all(inventory.get(name, {}).get('kind') == 'file' for name in _LOCAL_SOURCE_FILES[recipe]),
             'local-source recipe omits a required regular source file')
    for name in _LOCAL_SOURCE_FILES[recipe]:
        _sha(inventory[name].get('sha256'))
    values = metadata_values(row['distfile_metadata'])
    _require(values['PKGPATH'] == recipe and values['DISTFILES'] == '' and values['PATCHFILES'] == '',
             'local-source metadata must name this recipe and no distfiles or patchfiles')


def recipe_distfiles(plan):
    """Map explicit DIST_SUBDIR-relative destinations to pinned original files."""
    files = {}
    for recipe in recipe_order(plan):
        recipe_row = plan['recipes'][recipe]
        rows = recipe_row['distfiles']
        _require(isinstance(rows, list) and len(rows) <= 128, 'invalid bound recipe distfiles')
        if not rows:
            _local_source_distfiles(recipe, recipe_row)
            continue
        _require('distfile_metadata' not in recipe_row,
                 'distfile observations only belong to an empty local-source recipe')
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
    if initial_darwin_plan(plan):
        initial_llvm_directory(plan)
    _require(descriptor['host']['os'] == 'Darwin' and descriptor['host']['cpu'] in ('arm64', 'aarch64') and
             descriptor['make_binding']['kind'] == 'ports-bootstrap-make-v1' and
             descriptor.get('driver_binding', {}).get('kind') == DARWIN_LLVM_MC_LLD_SEED,
             'bound full plans need the recognized Darwin make and v2 driver bindings')
    _require({'pkg_add', 'pkg_create'} <= set(plan['metadata']['native_tools']) and
             all(descriptor['tools'].get(role, {}).get('kind') == 'native'
                 for role in ('pkg_add', 'pkg_create')),
             'bound recipe installation needs explicit native pkg_add and pkg_create roles')
    from .bootstrap_producer import _native_provider_profile
    if _native_provider_profile(plan['metadata']['profile']['sha256']):
        _require({'lang/llvm', 'devel/lld'} <= set(plan['recipes']),
                 'native GCC defaults require the common source-built LLVM and LLD recipes')
    from .bootstrap_pilot import _references, _validate_resources
    parallel = 'link_control' in plan
    if parallel:
        validate_link_control(plan['link_control'], inspect_inputs=inspect_inputs)
    _validate_resources(plan['resources'], recipe=True, parallel=parallel, inspect_inputs=inspect_inputs)
    validate_metadata_inputs(plan, descriptor, commands, flags,
                             inspect_inputs=inspect_inputs, recipe_limit=RECIPE_LIMIT)
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
    _require(plan['comparison_command'] == ('initial-llvm-tools' if initial_darwin_plan(plan) else 'gcc16-compare') and
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
