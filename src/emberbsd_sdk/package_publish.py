# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Explicit maintainer publication using existing Ports and release mechanisms."""

from copy import deepcopy
import hashlib
from pathlib import Path
import re
import shutil

from .binary_catalog import node_root
from .binary_release import compose, _statistics
from .errors import SDKError
from .jsonio import read_json, write_json
from .package_cache import directory_lock
from .package_selection import _parse_root
from .paths import real_directory
from .release_catalog import parse_channel, parse_release
from .release_delivery import fetch_release, publish_release
from .release_signing import read_bytes


def packages_from_state(spec, state, packages):
    """Map successful Ports results to an existing producer graph, without solving."""
    spec = deepcopy(spec)
    if len(spec['repositories']) != 1:
        raise SDKError('--state requires exactly one selected repository in the package-build input')
    repo = spec['repositories'][0]
    role = 'host' if repo['host'] is not None else 'target'
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
            if root.endswith('@native') != (role == 'host'):
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
        if root in excluded:
            continue
        done = completed.get(root)
        if not done or done[0] != pkgname or done[1] is None or done[1] == 'native':
            continue
        nodes = by_root.get(root, [])
        if len(nodes) != 1 or nodes[0]['version'] != pkgname.rsplit('-', 1)[-1]:
            raise SDKError('successful Ports result is missing or differs from graph: ' + root)
        path = str(packages / (pkgname + '.tgz'))
        paths[nodes[0]['id']] = path
        checksums[path] = done[1]
        roots.append(root)
    if not roots:
        raise SDKError('no successful packages of the selected role in Ports state')
    repo['packages'] = paths
    repo['roots'] = sorted(set(repo.get('roots', [])) | set(roots))
    return spec, checksums


def update_packages(crypto, store, public, spec, ports_root, workdir, identity, trust,
                    *, prefix='', ttl=7 * 86400, checksums=None):
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
            channel = parse_channel(crypto.verify_cms(read_bytes(snapshot / 'current.p7m'), trust), 'current')
            statistics = {}
            release = compose(crypto, spec, ports_root, output, identity, trust, current=snapshot,
                              additions=True, statistics=statistics, checksums=checksums)
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
