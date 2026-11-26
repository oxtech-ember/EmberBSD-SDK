# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Explicit maintainer publication using existing Ports and release mechanisms."""

from copy import deepcopy
from functools import cache
from graphlib import TopologicalSorter
import hashlib
from pathlib import Path
import re
import shutil
import time

from .binary_catalog import node_root, runtime_projection
from .binary_release import compose, load_catalogs, _matches, _statistics
from .errors import SDKError
from .jsonio import json_bytes, read_json, write_json
from .package_cache import directory_lock
from .package_selection import _parse_root
from .paths import real_directory
from .release_catalog import MAX_ENVELOPE, parse_channel, parse_release
from .release_delivery import _key, fetch_release, publish_release, release_snapshot
from .release_signing import read_bytes


def packages_from_state(spec, state, packages, *, selected_roots=(), allow_empty=False, graph=None):
    """Map successful Ports results to an existing producer graph, without solving."""
    spec = deepcopy(spec)
    if len(spec['repositories']) != 1:
        raise SDKError('--state requires exactly one selected repository in the package-build input')
    repo = spec['repositories'][0]
    role = 'host' if repo['host'] is not None else 'target'
    selected = {_parse_root(root, '--state-root') for root in selected_roots}
    if any(root.endswith('@native') != (role == 'host') for root in selected):
        raise SDKError('selected state root has a different repository role')
    if graph is None:
        graph = read_json(repo['graph'])
    by_root = {}
    for node in graph['nodes']:
        if node['id'].startswith(role + ':'):
            by_root.setdefault(node_root(node['id']), []).append(node)
    state, packages = Path(state), Path(packages).absolute()
    planned, completed = {}, {}
    for name, result in (('packages', planned), ('done', completed)):
        for line in (state / name).read_text(encoding='utf-8').splitlines():
            fields = line.split()
            if not fields:
                continue
            root = fields[0]
            # Cross-bulk STATE can contain successful results for both roles.
            # Ignore the other role before interpreting its row payload.
            if root.endswith('@native') != (role == 'host') or (selected and root not in selected):
                continue
            if ((name == 'packages' and len(fields) != 2) or
                    (name == 'done' and len(fields) not in (2, 3))):
                raise SDKError('invalid Ports state row: ' + name)
            root, pkgname = fields[:2]
            _parse_root(root, 'Ports state')
            if (name == 'packages' and root in result) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+\-]*-[0-9][A-Za-z0-9._+]*', pkgname):
                raise SDKError('invalid or duplicate package in Ports state: ' + root)
            if name == 'done' and len(fields) == 3 and not (role == 'host' and fields[2] == 'native'):
                if not re.fullmatch('[0-9a-f]{64}', fields[2]):
                    raise SDKError('invalid successful package SHA256: ' + root)
            if name == 'done' and len(fields) == 2:
                # A successful install row without an archive checksum is not
                # publishable and supersedes any older archived result.
                result[root] = [pkgname, None]
            else:
                # The latest result can also be an installed-only native tool.
                result[root] = fields[1:]
    excluded = set()
    for name in ('failed', 'blocked', 'skipped'):
        path = state / name
        if path.exists():
            excluded.update(line.split()[0] for line in path.read_text(encoding='utf-8').splitlines() if line.split())
    paths, checksums, roots = {}, {}, []
    for root, (pkgname,) in sorted(planned.items()):
        # The shared STATE also contains successful packages from other tasks.
        # This publication is bounded by its existing producer graph.
        if root in excluded or root not in by_root:
            continue
        done = completed.get(root)
        if not done or done[0] != pkgname or done[1] is None or done[1] == 'native':
            continue
        nodes = by_root[root]
        if len(nodes) != 1 or nodes[0]['version'] != pkgname.rsplit('-', 1)[-1]:
            raise SDKError('successful Ports result is missing or differs from graph: ' + root)
        path = str(packages / (pkgname + '.tgz'))
        paths[nodes[0]['id']] = path
        checksums[path] = done[1]
        roots.append(root)
    missing = selected.difference(roots)
    if missing:
        raise SDKError('no successful archive receipt for selected state roots: ' + ', '.join(sorted(missing)))
    if not roots and not allow_empty:
        raise SDKError('no successful packages of the selected role in Ports state')
    repo['packages'] = paths
    if selected:
        repo['sets'] = []
        repo['roots'] = sorted(selected)
    else:
        repo['roots'] = sorted(set(repo.get('roots', [])) | set(roots))
    return spec, checksums


