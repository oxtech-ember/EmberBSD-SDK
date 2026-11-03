# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Keep one signed current repository, replacing obsolete packages after publication."""

from contextlib import contextmanager
import hashlib
import io
from pathlib import Path
import secrets
import shutil
import tempfile
import time

from .errors import SDKError
from .jsonio import json_bytes, loads_json
from .paths import open_regular, verify_stable
from .publication import publish_directory_noreplace
from .release_catalog import (MAX_ENVELOPE, _relative, _pattern, _SLUG, commit_channel,
                              local_artifact, parse_channel, parse_release)
from .release_cli import verify_release
from .release_signing import read_bytes, write_new
from .release_s3 import HTTPStatusError

IMMUTABLE_CACHE = 'public, max-age=31536000, immutable'
CHANNEL_CACHE = 'no-cache, max-age=0, must-revalidate'
CHANNEL = 'current'


def _key(prefix, path):
    if prefix:
        _relative(prefix)
    _relative(path)
    return '/'.join(item for item in (prefix, path) if item)


def _digest(data):
    return dict(size=len(data), sha256=hashlib.sha256(data).hexdigest())


def _check(data, reference):
    if _digest(data) != {key: reference[key] for key in ('size', 'sha256')}:
        raise SDKError('downloaded metadata size or SHA256 differs from signed inventory')


def _optional_read(store, key):
    try:
        return store.read(key, MAX_ENVELOPE, headers={'cache-control': 'no-cache'})
    except HTTPStatusError as exc:
        if exc.status != 404:
            raise
        return None, {}


def _immutable(store, public, key, body, size, sha256, content_type):
    try:
        headers = store.head(key)
        if int(headers['content-length']) != size:
            raise SDKError(f'immutable object already has different bytes: {key}')
    except HTTPStatusError as exc:
        if exc.status != 404:
            raise
        try:
            store.put(key, body, size, sha256, cache_control=IMMUTABLE_CACHE, content_type=content_type)
        except HTTPStatusError as conflict:
            if conflict.status != 412:
                raise
            # A concurrent identical upload is harmless; compare the public bytes.
    public.verify(key, size, sha256)


def artifact_key(artifact, metadata_base):
    """Ordinary versioned package names; only metadata needs a temporary snapshot."""
    if artifact['kind'] == 'package-index':
        return metadata_base + '/' + artifact['path']
    if artifact['host']:
        host = artifact['host']
        platform = host['os'].lower() + '-' + host['arch']
        role = 'host'
    else:
        platform, role = artifact['target']['arch'], 'target'
    _pattern(platform, _SLUG, 'repository platform')
    return 'current/' + role + '/' + platform + '/' + artifact['path'].split('/')[-1]


@contextmanager
def _publisher_lock(store, prefix):
    # Covers both switching and deletion: CAS on current alone cannot prevent
    # an earlier publisher's cleanup from deleting a later publisher's inputs.
    key = _key(prefix, '.publisher-lock')
    token = json_bytes({'token': secrets.token_hex(16), 'started_at': int(time.time())})
    try:
        store.put(key, token, **_digest(token))
    except HTTPStatusError as exc:
        if exc.status in (409, 412):
            raise SDKError(f'publisher is busy; if it crashed, inspect and remove {key}') from exc
        raise
    try:
        yield
    finally:
        store.delete(key)


def _clean_current(store, prefix, keep):
    # Collect before deleting, so pagination stays stable even for stores using
    # positional continuation tokens. Never inspect or delete outside current/.
    obsolete = set(store.list_keys(_key(prefix, 'current') + '/')) - keep
    for key in sorted(obsolete):
        store.delete(key)


def publish_release(crypto, store, public, envelope_path, identity, trust,
                    *, prefix='', ttl=7 * 86400, refresh=False, previous_channel_time=None):
    if type(ttl) is not int or ttl <= 0:
        raise SDKError('channel lifetime must be a positive number of seconds')
    envelope_path = Path(envelope_path)
    envelope = read_bytes(envelope_path)
    release = verify_release(crypto, envelope, envelope_path.parent, trust)
    base = 'current/metadata/' + hashlib.sha256(envelope).hexdigest()
    keys = [artifact_key(item, base) for item in release['artifacts']]
    if len({key.casefold() for key in keys}) != len(keys):
        raise SDKError('multiple artifacts have the same current path')
    packages = set()
    for artifact, key in zip(release['artifacts'], keys):
        if artifact['kind'] in ('host-package', 'target-package'):
            directory, name = key.rsplit('/', 1)
            if not name.endswith('.tgz') or '-' not in name:
                raise SDKError('current requires standard name-version[nbN].tgz package names')
            package = (directory.casefold(), name.rsplit('-', 1)[0].casefold())
            if package in packages:
                raise SDKError('current permits only one version of each package per platform')
            packages.add(package)
    with _publisher_lock(store, prefix):
        return _publish_current(crypto, store, public, envelope_path, envelope, release,
                                base, identity, trust, prefix, ttl, refresh, previous_channel_time)


