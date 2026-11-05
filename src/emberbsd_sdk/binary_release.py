# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Compose signed package repositories from existing Ports graphs and archives."""

from copy import deepcopy
import gzip
import hashlib
import io
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile

from .binary_catalog import (CATALOG_SCHEMA, load_release_catalogs, node_root, parse_catalog, parse_summary,
                             runtime_projection, scalar)
from .elf import parse_elf_header
from .errors import SDKError
from .jsonio import json_bytes, read_json
from .package_selection import _package_sets, _parse_root, _read_roots
from .paths import open_regular, regular_file, verify_stable
from .publication import publish_directory_noreplace
from .release_catalog import (_object, _pattern, _SLUG, local_artifact, parse_release)
from .release_cli import seal_release
from .release_signing import read_bytes, write_new

_ARCH = {'arm64': 'aarch64', 'amd64': 'x86_64'}
_MACHINES = {'aarch64': 183, 'x86_64': 62, 'i386': 3, 'earmv7hf': 40, 'riscv64': 243, 'riscv32': 243}
_MACHO = {b'\xcf\xfa\xed\xfe', b'\xce\xfa\xed\xfe', b'\xfe\xed\xfa\xcf', b'\xfe\xed\xfa\xce'}


def _arch(value):
    return _ARCH.get(value, value)


def _artifact(repository, identity, path, kind, *, size=0, depends=(), licenses=('2-clause-bsd',)):
    return dict(id=identity, path=path, kind=kind, host=repository['host'], target=repository['target'],
                size=0, sha256='0' * 64, unpacked_size=size, depends=list(depends), licenses=list(licenses))


class _PayloadReader:
    def __init__(self, stream, remaining):
        self.stream, self.remaining = stream, remaining

    def read(self, size=-1):
        size = self.remaining if size < 0 else min(size, self.remaining)
        data = self.stream.read(size)
        self.remaining -= len(data)
        return data


def _package_payload(stream):
    """Locate TAR bytes inside pkg_tools' BSD ar X.509 wrapper, without extraction.

    This parses the container only. The caller authenticates signed inputs with
    pkg_admin before treating their contents as a reusable producer package.
    """
    if stream.read(8) != b'!<arch>\n':
        stream.seek(0)
        return stream
    for number in range(3):
        header = stream.read(60)
        if len(header) != 60 or header[58:] != b'`\n' or not header[48:58].strip().isdigit():
            raise SDKError('invalid pkg_tools signature container')
        size = int(header[48:58])
        end = stream.tell() + size + size % 2
        name = header[:16].rstrip(b' ')
        if name.startswith(b'#1/'):
            length = name[3:]
            if not length.isdigit() or not 0 < int(length) <= min(size, 4096):
                raise SDKError('invalid BSD ar member name')
            length = int(length)
            name = stream.read(length).rstrip(b'\0')
            size -= length
        if number < 2:
            if name != (b'+PKG_HASH', b'+PKG_SIGNATURE')[number]:
                raise SDKError('expected pkg_tools X.509 signature members')
            stream.seek(end)
        else:
            return _PayloadReader(stream, size)


def inspect_package(path, target, *, require_native_relocation=False):
    """Inspect producer tar payloads without extracting or executing them.

    Header checks catch foreign dynamic payloads. They do not replace the recipe's
    target linkage/ABI checks or inspect objects nested in static archives.
    """
    handle, before = open_regular(path)
    payload_size = 0
    contents_seen = False
    try:
        with handle, tarfile.open(fileobj=_package_payload(handle), mode='r|*') as archive:
            for member in archive:
                name = member.name.removeprefix('./')
                if require_native_relocation and name == '+CONTENTS':
                    if contents_seen or not member.isfile() or member.size > 8 * 1024 * 1024:
                        raise SDKError('invalid or duplicate native package packing list')
                    contents_seen = True
                    from . import package_relocation
                    lines = archive.extractfile(member).read().decode('utf-8').splitlines()
                    independent = lines.count(package_relocation.INDEPENDENT)
                    markers = lines.count(package_relocation.MARKER)
                    declaration = package_relocation.declaration(lines)
                    if independent + markers != 1 or (markers == 1 and declaration is None):
                        raise SDKError('native package requires one qualified relocation declaration')
                    continue
                if not member.isfile():
                    continue
                stream = archive.extractfile(member)
                if name == '+BUILD_INFO':
                    if member.size > 1024 * 1024:
                        raise SDKError('package build info exceeds size limit')
                    fields = stream.read().decode('utf-8').splitlines()
                    for line in fields:
                        key, separator, value = line.partition('=')
                        if separator and key in ('NO_BIN_ON_FTP', 'RESTRICTED') and value.strip():
                            raise SDKError(f'package redistribution restriction: {path}: {key}')
                elif not name.startswith('+'):
                    payload_size += member.size
                    if target:
                        header = stream.read(64)
                        if header[:4] in _MACHO or header.startswith(b'MZ'):
                            raise SDKError(f'foreign host binary in target package: {name}')
                        if header.startswith(b'\x7fELF'):
                            machine = _MACHINES.get(_arch(target['arch']))
                            if machine is None:
                                raise SDKError('target binary inspection does not support this architecture yet')
                            parse_elf_header(header, label=name, machine=machine)
    except (tarfile.TarError, UnicodeError) as exc:
        raise SDKError(f'expected a pkg_tools producer archive: {path}: {exc}') from exc
    if require_native_relocation and not contents_seen:
        raise SDKError('native package missing packing list')
    verify_stable(path, before)
    return payload_size


