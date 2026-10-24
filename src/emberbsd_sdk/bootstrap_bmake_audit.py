# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Independent, scoped behavior audit for the first Ports-owned bmake."""

import os
import stat
import threading
import time

from .bootstrap_bmake_seed import (ACCEPTANCE_FORMAT, PORTS_VERSION, RESULT_FORMAT, SCOPE,
    _compiler_seed_references, _guard_lock_root, _hash_ref, _references, _tool_map,
    validate_compiler_seed, validate_plan, validate_source_descriptor)
from .errors import CommandError, ProcessOwnershipError, SDKError
from .inventory import sha256_file
from .jsonio import write_json
from .native_toolset import _absolute, _json, _require, _sha
from .paths import canonical_existing, canonical_new, entry, reject_overlap
from .process import ContextLocks, run_logged

AUDIT_PLAN_FORMAT = 'emberbsd-bootstrap-bmake-audit-plan'
PRODUCER_ACCEPTANCE_FORMAT = 'emberbsd-bootstrap-bmake-producer-acceptance'
PRODUCER_ACCEPTANCE_BOUNDARY = (
    'independently reviewed source-bound bmake execution evidence only; '
    'no NativeToolset qualification')
_MAKEFILE = (b'.SHELL: name=sh path=@SHELL@\n'
             b'.PHONY: all child fail\n'
             b'all: child\n\t@printf \'bmake-bootstrap-seed-all-ok\\n\'\n'
             b'child:\n\t@printf \'bmake-bootstrap-seed-child-ok\\n\'\n'
             b'fail:\n\t@printf \'expected-bmake-failure\\n\'; exit 7\n')


def audit_steps(plan):
    binary, work = plan['output_binary'], plan['work_root']
    shell = plan['host_tools']['sh']['path']
    environment = {'PATH': os.path.join(work, 'bin'), 'LC_ALL': 'C', 'TZ': 'UTC',
                   'TMPDIR': os.path.join(work, 'tmp'), 'MAKECONF': '/dev/null', 'SHELL': shell}
    common = [binary, '-r', '-m', os.path.join(work, 'mk'), '-f', 'Makefile']
    return [
        dict(id='version', phase='version', cwd=work,
             argv=[binary, '-r', '-m', os.path.join(work, 'mk'), '-V', '.MAKE.VERSION'],
             env=dict(environment), expected_exit=0),
        dict(id='positive', phase='behavior', cwd=work,
             argv=[*common, 'all'], env=dict(environment), expected_exit=0),
        dict(id='expected-failure', phase='failure', cwd=work,
             argv=[*common, 'fail'], env=dict(environment), expected_exit=1),
    ]


def validate_producer_result(result, seed, source, *, seed_ref=None, source_ref=None,
                             inspect_inputs=True):
    """Bind a pending result to the exact source-owned producer plan and drained logs."""
    fields = {'format', 'version', 'scope', 'status', 'seed_descriptor', 'source_descriptor',
              'bootstrap_plan', 'commands', 'all_commands_drained', 'evidence_level',
              'ordinary_provider_qualified', 'output_binary', 'output',
              'inputs_revalidated_after_drain'}
    _require(isinstance(result, dict) and set(result) == fields and
             result.get('format') == RESULT_FORMAT and type(result.get('version')) is int and
             result['version'] == 1 and result.get('scope') == SCOPE and
             result.get('status') == 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW' and
             result.get('all_commands_drained') is True and
             result.get('inputs_revalidated_after_drain') is True and
             result.get('evidence_level') == 'accepted-GCC16-command-routes-and-actual-exits' and
             result.get('ordinary_provider_qualified') is False,
             'independent audit requires a drained source-bound producer result')
    result_seed_ref = _hash_ref(result.get('seed_descriptor'), 'producer result compiler seed')
    result_source_ref = _hash_ref(result.get('source_descriptor'), 'producer result source descriptor')
    if seed_ref is not None:
        _require(result_seed_ref == seed_ref,
                 'producer result descriptor reference differs from the requested audit inputs')
    if source_ref is not None:
        _require(result_source_ref == source_ref,
                 'producer result descriptor reference differs from the requested audit inputs')
    plan_ref = _hash_ref(result.get('bootstrap_plan'), 'producer bootstrap plan')
    producer_plan = _json(plan_ref)
    validate_plan(producer_plan, seed, source, seed_ref=result_seed_ref,
                  source_ref=result_source_ref, inspect_inputs=inspect_inputs)
    _require(result.get('output_binary') == producer_plan['output_binary'],
             'producer result output path differs from its source-bound plan')
    output = result.get('output')
    _require(isinstance(output, dict) and set(output) == {'path', 'sha256', 'mode'} and
             output.get('path') == producer_plan['output_binary'] and
             _sha(output.get('sha256')) and type(output.get('mode')) is int and
             output['mode'] & 0o111,
             'producer result output differs from its source-bound plan')
    commands = result.get('commands')
    _require(isinstance(commands, list) and len(commands) == len(producer_plan['commands']) == 2,
             'producer result omits a fixed bmake bootstrap command')
    for actual, expected in zip(commands, producer_plan['commands']):
        log_path = os.path.join(producer_plan['work_root'], expected['id'] + '.log')
        _require(isinstance(actual, dict) and set(actual) == {*expected, 'actual_exit', 'log'} and
                 all(actual.get(key) == value for key, value in expected.items()) and
                 type(actual.get('actual_exit')) is int and actual.get('actual_exit') == 0 and
                 _hash_ref(actual.get('log'), 'producer command log')['path'] == log_path,
                 'producer result command or log differs from the fixed bmake route')
        if inspect_inputs:
            log_ref = actual['log']
            _require(sha256_file(log_ref['path']) == log_ref['sha256'],
                     f'producer command log changed: {expected["id"]}')
    if inspect_inputs:
        info = os.lstat(output['path'])
        _require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == output['mode'] and
                 os.access(output['path'], os.X_OK) and sha256_file(output['path']) == output['sha256'],
                 'producer output differs from its completed result')
    return producer_plan, plan_ref


