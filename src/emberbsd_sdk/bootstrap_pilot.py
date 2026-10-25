# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Finite source-owned pilots for independently accepted bootstrap inputs."""

import os
from pathlib import Path
import signal
import sys
import threading
import time

from .errors import CommandError, ProcessOwnershipError, SDKError
from .inventory import sha256_file, snapshot_tree
from .jsonio import write_json
from .native_toolset import (NATIVE_C_PROBE, NATIVE_CXX_PROBE, _absolute,
                             _envelope, _native_image, _require, _source)
from .paths import canonical_existing, canonical_new, entry, inside, reject_overlap
from .process import ContextLocks, run_logged

BOOTSTRAP_PILOT_API = 1
PILOT_FORMAT = 'emberbsd-native-gcc16-pilot-plan'
_SCOPE = 'gcc16-bootstrap'
_SOURCE = {'c': ('native.c', NATIVE_C_PROBE), 'cxx': ('native.cc', NATIVE_CXX_PROBE)}
STATIC_RUNTIME_CXX_PROBE = (b'#include <iostream>\n'
    b'int main() { try { throw 16; } catch (int value) { if (value != 16) return 1; '
    b'std::cout << "emberbsd-native-cxx-v1" << std::endl; return !std::cout; } return 1; }\n')
_STATIC_RUNTIME_PURPOSE = 'native-c-and-cxx-static-runtime-pilot'


def _pilot_sources(static_runtime):
    return {**_SOURCE, 'cxx': ('native.cc', STATIC_RUNTIME_CXX_PROBE)} if static_runtime else _SOURCE


def pilot_steps(descriptor, commands, flags, work_root, *, static_runtime=False):
    """Construct the complete supported argv and environment. Do not run it."""
    _require(type(static_runtime) is bool, 'pilot static-runtime selector must be a boolean')
    work = _absolute(work_root)
    directories = sorted({os.path.dirname(words[0]) for words in commands.values()})
    _require(all(os.pathsep not in path for path in directories),
             'pilot tool directory cannot be encoded in PATH')
    environment = {'PATH': os.pathsep.join(directories), 'LC_ALL': 'C', 'TZ': 'UTC',
                   'TMPDIR': work}
    steps = []
    for language, (name, source) in _pilot_sources(static_runtime).items():
        driver = commands['cc' if language == 'c' else 'cxx']
        language_flags = flags['cflags' if language == 'c' else 'cxxflags']
        src = os.path.join(work, name)
        obj = os.path.join(work, language + '.o')
        artifact = os.path.join(work, language + '-probe')
        base = [*driver, *flags['cppflags'], *language_flags, '-v']
        steps.append(dict(id=language + '-compile', phase='compile', cwd=work,
                          argv=[*base, '-c', src, '-o', obj], env=dict(environment)))
        link_flags = ['-Wl,--threads=1'] if descriptor['host']['os'] == 'Darwin' else []
        if static_runtime:
            link_flags += ['-static-libstdc++', '-static-libgcc']
        steps.append(dict(id=language + '-link', phase='link', cwd=work,
                          argv=[*base, obj, *flags['ldflags'], *link_flags, '-o', artifact],
                          env=dict(environment)))
        run_environment = dict(environment)
        if descriptor['host']['os'] == 'Darwin':
            run_environment['DYLD_PRINT_LIBRARIES'] = '1'
        steps.append(dict(id=language + '-run', phase='run', cwd=work,
                          argv=[artifact], env=run_environment))
    return steps