def publication_candidates(crypto, spec, state, packages, current, trust, *, ready=False, held=()):
    """Compare completed receipts with an authenticated catalog without opening archives."""
    graph = read_json(spec['repositories'][0]['graph']) if ready and len(spec['repositories']) == 1 else None
    spec, _ = packages_from_state(spec, state, packages, allow_empty=True, graph=graph)
    repo = spec['repositories'][0]
    release, (catalog,) = load_catalogs(crypto, current / 'release.p7m', current, trust, [repo['id']])
    summary = next(item for item in release['artifacts'] if item['id'] == catalog['summary'])
    if (repo['host'], repo['target']) != (summary['host'], summary['target']):
        raise SDKError('current repository platform differs from producer: ' + repo['id'])
    published = {entry['root']: entry['pkgname'] for entry in catalog['packages']}
    missing, changed = [], []
    for identity, path in sorted(repo['packages'].items()):
        root = node_root(identity)
        name = Path(path).name.removesuffix('.tgz')
        if root not in published:
            missing.append(root)
        elif published[root] != name:
            changed.append(dict(root=root, current=published[root], built=name))
    result = dict(release_id=release['release_id'], missing=missing, changed=changed)
    if ready:
        excluded = {_parse_root(root, 'held publication root') for root in held}
        changed_roots = set(missing).union(item['root'] for item in changed) - excluded
        names = {identity: Path(path).name.removesuffix('.tgz') for identity, path in repo['packages'].items()
                 if node_root(identity) in changed_roots}
        role = 'host' if repo['host'] is not None else 'target'
        batch, waiting = _ready_roots(crypto, graph, names, published, excluded, role)
        result.update(ready=batch, waiting=waiting)
    return result


def _ready_roots(crypto, graph, names, published, held, role):
    if not names:
        return [], {}
    projection = runtime_projection(graph, names, role)
    by_id = {node['id']: node for node in graph['nodes']}
    edges = {row['root']: {node_root(edge['id']): edge.get('patterns', ())
                           for edge in by_id[identity]['dependencies'] if edge['kind'] == 'runtime'}
             for identity, row in projection.items()}
    candidates = {node_root(identity): name for identity, name in names.items()}
    available = dict(published)
    excluded = set(held)
    batch, waiting = [], {}

    @cache
    def matches(pattern, name):
        return _matches(crypto, pattern, name)

    for root in TopologicalSorter(edges).static_order():
        requirements = edges[root]
        if root in excluded or excluded.intersection(requirements):
            excluded.add(root)
            available.pop(root, None)
        if root not in candidates:
            continue
        absent = sorted(provider for provider, patterns in requirements.items()
                        if provider not in available or
                        not all(matches(pattern, available[provider]) for pattern in patterns))
        if absent:
            waiting[root] = absent
            available.pop(root, None)
        else:
            batch.append(root)
            available[root] = candidates[root]
    return batch, waiting


def update_packages(crypto, store, public, spec, ports_root, workdir, identity, trust,
                    *, prefix='', ttl=7 * 86400, checksums=None, forbidden_prefixes=(), expected_parent=None):
    """Fetch, merge, seal and publish; resume a prepared release without signing again.

    A prepared operation is frozen until success. Use a new release_id for a
    new batch. Ordinary installers never invoke this function.
    """
    workdir = Path(workdir).absolute()
    workdir.mkdir(parents=True, exist_ok=True, mode=0o700)
    real_directory(workdir)
    with directory_lock(workdir / 'update.lock'):
        snapshot, output = workdir / 'snapshot', workdir / 'release'
        # Check before any cleanup, including aliases through symlink parents.
        for repo in spec['repositories']:
            for value in [repo['graph'], *repo['packages'].values()]:
                if any(Path(value).resolve().is_relative_to(path.resolve()) for path in (snapshot, output)):
                    raise SDKError('publication inputs must not be inside operation outputs')
        request = dict(schema='emberbsd.package-publication.v1', build=spec,
                       ports_root=str(Path(ports_root).absolute()), prefix=prefix,
                       checksums=checksums or {})
        if expected_parent is not None:
            request['expected_parent'] = expected_parent
        if forbidden_prefixes:
            request['forbidden_prefixes'] = sorted(set(map(str, forbidden_prefixes)))
        request_path, result_path = workdir / 'request.json', workdir / 'result.json'
        previous_request = read_json(request_path) if request_path.exists() else None
        if previous_request is not None:
            if not isinstance(previous_request, dict) or previous_request.get('schema') != request['schema']:
                raise SDKError('workdir does not belong to the package publisher')
        elif any(path.exists() or path.is_symlink() for path in (snapshot, output, result_path)):
            raise SDKError('choose an empty publication workdir; existing outputs are not owned by this command')
        if result_path.exists():
            # Successful output is disposable; interrupted output stays for retry.
            for path in (snapshot, output):
                if path.exists():
                    real_directory(path)
                    shutil.rmtree(path)
            request_path.unlink(missing_ok=True)
            result_path.unlink()
        elif previous_request is not None and previous_request != request:
            if (output / 'release.p7m').exists():
                raise SDKError('unfinished publication has different inputs; resume the original batch first')
            request_path.unlink()
        if not request_path.exists():
            write_json(request_path, request)
        if not (output / 'release.p7m').is_file():
            if snapshot.exists():
                real_directory(snapshot)
                shutil.rmtree(snapshot)
            previous = fetch_release(crypto, public, trust, snapshot, prefix=prefix)
            if expected_parent is not None and hashlib.sha256(read_bytes(snapshot / 'current.p7m')).hexdigest() != expected_parent:
                raise SDKError('current changed since the completed batch was selected')
            channel = parse_channel(crypto.verify_cms(read_bytes(snapshot / 'current.p7m'), trust), 'current')
            statistics = {}
            release = compose(crypto, spec, ports_root, output, identity, trust, current=snapshot,
                              additions=True, statistics=statistics, checksums=checksums,
                              forbidden_prefixes=forbidden_prefixes)
            if not (output / 'release.p7m').is_file():
                result = dict(channel=channel, statistics=statistics)
                write_json(result_path, result, exclusive=False)
                return result
        else:
            previous = parse_release(crypto.verify_cms(read_bytes(snapshot / 'release.p7m'), trust))
            release = parse_release(crypto.verify_cms(read_bytes(output / 'release.p7m'), trust))
            statistics = _statistics(previous, release)
        expected_current = hashlib.sha256(read_bytes(snapshot / 'current.p7m')).hexdigest()
        channel = publish_release(crypto, store, public, output / 'release.p7m', identity, trust,
                                  prefix=prefix, ttl=ttl, expected_current=expected_current,
                                  reuse_current=True)
        result = dict(channel=channel, statistics=statistics)
        write_json(result_path, result, exclusive=False)
        return result