def validate_producer_acceptance(acceptance_ref, result, result_ref, seed_ref, source_ref):
    """Require a separately hash-pinned gate receipt for the claimed build provenance."""
    acceptance_ref = _hash_ref(acceptance_ref, 'independent producer acceptance receipt')
    receipt = _json(acceptance_ref)
    fields = {'format', 'version', 'scope', 'status', 'evidence_level', 'producer_result',
              'bootstrap_plan', 'seed_descriptor', 'source_descriptor', 'output', 'commands',
              'all_commands_drained', 'inputs_revalidated_after_drain',
              'ordinary_provider_qualified', 'scope_boundary'}
    _require(isinstance(receipt, dict) and set(receipt) == fields and
             receipt.get('format') == PRODUCER_ACCEPTANCE_FORMAT and
             type(receipt.get('version')) is int and receipt['version'] == 1 and
             receipt.get('scope') == SCOPE and receipt.get('status') == 'PASS' and
             receipt.get('evidence_level') == 'independent-producer-evidence-review' and
             receipt.get('all_commands_drained') is True and
             receipt.get('inputs_revalidated_after_drain') is True and
             receipt.get('ordinary_provider_qualified') is False and
             receipt.get('scope_boundary') == PRODUCER_ACCEPTANCE_BOUNDARY,
             'independent producer acceptance receipt is missing or invalid')
    bindings = (
        ('producer_result', result_ref),
        ('bootstrap_plan', _hash_ref(result['bootstrap_plan'], 'producer bootstrap plan')),
        ('seed_descriptor', seed_ref),
        ('source_descriptor', source_ref),
    )
    for field, expected in bindings:
        _require(_hash_ref(receipt.get(field), f'producer acceptance {field.replace("_", " ")}') == expected,
                 'producer acceptance receipt differs from the reviewed result')
    _require(receipt.get('output') == result.get('output') and
             receipt.get('commands') == result.get('commands'),
             'producer acceptance receipt differs from the reviewed result')
    return acceptance_ref, receipt


