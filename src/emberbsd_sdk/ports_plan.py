# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD; AI-assisted immutable Ports planning contracts.
"""Plan explicit package graphs. Never resolve versions or execute producers."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

from .errors import SDKError
from .inventory import sha256_file
from .jsonio import json_bytes, read_json, write_json

PORTS_PLAN_API = 1
PORTS_JSON_LIMIT = 64 * 1024 * 1024


def _fields(value, names, where):
    if not isinstance(value, dict) or set(value) != set(names.split()):
        raise SDKError(f'{where}: expected fields {names}')


def _text(value, where):
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        raise SDKError(f'{where}: expected nonempty text without control characters')
    return value


def _sha(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise SDKError('expected a lowercase SHA256')
    return value


def _list(value, where):
    if not isinstance(value, list):
        raise SDKError(f'{where}: expected a list')
    return value


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _sealed(value, field):
    return dict(value, **{field: _hash(value)})


def _waves(nodes, selected=None):
    """Kahn traversal. It also detects cycles without recursive Python calls."""
    selected = set(nodes) if selected is None else set(selected)
    remaining = {name: {dep['id'] for dep in nodes[name]['dependencies']} & selected
                 for name in selected}
    reverse = {}
    for name, dependencies in remaining.items():
        for dependency in dependencies:
            reverse.setdefault(dependency, set()).add(name)
    ready = sorted(name for name, deps in remaining.items() if not deps)
    waves = []
    while ready:
        waves.append(ready)
        next_ready = set()
        for name in ready:
            del remaining[name]
            for consumer in reverse.get(name, ()):
                remaining[consumer].remove(name)
                if not remaining[consumer]:
                    next_ready.add(consumer)
        ready = sorted(next_ready)
    if remaining:
        raise SDKError('dependency cycle: ' + ', '.join(sorted(remaining)))
    return waves


def lock_graph(graph):
    """Validate and normalize an evaluated graph, then compute transitive keys."""
    _fields(graph, 'schema context provenance nodes', 'graph')
    if graph['schema'] != 'ports-graph-v1':
        raise SDKError('unsupported graph schema')
    context = graph['context']
    _fields(context, 'target triplet toolchain_sha256 base_sha256 shared_inputs', 'context')
    for key in ('target', 'triplet'):
        _text(context[key], key)
    for key in ('toolchain_sha256', 'base_sha256'):
        _sha(context[key])
    if not isinstance(context['shared_inputs'], dict):
        raise SDKError('shared_inputs: expected a hash map')
    for name, digest in context['shared_inputs'].items():
        _text(name, 'shared input name')
        _sha(digest)
    _fields(graph['provenance'], 'source revision', 'provenance')
    for name, value in graph['provenance'].items():
        _text(value, name)
    nodes = {}
    for node in _list(graph['nodes'], 'nodes'):
        _fields(node, 'id version input_sha256 dependencies', 'node')
        name = _text(node['id'], 'node id')
        _text(node['version'], 'version')
        _sha(node['input_sha256'])
        if name in nodes:
            raise SDKError(f'duplicate node: {name}')
        edges = set()
        for dep in _list(node['dependencies'], 'dependencies'):
            _fields(dep, 'id kind', 'dependency')
            _text(dep['id'], 'dependency id')
            if dep['kind'] not in ('build', 'tool', 'runtime', 'test'):
                raise SDKError('unsupported dependency kind')
            edge = (dep['id'], dep['kind'])
            if edge in edges:
                raise SDKError(f'duplicate dependency: {name} -> {edge}')
            edges.add(edge)
        nodes[name] = dict(node, dependencies=[dict(id=n, kind=k) for n, k in sorted(edges)])
    for name, node in nodes.items():
        for dep in node['dependencies']:
            if dep['id'] not in nodes:
                raise SDKError(f'missing dependency: {name} -> {dep["id"]}')
    keys = {}
    for wave in _waves(nodes):
        for name in wave:
            node = nodes[name]
            dependencies = [dict(dep, input_key=keys[dep['id']]) for dep in node['dependencies']]
            keys[name] = _hash(dict(node, context=context, dependencies=dependencies))
    # Round-trip to detach mutable caller-owned values and sort all map keys.
    normalized = json.loads(json.dumps(dict(graph, nodes=[nodes[n] for n in sorted(nodes)]), sort_keys=True))
    return _sealed(dict(schema='ports-lock-v1', graph=normalized,
                        input_keys=dict(sorted(keys.items()))), 'lock_sha256')


def validate_lock(value):
    _fields(value, 'schema graph input_keys lock_sha256', 'lock')
    if value != lock_graph(value['graph']):
        raise SDKError('lock schema, input keys or hash do not match graph')
    return value


def make_plan(baseline, candidate):
    """Explain changes and retain both immutable snapshots for later validation."""
    validate_lock(baseline)
    validate_lock(candidate)
    old = {node['id']: node for node in baseline['graph']['nodes']}
    new = {node['id']: node for node in candidate['graph']['nodes']}
    context_changed = sorted(key for key in candidate['graph']['context']
                             if baseline['graph']['context'][key] != candidate['graph']['context'][key])
    added, removed = sorted(new.keys() - old.keys()), sorted(old.keys() - new.keys())
    changed = sorted(name for name in new.keys() & old.keys() if new[name] != old[name])
    seeds = set(added + removed + changed)
    if context_changed:
        seeds.update(new)
    reverse = {}
    for nodes in (old, new):
        for name, node in nodes.items():
            for dep in node['dependencies']:
                reverse.setdefault(dep['id'], set()).add(name)
    impacted = set(seeds)
    pending = list(seeds)
    while pending:
        for name in reverse.get(pending.pop(), ()):
            if name not in impacted:
                impacted.add(name)
                pending.append(name)
    impacted.intersection_update(new)
    reasons = {}
    for name in sorted(impacted):
        reasons[name] = []
        if name in added:
            reasons[name].append('new')
        if name in changed:
            reasons[name].extend('changed:' + field for field in ('version', 'input_sha256', 'dependencies')
                                 if old[name][field] != new[name][field])
        reasons[name].extend('context:' + field for field in context_changed)
        if not reasons[name]:
            reasons[name].append('reverse-dependency')
    return _sealed(dict(schema='ports-plan-v1', baseline=baseline, candidate=candidate,
                        context_changed=context_changed, new=added, removed=removed,
                        changed=changed, impacted=sorted(impacted), reasons=reasons,
                        waves=_waves(new, impacted)), 'plan_sha256')


def validate_plan(plan):
    _fields(plan, 'schema baseline candidate context_changed new removed changed impacted reasons waves plan_sha256', 'plan')
    if plan != make_plan(plan['baseline'], plan['candidate']):
        raise SDKError('plan schema, changes or hash do not match snapshots')
    return plan


def _dependencies(node, keys):
    return {dep['id']: keys[dep['id']] for dep in node['dependencies']}


def _file_evidence(path):
    path = Path(_text(str(path), 'evidence path'))
    if not path.is_absolute():
        raise SDKError('evidence path must be absolute')
    return dict(path=str(path), sha256=sha256_file(path))


def record_result(plan, name, outcome, log, artifacts=()):
    """Record an explicit producer outcome; never infer it from output files."""
    validate_plan(plan)
    nodes = {n['id']: n for n in plan['candidate']['graph']['nodes']}
    _text(name, 'result node')
    if name not in nodes:
        raise SDKError(f'unknown result node: {name}')
    if outcome not in ('success', 'failure'):
        raise SDKError('unsupported result outcome')
    if (outcome == 'success' and not artifacts) or (outcome == 'failure' and artifacts):
        raise SDKError('success requires artifacts; failure forbids artifacts')
    files = [_file_evidence(path) for path in artifacts]
    if len({item['path'] for item in files}) != len(files):
        raise SDKError('duplicate artifact path')
    keys = plan['candidate']['input_keys']
    return dict(schema='ports-result-v1', plan_sha256=plan['plan_sha256'], id=name,
                input_key=keys[name], dependency_input_keys=_dependencies(nodes[name], keys),
                outcome=outcome, log=_file_evidence(log), artifacts=files)


def plan_status(plan, receipts):
    """Verify current artifact bytes and receipt scope. Return actionable nodes.

    Receipts are explicit local evidence, not authenticated attestations. Success
    means artifact integrity after a producer run, never target acceptance.
    """
    validate_plan(plan)
    nodes = {n['id']: n for n in plan['candidate']['graph']['nodes']}
    keys = plan['candidate']['input_keys']
    evidence = {}
    for receipt in receipts:
        _fields(receipt, 'schema plan_sha256 id input_key dependency_input_keys outcome log artifacts', 'receipt')
        if receipt['schema'] != 'ports-result-v1' or receipt['plan_sha256'] != plan['plan_sha256']:
            raise SDKError('stale receipt or unsupported receipt schema')
        name = _text(receipt['id'], 'receipt id')
        if name not in nodes or name in evidence:
            raise SDKError(f'unknown or duplicate receipt node: {name}')
        if receipt['input_key'] != keys[name] or receipt['dependency_input_keys'] != _dependencies(nodes[name], keys):
            raise SDKError(f'stale receipt input keys: {name}')
        if receipt['outcome'] not in ('success', 'failure'):
            raise SDKError('unsupported receipt outcome')
        artifacts = _list(receipt['artifacts'], 'artifacts')
        if receipt['outcome'] == 'failure' and artifacts:
            raise SDKError('failure receipt cannot authenticate artifacts')
        if receipt['outcome'] == 'success' and not artifacts:
            raise SDKError(f'success receipt has no artifacts: {name}')
        seen = set()
        _fields(receipt['log'], 'path sha256', 'log')
        log = receipt['log']
        if _file_evidence(log['path'])['sha256'] != _sha(log['sha256']):
            raise SDKError(f'log hash mismatch: {name}')
        for artifact in artifacts:
            _fields(artifact, 'path sha256', 'artifact')
            path = Path(_text(artifact['path'], 'artifact path'))
            if not path.is_absolute() or str(path) in seen:
                raise SDKError('artifact path must be absolute and unique')
            seen.add(str(path))
            if sha256_file(path) != _sha(artifact['sha256']):
                raise SDKError(f'artifact hash mismatch: {path}')
        evidence[name] = receipt['outcome']
    accepted = set()
    for wave in _waves(nodes):
        for name in wave:
            if evidence.get(name) == 'success':
                missing = set(_dependencies(nodes[name], keys)) - accepted
                if missing:
                    raise SDKError(f'success receipt has unverified or failed dependencies: {name}: {sorted(missing)}')
                accepted.add(name)
    failed = sorted(name for name in plan['impacted'] if evidence.get(name) == 'failure')
    ready, blocked = [], {}
    for name in plan['impacted']:
        if name in accepted or name in failed:
            continue
        missing = sorted(set(_dependencies(nodes[name], keys)) - accepted)
        if missing:
            blocked[name] = missing
        else:
            ready.append(name)
    unchanged_needed = set()
    pending = list(plan['impacted'])
    visited = set(pending)
    while pending:
        for dep in _dependencies(nodes[pending.pop()], keys):
            if dep not in visited:
                visited.add(dep)
                pending.append(dep)
                if dep not in plan['impacted'] and dep not in accepted:
                    unchanged_needed.add(dep)
    return dict(schema='ports-status-v1', plan_sha256=plan['plan_sha256'],
                accepted=sorted(accepted), failed=failed, ready=ready, blocked=blocked,
                required_evidence=sorted(unchanged_needed), removed=plan['removed'],
                complete=set(plan['impacted']) <= accepted, acceptance='artifact-integrity-only')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    lock = commands.add_parser('lock', help='validate an evaluated graph and create a new lock')
    lock.add_argument('graph')
    lock.add_argument('--output', required=True)
    plan = commands.add_parser('plan', help='compare two locks and create a new plan')
    plan.add_argument('baseline')
    plan.add_argument('candidate')
    plan.add_argument('--output', required=True)
    record = commands.add_parser('record', help='record an explicit outcome and hash its evidence')
    record.add_argument('plan')
    record.add_argument('node')
    record.add_argument('--outcome', choices=('success', 'failure'), required=True)
    record.add_argument('--log', required=True)
    record.add_argument('--artifact', action='append', default=[])
    record.add_argument('--output', required=True)
    status = commands.add_parser('status', help='verify explicit receipts and report ready work')
    status.add_argument('plan')
    status.add_argument('--receipt', action='append', default=[])
    args = parser.parse_args(argv)
    try:
        if args.command == 'lock':
            value = lock_graph(read_json(args.graph, max_bytes=PORTS_JSON_LIMIT))
        elif args.command == 'plan':
            value = make_plan(read_json(args.baseline, max_bytes=PORTS_JSON_LIMIT),
                              read_json(args.candidate, max_bytes=PORTS_JSON_LIMIT))
        elif args.command == 'record':
            value = record_result(read_json(args.plan, max_bytes=PORTS_JSON_LIMIT), args.node, args.outcome, args.log, args.artifact)
        else:
            value = plan_status(read_json(args.plan, max_bytes=PORTS_JSON_LIMIT), [read_json(p, max_bytes=PORTS_JSON_LIMIT) for p in args.receipt])
        if args.command != 'status':
            if len(json_bytes(value)) > PORTS_JSON_LIMIT:
                raise SDKError('output exceeds the JSON size limit; reduce graph scope')
            write_json(args.output, value)
        else:
            print(json.dumps(value, indent=2))
        return 0
    except (SDKError, OSError, ValueError) as exc:
        print(f'ports plan: {exc}', file=sys.stderr)
        return 1
