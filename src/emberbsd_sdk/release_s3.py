# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Streaming HTTPS and path-style S3 SigV4, using Python's standard library.

No redirects, credential logging, package execution or implicit retries.
ETags are opaque concurrency tokens, never artifact checksums.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import http.client
import os
import re
import ssl
from urllib.parse import quote, urlsplit
import xml.etree.ElementTree as ET

from .errors import SDKError
from .release_catalog import _relative

EMPTY_SHA256 = hashlib.sha256(b'').hexdigest()
BLOCK_SIZE = 1024 * 1024
MAX_PUT_SIZE = 5 * 1024 ** 3


class HTTPStatusError(SDKError):
    def __init__(self, status, method, key):
        self.status = status
        super().__init__(f'{method} {key}: HTTP {status}')


def sign_request(method, path, headers, region, access_key, secret_key):
    """S3 header authentication for an encoded path and sorted encoded query."""
    path, _, query = path.partition('?')
    names = sorted(headers)
    canonical_headers = ''.join(name + ':' + ' '.join(headers[name].split()) + '\n' for name in names)
    signed = ';'.join(names)
    timestamp = headers['x-amz-date']
    scope = timestamp[:8] + '/' + region + '/s3/aws4_request'
    canonical = '\n'.join((method, path, query, canonical_headers, signed, headers['x-amz-content-sha256']))
    value = '\n'.join(('AWS4-HMAC-SHA256', timestamp, scope, hashlib.sha256(canonical.encode()).hexdigest()))
    key = ('AWS4' + secret_key).encode()
    for part in (timestamp[:8], region, 's3', 'aws4_request'):
        key = hmac.digest(key, part.encode(), 'sha256')
    signature = hmac.new(key, value.encode(), hashlib.sha256).hexdigest()
    return f'AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed}, Signature={signature}'


class HTTPStore:
    def __init__(self, base_url, *, ca_file=None, timeout=60):
        url = urlsplit(base_url)
        if (url.scheme != 'https' or not url.hostname or url.username is not None or
                url.password is not None or url.query or url.fragment or
                any(ord(char) < 33 for char in base_url)):
            raise SDKError('repository base must be an HTTPS URL without credentials, query or fragment')
        self.host = url.netloc
        self.prefix = url.path.strip('/')
        if self.prefix:
            _relative(self.prefix)
        self.timeout = timeout
        self.context = ssl.create_default_context(cafile=ca_file)

    def _headers(self, method, path, headers):
        return headers

    def request(self, method, key, *, headers=None, body=None, statuses=(200,)):
        _relative(key)
        path = '/' + '/'.join(item for item in (self.prefix, key) if item)
        path = quote(path, safe='/~')
        return self._request(method, path, headers=headers, body=body, statuses=statuses)

    @contextmanager
    def _request(self, method, path, *, headers=None, body=None, statuses=(200,)):
        headers = dict(headers or {})
        headers.setdefault('host', self.host)
        headers.setdefault('accept-encoding', 'identity')
        connection = http.client.HTTPSConnection(self.host, timeout=self.timeout, context=self.context,
                                                  blocksize=64 * 1024)
        try:
            connection.request(method, path, body=body, headers=self._headers(method, path, headers))
            response = connection.getresponse()
            if response.status not in statuses:
                raise HTTPStatusError(response.status, method, path)
            if response.getheader('Content-Encoding', 'identity') != 'identity':
                raise SDKError('HTTP content encoding changes signed artifact bytes')
            yield response
        except (OSError, http.client.HTTPException) as exc:
            raise SDKError(f'{method} {path}: HTTPS transport failed ({type(exc).__name__})') from exc
        finally:
            connection.close()

    @staticmethod
    def _length(response):
        value = response.getheader('Content-Length', '')
        if not re.fullmatch('[0-9]+', value):
            raise SDKError('HTTP response requires a valid Content-Length')
        return int(value)

    def head(self, key):
        with self.request('HEAD', key) as response:
            self._length(response)
            return {name.lower(): value for name, value in response.getheaders()}

    def read(self, key, limit, *, headers=None):
        with self.request('GET', key, headers=headers) as response:
            size = self._length(response)
            if size > limit:
                raise SDKError('HTTP metadata exceeds the size limit')
            data = response.read(size + 1)
            if len(data) != size:
                raise SDKError('truncated HTTP metadata')
            return data, {name.lower(): value for name, value in response.getheaders()}

    def verify(self, key, size, sha256, *, sink=None):
        with self.request('GET', key, headers={'cache-control': 'no-cache'}) as response:
            if self._length(response) != size:
                raise SDKError(f'published size differs: {key}')
            digest, received = hashlib.sha256(), 0
            while block := response.read(BLOCK_SIZE):
                received += len(block)
                if received > size:
                    raise SDKError(f'published size exceeds inventory: {key}')
                digest.update(block)
                if sink is not None:
                    try:
                        sink.write(block)
                    except OSError as exc:
                        raise SDKError(f'cannot store downloaded artifact: {exc}') from exc
            if received != size or digest.hexdigest() != sha256:
                raise SDKError(f'published bytes differ from inventory: {key}')

    def range(self, key, start, end, total):
        if not 0 <= start <= end < total:
            raise SDKError('invalid requested byte range')
        with self.request('GET', key, headers={'range': f'bytes={start}-{end}'}, statuses=(206,)) as response:
            if (response.getheader('Content-Range') != f'bytes {start}-{end}/{total}' or
                    self._length(response) != end - start + 1):
                raise SDKError('server returned a different byte range')
            data = response.read(end - start + 2)
            if len(data) != end - start + 1:
                raise SDKError('truncated HTTP byte range')
            return data