def _publish_current(crypto, store, public, envelope_path, envelope, release, base,
                     identity, trust, prefix, ttl, refresh, previous_channel_time):
    release_key = _key(prefix, base + '/release.p7m')
    channel_key = _key(prefix, 'current.p7m')
    old_envelope, old_headers = _optional_read(store, channel_key)
    now, previous = int(time.time()), None
    if previous_channel_time is not None and (type(previous_channel_time) is not int or
                                              not 0 < previous_channel_time <= now):
        raise SDKError('previous channel verification time must be an explicit past Unix timestamp')
    if old_envelope is not None:
        content = crypto.verify_cms(old_envelope, trust, now=previous_channel_time)
        # A publisher can refresh an expired pointer; clients still check current time.
        raw = loads_json(content)
        previous = parse_channel(content, CHANNEL, now=raw.get('issued_at') if isinstance(raw, dict) else None)
        if previous['issued_at'] > now:
            raise SDKError('existing channel is from the future; check the publisher clock')
        if previous_channel_time is not None and previous_channel_time < previous['issued_at']:
            raise SDKError('historical verification time precedes the existing channel issuance')
        if 'etag' not in old_headers:
            raise SDKError('S3 channel response is missing its concurrency ETag')
        if (previous['release']['id'] == release['release_id'] and
                previous['release']['sha256'] != hashlib.sha256(envelope).hexdigest()):
            raise SDKError('current release ID already has different metadata; choose a new ID')
    keep = {release_key}
    # Packages precede indexes. The release envelope is always uploaded last.
    for artifact in sorted(release['artifacts'], key=lambda item: item['kind'] == 'package-index'):
        path = local_artifact(envelope_path.parent, artifact['path'])
        handle, before = open_regular(path)
        key = _key(prefix, artifact_key(artifact, base))
        keep.add(key)
        with handle:
            content_type = 'application/json' if path.suffix == '.json' else 'application/octet-stream'
            _immutable(store, public, key, handle,
                       artifact['size'], artifact['sha256'], content_type)
        local_artifact(envelope_path.parent, artifact['path'])
        verify_stable(path, before)
    _immutable(store, public, release_key, io.BytesIO(envelope), **_digest(envelope),
               content_type='application/pkcs7-mime')
    reference = dict(id=release['release_id'], path=base + '/release.p7m', **_digest(envelope))
    # Upload/verification time must not consume the new channel's validity window.
    now = int(time.time())
    if previous and previous['issued_at'] > now:
        raise SDKError('publisher clock moved behind the existing channel')
    if previous and previous['release'] == reference and previous['expires_at'] > now and not refresh:
        public.verify(channel_key, **_digest(old_envelope))
        _clean_current(store, prefix, keep)
        return previous
    pointer = dict(schema='emberbsd.channel.v1', channel=CHANNEL,
                   generation=previous['generation'] + 1 if previous else 1,
                   issued_at=now, expires_at=now + ttl, release=reference)
    data = json_bytes(pointer)
    parse_channel(data, CHANNEL)
    signed = crypto.sign_cms(data, identity, trust)
    try:
        store.put(channel_key, signed, **_digest(signed), etag=old_headers.get('etag'),
                  cache_control=CHANNEL_CACHE, content_type='application/pkcs7-mime')
    except HTTPStatusError as exc:
        if exc.status in (409, 412):
            raise SDKError('channel changed during publication; inspect it and retry explicitly') from exc
        raise
    public.verify(channel_key, **_digest(signed))
    _clean_current(store, prefix, keep)
    return pointer


def fetch_release(crypto, public, trust, state, output, *, prefix='', repositories=()):
    """Pin one authenticated release and its selected indexes, without payloads."""
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise SDKError('metadata destination already exists; select a new snapshot directory')
    channel_bytes, _ = public.read(_key(prefix, 'current.p7m'), MAX_ENVELOPE,
                                   headers={'cache-control': 'no-cache'})
    for attempt in range(2):
        try:
            return _fetch_snapshot(crypto, public, trust, state, output, prefix, repositories, channel_bytes)
        except HTTPStatusError as exc:
            if exc.status != 404 or attempt:
                raise
            newer, _ = public.read(_key(prefix, 'current.p7m'), MAX_ENVELOPE,
                                   headers={'cache-control': 'no-cache'})
            if newer == channel_bytes:
                raise
            channel_bytes = newer


def _fetch_snapshot(crypto, public, trust, state, output, prefix, repositories, channel_bytes):
    content = crypto.verify_cms(channel_bytes, trust)
    pointer = parse_channel(content, CHANNEL)
    reference = pointer['release']
    envelope, _ = public.read(_key(prefix, reference['path']), MAX_ENVELOPE)
    _check(envelope, reference)
    release = parse_release(crypto.verify_cms(envelope, trust))
    if release['release_id'] != reference['id']:
        raise SDKError('channel and release identities differ')
    base = reference['path'].removesuffix('/release.p7m')
    requested, found = set(repositories), set()
    scratch = Path(tempfile.mkdtemp(prefix='.release-metadata-', dir=output.parent))
    try:
        write_new(scratch / 'release.p7m', envelope)
        for artifact in release['artifacts']:
            if artifact['kind'] != 'package-index':
                continue
            repository = artifact['path'].split('/')[0]
            if requested and repository not in requested:
                continue
            found.add(repository)
            destination = scratch / artifact['path']
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open('xb') as stream:
                public.verify(_key(prefix, base + '/' + artifact['path']),
                              artifact['size'], artifact['sha256'], sink=stream)
        if requested - found:
            raise SDKError('requested repository is not present in this release')
        commit_channel(content, state, CHANNEL)
        publish_directory_noreplace(scratch, output)
        return release
    finally:
        if scratch.exists():
            shutil.rmtree(scratch)
