# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Publish immutable releases before one conditional channel update; fetch metadata."""

import hashlib
import io
from pathlib import Path
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


def publish_release(crypto, store, public, envelope_path, identity, trust, channel,
                    *, prefix='', ttl=7 * 86400, refresh=False, previous_channel_time=None):
    _pattern(channel, _SLUG, 'channel')
    if type(ttl) is not int or ttl <= 0:
        raise SDKError('channel lifetime must be a positive number of seconds')
    envelope_path = Path(envelope_path)
    envelope = read_bytes(envelope_path)
    release = verify_release(crypto, envelope, envelope_path.parent, trust)
    base = 'releases/' + release['release_id']
    release_key = _key(prefix, base + '/release.p7m')
    existing, _ = _optional_read(store, release_key)
    if existing is not None and existing != envelope:
        raise SDKError('release ID is already sealed with different metadata; choose a new ID')
    channel_key = _key(prefix, 'channels/' + channel + '.p7m')
    old_envelope, old_headers = _optional_read(store, channel_key)
    now, previous = int(time.time()), None
    if previous_channel_time is not None and (type(previous_channel_time) is not int or
                                              not 0 < previous_channel_time <= now):
        raise SDKError('previous channel verification time must be an explicit past Unix timestamp')
    if old_envelope is not None:
        content = crypto.verify_cms(old_envelope, trust, now=previous_channel_time)
        # A publisher can refresh an expired pointer; clients still check current time.
        raw = loads_json(content)
        previous = parse_channel(content, channel, now=raw.get('issued_at') if isinstance(raw, dict) else None)
        if previous['issued_at'] > now:
            raise SDKError('existing channel is from the future; check the publisher clock')
        if previous_channel_time is not None and previous_channel_time < previous['issued_at']:
            raise SDKError('historical verification time precedes the existing channel issuance')
        if 'etag' not in old_headers:
            raise SDKError('S3 channel response is missing its concurrency ETag')
    # Packages precede indexes. The release envelope is always uploaded last.
    for artifact in sorted(release['artifacts'], key=lambda item: item['kind'] == 'package-index'):
        path = local_artifact(envelope_path.parent, artifact['path'])
        handle, before = open_regular(path)
        with handle:
            content_type = 'application/json' if path.suffix == '.json' else 'application/octet-stream'
            _immutable(store, public, _key(prefix, base + '/' + artifact['path']), handle,
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
        return previous
    pointer = dict(schema='emberbsd.channel.v1', channel=channel,
                   generation=previous['generation'] + 1 if previous else 1,
                   issued_at=now, expires_at=now + ttl, release=reference)
    data = json_bytes(pointer)
    parse_channel(data, channel)
    signed = crypto.sign_cms(data, identity, trust)
    try:
        store.put(channel_key, signed, **_digest(signed), etag=old_headers.get('etag'),
                  cache_control=CHANNEL_CACHE, content_type='application/pkcs7-mime')
    except HTTPStatusError as exc:
        if exc.status in (409, 412):
            raise SDKError('channel changed during publication; inspect it and retry explicitly') from exc
        raise
    public.verify(channel_key, **_digest(signed))
    return pointer


def fetch_release(crypto, public, trust, channel, state, output, *, prefix='', repositories=()):
    """Pin one authenticated release and its selected indexes, without payloads."""
    _pattern(channel, _SLUG, 'channel')
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise SDKError('metadata destination already exists; select a new snapshot directory')
    channel_bytes, _ = public.read(_key(prefix, 'channels/' + channel + '.p7m'), MAX_ENVELOPE,
                                   headers={'cache-control': 'no-cache'})
    content = crypto.verify_cms(channel_bytes, trust)
    pointer = parse_channel(content, channel)
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
        commit_channel(content, state, channel)
        publish_directory_noreplace(scratch, output)
        return release
    finally:
        if scratch.exists():
            shutil.rmtree(scratch)