def _matches(crypto, pattern, name):
    # Version syntax belongs to pkg_tools; do not implement it in Python.
    result = subprocess.run([crypto.prefix.tool('pkg_admin'), 'pmatch', pattern, name],
                            env={'PATH': crypto.prefix.path, 'LC_ALL': 'C'},
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    if result.returncode not in (0, 1):
        raise SDKError('pkg_admin pmatch failed')
    return result.returncode == 0


def _package_license(record):
    """Represent omitted package metadata without asserting license terms."""
    values = record.get('LICENSE', [])
    if len(values) > 1:
        raise SDKError('pkg_summary has duplicate LICENSE')
    if not values or values[0] == '':
        return 'NOASSERTION'
    if not values[0].strip():
        raise SDKError('pkg_summary has malformed LICENSE')
    return values[0]


def _prepare_repository(crypto, spec, ports_root, sources, trust, previous=None, *, additions=False,
                        checksums=None):
    fields = ('id', 'host', 'target', 'graph', 'packages', 'sets')
    _object(spec, (*fields, 'roots') if isinstance(spec, dict) and 'roots' in spec else fields,
            'package repository')
    _pattern(spec['id'], _SLUG, 'repository id')
    # Reuse the release contract for platform shape and role checks.
    role = 'host' if spec['host'] is not None else 'target'
    probe = _artifact(spec, spec['id'], spec['id'] + '/probe', role + '-package')
    parse_release(json_bytes(dict(schema='emberbsd.release.v1', release_id='probe', created_at=1,
                                  sources=sources, artifacts=[probe])))
    explicit = spec.get('roots', [])
    if (not isinstance(spec['packages'], dict) or not isinstance(spec['sets'], list) or
            not isinstance(explicit, list) or not all(isinstance(root, str) for root in explicit)):
        raise SDKError('repository requires package paths, selected sets and valid roots')
    try:
        explicit = [_parse_root(root, 'package repository') for root in explicit]
    except ValueError as exc:
        raise SDKError(str(exc)) from exc
    if any(root.endswith('@native') != (role == 'host') for root in explicit):
        raise SDKError('package root has a different repository role')
    graph = read_json(spec['graph'])
    if graph.get('provenance', {}).get('revision') != sources['ports']:
        raise SDKError('graph Ports revision differs from release sources')
    if spec['target'] is not None:
        context, target = graph['context'], spec['target']
        if _arch(context['target']) != _arch(target['arch']) or context['triplet'] != target['triplet']:
            raise SDKError('graph target architecture/triplet differs from repository')
    sets = {}
    available = _package_sets(Path(ports_root))
    for name in spec['sets']:
        if not isinstance(name, str) or name not in available:
            raise SDKError('unknown Ports package set')
        roots = [root for root in _read_roots(available[name])
                 if root.endswith('@native') == (role == 'host')]
        if roots:
            sets[name] = roots
    # Match roots to resolved graph IDs, including native generator contexts.
    by_root = {}
    for node in graph['nodes']:
        if node['id'].startswith(role + ':'):
            by_root.setdefault(node_root(node['id']), []).append(node['id'])
    roots = set(explicit) | {root for group in sets.values() for root in group}
    if not roots:
        raise SDKError('package repository requires at least one selected root')
    selected = []
    for root in sorted(roots):
        candidates = by_root.get(root, [])
        if len(candidates) != 1:
            raise SDKError(f'missing or ambiguous graph root: {root}')
        selected.extend(candidates)
    nodes = runtime_projection(graph, selected, role)
    previous = previous or {}
    paths, reused = [], {}
    for identity in nodes:
        old = previous.get(nodes[identity]['root'])
        same_version = old is not None and old['name'].rsplit('-', 1)[-1] == nodes[identity]['version']
        same_name = same_version and (identity not in spec['packages'] or
                    Path(spec['packages'][identity]).name == old['name'] + '.tgz')
        if old and not same_version and _matches(crypto,
                old['name'].rsplit('-', 1)[0] + '>' + nodes[identity]['version'], old['name']):
            raise SDKError('package would downgrade current: ' + nodes[identity]['root'])
        if identity not in spec['packages'] or (additions and same_name):
            if old is None or old['name'].rsplit('-', 1)[-1] != nodes[identity]['version']:
                raise SDKError(f'missing runtime binary: {identity}')
            reused[identity] = dict(old, node=nodes[identity], reused=True)
            continue
        path = Path(spec['packages'][identity]).absolute()
        before = regular_file(path)
        if checksums and str(path) in checksums:
            handle, before = open_regular(path)
            with handle:
                digest = hashlib.file_digest(handle, 'sha256').hexdigest()
            verify_stable(path, before)
            if digest != checksums[str(path)]:
                raise SDKError('archive differs from successful Ports result: ' + str(path))
        paths.append((identity, path, before))
    # Batch metadata queries, bounded below the OS argv limit.
    records = []
    for offset in range(0, len(paths), 64):
        records.extend(parse_summary(crypto.run_package('pkg_info',
            ['-X', *(path for _, path, _ in paths[offset:offset + 64])])))
    if len(records) != len(paths):
        raise SDKError('pkg_info did not return one record per archive')
    entries = dict(reused)
    bases = {item['name'].rsplit('-', 1)[0] for item in reused.values()}
    for (identity, path, before), record in zip(paths, records):
        node = nodes[identity]
        name, pkgpath = scalar(record, 'PKGNAME'), scalar(record, 'PKGPATH')
        if additions and path.name != name + '.tgz':
            raise SDKError('package publisher requires the producer filename PKGNAME.tgz: ' + str(path))
        if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+\-]*-[0-9][A-Za-z0-9._+]*', name) or
                name.rsplit('-', 1)[-1] != node['version'] or
                pkgpath != node['root'].removesuffix('@native')):
            raise SDKError(f'archive name/path/version differs from graph: {identity}')
        base = name.rsplit('-', 1)[0]
        if base in bases:
            raise SDKError(f'multiple versions or providers of {base}')
        bases.add(base)
        platform = spec['host'] or spec['target']
        opsys = spec['host']['os'] if role == 'host' else 'NetBSD'
        if scalar(record, 'OPSYS') != opsys or _arch(scalar(record, 'MACHINE_ARCH')) != _arch(platform['arch']):
            raise SDKError(f'archive platform differs from repository: {identity}')
        size_text = scalar(record, 'SIZE_PKG')
        if not re.fullmatch('[0-9]+', size_text):
            raise SDKError('invalid package unpacked size')
        size = int(size_text)
        with path.open('rb') as stream:
            signed = stream.read(8) == b'!<arch>\n'
        if signed:
            crypto.verify_package(path, trust)
        if inspect_package(path, spec['target'], require_native_relocation=role == 'host') > size:
            raise SDKError('package payload exceeds its declared unpacked size')
        verify_stable(path, before)
        old = previous.get(node['root'])
        entries[identity] = dict(node=node, path=path, before=before, record=record, name=name, size=size, signed=signed,
                                 license=_package_license(record),
                                 artifact=old['artifact'] if old else
                                 spec['id'] + '-' + hashlib.sha256(identity.encode()).hexdigest()[:24])
    for item in reused.values():
        dependencies = sorted(entries[child]['artifact'] for child in item['node']['dependencies'])
        if dependencies != sorted(item['release_artifact']['depends']):
            raise SDKError('reused package runtime graph changed: ' + item['name'])
    return spec, sets, entries


