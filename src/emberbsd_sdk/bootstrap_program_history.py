# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Read-only provenance for copied, independently accepted OS bootstrap images."""

import hashlib
import os
from pathlib import Path

from .bootstrap_compat import EFUN_PROGRAM_STAGES
from .bootstrap_program_data import decode_data, data_size
from .errors import SDKError
from .jsonio import json_bytes
from .native_toolset import (MAX_JSON, _absolute, _bytes, _envelope, _file, _gcc, _json,
                             _native_image, _require, _sha, _text)
from .paths import canonical_existing, inside, regular_file

BOOTSTRAP_PROGRAM_HISTORY_API = 1
_SCOPE = 'gcc16-bootstrap'
_CHECKS = ['git-source-membership', 'native-compile-link', 'source-semantics',
           'reported-tool-routes', 'darwin-output-runtime']


def _original_inputs(record):
    # Import here: the seed loader dispatches to this module before program APIs.
    from .bootstrap_binding import seed_commands
    from .bootstrap_program import PROGRAM_STAGES, program_manifest, validate_program_plan
    plan = _json(record['bootstrap_plan'])
    _require(plan.get('stage') in PROGRAM_STAGES, 'history requires a fixed OS program profile')
    result = _json(record['producer_result'])
    _envelope(result, 'emberbsd-native-gcc16-producer-result', scope=_SCOPE)
    _require(type(result.get('api')) is int and result['api'] == 1 and
             result.get('bootstrap_plan') == record['bootstrap_plan'] and
             result.get('status') == 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW' and
             result.get('ordinary_provider_qualified') is False and
             all(result.get(key) is True for key in ('all_commands_drained', 'all_leases_closed',
                                                    'inputs_revalidated_after_drain')),
             'program history needs completed, drained, unqualified production')
    _require(result.get('seed_descriptor') == plan.get('seed_descriptor'), 'historical seed selection mismatch')
    seed = _json(result['seed_descriptor'])
    _envelope(seed, 'emberbsd-bootstrap-seed', scope=_SCOPE)
    _gcc(seed['compiler'], seed['host'])
    accepted = _json(result['accepted_seed_receipt'])
    _envelope(accepted, 'emberbsd-bootstrap-seed-acceptance', scope=_SCOPE)
    _require(accepted.get('status') == 'PASS' and accepted.get('accepted_input_scope') == 'bootstrap-seed' and
             accepted.get('descriptor_sha256') == result['seed_descriptor']['sha256'] and
             accepted.get('bootstrap_plan') == record['bootstrap_plan'],
             'program history lacks its original scoped input acceptance')
    for key in ('host', 'compiler', 'sources', 'providers', 'host_abi', 'abi_flags',
                'history', 'dynamic_dependencies', 'driver_prefix', 'driver_binding', 'make_binding'):
        _require(accepted.get(key) == seed.get(key), 'historical seed acceptance differs: ' + key)
    from .bootstrap_seed import _history
    unknowns = _history(seed['history'], seed['providers'], descriptor=seed, inspect_inputs=False)
    _require(accepted.get('accepted_history_unknowns') == unknowns,
             'original seed acceptance differs from its complete historical unknowns')
    # Do not load the old seed, its inventory, executables or current ABI roots.
    # These APIs reconstruct tuples and check the retained source snapshot only.
    root = _absolute(seed['bundle_root'])
    source_root = _absolute(plan['program']['source']['root'])
    _require(os.path.commonpath((root, source_root)) not in (root, source_root),
             'historical program source snapshot must be separate from its retired seed')
    commands = seed_commands(seed, inspect_inputs=False)
    validate_program_plan(plan, seed, commands, seed['abi_flags'])
    _require(record['source_proof'] == plan['program']['source']['source_proof'] and
             record['manifest'] == plan['program']['manifest'], 'program history source references differ')
    return plan, result, program_manifest(plan)


def _streams(actual, case, work, outputs):
    for stream in ('stdout', 'stderr'):
        reference = actual[stream]
        path = os.path.join(work, case['name'] + '.' + stream)
        _require(reference['path'] == path and outputs.get(os.path.basename(path)) == reference['sha256'],
                 'historical semantic stream differs from the producer output roster')
        data = _bytes(reference, limit=max(MAX_JSON, data_size(case[stream]))
                      if stream in case else MAX_JSON)
        if stream == 'stderr' and case.get('stderr_nonempty') is True:
            _require(bool(data), 'historical error case has empty stderr')
        elif stream == 'stdout' and 'stdout' not in case:
            # The original independent review checked this dynamic OS oracle.
            # Retain its exact bytes. Never recompute facts on the current host.
            pass
        else:
            _require(data == decode_data(case[stream]), 'historical source-owned semantic output differs')