def create_audit_plan(result_ref, seed_ref, source_ref, producer_acceptance_ref,
                      work_root, lock_root, host_tools, *, runtime_seconds=300):
    result_ref = _hash_ref(result_ref, 'producer result')
    seed_ref = _hash_ref(seed_ref, 'compiler seed')
    source_ref = _hash_ref(source_ref, 'source descriptor')
    result = _json(result_ref)
    _require(result.get('format') == RESULT_FORMAT and result.get('scope') == SCOPE and
             result.get('status') == 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW' and
             result.get('ordinary_provider_qualified') is False,
             'independent audit requires a pending scoped producer result')
    _require(_hash_ref(result.get('seed_descriptor'), 'producer result compiler seed') == seed_ref and
             _hash_ref(result.get('source_descriptor'), 'producer result source descriptor') == source_ref,
             'producer result descriptor reference differs from the requested audit inputs')
    seed, source = _json(seed_ref), _json(source_ref)
    validate_compiler_seed(seed, inspect_inputs=False)
    validate_source_descriptor(source, inspect_inputs=False)
    producer_plan, _ = validate_producer_result(
        result, seed, source, seed_ref=seed_ref, source_ref=source_ref, inspect_inputs=False)
    producer_acceptance_ref, _ = validate_producer_acceptance(
        producer_acceptance_ref, result, result_ref, seed_ref, source_ref)
    output = result.get('output', {})
    _require(isinstance(output, dict) and set(output) == {'path', 'sha256', 'mode'},
             'producer result omits its exact bmake output')
    work = _absolute(work_root)
    plan = dict(format=AUDIT_PLAN_FORMAT, version=1, scope=SCOPE,
                purpose='independent-bmake-seed-audit', producer_result=result_ref,
                producer_acceptance=producer_acceptance_ref,
                seed_descriptor=seed_ref, source_descriptor=source_ref,
                output_binary=_absolute(output['path']), output_sha256=_sha(output['sha256']),
                work_root=work, host_tools=host_tools,
                resources=dict(lock_root=_absolute(lock_root), runtime_seconds=runtime_seconds),
                commands=[])
    plan['commands'] = audit_steps(plan)
    validate_audit_plan(plan, result, seed, source, result_ref=result_ref,
                        seed_ref=seed_ref, source_ref=source_ref,
                        producer_acceptance_ref=producer_acceptance_ref, inspect_inputs=False)
    return plan


