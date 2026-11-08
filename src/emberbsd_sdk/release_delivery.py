# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Keep one signed current repository, replacing obsolete packages after publication."""

from contextlib import contextmanager
import hashlib
import io
import os
import re
import stat
from pathlib import Path
import secrets
import tempfile
import time

from .binary_catalog import load_release_catalogs
from .errors import SDKError
from .jsonio import json_bytes, loads_json
from .paths import open_regular, verify_stable
from .package_cache import directory_lock
from .publication import publish_directory_noreplace
from .release_catalog import (MAX_ENVELOPE, _relative, _pattern, _SLUG, _SHA256, CHANNEL_SCHEMA,
                              file_reference, local_artifact, parse_channel, parse_release)
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
                    *, prefix='', ttl=7 * 86400, refresh=False, previous_channel_time=None,
                    expected_current=None, reuse_current=False):
    if type(ttl) is not int or ttl <= 0:
        raise SDKError('channel lifetime must be a positive number of seconds')
    if expected_current is not None and expected_current != 'absent':
        _pattern(expected_current, _SHA256, 'expected current SHA256')
    if reuse_current and expected_current is None:
        raise SDKError('--reuse-current requires --expected-current')
    envelope_path = Path(envelope_path)
    envelope = read_bytes(envelope_path)
    release = (parse_release(crypto.verify_cms(envelope, trust)) if reuse_current else
               verify_release(crypto, envelope, envelope_path.parent, trust))
    if reuse_current:
        load_release_catalogs(release, envelope_path.parent)
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
                                base, identity, trust, prefix, ttl, refresh, previous_channel_time,
                                expected_current, reuse_current)