def _commands(plan, result, manifest):
    from .bootstrap_program import program_outputs, program_sources, program_step_exits
    expected, actual = plan['commands'], result['commands']
    _require(isinstance(actual, list) and len(actual) == len(expected), 'historical command roster is incomplete')
    outputs = result['outputs']
    _require(isinstance(outputs, dict) and set(outputs) <= set(program_outputs(plan)),
             'historical output hashes exceed the finite producer roster')
    for name, sha in outputs.items():
        _require(isinstance(name, str) and Path(name).name == name, 'invalid historical output name')
        _sha(sha)
    for name, raw in program_sources(plan).items():
        _require(outputs.get(name) == hashlib.sha256(raw).hexdigest(),
                 'historical created source or fixture differs from its owner bytes')
    cases = {'case-' + case['name']: case for case in manifest['runtime_cases']}
    for wanted, row in zip(expected, actual):
        _require(json_bytes({key: row.get(key) for key in wanted}) == json_bytes(wanted),
                 'historical command differs from its finite plan')
        status = row.get('actual_exit')
        _require(type(status) is int and status in program_step_exits(wanted, plan) and 'error' not in row,
                 'historical command lacks its permitted actual exit')
        _require('unfinalized_log_path' not in row and
                 (wanted['phase'] == 'program-case' or not {'stdout', 'stderr'} & set(row)),
                 'historical command must retain its drained combined log')
        _require(row['log']['path'] == os.path.join(plan['work_root'], wanted['id'] + '.log'),
                 'historical command log path differs')
        _file(row['log'])
        if wanted['phase'] in ('compile', 'link', 'compat-probe-compile', 'compat-probe-link') and status == 0:
            output = row['output']
            _require(output['path'] == wanted['argv'][-1] and
                     outputs.get(os.path.basename(output['path'])) == _sha(output['sha256']),
                     'historical compiler output differs from the recorded SHA256 roster')
        if wanted['id'] in cases:
            _streams(row, cases[wanted['id']], plan['work_root'], outputs)
    aliases = {name: manifest['name'] for name in manifest['invocations'] if name != manifest['name']}
    if aliases:
        link = next(row for row in actual if row['phase'] == 'link')
        _require(link.get('invocation_aliases') == aliases,
                 'historical program invocation aliases differ')
    if plan['stage'] in EFUN_PROGRAM_STAGES:
        from .bootstrap_compat import compat_manifest, configuration_bytes
        contract = compat_manifest(plan, manifest)
        final = next(row for row in actual if row['id'] == 'compat-' + contract['probes'][-1]['name'])
        reference = final['configuration_header']
        _require(reference['path'] == os.path.join(plan['work_root'], 'nbtool_config.h') and
                 outputs.get('nbtool_config.h') == reference['sha256'] and
                 _bytes(reference) == configuration_bytes(actual, contract),
                 'historical efun configuration differs from its actual observations')
    image = os.path.join(plan['work_root'], manifest['name'])
    link = next(row for row in actual if row['id'] == 'link-' + manifest['name'])
    _require(link['output'] == dict(path=image, sha256=outputs[manifest['name']]),
             'historical program image differs from its successful link output')
    return link['output']


def _acceptance(reference, record, plan, result, image):
    from .darwin_runtime import DARWIN_RUNTIME_AUDIT_API, darwin_runtime_closure
    accepted = _json(reference)
    _require(set(accepted) == {'format', 'version', 'scope', 'status', 'accepted_input_scope',
        'ordinary_provider_qualified', 'bootstrap_plan', 'producer_result', 'source_proof',
        'checks', 'limitations', 'semantic_review', 'runtime_audit', 'runtime_closure', 'artifact'},
        'unsupported independent program output acceptance fields')
    _envelope(accepted, 'emberbsd-bootstrap-program-output-acceptance', scope=_SCOPE)
    _require(accepted['status'] == 'PASS' and accepted['accepted_input_scope'] == 'bootstrap-seed' and
             accepted['ordinary_provider_qualified'] is False and accepted['checks'] == _CHECKS and
             accepted['bootstrap_plan'] == record['bootstrap_plan'] and
             accepted['producer_result'] == record['producer_result'] and
             accepted['source_proof'] == record['source_proof'] and accepted['artifact'] == image,
             'independent program acceptance does not bind this exact production')
    _require(isinstance(accepted['limitations'], list) and accepted['limitations'],
             'bootstrap output acceptance must retain its qualification limits')
    for text in accepted['limitations']:
        _text(text, 'bootstrap output qualification limit')
    _require(bool(_bytes(accepted['semantic_review'])), 'historical independent semantic review is empty')
    audit = _json(accepted['runtime_audit'])
    _envelope(audit, 'emberbsd-darwin-output-runtime-audit', scope='producer-output')
    nodes = audit['nodes']
    _require(type(audit.get('api')) is int and audit['api'] == DARWIN_RUNTIME_AUDIT_API and
             audit['host'] == plan['host'] and isinstance(nodes, dict) and 0 < len(nodes) <= 8192 and
             image in audit['output_runtime']['outputs'], 'historical runtime audit omits the program output')
    node = nodes.get(image['path'], {})
    _require(node.get('kind') == 'output-executable' and node.get('sha256') == image['sha256'],
             'historical runtime image differs from the accepted artifact')
    for path, value in nodes.items():
        _absolute(path)
        _require(value['kind'] in ('file', 'cache-image', 'output-executable'), 'unknown historical runtime node kind')
        if value['kind'] != 'cache-image':
            _sha(value['sha256'])
        for edge in value['image']['loads'] + value['image']['implicit_loads']:
            _require(edge['target'] is None or edge['target'] in nodes, 'historical runtime edge is incomplete')
    _require(_json(accepted['runtime_closure']) == darwin_runtime_closure(audit, accepted['runtime_audit']),
             'historical runtime closure differs from its audit')
    observed = _json(audit['output_runtime']['observed_cache_roots'])
    _envelope(observed, 'emberbsd-darwin-observed-cache-roots', scope='producer-output')
    rows = [row for row in observed['observations'] if row['output'] == image]
    _require(observed['cache_uuid'] == audit['cache']['uuid'] and len(rows) == 1 and
             rows[0]['trace'] == result['commands'][-1]['log'],
             'historical runtime observation differs from the actual producer trace')
    _file(rows[0]['trace'])