def _completed_batch(crypto, store, public, spec, ports_root, workdir, identity, trust,
                     state, packages, held, prefix, ttl, forbidden_prefixes):
    """Select against signed current and retry a displaced selection from a new parent."""
    key = _key(prefix, 'current.p7m')
    for attempt in range(3):
        with release_snapshot(crypto, public, trust, workdir, prefix=prefix,
                              repositories=[spec['repositories'][0]['id']]) as (_, snapshot):
            parent = hashlib.sha256(read_bytes(snapshot / 'current.p7m')).hexdigest()
            selection = publication_candidates(crypto, spec, state, packages, snapshot, trust,
                                               ready=True, held=held)
        roots = selection['ready']
        if not roots:
            return dict(parent=parent, roots=[], waiting=selection['waiting'])
        batch, checksums = packages_from_state(spec, state, packages, selected_roots=roots)
        fingerprint = hashlib.sha256(json_bytes([parent, spec, checksums])).hexdigest()
        batch['release_id'] = spec['release_id'] + '-' + fingerprint[:20]
        try:
            result = update_packages(crypto, store, public, batch, ports_root, workdir / fingerprint,
                identity, trust, prefix=prefix, ttl=ttl, checksums=checksums,
                forbidden_prefixes=forbidden_prefixes, expected_parent=parent)
        except SDKError:
            current, _ = public.read(key, MAX_ENVELOPE, headers={'cache-control': 'no-cache'})
            if attempt == 2 or hashlib.sha256(current).hexdigest() == parent:
                raise
            continue
        return dict(parent=parent, release_id=batch['release_id'], roots=roots,
                    waiting=selection['waiting'], statistics=result['statistics'])


def follow_completions(crypto, store, public, spec, ports_root, workdir, identity, trust,
                       state, packages, *, held=(), prefix='', ttl=7 * 86400, forbidden_prefixes=(),
                       interval=30):
    """Yield release results while this Ports build owns its build.lock."""
    if interval < 1 or len(spec['repositories']) != 1:
        raise SDKError('follow requires a positive interval and one producer repository')
    workdir, state = Path(workdir).absolute(), Path(state)
    workdir.mkdir(mode=0o700, parents=True, exist_ok=True)
    real_directory(workdir)
    graph = Path(spec['repositories'][0]['graph'])
    source = graph.stat()
    lock = state / 'build.lock'

    def owner():
        try:
            entry = lock.stat()
        except FileNotFoundError:
            return None
        return entry.st_dev, entry.st_ino

    producer = owner()
    previous = event = None
    with directory_lock(workdir / 'follow.lock'):
        while True:
            active = owner()
            if active is not None and active != producer:
                raise SDKError('Ports started another build in this state; select its producer graph')
            current_graph = graph.stat()
            if ((current_graph.st_dev, current_graph.st_ino, current_graph.st_mtime_ns, current_graph.st_size) !=
                    (source.st_dev, source.st_ino, source.st_mtime_ns, source.st_size)):
                raise SDKError('producer graph changed; select the new graph before publishing')
            marks = []
            for name in ('packages', 'done', 'failed', 'blocked', 'skipped'):
                try:
                    st = (state / name).stat()
                    marks.append((st.st_ino, st.st_mtime_ns, st.st_size))
                except FileNotFoundError:
                    if name in ('packages', 'done'):
                        raise
                    marks.append(None)
            changed = marks != previous
            if not changed and event['waiting']:
                current, _ = public.read(_key(prefix, 'current.p7m'), MAX_ENVELOPE,
                                         headers={'cache-control': 'no-cache'})
                changed = hashlib.sha256(current).hexdigest() != event['parent']
            if changed:
                event = _completed_batch(crypto, store, public, spec, ports_root, workdir,
                    identity, trust, state, packages, held, prefix, ttl, forbidden_prefixes)
                yield event
                previous = marks
            if active is None:
                break
            time.sleep(interval)
