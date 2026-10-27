# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD; AI-assisted release version preparation and immutable tag creation.
"""Prepare the release version and create local annotated tags for its sources."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

from .errors import CommandError, SDKError
from .inventory import sha256_file
from .jsonio import json_bytes, read_json, write_json
from .paths import open_regular, verify_stable
from .ports_plan import PORTS_JSON_LIMIT
from .process import capture, git_environment
from .provenance import git_source

PORTS_RELEASE_API = 2
COMPONENTS = ('os', 'ports', 'sdk')
VERSION_FILE = 'share/mk/bsd.own.mk'


class ReleaseError(SDKError):
    """A release failed; any listed refs must be preserved for inspection."""

    def __init__(self, message, created_refs):
        self.created_refs = created_refs
        super().__init__(message + '; created refs: ' + json.dumps(created_refs, sort_keys=True))


def _git(root, *args, input=None):
    return capture(['git', '-C', str(root), *args], input=input,
                   env=git_environment()).stdout.decode('utf-8').strip()


def _tag_name(version):
    if not isinstance(version, str) or version.endswith('.lock') or not re.fullmatch(r'[0-9][A-Za-z0-9]*(?:[.\-+][A-Za-z0-9]+)*', version):
        raise SDKError('invalid release version')
    return 'emberbsd-' + version


def _read(path):
    before = sha256_file(path)
    value = read_json(path, max_bytes=PORTS_JSON_LIMIT)
    if sha256_file(path) != before:
        raise SDKError(f'release input changed while reading: {path}')
    return value, before


def _sources(repos):
    if not isinstance(repos, dict) or set(repos) != set(COMPONENTS):
        raise SDKError('release requires exactly os, ports, and sdk repositories')
    sources = {name: git_source(repos[name], kind=name) for name in COMPONENTS}
    roots = {source['path'] for source in sources.values()}
    common = {str((Path(source['path']) / _git(source['path'], 'rev-parse', '--git-common-dir')).resolve())
              for source in sources.values()}
    if len(roots) != 3 or len(common) != 3:
        raise SDKError('release repositories must have distinct roots and Git storage')
    return sources


def _version_file(root):
    path = Path(root) / VERSION_FILE
    handle, before = open_regular(path)
    with handle:
        raw = handle.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise SDKError('OS version source exceeds the size limit')
    verify_stable(path, before)
    try:
        tracked = capture(['git', '-C', str(root), 'cat-file', 'blob', 'HEAD:' + VERSION_FILE],
                          env=git_environment()).stdout
    except SDKError as exc:
        raise SDKError('OS version source must be tracked in HEAD') from exc
    if tracked != raw:
        raise SDKError('OS version source bytes must match the HEAD blob')
    assignments = re.findall(rb'(?m)^EMBER_VERSION[ \t]*[?:+]?=.*$', raw)
    match = re.search(rb'(?m)^EMBER_VERSION[ \t]*\?=[ \t]*([A-Za-z0-9.+-]+)[ \t]*\r?$', raw)
    if len(assignments) != 1 or match is None:
        raise SDKError('OS must contain exactly one simple EMBER_VERSION?= assignment')
    _tag_name(match[1].decode('ascii'))
    return path, raw, match


def _outside(path, repos):
    path = Path(path)
    if path.is_symlink():
        raise SDKError('release output must not be a symlink')
    path = path.resolve()
    if any(path == Path(root) or Path(root) in path.parents or path in Path(root).parents for root in repos.values()):
        raise SDKError('release output must be outside source repositories')
    return path


def prepare_release(version, repos, output):
    """Set only the OS release constant, commit it locally, and pin all sources."""
    _tag_name(version)
    sources = _sources(repos)
    repos = {name: source['path'] for name, source in sources.items()}
    output = _outside(output, repos)
    if output.exists() or output.is_symlink() or not output.parent.is_dir():
        raise SDKError('release sources output must be a new file in an existing directory')
    path, original, match = _version_file(repos['os'])
    if match[1].decode('ascii') != version:
        if _sources(repos) != sources or path.read_bytes() != original:
            raise SDKError('sources changed before version preparation')
        expected = original[:match.start(1)] + version.encode('ascii') + original[match.end(1):]
        path.write_bytes(expected)
        try:
            _git(repos['os'], 'commit', '--only', '-m', 'Set EmberBSD release version to ' + version, '--', VERSION_FILE)
        except SDKError as exc:
            raise SDKError('version preparation could not commit; preserve and inspect the OS version edit or local commits: ' + str(exc)) from exc
        updated = _sources(repos)
        if any(updated[name] != sources[name] for name in ('ports', 'sdk')):
            raise SDKError('other sources changed during version preparation; preserve the local OS commit')
        if (_version_file(repos['os'])[1] != expected or
                _git(repos['os'], 'rev-parse', 'HEAD^') != sources['os']['commit'] or
                _git(repos['os'], 'diff-tree', '--no-commit-id', '--name-only', '-r', 'HEAD') != VERSION_FILE):
            raise SDKError('OS source changed during commit; preserve the local OS commit')
        sources = updated
    if _sources(repos) != sources:
        raise SDKError('sources changed before publishing release inputs')
    value = dict(schema='ports-release-sources-v1', version=version,
                 sources={name: sources[name]['commit'] for name in COMPONENTS})
    write_json(output, value)
    return value


def _release_sources(path, sources):
    value, checksum = _read(path)
    if not isinstance(value, dict) or set(value) != {'schema', 'version', 'sources'} or value['schema'] != 'ports-release-sources-v1':
        raise SDKError('unsupported release sources schema')
    _tag_name(value['version'])
    if value['sources'] != {name: sources[name]['commit'] for name in COMPONENTS}:
        raise SDKError('release source commits do not match current HEADs')
    if _version_file(sources['os']['path'])[2][1].decode('ascii') != value['version']:
        raise SDKError('OS EMBER_VERSION does not match the prepared release version')
    return value, checksum


def _absent(repos, ref):
    for root in repos.values():
        try:
            _git(root, 'show-ref', '--verify', '--quiet', ref)
        except CommandError as exc:
            if exc.returncode == 1:
                continue
            raise
        raise SDKError(f'release tag already exists: {root}: {ref}')


def create_release(sources_path, repos):
    """Create three local refs for the prepared sources. Ref writes are not distributed atomic."""
    sources = _sources(repos)
    declared, sources_sha256 = _release_sources(sources_path, sources)
    version = declared['version']
    tag = _tag_name(version)
    ref = 'refs/tags/' + tag
    repos = {name: source['path'] for name, source in sources.items()}
    _absent(repos, ref)
    manifest = dict(schema='ports-release-v2', version=version, sources_sha256=sources_sha256,
                    sources={name: sources[name]['commit'] for name in COMPONENTS})
    manifest['manifest_sha256'] = hashlib.sha256(json_bytes(manifest)).hexdigest()
    message = json_bytes(manifest)
    objects, created = {}, []
    try:
        for name, root in repos.items():
            identity = _git(root, 'var', 'GIT_COMMITTER_IDENT')
            header = f'object {sources[name]["commit"]}\ntype commit\ntag {tag}\ntagger {identity}\n\n'
            objects[name] = _git(root, 'mktag', input=header.encode() + message)
        for name, root in repos.items():
            transaction = f'start\nverify HEAD {sources[name]["commit"]}\ncreate {ref} {objects[name]}\nprepare\ncommit\n'
            _git(root, 'update-ref', '--stdin', input=transaction.encode())
            created.append(dict(component=name, ref=ref, object=objects[name]))
    except (SDKError, OSError, KeyboardInterrupt) as exc:
        # Reconcile a command that wrote its ref before an interruption/error.
        for name, obj in objects.items():
            if any(item['component'] == name for item in created):
                continue
            try:
                if _git(repos[name], 'rev-parse', '--verify', ref) == obj:
                    created.append(dict(component=name, ref=ref, object=obj))
            except SDKError:
                pass
        raise ReleaseError(str(exc), created) from exc
    return dict(manifest=manifest, created_refs=created)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    prepare = commands.add_parser('prepare', help='commit the OS version and pin clean sources')
    prepare.add_argument('--version', required=True)
    prepare.add_argument('--output', required=True)
    finalize = commands.add_parser('finalize', help='create local tags for the prepared sources')
    finalize.add_argument('--sources', required=True)
    for command in (prepare, finalize):
        command.add_argument('--repo', action='append', required=True, metavar='COMPONENT=PATH')
    args = parser.parse_args(argv)
    try:
        repos = {}
        for value in args.repo:
            name, separator, path = value.partition('=')
            if not separator or not path or name in repos:
                raise SDKError('invalid or duplicate --repo')
            repos[name] = path
        if args.command == 'prepare':
            result = prepare_release(args.version, repos, args.output)
        else:
            result = create_release(args.sources, repos)
        print(json.dumps(result, indent=2))
        return 0
    except (SDKError, OSError, ValueError) as exc:
        print(f'ports release: {exc}', file=sys.stderr)
        return 1
