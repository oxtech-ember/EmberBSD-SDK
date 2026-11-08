# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Project resolved Ports runtime edges and select authenticated binary artifacts.

No version solver, source tree, build tools or package execution is used here.
Authentication is provided by the enclosing signed release, before parsing.
"""

import gzip
import hashlib
import io
import re
import zlib

from .errors import SDKError
from .jsonio import loads_json
from .package_selection import _parse_root
from .ports_plan import lock_graph
from .release_catalog import local_artifact
from .release_signing import read_bytes

CATALOG_SCHEMA = 'emberbsd.packages.v1'
_REPEATED = {'DEPENDS', 'CONFLICTS', 'REQUIRES', 'PROVIDES', 'DESCRIPTION', 'SUPERSEDES'}


def node_root(identity):
    role, separator, path = identity.partition(':')
    if not separator or role not in ('host', 'target'):
        raise SDKError(f'invalid Ports package identity: {identity}')
    path = path.split('@', 1)[0]
    try:
        return _parse_root(path + ('@native' if role == 'host' else ''), 'catalog')
    except ValueError as exc:
        raise SDKError(str(exc)) from exc


def runtime_projection(graph, roots, role):
    """Retain the already resolved runtime closure, excluding production tools."""
    if role not in ('host', 'target'):
        raise SDKError('repository role must be host or target')
    nodes = {node['id']: node for node in lock_graph(graph)['graph']['nodes']}
    pending, result, identities = list(roots), {}, {}
    while pending:
        name = pending.pop()
        if name in result:
            continue
        if name not in nodes or not name.startswith(role + ':'):
            raise SDKError(f'missing package or wrong repository role: {name}')
        root = node_root(name)
        if root in identities:
            raise SDKError(f'conflicting package variants for {root}')
        identities[root] = name
        node = nodes[name]
        dependencies = sorted({edge['id'] for edge in node['dependencies'] if edge['kind'] == 'runtime'})
        result[name] = dict(root=root, version=node['version'], dependencies=dependencies)
        pending.extend(dependencies)
    return dict(sorted(result.items()))


def parse_summary(data):
    """Read the standard pkg_info -X record format, preserving repeated fields."""
    if not isinstance(data, bytes) or len(data) > 16 * 1024 * 1024:
        raise SDKError('invalid or oversized pkg_summary')
    try:
        text = data.decode('utf-8')
    except UnicodeError as exc:
        raise SDKError('pkg_summary is not UTF-8') from exc
    records, record = [], {}
    for line in text.splitlines() + ['']:
        if not line:
            if record:
                records.append(record)
                record = {}
            continue
        key, separator, value = line.partition('=')
        if not separator or not re.fullmatch('[A-Z][A-Z0-9_]*', key) or '\x00' in value:
            raise SDKError('invalid pkg_summary field')
        if key in record and key not in _REPEATED:
            raise SDKError(f'duplicate pkg_summary field: {key}')
        record.setdefault(key, []).append(value)
    return records


def summary_conflicts(data, pkgnames):
    """Map each catalog package to its explicit CONFLICTS patterns from the signed summary."""
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
            records = parse_summary(stream.read(16 * 1024 * 1024 + 1))
    except (OSError, EOFError, zlib.error) as exc:
        raise SDKError('pkg_summary is not valid gzip data') from exc
    conflicts = {}
    for record in records:
        name = scalar(record, 'PKGNAME')
        if name in conflicts:
            raise SDKError(f'duplicate pkg_summary package: {name}')
        conflicts[name] = list(record.get('CONFLICTS', []))
    if set(conflicts) != set(pkgnames):
        raise SDKError('binary summary and catalog packages differ')
    return conflicts


def scalar(record, key):
    values = record.get(key, [])
    if len(values) != 1 or not values[0]:
        raise SDKError(f'pkg_summary requires one nonempty {key}')
    return values[0]


def parse_catalog(data, release, catalog_artifact):
    """Validate a catalog against an already authenticated release inventory."""
    value = loads_json(data)
    if (not isinstance(value, dict) or set(value) != {'schema', 'repository', 'summary', 'packages', 'sets'} or
            value['schema'] != CATALOG_SCHEMA or
            not isinstance(value['repository'], str) or
            not re.fullmatch('[a-z0-9][a-z0-9._-]*', value['repository'])):
        raise SDKError('invalid binary package catalog')
    artifacts = {item['id']: item for item in release['artifacts']}
    platform = (catalog_artifact['host'], catalog_artifact['target'])
    role = 'host' if platform[0] else 'target'
    summary = artifacts.get(value['summary']) if isinstance(value['summary'], str) else None
    if (summary is None or summary['kind'] != 'package-index' or
            (summary['host'], summary['target']) != platform):
        raise SDKError('catalog summary has a different platform or is missing')
    if not isinstance(value['packages'], list) or not isinstance(value['sets'], dict):
        raise SDKError('catalog requires packages and sets')
    roots, names, selected = set(), set(), set()
    for package in value['packages']:
        if not isinstance(package, dict) or set(package) != {'root', 'pkgname', 'artifact'}:
            raise SDKError('invalid binary package record')
        root, name, identity = (package[key] for key in ('root', 'pkgname', 'artifact'))
        if not all(isinstance(item, str) for item in (root, name, identity)):
            raise SDKError('invalid package identity')
        try:
            _parse_root(root, 'catalog')
        except ValueError as exc:
            raise SDKError(str(exc)) from exc
        if root.endswith('@native') != (role == 'host'):
            raise SDKError('package root has a different role')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+\-]*-[0-9][A-Za-z0-9._+]*', name):
            raise SDKError('invalid package name')
        base = name.rsplit('-', 1)[0]
        artifact = artifacts.get(identity)
        if (artifact is None or artifact['kind'] != role + '-package' or
                (artifact['host'], artifact['target']) != platform):
            raise SDKError('package has a different platform or is missing')
        if root in roots or base in names or identity in selected:
            raise SDKError('multiple packages for one root, base name or artifact')
        roots.add(root)
        names.add(base)
        selected.add(identity)
    for identity in selected:
        if not set(artifacts[identity]['depends']) <= selected:
            raise SDKError('runtime dependency escapes the binary repository')
    for name, entries in value['sets'].items():
        if (not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_-]*', name) or
                not isinstance(entries, list) or not all(isinstance(item, str) for item in entries) or
                len(entries) != len(set(entries)) or not set(entries) <= roots):
            raise SDKError(f'invalid package set: {name}')
    return value


def load_release_catalogs(release, directory, repositories=()):
    """Validate selected local package catalogs against the full release inventory."""
    catalogs = []
    requested = set(repositories)
    for artifact in release['artifacts']:
        if artifact['kind'] != 'package-index' or not artifact['path'].endswith('/catalog.json'):
            continue
        repository = artifact['path'].split('/')[0]
        if requested and repository not in requested:
            continue
        data = read_bytes(local_artifact(directory, artifact['path']))
        if len(data) != artifact['size'] or hashlib.sha256(data).hexdigest() != artifact['sha256']:
            raise SDKError('binary catalog size or SHA256 differs from signed release')
        catalog = parse_catalog(data, release, artifact)
        if catalog['repository'] != repository:
            raise SDKError('catalog repository differs from its signed path')
        summary = next(item for item in release['artifacts'] if item['id'] == catalog['summary'])
        data = read_bytes(local_artifact(directory, summary['path']))
        if len(data) != summary['size'] or hashlib.sha256(data).hexdigest() != summary['sha256']:
            raise SDKError('binary summary size or SHA256 differs from signed release')
        catalog['conflicts'] = summary_conflicts(data, {p['pkgname'] for p in catalog['packages']})
        catalogs.append(catalog)
    if requested - {catalog['repository'] for catalog in catalogs}:
        raise SDKError('requested binary repository is missing')
    return catalogs


def select_packages(release, catalogs, *, sets=(), roots=()):
    """Union selected roots and their fixed runtime edges across explicit repositories."""
    artifacts = {item['id']: item for item in release['artifacts']}
    names = {name for catalog in catalogs for name in catalog['sets']}
    available = {entry['root'] for catalog in catalogs for entry in catalog['packages']}
    if set(sets) - names or set(roots) - available:
        raise SDKError('unknown package set or package root in selected repositories')
    pending = []
    for catalog in catalogs:
        by_root = {entry['root']: entry['artifact'] for entry in catalog['packages']}
        requested = set(roots) & by_root.keys()
        for name in sets:
            requested.update(catalog['sets'].get(name, []))
        pending.extend(by_root[root] for root in requested)
    chosen = set()
    while pending:
        identity = pending.pop()
        if identity in chosen:
            continue
        chosen.add(identity)
        pending.extend(artifacts[identity]['depends'])
    packages = [artifacts[name] for name in sorted(chosen)]
    return dict(release_id=release['release_id'], packages=packages,
                download_size=sum(item['size'] for item in packages),
                unpacked_size=sum(item['unpacked_size'] for item in packages))