def _check_runtime(crypto, entries, role, selected):
    """Check affected consumers against fixed producer edges, using pkg_tools versions."""
    match_cache = {}
    for identity, item in entries.items():
        if identity not in selected:
            continue
        providers = [entries[child]['name'] for child in item['node']['dependencies']]
        for pattern in item['record'].get('DEPENDS', []):
            count = 0
            for provider in providers:
                key = (pattern, provider)
                if key not in match_cache:
                    match_cache[key] = _matches(crypto, pattern, provider)
                count += match_cache[key]
            if count != 1:
                raise SDKError(f'archive dependency absent or ambiguous in resolved runtime graph: {identity}: {pattern}')
    if role == 'target':
        # Resolve provided package-prefix paths through the already selected
        # runtime graph.  A separate root cannot satisfy this consumer.
        provided = {key: set(item['record'].get('PROVIDES', []))
                    for key, item in entries.items()}
        while True:
            changed = False
            for key, item in entries.items():
                before = len(provided[key])
                for child in item['node']['dependencies']:
                    provided[key].update(provided[child])
                changed |= len(provided[key]) != before
            if not changed:
                break
        for key, item in entries.items():
            if key not in selected:
                continue
            for path in item['record'].get('REQUIRES', []):
                if path.startswith('/usr/pkg/') and path not in provided[key]:
                    raise SDKError(f'package runtime file absent from dependency closure: {key}: {path}')