def validate_pilot_plan(plan, descriptor, commands, flags):
    """Validate this finite pilot. It does not accept a recipe build or provider."""
    _envelope(plan, PILOT_FORMAT, scope=_SCOPE)
    _require(set(plan) == {'format', 'version', 'scope', 'purpose', 'seed_descriptor',
                          'host', 'sources', 'work_root', 'required_roles', 'resources', 'commands'},
             'unsupported pilot plan fields')
    _require(plan['purpose'] in ('native-c-and-cxx-pilot', _STATIC_RUNTIME_PURPOSE) and
             plan['host'] == descriptor['host'] and plan['sources'] == descriptor['sources'],
             'pilot source or host pins mismatch')
    for source in plan['sources'].values():
        _source(source)
    work = _absolute(plan['work_root'])
    _require(isinstance(plan['required_roles'], list) and
             len(plan['required_roles']) == len(set(plan['required_roles'])) and
             {'cc', 'cxx', 'cpp', *descriptor['driver_components'].values()} <=
             set(plan['required_roles']) <= set(commands), 'pilot native roles are incomplete')
    _validate_resources(plan['resources'])
    _require(plan['commands'] == pilot_steps(descriptor, commands, flags, work,
             static_runtime=plan['purpose'] == _STATIC_RUNTIME_PURPOSE),
             'pilot commands differ from the source-owned finite route')
    return plan


def _validate_resources(resources, *, metadata=False, recipe=False, parallel=False, inspect_inputs=True):
    _require(isinstance(resources, dict) and set(resources) ==
             {'cpu_jobs', 'link_jobs', 'runtime_seconds', 'lock_root', 'heavy_link_context'},
             'pilot resource agreement is incomplete')
    _require(not parallel or recipe, 'parallel resource control requires the full recipe route')
    limit = 4 if metadata else 1
    if parallel:
        limit = os.cpu_count() if inspect_inputs else None
    _require(not (parallel and inspect_inputs) or type(limit) is int and limit > 0,
             'the current host CPU budget is unavailable')
    _require(type(resources['cpu_jobs']) is int and resources['cpu_jobs'] > 0 and
             (limit is None or resources['cpu_jobs'] <= limit) and
             type(resources['link_jobs']) is int and resources['link_jobs'] == 1 and
             type(resources['runtime_seconds']) is int and
             0 < resources['runtime_seconds'] <= (86400 if recipe else 1200 if metadata else 120),
             'finite producer concurrency or observation budget is unsupported')
    _absolute(resources['lock_root'])
    _absolute(resources['heavy_link_context'])


def _references(value):
    if isinstance(value, dict):
        if set(value) == {'path', 'sha256'}:
            yield _absolute(value['path'])
        for child in value.values():
            yield from _references(child)
    elif isinstance(value, list):
        for child in value:
            yield from _references(child)


def _hold():
    # Keep both the input leases and the writable-work fence after failed drain.
    for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(number, signal.SIG_IGN)
        except (OSError, ValueError):
            pass
    while True:
        time.sleep(30)


def run_pilot(descriptor_path, *, descriptor_sha256, accepted_seed_receipt,
              bootstrap_plan, required_roles):
    """Run once; retain actual failure evidence. Never issue qualification.

    The caller must use a main thread and retain this SDK source unchanged.
    The CLI supplies signal policy. Failed ownership drainage holds the leases.
    Startup, input validation and drainage can exceed the observation budget.
    """
    return _run(descriptor_path, descriptor_sha256=descriptor_sha256,
                accepted_seed_receipt=accepted_seed_receipt, bootstrap_plan=bootstrap_plan,
                required_roles=required_roles, profile='native')


def run_make_probe(descriptor_path, *, descriptor_sha256, accepted_seed_receipt,
                   bootstrap_plan, required_roles):
    """Run only the fixed make/shell probe with the existing pilot fences."""
    return _run(descriptor_path, descriptor_sha256=descriptor_sha256,
                accepted_seed_receipt=accepted_seed_receipt, bootstrap_plan=bootstrap_plan,
                required_roles=required_roles, profile='make')


def run_producer(descriptor_path, *, descriptor_sha256, accepted_seed_receipt,
                 bootstrap_plan, required_roles):
    """Run only a recognized accepted producer stage. Never qualify it."""
    return _run(descriptor_path, descriptor_sha256=descriptor_sha256,
                accepted_seed_receipt=accepted_seed_receipt, bootstrap_plan=bootstrap_plan,
                required_roles=required_roles, profile='producer')


