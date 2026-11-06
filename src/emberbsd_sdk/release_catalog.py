# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Strict release metadata and channel validity checks.

Parsing checks structure and consistency, not authenticity. Callers must verify
the CMS signature of the exact input bytes before trusting these records or
using these records. Artifact roots are owner-controlled.
"""

import hashlib
import os
from pathlib import Path
import re
import time

from .errors import ChannelExpired, SDKError
from .jsonio import MAX_JSON, loads_json
from .paths import (canonical_existing, open_regular,
                    real_directory, regular_file, verify_stable)


RELEASE_SCHEMA = 'emberbsd.release.v1'
CHANNEL_SCHEMA = 'emberbsd.channel.v2'
MAX_ENVELOPE = 16 * 1024 * 1024
_SLUG = re.compile(r'[a-z0-9][a-z0-9._-]*\Z')
_SEGMENT = re.compile(r'[A-Za-z0-9._+-]+\Z')
_SHA256 = re.compile(r'[0-9a-f]{64}\Z')
_COMMIT = re.compile(r'[0-9a-f]{40}\Z')
_HOST_KINDS = {'host-package', 'bootstrap'}
_TARGET_KINDS = {'target-package', 'sysroot', 'os-set', 'image'}


def _object(value, fields, label):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise SDKError(f'{label}: expected exactly these fields: {", ".join(fields)}')


def _text(value, label):
    if (not isinstance(value, str) or not value.strip() or
            any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise SDKError(f'{label}: expected a nonempty string without control characters')


def _pattern(value, pattern, label):
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise SDKError(f'{label}: invalid value')


def _integer(value, label, minimum=1, maximum=None):
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise SDKError(f'{label}: expected an integer in the permitted range')


def _relative(value):
    if not isinstance(value, str) or not value:
        raise SDKError('artifact path: expected a nonempty relative URL path')
    parts = value.split('/')
    if any(part in ('.', '..') or _SEGMENT.fullmatch(part) is None for part in parts):
        raise SDKError(f'artifact path: invalid relative URL path: {value!r}')
    return parts


def _input(data):
    if not isinstance(data, bytes):
        raise SDKError('signed metadata input must be the exact bytes')
    return loads_json(data, max_bytes=MAX_JSON)


def _reference(value, fields, label, *, envelope=False):
    _object(value, fields, label)
    _pattern(value['id'], _SLUG, label + ' id')
    _relative(value['path'])
    _integer(value['size'], label + ' size', minimum=1 if envelope else 0,
             maximum=MAX_ENVELOPE if envelope else None)
    _pattern(value['sha256'], _SHA256, label + ' sha256')


def _platform(value, fields, label):
    if value is not None:
        _object(value, fields, label)
        for key in fields:
            _text(value[key], label + ' ' + key)


def _release(value):
    _object(value, ('schema', 'release_id', 'created_at', 'sources', 'artifacts'), 'release')
    if value['schema'] != RELEASE_SCHEMA:
        raise SDKError('unsupported release schema')
    _pattern(value['release_id'], _SLUG, 'release id')
    _integer(value['created_at'], 'release created_at')
    _object(value['sources'], ('os', 'ports', 'sdk'), 'release sources')
    for name, revision in value['sources'].items():
        _pattern(revision, _COMMIT, 'source ' + name)
    artifacts = value['artifacts']
    if not isinstance(artifacts, list) or not artifacts:
        raise SDKError('release artifacts: expected a nonempty array')
    by_id, paths = {}, set()
    for artifact in artifacts:
        _reference(artifact, ('id', 'path', 'kind', 'host', 'target', 'size', 'unpacked_size',
                              'sha256', 'depends', 'licenses'), 'artifact')
        _integer(artifact['unpacked_size'], 'artifact unpacked_size', minimum=0)
        name = artifact['id']
        path = artifact['path'].casefold()
        if name in by_id or path in paths:
            raise SDKError('duplicate artifact id or case-insensitive path')
        by_id[name] = artifact
        paths.add(path)
        _platform(artifact['host'], ('os', 'arch', 'abi', 'runtime'), name + ' host')
        _platform(artifact['target'], ('arch', 'triplet', 'abi', 'runtime'), name + ' target')
        host, target = artifact['host'] is not None, artifact['target'] is not None
        kind = artifact['kind']
        if not isinstance(kind, str) or kind not in _HOST_KINDS | _TARGET_KINDS | {'package-index'}:
            raise SDKError(f'{name}: unsupported artifact kind')
        if ((kind in _HOST_KINDS and (not host or target)) or
                (kind in _TARGET_KINDS and (host or not target)) or
                (kind == 'package-index' and host == target)):
            raise SDKError(f'{name}: host/target role does not match artifact kind')
        depends = artifact['depends']
        if not isinstance(depends, list):
            raise SDKError(f'{name}: depends must be an array')
        for dependency in depends:
            _pattern(dependency, _SLUG, name + ' dependency')
        if len(depends) != len(set(depends)):
            raise SDKError(f'{name}: duplicate dependency')
        licenses = artifact['licenses']
        if not isinstance(licenses, list) or not licenses:
            raise SDKError(f'{name}: licenses must be a nonempty array')
        for license_name in licenses:
            _text(license_name, name + ' license')
    for path in paths:
        parts = path.split('/')
        if any('/'.join(parts[:index]) in paths for index in range(1, len(parts))):
            raise SDKError('artifact paths overlap')
    # Check only declared artifact edges; package resolution belongs to Ports.
    waiting, consumers = {}, {name: [] for name in by_id}
    for name, artifact in by_id.items():
        waiting[name] = len(artifact['depends'])
        for dependency in artifact['depends']:
            if dependency not in by_id:
                raise SDKError(f'{name}: unknown dependency {dependency}')
            consumers[dependency].append(name)
    ready = [name for name, count in waiting.items() if count == 0]
    visited = 0
    while ready:
        name = ready.pop()
        visited += 1
        for consumer in consumers[name]:
            waiting[consumer] -= 1
            if waiting[consumer] == 0:
                ready.append(consumer)
    if visited != len(by_id):
        raise SDKError('artifact dependencies contain a cycle')
    return value


def parse_release(data: bytes) -> dict:
    """Validate a release without treating unverified JSON as authenticated."""
    return _release(_input(data))


def parse_channel(data: bytes, expected_name: str, now: int | None = None) -> dict:
    """Validate a channel against the caller-selected name and local UTC clock."""
    value = _input(data)
    _object(value, ('schema', 'channel', 'issued_at', 'expires_at', 'release'), 'channel')
    if value['schema'] != CHANNEL_SCHEMA:
        raise SDKError('unsupported channel schema')
    _pattern(expected_name, _SLUG, 'expected channel name')
    _pattern(value['channel'], _SLUG, 'channel name')
    if value['channel'] != expected_name:
        raise SDKError('channel name differs from the selected channel')
    for name in ('issued_at', 'expires_at'):
        _integer(value[name], 'channel ' + name)
    _reference(value['release'], ('id', 'path', 'size', 'sha256'), 'channel release', envelope=True)
    if not value['release']['path'].endswith('/release.p7m'):
        raise SDKError('channel release path must end with /release.p7m')
    if value['expires_at'] <= value['issued_at']:
        raise SDKError('channel expiry must follow issuance; check publisher clock')
    now = int(time.time()) if now is None else now
    _integer(now, 'local clock', minimum=0)
    if now < value['issued_at']:
        raise SDKError('channel is not yet valid; check the local clock')
    if now >= value['expires_at']:
        raise ChannelExpired('channel has expired; check the local clock or obtain a fresh channel')
    return value


def local_artifact(root, relative) -> Path:
    """Locate an existing regular artifact, rejecting every relative symlink.

    The explicit root may be a user-selected directory alias. Its contents must
    remain under the caller's ownership throughout verification and later use.
    """
    parts = _relative(relative)
    try:
        path = Path(canonical_existing(root))
        real_directory(path)
        for part in parts[:-1]:
            path /= part
            real_directory(path)
        path /= parts[-1]
        regular_file(path)
        return path
    except OSError as exc:
        raise SDKError(f'cannot locate artifact {relative}: {exc}') from exc


def file_reference(path) -> dict:
    """Hash one stable regular file without following a final symbolic link."""
    try:
        handle, before = open_regular(path)
        digest, size = hashlib.sha256(), 0
        with handle:
            while block := handle.read(1024 * 1024):
                digest.update(block)
                size += len(block)
        verify_stable(path, before)
        if size != before.st_size:
            raise SDKError(f'file changed during inspection: {path}')
        return {'size': size, 'sha256': digest.hexdigest()}
    except OSError as exc:
        raise SDKError(f'cannot hash artifact {path}: {exc}') from exc


def verify_payloads(release: dict, root) -> None:
    """Check every declared payload against a structurally valid release."""
    _release(release)
    for artifact in release['artifacts']:
        path = local_artifact(root, artifact['path'])
        actual = file_reference(path)
        # Detect aliases introduced during a read as well as preexisting ones.
        local_artifact(root, artifact['path'])
        if actual != {key: artifact[key] for key in ('size', 'sha256')}:
            raise SDKError(f'artifact size or SHA256 mismatch: {artifact["id"]}')