def _snapshot(crypto, directory, trust):
    """Read authenticated metadata only; old package payloads are never opened."""
    directory = Path(directory)
    release, catalogs = load_catalogs(crypto, directory / 'release.p7m', directory, trust, ())
    artifacts = {item['id']: item for item in release['artifacts']}
    indexes = {}
    for item in release['artifacts']:
        if item['kind'] != 'package-index':
            continue
        data = read_bytes(local_artifact(directory, item['path']))
        if len(data) != item['size'] or hashlib.sha256(data).hexdigest() != item['sha256']:
            raise SDKError('current index differs from signed inventory: ' + item['path'])
        indexes[item['path']] = data
    repositories = {}
    for catalog in catalogs:
        summary = artifacts[catalog['summary']]
        with gzip.GzipFile(fileobj=io.BytesIO(indexes[summary['path']])) as stream:
            records = parse_summary(stream.read(16 * 1024 * 1024 + 1))
        by_name = {scalar(record, 'PKGNAME'): record for record in records}
        if len(by_name) != len(records) or set(by_name) != {p['pkgname'] for p in catalog['packages']}:
            raise SDKError('current summary and catalog packages differ')
        entries = {}
        for package in catalog['packages']:
            record, item = by_name[package['pkgname']], artifacts[package['artifact']]
            if (scalar(record, 'PKGPATH') != package['root'].removesuffix('@native') or
                    scalar(record, 'FILE_NAME') != Path(item['path']).name or
                    scalar(record, 'FILE_SIZE') != str(item['size'])):
                raise SDKError('current package summary differs from inventory: ' + package['pkgname'])
            entries[package['root']] = dict(name=package['pkgname'], record=record,
                artifact=item['id'], release_artifact=item, size=item['unpacked_size'],
                license=_package_license(record))
        repositories[catalog['repository']] = dict(catalog=catalog, entries=entries,
                                                   host=summary['host'], target=summary['target'])
    return release, repositories, indexes


def _summary_bytes(records):
    return ''.join(''.join(key + '=' + value + '\n' for key, values in record.items()
                           for value in values) + '\n' for record in records).encode('utf-8')


