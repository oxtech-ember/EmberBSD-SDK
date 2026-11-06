#!/usr/bin/env python3
"""One-time reviewed v1 -> v2 publication artifact. NOT an installed SDK command.

Import with the reviewed v2 SDK on PYTHONPATH. No automatic production CLI is
provided. Before invoking cutover, the operator must retain the exact old signed
channel/release and provide actual bootstrap/client readiness evidence: the
candidate bootstrap hash and executed checks of its v2 reader. A boolean flag
is not that evidence. Consumer updates and S3 cutover may proceed sequentially
(the owner accepts a temporary mismatch); an old reader must fail clearly, not
use a legacy fallback. Switching S3 current is NOT atomic with GitHub main or
installed clients. This artifact neither deploys those consumers nor proves production
backend conditional PUT semantics. Test those prerequisites separately.
"""
import hashlib
import io
from pathlib import Path
import time

from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.jsonio import json_bytes, loads_json
from emberbsd_sdk.paths import open_regular, verify_stable
from emberbsd_sdk.release_catalog import (
    CHANNEL_SCHEMA, MAX_ENVELOPE, _SHA256, _pattern, _integer,
    local_artifact, parse_channel, parse_release)
from emberbsd_sdk.release_cli import verify_release
from emberbsd_sdk.release_delivery import (
    CHANNEL, CHANNEL_CACHE, _check, _clean_current, _digest, _immutable, _key,
    _publisher_lock, _require_current_coverage, artifact_key)
from emberbsd_sdk.release_signing import read_bytes


def cutover(crypto, store, public, candidate_path, previous_channel_path,
            previous_release_path, identity, trust, *, expected_current,
            prefix='', ttl=7 * 86400):
    """Switch one exact signed v1 snapshot, or finish an exact v2 retry.

    Native crypto verifies every package in the complete local candidate. No
    sparse/local-missing reuse, reset, compatibility alias or expiry refresh.
    Failures after current PUT can have committed: inspect and retry this exact
    input, never assume failure means no switch. Keep the input snapshot intact.
    """
    _pattern(expected_current, _SHA256, 'expected old current SHA256')
    _integer(ttl, 'channel lifetime')
    old_bytes = read_bytes(previous_channel_path)
    if hashlib.sha256(old_bytes).hexdigest() != expected_current:
        raise SDKError('saved old current does not match the expected SHA256')
    candidate_path = Path(candidate_path)
    envelope = read_bytes(candidate_path)
    candidate = verify_release(crypto, envelope, candidate_path.parent, trust)
    base = 'current/metadata/' + hashlib.sha256(envelope).hexdigest()
    release_key = _key(prefix, base + '/release.p7m')
    reference = dict(id=candidate['release_id'], path=base + '/release.p7m', **_digest(envelope))
    keys = [artifact_key(item, base) for item in candidate['artifacts']]
    if len({key.casefold() for key in keys}) != len(keys):
        raise SDKError('candidate artifacts collide in publication paths')
    packages = set()
    for item, key in zip(candidate['artifacts'], keys):
        if item['kind'] in ('host-package', 'target-package'):
            directory, name = key.rsplit('/', 1)
            if not name.endswith('.tgz') or '-' not in name:
                raise SDKError('candidate requires standard versioned package names')
            slot = directory.casefold(), name.rsplit('-', 1)[0].casefold()
            if slot in packages:
                raise SDKError('candidate contains multiple versions of one package')
            packages.add(slot)
    channel_key = _key(prefix, 'current.p7m')
    with _publisher_lock(store, prefix):
        current_bytes, headers = store.read(channel_key, MAX_ENVELOPE,
                                             headers={'cache-control': 'no-cache'})
        retry = current_bytes != old_bytes
        if retry:
            current = parse_channel(crypto.verify_cms(current_bytes, trust), CHANNEL)
            if current['release'] != reference:
                raise SDKError('current changed; not this exact cutover result')
        else:
            # Only an initial switch depends on the retired signer/snapshot.
            # Exact v2 retry trusts its current signed reference instead.
            old = loads_json(crypto.verify_cms(old_bytes, trust))
            if not isinstance(old, dict) or old.get('schema') != 'emberbsd.channel.v1':
                raise SDKError('cutover requires a saved signed v1 channel')
            _integer(old.get('generation'), 'old channel generation')
            # This is the sole one-time v1 parser. The installed SDK has no adapter.
            converted = dict(old)
            del converted['generation']
            converted['schema'] = CHANNEL_SCHEMA
            old_channel = parse_channel(json_bytes(converted), CHANNEL)
            old_release_bytes = read_bytes(previous_release_path)
            _check(old_release_bytes, old_channel['release'])
            old_release = parse_release(crypto.verify_cms(old_release_bytes, trust))
            if old_release['release_id'] != old_channel['release']['id']:
                raise SDKError('saved old channel and release identities differ')

            _require_current_coverage(candidate, old_release)
            if (candidate['release_id'] == old_release['release_id'] and
                    envelope != old_release_bytes):
                raise SDKError('release ID already has different signed metadata')
            current = None
        if 'etag' not in headers:
            raise SDKError('current response lacks concurrency ETag')
        keep = {release_key}
        for item in sorted(candidate['artifacts'], key=lambda item: item['kind'] == 'package-index'):
            key = _key(prefix, artifact_key(item, base))
            keep.add(key)
            path = local_artifact(candidate_path.parent, item['path'])
            handle, before = open_regular(path)
            with handle:
                _immutable(store, public, key, handle, item['size'], item['sha256'],
                           'application/octet-stream')
            local_artifact(candidate_path.parent, item['path'])
            verify_stable(path, before)
        _immutable(store, public, release_key, io.BytesIO(envelope), **_digest(envelope),
                   content_type='application/pkcs7-mime')
        if retry:
            # Recheck expiry after potentially lengthy payload verification.
            parse_channel(crypto.verify_cms(current_bytes, trust), CHANNEL)
            public.verify(channel_key, **_digest(current_bytes))
            _clean_current(store, prefix, keep)
            return current
        # Reject an expired saved pointer rather than silently broadening scope.
        parse_channel(json_bytes(converted), CHANNEL)
        now = int(time.time())
        pointer = dict(schema=CHANNEL_SCHEMA, channel=CHANNEL, issued_at=now,
                       expires_at=now + ttl, release=reference)
        data = json_bytes(pointer)
        parse_channel(data, CHANNEL)
        signed = crypto.sign_cms(data, identity, trust)
        store.put(channel_key, signed, **_digest(signed), etag=headers['etag'],
                  cache_control=CHANNEL_CACHE, content_type='application/pkcs7-mime')
        public.verify(channel_key, **_digest(signed))
        _clean_current(store, prefix, keep)
        return pointer


if __name__ == '__main__':
    raise SystemExit('No deployment CLI: review prerequisites and invoke only in an explicitly authorized cutover.')