def _publish_current(crypto, store, public, envelope_path, envelope, release, base,
                     identity, trust, prefix, ttl, refresh, previous_channel_time, expected_current,
                     reuse_current):
    release_key = _key(prefix, base + '/release.p7m')
    reference = dict(id=release['release_id'], path=base + '/release.p7m', **_digest(envelope))
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
    current_digest = hashlib.sha256(old_envelope).hexdigest() if old_envelope is not None else 'absent'
    stale_current = expected_current is not None and current_digest != expected_current
    # The previous attempt may have switched current before losing its response
    # or failing verification/cleanup. Only the exact authenticated envelope can
    # reconcile that attempt; a refresh is a new conditional channel write.
    current_retry = (previous is not None and previous['release'] == reference and
                     previous['expires_at'] > now and not refresh)
    if stale_current and not current_retry:
        raise SDKError(f'current changed: expected {expected_current}, found {current_digest}; '
                       'merge the latest current before publishing')
    if reuse_current:
        if previous is None:
            raise SDKError('--reuse-current requires an existing authenticated current release')
        old = previous['release']
        previous_release_envelope, _ = public.read(_key(prefix, old['path']), MAX_ENVELOPE)
        _check(previous_release_envelope, old)
        previous_release = parse_release(crypto.verify_cms(previous_release_envelope, trust))
        if previous_release['release_id'] != old['id']:
            raise SDKError('current channel and authenticated release identities differ')
        reusable_ids = _verify_sparse_local(crypto, release, previous_release, envelope_path.parent, trust)
        _require_current_coverage(release, previous_release)
    else:
        reusable_ids = set()
    keep = {release_key}
    # Packages precede indexes. The release envelope is always uploaded last.
    for artifact in sorted(release['artifacts'], key=lambda item: item['kind'] == 'package-index'):
        key = _key(prefix, artifact_key(artifact, base))
        keep.add(key)
        path = _local_if_present(envelope_path.parent, artifact['path'])
        if path is None:
            if artifact['id'] not in reusable_ids:
                raise SDKError(f'local artifact disappeared before upload: {artifact["id"]}')
            headers = public.head(key)
            if int(headers['content-length']) != artifact['size']:
                raise SDKError(f'reused current object size differs: {key}')
            continue
        handle, before = open_regular(path)
        with handle:
            content_type = 'application/json' if path.suffix == '.json' else 'application/octet-stream'
            _immutable(store, public, key, handle,
                       artifact['size'], artifact['sha256'], content_type)
        local_artifact(envelope_path.parent, artifact['path'])
        verify_stable(path, before)
    _immutable(store, public, release_key, io.BytesIO(envelope), **_digest(envelope),
               content_type='application/pkcs7-mime')
    # Upload/verification time must not consume the new channel's validity window.
    now = int(time.time())
    if previous and previous['issued_at'] > now:
        raise SDKError('publisher clock moved behind the existing channel')
    if previous and previous['release'] == reference and previous['expires_at'] > now and not refresh:
        public.verify(channel_key, **_digest(old_envelope))
        _clean_current(store, prefix, keep)
        return previous
    if stale_current:
        # Payload verification can outlive the pointer. A retry with an old
        # snapshot must never turn into an unguarded expiry refresh.
        raise SDKError(f'current changed: expected {expected_current}, found {current_digest}; '
                       'current expired during retry; read current before refreshing')
    pointer = dict(schema=CHANNEL_SCHEMA, channel=CHANNEL,
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


def _local_if_present(root, relative):
    """Return a safely located artifact, or None only when its leaf is absent."""
    candidate = Path(root)
    parts = relative.split('/')
    for part in parts[:-1]:
        candidate /= part
        if candidate.is_symlink():
            return local_artifact(root, relative)
        if not candidate.exists():
            return None
        if not candidate.is_dir():
            return local_artifact(root, relative)
    candidate /= parts[-1]
    if not candidate.exists() and not candidate.is_symlink():
        return None
    return local_artifact(root, relative)


def _slot(artifact):
    if artifact['host']:
        platform = artifact['host']['os'].lower() + '-' + artifact['host']['arch']
        role = 'host'
    else:
        platform, role = artifact['target']['arch'], 'target'
    return artifact['id'], role, platform.casefold()


def _require_current_coverage(candidate, current):
    slots = {_slot(item) for item in candidate['artifacts']}
    missing = [_slot(item) for item in current['artifacts'] if _slot(item) not in slots]
    if missing:
        raise SDKError('candidate release drops current artifacts; preserve each artifact ID and platform')


def _verify_sparse_local(crypto, candidate, current, directory, trust):
    old_by_slot = {_slot(item): item for item in current['artifacts']}
    reusable_ids = set()
    for artifact in candidate['artifacts']:
        path = _local_if_present(directory, artifact['path'])
        if path is None:
            if artifact['kind'] == 'package-index':
                raise SDKError(f'package indexes must be present locally: {artifact["id"]}')
            if old_by_slot.get(_slot(artifact)) != artifact:
                raise SDKError(f'missing artifact is not an exact current match: {artifact["id"]}')
            reusable_ids.add(artifact['id'])
            continue
        if file_reference(path) != {key: artifact[key] for key in ('size', 'sha256')}:
            raise SDKError(f'artifact size or SHA256 mismatch: {artifact["id"]}')
        if artifact['kind'] in ('host-package', 'target-package'):
            crypto.verify_package(path, trust)
    return reusable_ids


class MetadataCache:
    """Hash-addressed copies of immutable release metadata.

    Every entry is used only after its size and SHA256 match the signed
    reference, exactly like a fresh download. The channel pointer itself is
    never cached.
    """

    def __init__(self, root):
        self.root = Path(root)

    def _path(self, reference):
        _pattern(reference['sha256'], _SHA256, 'metadata digest')
        return self.root / reference['sha256']

    def read(self, reference, fetch):
        path = self._path(reference)
        try:
            data = read_bytes(path, reference['size'])
        except (OSError, SDKError):
            data = None
        if data is not None and _digest(data) == {key: reference[key] for key in ('size', 'sha256')}:
            _ignore_os_error(os.utime, path, None)
            return data
        # Anything else at this path is wrong: a changed, oversized or non-regular entry.
        _ignore_os_error(path.unlink, missing_ok=True)
        data = fetch()
        _check(data, reference)
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            write_new(path, data, 0o600)
        except OSError:
            pass  # A cache miss is not an error; another reader may have stored it.
        return data

    def keep(self, references):
        """Drop entries that this release no longer references and stale temporary files."""
        wanted = {reference['sha256'] for reference in references}
        try:
            entries = list(self.root.iterdir())
        except OSError:
            return
        for path in entries:
            if path.name in wanted or path.is_symlink():
                continue
            if re.fullmatch('[0-9a-f]{64}', path.name) or (
                    path.name.startswith('.release-') and _older_than(path, 86400)):
                _ignore_os_error(path.unlink, missing_ok=True)


def _ignore_os_error(call, *args, **kwargs):
    try:
        call(*args, **kwargs)
    except OSError:
        pass  # The cache is an accelerator; a failed entry only costs a download.


def _older_than(path, seconds):
    try:
        return path.lstat().st_mtime < time.time() - seconds
    except OSError:
        return False


@contextmanager
def release_snapshot(crypto, public, trust, directory, *, prefix='', repositories=(), cache=None):
    """Hold authenticated metadata in an owned private directory until exit."""
    channel_bytes, _ = public.read(_key(prefix, 'current.p7m'), MAX_ENVELOPE,
                                  headers={'cache-control': 'no-cache'})
    for attempt in range(2):
        with tempfile.TemporaryDirectory(prefix='.release-metadata-', dir=directory) as work:
            snapshot = Path(work) / 'snapshot'
            snapshot.mkdir(mode=0o700)
            try:
                release = _fetch_snapshot(crypto, public, trust, snapshot, prefix,
                                          repositories, channel_bytes, cache)
            except HTTPStatusError as exc:
                if exc.status != 404 or attempt:
                    raise
                newer, _ = public.read(_key(prefix, 'current.p7m'), MAX_ENVELOPE,
                                      headers={'cache-control': 'no-cache'})
                if newer == channel_bytes:
                    raise
                channel_bytes = newer
                continue
            yield release, snapshot
            return


def fetch_release(crypto, public, trust, output, *, prefix='', repositories=()):
    """Export one authenticated snapshot without replacing a destination entry."""
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise SDKError('metadata destination already exists; select a new snapshot directory')
    with release_snapshot(crypto, public, trust, output.parent, prefix=prefix,
                          repositories=repositories) as (release, snapshot):
        publish_directory_noreplace(snapshot, output)
        return release


def _fetch_snapshot(crypto, public, trust, snapshot, prefix, repositories, channel_bytes, cache=None):
    content = crypto.verify_cms(channel_bytes, trust)
    pointer = parse_channel(content, CHANNEL)
    reference = pointer['release']

    def fetch_envelope():
        envelope, _ = public.read(_key(prefix, reference['path']), MAX_ENVELOPE)
        return envelope

    envelope = fetch_envelope() if cache is None else cache.read(reference, fetch_envelope)
    _check(envelope, reference)
    release = parse_release(crypto.verify_cms(envelope, trust))
    if release['release_id'] != reference['id']:
        raise SDKError('channel and release identities differ')
    base = reference['path'].removesuffix('/release.p7m')
    requested, found = set(repositories), set()
    write_new(snapshot / 'current.p7m', channel_bytes)
    write_new(snapshot / 'release.p7m', envelope)
    cached = [reference]
    for artifact in release['artifacts']:
        if artifact['kind'] != 'package-index':
            continue
        repository = artifact['path'].split('/')[0]
        if requested and repository not in requested:
            continue
        found.add(repository)
        destination = snapshot / artifact['path']
        destination.parent.mkdir(parents=True, exist_ok=True)
        key = _key(prefix, base + '/' + artifact['path'])
        if cache is None:
            with destination.open('xb') as stream:
                public.verify(key, artifact['size'], artifact['sha256'], sink=stream)
            continue

        def fetch_index(key=key, artifact=artifact):
            stream = io.BytesIO()
            public.verify(key, artifact['size'], artifact['sha256'], sink=stream)
            return stream.getvalue()

        write_new(destination, cache.read(artifact, fetch_index))
        cached.append(artifact)
    if requested - found:
        raise SDKError('requested repository is not present in this release')
    if cache is not None:
        cache.keep(cached)
    return release


def fetch_artifact(crypto, public, trust, output, artifact_id, *, prefix=''):
    """Download one artifact from authenticated current and atomically replace output."""
    _pattern(artifact_id, _SLUG, 'artifact id')
    output = Path(output).absolute()
    with directory_lock(output.parent / (output.name + '.sdk-lock')):
        with release_snapshot(crypto, public, trust, output.parent, prefix=prefix) as (release, snapshot):
            selected = [item for item in release['artifacts'] if item['id'] == artifact_id]
            if len(selected) != 1:
                raise SDKError('artifact id is absent or ambiguous in current')
            artifact = selected[0]
            current = parse_channel(crypto.verify_cms((snapshot / 'current.p7m').read_bytes(), trust), CHANNEL)
            base = current['release']['path'].removesuffix('/release.p7m')
            key = _key(prefix, artifact_key(artifact, base))
            try:
                before = output.lstat()
                if not stat.S_ISREG(before.st_mode):
                    raise SDKError('artifact output must be a regular file or absent')
            except FileNotFoundError:
                before = None
            fd, temporary = tempfile.mkstemp(prefix='.artifact-', dir=output.parent)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    public.verify(key, artifact['size'], artifact['sha256'], sink=stream)
                    os.fchmod(stream.fileno(), stat.S_IMODE(before.st_mode) if before else 0o644)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    after = output.lstat()
                except FileNotFoundError:
                    after = None
                if ((before is None) != (after is None) or
                        (before is not None and (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
                         (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns))):
                    raise SDKError('artifact output changed during download')
                directory = os.open(output.parent, os.O_RDONLY)
                try:
                    os.replace(temporary, output)
                    try:
                        os.fsync(directory)
                    except OSError as exc:
                        raise SDKError(f'artifact installed, but output directory sync failed: {exc}') from exc
                finally:
                    os.close(directory)
            finally:
                if os.path.lexists(temporary):
                    os.unlink(temporary)
            return artifact