class S3Store(HTTPStore):
    def __init__(self, endpoint, region, bucket, access_key, secret_key, *, ca_file=None, timeout=60):
        super().__init__(endpoint, ca_file=ca_file, timeout=timeout)
        if (not re.fullmatch('[A-Za-z0-9][A-Za-z0-9._-]*', bucket) or
                not re.fullmatch('[A-Za-z0-9-]+', region) or not access_key or not secret_key):
            raise SDKError('S3 requires bucket, region and credentials')
        self.prefix = '/'.join(item for item in (self.prefix, bucket) if item)
        self.region, self.access_key, self.secret_key = region, access_key, secret_key

    @classmethod
    def from_environment(cls, *, endpoint=None, region=None, bucket=None, ca_file=None):
        values = {key: os.environ.get('EMBERBSD_S3_' + key, '') for key in
                  ('ENDPOINT', 'REGION', 'BUCKET', 'ACCESS_KEY', 'SECRET_KEY')}
        return cls(endpoint or values['ENDPOINT'], region or values['REGION'], bucket or values['BUCKET'],
                   values['ACCESS_KEY'], values['SECRET_KEY'], ca_file=ca_file)

    def _headers(self, method, path, headers):
        headers.setdefault('x-amz-content-sha256', EMPTY_SHA256)
        headers['x-amz-date'] = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        headers['authorization'] = sign_request(method, path, headers, self.region, self.access_key, self.secret_key)
        return headers

    def put(self, key, body, size, sha256, *, etag=None,
            cache_control='no-cache, max-age=0, must-revalidate', content_type='application/octet-stream'):
        if not 0 <= size <= MAX_PUT_SIZE:
            raise SDKError('S3 single PUT supports objects up to 5 GiB; multipart is not implemented')
        headers = {'content-length': str(size), 'x-amz-content-sha256': sha256,
                   'cache-control': cache_control, 'content-type': content_type}
        if etag is None:
            headers['if-none-match'] = '*'
        else:
            if not re.fullmatch(r'"[^"\r\n]+"', etag):
                raise SDKError('S3 conditional update requires a quoted ETag')
            # S3 takes the ETag value, without the GET response's quoting.
            headers['if-match'] = etag[1:-1]
        with self.request('PUT', key, headers=headers, body=body) as response:
            return {name.lower(): value for name, value in response.getheaders()}

    def delete(self, key):
        """Delete an exact caller-owned key; publication owns only its prefix."""
        with self.request('DELETE', key, statuses=(204, 200)):
            pass

    def list_keys(self, prefix):
        """Enumerate a caller-owned prefix, using paginated S3 ListObjectsV2."""
        if not prefix.endswith('/'):
            raise SDKError('listing requires a directory prefix')
        _relative(prefix[:-1])
        query = {'list-type': '2', 'prefix': prefix}
        tokens = set()
        while True:
            encoded = '&'.join(quote(key, safe='~') + '=' + quote(value, safe='~')
                               for key, value in sorted(query.items()))
            path = '/' + quote(self.prefix, safe='/~') + '?' + encoded
            with self._request('GET', path) as response:
                # ListObjectsV2 can use chunked transfer, unlike signed object
                # downloads whose exact Content-Length is part of our contract.
                data = response.read(8 * 1024 * 1024 + 1)
                if len(data) > 8 * 1024 * 1024:
                    raise SDKError('S3 listing page is too large')
            try:
                root = ET.fromstring(data)
                for child in root.findall('{*}Contents'):
                    key = child.findtext('{*}Key')
                    if not key or not key.startswith(prefix):
                        raise SDKError('S3 listing escaped the selected prefix')
                    _relative(key)
                    yield key
                truncated = root.findtext('{*}IsTruncated')
                if truncated == 'false':
                    return
                token = root.findtext('{*}NextContinuationToken')
                if truncated != 'true' or not token or token in tokens:
                    raise SDKError('invalid S3 listing pagination')
                tokens.add(token)
                query['continuation-token'] = token
            except ET.ParseError as exc:
                raise SDKError('invalid S3 listing XML') from exc