def compose(crypto, spec, ports_root, output, identity, trust, *, current=None,
            additions=False, statistics=None, checksums=None):
    """Sign selected packages, generate pkg_summary, then seal their release."""
    _object(spec, ('schema', 'release_id', 'created_at', 'sources', 'repositories'), 'package build')
    if spec['schema'] != 'emberbsd.package-build.v1' or not isinstance(spec['repositories'], list):
        raise SDKError('invalid package build schema')
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise SDKError('release output already exists')
    previous, repositories, indexes = _snapshot(crypto, current, trust) if current else (None, {}, {})
    prepared = []
    for repo in spec['repositories']:
        old = repositories.get(repo['id'])
        if old and (repo['host'], repo['target']) != (old['host'], old['target']):
            raise SDKError('current repository platform cannot change: ' + repo['id'])
        prepared.append(_prepare_repository(crypto, repo, ports_root, spec['sources'], trust,
                                           old['entries'] if old else None, additions=additions, checksums=checksums))
    names = [repo['id'] for repo, _, _ in prepared]
    if len(names) != len(set(names)):
        raise SDKError('duplicate repository identity')
    if additions and previous and all(
            all(item.get('reused') for item in entries.values()) and
            all(repositories[repo['id']]['catalog']['sets'].get(name) == roots for name, roots in sets.items())
            for repo, sets, entries in prepared):
        if statistics is not None:
            statistics.update(_statistics(previous, previous))
        return previous
    if previous and spec['release_id'] == previous['release_id']:
        raise SDKError('changed packages or sets require a new release_id')
    release = {key: spec[key] for key in ('release_id', 'created_at', 'sources')}
    artifacts = {item['id']: deepcopy(item) for item in previous['artifacts']} if previous else {}
    release.update(schema='emberbsd.release.v1', artifacts=[])
    scratch = Path(tempfile.mkdtemp(prefix='.binary-release-', dir=output.parent))
    try:
        for path, data in indexes.items():
            destination = scratch / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            write_new(destination, data)
        for repo, sets, entries in prepared:
            if not entries:
                continue
            directory = scratch / repo['id']
            directory.mkdir(exist_ok=True)
            old = repositories.get(repo['id'])
            packages = {p['root']: dict(p) for p in old['catalog']['packages']} if old else {}
            records = {root: item['record'] for root, item in old['entries'].items()} if old else {}
            groups = dict(old['catalog']['sets']) if old else {}
            groups.update(sets)
            changed = set()
            signed_paths = []
            for item in entries.values():
                if item.get('reused'):
                    continue
                name = item['name'] + '.tgz'
                destination = directory / name
                verify_stable(item['path'], item['before'])
                if item['signed']:
                    shutil.copyfile(item['path'], destination)
                else:
                    crypto.sign_package(item['path'], destination, identity, trust)
                verify_stable(item['path'], item['before'])
                signed_paths.append((item['node']['root'], destination))
                artifacts[item['artifact']] = _artifact(repo, item['artifact'], repo['id'] + '/' + name,
                    ('host' if repo['host'] else 'target') + '-package', size=item['size'],
                    depends=[entries[dep]['artifact'] for dep in item['node']['dependencies']],
                    licenses=[item['license']])
                packages[item['node']['root']] = dict(root=item['node']['root'], pkgname=item['name'], artifact=item['artifact'])
                changed.add(item['artifact'])
            for offset in range(0, len(signed_paths), 64):
                batch = signed_paths[offset:offset + 64]
                signed_records = parse_summary(crypto.run_package('pkg_info', ['-X', *(path for _, path in batch)]))
                if len(signed_records) != len(batch):
                    raise SDKError('signed packages require one summary record per archive')
                records.update((root, record) for (root, _), record in zip(batch, signed_records))
            # Include unchanged reverse consumers when a dependency changes.
            checks = {p['artifact']: dict(name=p['pkgname'], record=records[root],
                       node=dict(dependencies=artifacts[p['artifact']]['depends'])) for root, p in packages.items()}
            affected = set(changed)
            while True:
                extra = {key for key, item in checks.items() if set(item['node']['dependencies']) & affected}
                if extra <= affected:
                    break
                affected.update(extra)
            _check_runtime(crypto, checks, 'host' if repo['host'] else 'target', affected)
            summary = _summary_bytes(records[root] for root in sorted(records))
            (directory / 'pkg_summary.gz').write_bytes(gzip.compress(summary, mtime=0))
            summary_id = repo['id'] + '-summary'
            artifacts[summary_id] = _artifact(repo, summary_id, repo['id'] + '/pkg_summary.gz',
                                             'package-index', size=len(summary))
            catalog = dict(schema=CATALOG_SCHEMA, repository=repo['id'], summary=summary_id,
                           packages=[packages[root] for root in sorted(packages)], sets=groups)
            data = json_bytes(catalog)
            artifact = _artifact(repo, repo['id'] + '-catalog', repo['id'] + '/catalog.json',
                                 'package-index', size=len(data))
            artifacts[artifact['id']] = artifact
            release['artifacts'] = list(artifacts.values())
            parse_catalog(data, release, artifact)
            (directory / 'catalog.json').write_bytes(data)
        release['artifacts'] = list(artifacts.values())
        release = seal_release(crypto, json_bytes(release), scratch, identity, trust, scratch / 'release.p7m',
                               reuse_current=Path(current) / 'release.p7m' if current else None)
        publish_directory_noreplace(scratch, output)
        if statistics is not None:
            statistics.update(_statistics(previous, release))
        return release
    finally:
        if scratch.exists():
            shutil.rmtree(scratch)


def _statistics(previous, release):
    old = {item['id']: item for item in previous['artifacts']} if previous else {}
    packages = [item for item in release['artifacts'] if item['kind'] in ('host-package', 'target-package')]
    added = [item for item in packages if item['id'] not in old]
    replaced = [item for item in packages if item['id'] in old and item != old[item['id']]]
    return dict(added=len(added), replaced=len(replaced), reused=len(packages) - len(added) - len(replaced),
                uploaded_packages=len(added) + len(replaced), upload_bytes=sum(p['size'] for p in added + replaced),
                built=0)


def load_catalogs(crypto, envelope, directory, trust, repositories):
    """Authenticate the release and load its selected catalog metadata."""
    release = parse_release(crypto.verify_cms(read_bytes(envelope), trust))
    return release, load_release_catalogs(release, directory, repositories)