def verify_program_build_history(history, provider_name, descriptor, *, inspect_inputs=True):
    """Check caller-accepted bootstrap history. Never copy or qualify an image.

    The trusted, independently pinned next seed acceptance binds this history.
    An acceptance advertised by an untrusted payload is not an admission gate.
    Retain source snapshots and evidence. Retired seed payloads are not reopened.
    """
    try:
        _require(isinstance(descriptor, dict) and set(history) ==
            {'kind', 'source_archives', 'patches', 'program_build', 'provisioned_members'} and
            history['kind'] == 'source-build' and history['source_archives'] == [] and history['patches'] == [],
            'unsupported Git-program source-build history')
        record = _json(history['program_build'])
        _envelope(record, 'emberbsd-bootstrap-program-build-history', scope=_SCOPE)
        _require(set(record) == {'format', 'version', 'scope', 'manifest', 'source_proof',
            'bootstrap_plan', 'producer_result', 'output_acceptance'}, 'unsupported program build history fields')
        plan, result, manifest = _original_inputs(record)
        provider = descriptor['providers'][provider_name]
        source = plan['program']['source']['source']
        _require(set(provider) == {'origin', 'source', 'recipe', 'recipe_sha256'} and
                 provider['origin'] == 'source-build' and descriptor['sources'][provider['source']] == source and
                 provider['recipe'] == 'tools/' + manifest['name'] + '/bootstrap.json' and
                 provider['recipe_sha256'] == record['manifest']['sha256'] and descriptor['host'] == plan['host'],
                 'program provider differs from its original OS source and manifest')
        image = _commands(plan, result, manifest)
        _acceptance(record['output_acceptance'], record, plan, result, image)
        roles = {role: row for role, row in descriptor['tools'].items() if row['provider'] == provider_name}
        members = history['provisioned_members']
        _require(roles and set(roles) <= set(manifest['invocations']) and isinstance(members, list) and
                 len(members) == len(roles), 'program provider has unsupported or incomplete invocation roles')
        root, seen = _absolute(descriptor['bundle_root']), set()
        for member in members:
            _require(set(member) == {'invocation', 'original_path', 'selected_path', 'sha256'},
                     'unsupported provisioned program member fields')
            role = member['invocation']
            _require(role in roles and role not in seen, 'duplicate or unsupported program invocation')
            seen.add(role)
            selected, row = _absolute(member['selected_path']), roles[role]
            _require(selected != root and os.path.commonpath((selected, root)) == root and
                     os.path.basename(selected) == role and member['original_path'] == image['path'] and
                     member['sha256'] == image['sha256'] == row['sha256'] and row['path'] == selected and
                     row['kind'] == 'native' and row['version'] == 'os-' + source['commit'],
                     'provisioned program differs from its accepted image or selected spelling')
            if inspect_inputs:
                regular_file(selected)
                _require(inside(canonical_existing(selected), root), 'program member escapes its bundle')
                _file(dict(path=selected, sha256=member['sha256']))
                _require(os.access(selected, os.X_OK), 'program member is not executable')
                _native_image(selected, descriptor['host'])
        return []
    except (OSError, KeyError, TypeError, ValueError, AttributeError, StopIteration) as exc:
        raise SDKError(f'invalid Git-program bootstrap history: {exc}') from exc