def run_bootstrap(descriptor_path, *, descriptor_sha256, accepted_seed_receipt,
                  bootstrap_plan, required_roles):
    """Run the complete fixed bound-make plan. Leave graduation independent."""
    return _run(descriptor_path, descriptor_sha256=descriptor_sha256,
                accepted_seed_receipt=accepted_seed_receipt, bootstrap_plan=bootstrap_plan,
                required_roles=required_roles, profile='bootstrap')


def _run(descriptor_path, *, descriptor_sha256, accepted_seed_receipt,
         bootstrap_plan, required_roles, profile):
    from .bootstrap_make import MAKE_PROBE_FORMAT, make_probe_sources, verify_make_step
    from .bootstrap_producer import PRODUCER_FORMAT, metadata_paths
    from .bootstrap_program import program_outputs, program_sources, verify_program_step
    from .bootstrap_gnu_program import GNU_PROGRAM_STAGES, prepare_gnu_work
    from .bootstrap_bsdinstall import prepare_bsdinstall_work
    from .bootstrap_recipe import BOOTSTRAP_FORMAT, prepare_recipe_work, validate_link_control
    from .bootstrap_seed import load_bootstrap_seed
    from .native_toolset import _json
    _require(threading.current_thread() is threading.main_thread(),
             'pilot signal and HOLD policy requires the main thread')
    options = dict(descriptor_sha256=descriptor_sha256,
                   accepted_seed_receipt=accepted_seed_receipt,
                   bootstrap_plan=bootstrap_plan, required_roles=required_roles)
    descriptor_ref = dict(path=os.fspath(descriptor_path), sha256=descriptor_sha256)
    descriptor = _json(descriptor_ref)
    plan = _json(bootstrap_plan)
    make_probe = profile == 'make'
    metadata = profile == 'producer' and plan.get('stage') == 'metadata'
    gnu_program = profile == 'producer' and plan.get('stage') in GNU_PROGRAM_STAGES
    bsdinstall = profile == 'producer' and plan.get('stage') == 'bsdinstall-bootstrap'
    program = profile == 'producer' and (gnu_program or bsdinstall or plan.get('stage') in
                                        ('grep-bootstrap', 'cmp-bootstrap', 'hostname-bootstrap'))
    relocation = profile == 'producer' and plan.get('stage') == 'utility-relocation'
    recipe = profile == 'bootstrap'
    if relocation:
        from .bootstrap_relocation import (prepare_relocation_work, relocation_read_roots,
            relocation_work_outputs, verify_relocation_outputs, verify_relocation_step)
    _require(plan.get('format') == {'make': MAKE_PROBE_FORMAT, 'native': PILOT_FORMAT,
                                    'producer': PRODUCER_FORMAT, 'bootstrap': BOOTSTRAP_FORMAT}[profile],
             'the runner requires its exact finite plan format')
    if recipe:
        _require(descriptor.get('make_binding') is not None,
                 'the full recipe controller requires the fixed make binding')
    resources = plan['resources']
    root = canonical_existing(resources['lock_root'])
    work = _absolute(plan['work_root'])
    _require(canonical_new(work) == work, 'pilot work has aliased ancestors')
    roots = [descriptor['bundle_root']] + [row['root']
              for kind in ('headers', 'libraries') for row in descriptor['host_abi'][kind]]
    if metadata or recipe:
        roots += [plan['metadata']['export']['root'], plan['metadata']['utility_bin']]
    if program:
        roots += [plan['program']['source']['root']]
        if gnu_program or bsdinstall:
            roots += [plan['program']['owner']['root']]
        if bsdinstall:
            roots += [plan['program']['compat']['root']]
    if relocation:
        roots += list(relocation_read_roots(plan))
    sdk_root = str(Path(__file__).resolve().parents[2])
    _require(work != root and not inside(root, work), 'pilot work contains its lock root')
    for path in [*roots, sdk_root, resources['heavy_link_context']]:
        reject_overlap(work, path)
    acceptance = _json(accepted_seed_receipt)
    shared = set(roots + [sdk_root, os.fspath(descriptor_path),
                         *list(_references(descriptor)), *list(_references(accepted_seed_receipt)),
                         *list(_references(acceptance)), *list(_references(bootstrap_plan)),
                         *list(_references(plan))])
    if metadata or recipe:
        shared.update(_references(_json(plan['metadata']['export']['source_proof'])))
    if program:
        shared.update(_references(_json(plan['program']['source']['source_proof'])))
        if gnu_program or bsdinstall:
            shared.update(_references(_json(plan['program']['owner']['source_proof'])))
        if bsdinstall:
            component = _json(plan['program']['compat']['component_receipt'])
            shared.update(_references(component))
            shared.update(_references(_json(component['preparation'])['source_seed_descriptor']))
    if relocation:
        shared.update(_references(_json(plan['relocation']['originals'])))
    control_python = None
    if recipe and 'link_control' in plan:
        control_python = validate_link_control(plan['link_control'], inspect_inputs=True)
        shared.add(control_python)
    if recipe:
        for path in shared:
            reject_overlap(work, path)
    # Re-read all acceptance and plan bytes under their shared fences.
    with ContextLocks(sorted(shared), root, shared=True), ContextLocks([work], root):
        if control_python is not None:
            _require(validate_link_control(plan['link_control'], inspect_inputs=True) == control_python,
                     'control-plane Python target changed before dispatch')
        selected = load_bootstrap_seed(descriptor_path, **options)
        selected_plan = selected.as_dict()['plan']
        _require(selected_plan == plan, 'pilot plan changed before dispatch')
        _require(entry(work) is None, 'pilot work must be a fresh, unused directory')
        os.mkdir(work)
        if metadata or recipe:
            sources, outputs = {}, ()
        elif relocation:
            sources, outputs = {}, relocation_work_outputs(plan)
        elif program:
            sources, outputs = program_sources(plan), program_outputs(plan)
        elif make_probe:
            sources = make_probe_sources(descriptor, selected.commands)
            outputs = (*sources, 'fixture.result')
        else:
            sources = dict(_pilot_sources(plan['purpose'] == _STATIC_RUNTIME_PURPOSE).values())
            outputs = ('native.c', 'native.cc', 'c.o', 'cxx.o', 'c-probe', 'cxx-probe')
        if metadata:
            for path in metadata_paths(work).values():
                os.mkdir(path)
        if relocation:
            prepare_relocation_work(plan)
        if recipe:
            prepare_recipe_work(plan)
        if gnu_program:
            prepare_gnu_work(plan, descriptor, selected.commands)
        result = dict(format=('emberbsd-native-gcc16-producer-result' if metadata or program or relocation or recipe else
                              'emberbsd-bootstrap-make-probe-result' if make_probe else
                              'emberbsd-native-gcc16-pilot-result'), version=1,
                      scope=_SCOPE, api=BOOTSTRAP_PILOT_API, status='RUNNING',
                      seed_descriptor=descriptor_ref, accepted_seed_receipt=accepted_seed_receipt,
                      bootstrap_plan=bootstrap_plan, commands=[], all_commands_drained=False,
                      evidence_level='command-reported-routes-and-actual-exits',
                      ordinary_provider_qualified=False)
        if relocation:
            result['stage'] = 'utility-relocation'
        if recipe:
            result['stage'] = 'full-recipe-bootstrap'
        result_path = os.path.join(work, 'result.json')
        write_json(result_path, result)
        for name, source in sources.items():
            with open(os.path.join(work, name), 'xb') as output:
                output.write(source)
        if bsdinstall:
            prepare_bsdinstall_work(plan)
        deadline = time.monotonic() + resources['runtime_seconds']
        try:
            for step in plan['commands']:
                log = os.path.join(work, step['id'] + '.log')
                actual = dict(step)
                try:
                    if step['phase'] == 'run':
                        _native_image(step['argv'][0], descriptor['host'])
                    heavy = ((recipe and 'link_control' not in plan) or step['phase'] == 'link' or
                             gnu_program and step['phase'] in ('gnu-configure', 'gnu-build'))
                    context = [resources['heavy_link_context']] if heavy else []
                    with ContextLocks(context, root):
                        remaining = deadline - time.monotonic()
                        _require(remaining > 0, 'pilot observation budget expired before next command')
                        try:
                            returned = run_logged(step['argv'], log, cwd=step['cwd'], env=step['env'],
                                                  timeout_seconds=remaining)
                        except ProcessOwnershipError as exc:
                            try:
                                actual.update(actual_exit=None, error=str(exc), unfinalized_log_path=log)
                                result['commands'].append(actual)
                                result.update(status='HOLD_UNPROVEN_DRAIN', error=str(exc), owned_sid=exc.sid)
                                write_json(result_path, result, exclusive=False)
                                print(f'bootstrap pilot HOLD: {exc}; all leases retained',
                                      file=sys.stderr, flush=True)
                            finally:
                                _hold()
                        except CommandError as exc:
                            actual['actual_exit'] = exc.returncode
                            if not (make_probe or program) or exc.returncode != step['expected_exit']:
                                raise
                        else:
                            actual['actual_exit'] = returned.returncode
                        _require(actual['actual_exit'] == (step['expected_exit'] if make_probe or program else 0),
                                 'finite command exit differs from its expected observation')
                        if make_probe:
                            verify_make_step(step, log, work, descriptor)
                        if step['phase'] in ('compile', 'link'):
                            artifact = step['argv'][-1]
                            _native_image(artifact, descriptor['host'], object_file=step['phase'] == 'compile')
                            actual['output'] = dict(path=artifact, sha256=sha256_file(artifact))
                        if program:
                            actual.update(verify_program_step(step, work, plan))
                        if relocation:
                            actual['output'] = verify_relocation_step(plan, descriptor, selected.commands, step)
                except BaseException as exc:
                    if isinstance(exc, CommandError):
                        actual['actual_exit'] = exc.returncode
                    else:
                        actual.setdefault('actual_exit', None)
                    actual['error'] = str(exc)
                    raise
                finally:
                    if result['status'] != 'HOLD_UNPROVEN_DRAIN':
                        if entry(log) is not None:
                            actual['log'] = dict(path=log, sha256=sha256_file(log))
                        result['commands'].append(actual)
                        write_json(result_path, result, exclusive=False)
            for name, source in sources.items():
                _require(Path(work, name).read_bytes() == source, 'pilot source changed during execution')
            if relocation:
                result['relocation'] = verify_relocation_outputs(plan, descriptor, selected.commands)
            if recipe:
                inventory = snapshot_tree(plan['output_bundle_root'])
                _require(bool(inventory), 'the complete recipe plan produced no installed output')
                path = os.path.join(work, 'output-inventory.json')
                write_json(path, inventory)
                result['output_inventory'] = dict(path=path, sha256=sha256_file(path))
            result['status'] = 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW'
        except BaseException as exc:
            result.update(status='FAILED', error=str(exc))
        # A successful run_logged return or ordinary exception completed drainage.
        result['all_commands_drained'] = True
        try:
            load_bootstrap_seed(descriptor_path, **options)
            if control_python is not None:
                _require(validate_link_control(plan['link_control'], inspect_inputs=True) == control_python,
                         'control-plane Python target changed during execution')
            result['inputs_revalidated_after_drain'] = True
        except SDKError as exc:
            result.update(status='FAILED', error=str(exc), inputs_revalidated_after_drain=False)
        result['outputs'] = {}
        for name in outputs:
            try:
                path = os.path.join(work, name)
                if entry(path) is not None:
                    result['outputs'][name] = sha256_file(path)
            except (OSError, SDKError) as exc:
                result['status'] = 'FAILED'
                result.setdefault('error', str(exc))
                result.setdefault('output_errors', {})[name] = str(exc)
        write_json(result_path, result, exclusive=False)
    result['all_leases_closed'] = True
    return result