def validate_audit_plan(plan, result, seed, source, *, result_ref=None, seed_ref=None,
                        source_ref=None, producer_acceptance_ref=None, inspect_inputs=True):
    fields = {'format', 'version', 'scope', 'purpose', 'producer_result', 'seed_descriptor',
              'producer_acceptance', 'source_descriptor', 'output_binary', 'output_sha256',
              'work_root', 'host_tools', 'resources', 'commands'}
    _require(isinstance(plan, dict) and set(plan) == fields and
             plan.get('format') == AUDIT_PLAN_FORMAT and type(plan.get('version')) is int and
             plan['version'] == 1 and plan.get('scope') == SCOPE and
             plan.get('purpose') == 'independent-bmake-seed-audit',
             'unsupported first-bmake independent audit plan')
    _require(result.get('format') == RESULT_FORMAT and result.get('scope') == SCOPE and
             result.get('status') == 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW' and
             result.get('ordinary_provider_qualified') is False,
             'independent audit requires a pending bmake-only producer result')
    for name, expected_ref in (('seed_descriptor', seed_ref), ('source_descriptor', source_ref)):
        result_ref_value = _hash_ref(result.get(name), f'producer result {name.replace("_", " ")}')
        plan_ref_value = _hash_ref(plan[name], name.replace('_', ' '))
        _require(result_ref_value == plan_ref_value,
                 'producer result descriptor reference differs from the requested audit inputs')
        if expected_ref is not None:
            _require(result_ref_value == expected_ref,
                     'producer result descriptor reference differs from the requested audit inputs')
    for name, expected_ref in (('producer_result', result_ref), ('seed_descriptor', seed_ref),
                               ('source_descriptor', source_ref)):
        ref = _hash_ref(plan[name], name.replace('_', ' '))
        if expected_ref is not None:
            _require(ref == expected_ref, 'independent audit input reference changed')
    output = result.get('output', {})
    _require(plan['output_binary'] == output.get('path') and
             plan['output_sha256'] == output.get('sha256'),
             'independent audit bmake output differs from producer result')
    producer_plan, _ = validate_producer_result(
        result, seed, source, seed_ref=seed_ref, source_ref=source_ref,
        inspect_inputs=inspect_inputs)
    acceptance_ref = _hash_ref(plan['producer_acceptance'], 'independent producer acceptance receipt')
    if producer_acceptance_ref is not None:
        _require(acceptance_ref == producer_acceptance_ref,
                 'independent producer acceptance receipt reference changed')
    _, producer_acceptance = validate_producer_acceptance(
        acceptance_ref, result, _hash_ref(result_ref, 'producer result'),
        _hash_ref(plan['seed_descriptor'], 'compiler seed'),
        _hash_ref(plan['source_descriptor'], 'source descriptor'))
    _tool_map(plan['host_tools'], inspect_inputs=inspect_inputs)
    _require(plan['commands'] == audit_steps(plan),
             'independent audit commands differ from its source-owned fixed route')
    resources = plan['resources']
    _require(isinstance(resources, dict) and set(resources) == {'lock_root', 'runtime_seconds'} and
             type(resources['runtime_seconds']) is int and 0 < resources['runtime_seconds'] <= 300,
             'independent bmake audit budget is invalid')
    sdk_root = os.path.realpath(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    seed_refs = (_compiler_seed_references(seed) if inspect_inputs else list(_references(seed)))
    protected = [_absolute(plan['work_root']), _absolute(plan['output_binary']),
                 _absolute(seed['prefix']), _absolute(source['root']),
                 source['archive']['path'], sdk_root,
                 *list(_references(result)), *seed_refs,
                 *list(_references(source)), *list(_references(plan)),
                 *list(_references(producer_acceptance)),
                 producer_plan['work_root'], producer_plan['output_binary'],
                 *(row['log']['path'] for row in result['commands']),
                 *seed['host_runtime']['libraries'],
                 *(row['path'] for row in plan['host_tools'].values())]
    _guard_lock_root(resources['lock_root'], protected)
    _require(not (_MAKEFILE.count(b'@SHELL@') != 1), 'invalid independent fixture')
    if inspect_inputs:
        _require(canonical_new(plan['work_root']) == plan['work_root'],
                 'independent audit work directory has aliased ancestors')
        _require(sha256_file(plan['output_binary']) == _sha(plan['output_sha256']),
                 'producer bmake output changed before independent audit')
        info = os.lstat(plan['output_binary'])
        _require(stat.S_ISREG(info.st_mode) and os.access(plan['output_binary'], os.X_OK),
                 'independent audit input is not an executable regular bmake')
        _require(seed['scope'] == SCOPE and source['scope'] == SCOPE,
                 'independent audit selectors have an unsupported scope')
    return plan


def _verify_step(step, log, work, version):
    with open(log, 'rb') as handle:
        output = handle.read(1024 * 1024 + 1)
    _require(len(output) <= 1024 * 1024, 'independent bmake audit output exceeds its size limit')
    text = output.decode('utf-8', 'strict')
    if step['id'] == 'version':
        _require(text.strip() == version, 'built bmake reports a different version')
    elif step['id'] == 'positive':
        _require('bmake-bootstrap-seed-all-ok' in text and
                 'bmake-bootstrap-seed-child-ok' in text,
                 'built bmake failed the independent shell and target behavior probe')
    else:
        _require('expected-bmake-failure' in text,
                 'built bmake did not preserve the expected failing recipe')


def run_acceptance(result_ref, seed_ref, source_ref, producer_acceptance_ref, plan_ref):
    """Run an independent executable and recipe audit; issue only scoped acceptance."""
    _require(threading.current_thread() is threading.main_thread(),
             'independent bmake audit must run on the main thread')
    result_ref, seed_ref = _hash_ref(result_ref, 'producer result'), _hash_ref(seed_ref, 'compiler seed')
    source_ref = _hash_ref(source_ref, 'source descriptor')
    producer_acceptance_ref = _hash_ref(producer_acceptance_ref, 'independent producer acceptance receipt')
    plan_ref = _hash_ref(plan_ref, 'audit plan')
    result, seed, source, plan = _json(result_ref), _json(seed_ref), _json(source_ref), _json(plan_ref)
    validate_compiler_seed(seed, inspect_inputs=True)
    validate_source_descriptor(source, inspect_inputs=True)
    validate_audit_plan(plan, result, seed, source, result_ref=result_ref,
                        seed_ref=seed_ref, source_ref=source_ref,
                        producer_acceptance_ref=producer_acceptance_ref, inspect_inputs=True)
    producer_plan, _ = validate_producer_result(
        result, seed, source, seed_ref=seed_ref, source_ref=source_ref, inspect_inputs=True)
    work = plan['work_root']
    lock_root = canonical_existing(plan['resources']['lock_root'])
    _require(entry(work) is None, 'independent audit work must be fresh')
    shared = set([*list(_references(result_ref)), *list(_references(seed_ref)),
                  *list(_references(source_ref)), *list(_references(plan_ref)),
                  *list(_references(result)), *list(_references(seed)),
                  *list(_references(source)), *list(_references(plan)),
                  result['output']['path'], seed['prefix'], source['root'],
                  source['archive']['path'], producer_plan['work_root'],
                  producer_plan['output_binary'],
                  *(row['log']['path'] for row in result['commands'])])
    shared.update(_references(producer_acceptance_ref))
    shared.update(_references(_json(producer_acceptance_ref)))
    shared.update(row['path'] for row in seed['driver_components'].values())
    shared.update(seed['host_runtime']['libraries'])
    shared.update(_compiler_seed_references(seed))
    shared.update(row['path'] for row in plan['host_tools'].values())
    sdk_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    shared.add(os.path.realpath(sdk_root))
    for path in shared:
        reject_overlap(work, path)
    _guard_lock_root(lock_root, [work, *shared])
    with ContextLocks(sorted(shared), lock_root, shared=True), ContextLocks([work], lock_root):
        result, seed, source, plan = (_json(result_ref), _json(seed_ref), _json(source_ref), _json(plan_ref))
        validate_compiler_seed(seed, inspect_inputs=True)
        validate_source_descriptor(source, inspect_inputs=True)
        validate_audit_plan(plan, result, seed, source, result_ref=result_ref,
                            seed_ref=seed_ref, source_ref=source_ref,
                            producer_acceptance_ref=producer_acceptance_ref, inspect_inputs=True)
        os.mkdir(work)
        os.mkdir(os.path.join(work, 'bin'))
        os.mkdir(os.path.join(work, 'mk'))
        os.mkdir(os.path.join(work, 'tmp'))
        shell = plan['host_tools']['sh']['path']
        fixture = _MAKEFILE.replace(b'@SHELL@', shell.encode('ascii'))
        with open(os.path.join(work, 'Makefile'), 'xb') as handle:
            handle.write(fixture)
        for role, row in plan['host_tools'].items():
            os.symlink(row['path'], os.path.join(work, 'bin', role))
        result_path = os.path.join(work, 'acceptance.json')
        acceptance = dict(format=ACCEPTANCE_FORMAT, version=1, scope=SCOPE,
                          status='RUNNING', accepted_input_scope=SCOPE,
                          ordinary_provider_qualified=False, producer_result=result_ref,
                          seed_descriptor=seed_ref, source_descriptor=source_ref,
                          audit_plan=plan_ref, output_binary=plan['output_binary'],
                          output_sha256=plan['output_sha256'], independent_of_producer_execution=True,
                          commands=[], all_commands_drained=False)
        write_json(result_path, acceptance)
        deadline = time.monotonic() + plan['resources']['runtime_seconds']
        try:
            for step in plan['commands']:
                actual, log = dict(step), os.path.join(work, step['id'] + '.log')
                try:
                    remaining = deadline - time.monotonic()
                    _require(remaining > 0, 'independent bmake audit budget expired')
                    try:
                        returned = run_logged(step['argv'], log, cwd=step['cwd'], env=step['env'],
                                              timeout_seconds=remaining)
                    except CommandError as exc:
                        actual['actual_exit'] = exc.returncode
                        if step['id'] != 'expected-failure' or exc.returncode != step['expected_exit']:
                            raise
                    except ProcessOwnershipError as exc:
                        actual.update(actual_exit=None, error=str(exc), unfinalized_log_path=log)
                        acceptance['commands'].append(actual)
                        acceptance.update(status='HOLD_UNPROVEN_DRAIN', error=str(exc), owned_sid=exc.sid)
                        write_json(result_path, acceptance, exclusive=False)
                        from .bootstrap_pilot import _hold
                        _hold()
                    else:
                        actual['actual_exit'] = returned.returncode
                        _require(actual['actual_exit'] == step['expected_exit'],
                                 'independent bmake command returned an unexpected exit')
                    _verify_step(step, log, work, PORTS_VERSION)
                except BaseException as exc:
                    actual.setdefault('actual_exit', getattr(exc, 'returncode', None))
                    actual['error'] = str(exc)
                    raise
                finally:
                    if acceptance['status'] != 'HOLD_UNPROVEN_DRAIN':
                        if entry(log) is not None:
                            actual['log'] = dict(path=log, sha256=sha256_file(log))
                        acceptance['commands'].append(actual)
                        write_json(result_path, acceptance, exclusive=False)
            acceptance.update(status='PASS', evidence_level='independent-version-and-runtime-behavior')
        except BaseException as exc:
            if acceptance['status'] != 'HOLD_UNPROVEN_DRAIN':
                acceptance.update(status='FAILED', error=str(exc))
        acceptance['all_commands_drained'] = acceptance['status'] != 'HOLD_UNPROVEN_DRAIN'
        if acceptance['all_commands_drained']:
            try:
                validate_compiler_seed(_json(seed_ref), inspect_inputs=True)
                validate_source_descriptor(_json(source_ref), inspect_inputs=True)
                _require(sha256_file(plan['output_binary']) == plan['output_sha256'],
                         'bmake output changed during independent audit')
                acceptance['inputs_revalidated_after_drain'] = True
            except SDKError as exc:
                acceptance.update(status='FAILED', error=str(exc), inputs_revalidated_after_drain=False)
        write_json(result_path, acceptance, exclusive=False)
    acceptance['all_leases_closed'] = True
    return acceptance
