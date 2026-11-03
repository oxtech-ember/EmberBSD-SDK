# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""S3 signing and bounded HTTP transport, using a local object-server fixture."""

import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import ssl
from pathlib import Path
import sys
import threading
import unittest
from urllib.parse import urlsplit, parse_qs
import xml.etree.ElementTree as ET
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.release_s3 import HTTPStore, S3Store, HTTPStatusError, sign_request


class SignatureTests(unittest.TestCase):
    def test_aws_published_get_object_vector(self):
        # https://docs.aws.amazon.com/AmazonS3/latest/developerguide/sig-v4-header-based-auth.html
        headers = {'host': 'examplebucket.s3.amazonaws.com', 'range': 'bytes=0-9',
                   'x-amz-date': '20130524T000000Z',
                   'x-amz-content-sha256': hashlib.sha256(b'').hexdigest()}
        value = sign_request('GET', '/test.txt', headers, 'us-east-1',
                             'AKIAIOSFODNN7EXAMPLE',
                             'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY')
        self.assertTrue(value.endswith('Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41'))

    def test_rejects_credentials_and_insecure_base_urls(self):
        for url in ('http://example.org', 'https://user:secret@example.org',
                    'https://example.org/?token=secret', 'https://example.org/#fragment'):
            with self.subTest(url=url), self.assertRaises(SDKError):
                HTTPStore(url)


class ObjectHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_PUT(self):
        data = self.rfile.read(int(self.headers['Content-Length']))
        with self.server.lock:
            old = self.server.objects.get(self.path)
            etag = '"' + hashlib.md5(old).hexdigest() + '"' if old is not None else None
            if ((self.headers.get('If-None-Match') == '*' and old is not None) or
                    (self.headers.get('If-Match') and
                     self.headers['If-Match'].strip('"') != (etag or '').strip('"'))):
                self.send_response(412)
                self.end_headers()
                return
            self.server.objects[self.path] = data
            self.server.headers[self.path] = dict(self.headers)
        self.send_response(200)
        self.send_header('ETag', '"' + hashlib.md5(data).hexdigest() + '"')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_HEAD(self):
        self.get(head=True)

    def do_GET(self):
        query = parse_qs(urlsplit(self.path).query)
        if query.get('list-type') == ['2']:
            root = ET.Element('ListBucketResult')
            prefix = query['prefix'][0]
            start = int(query.get('continuation-token', ['0'])[0])
            keys = sorted(key.removeprefix('/bucket/') for key in self.server.objects
                          if key.startswith('/bucket/' + prefix))
            batch = keys[start:start + 2]  # Force pagination in tests.
            more = start + len(batch) < len(keys)
            ET.SubElement(root, 'IsTruncated').text = str(more).lower()
            if more:
                ET.SubElement(root, 'NextContinuationToken').text = str(start + len(batch))
            for key in batch:
                ET.SubElement(ET.SubElement(root, 'Contents'), 'Key').text = key
            data = ET.tostring(root)
            self.send_response(200)
            # S3 listings may be streamed without Content-Length (Timeweb).
            self.end_headers()
            self.wfile.write(data)
            return
        self.get()

    def do_DELETE(self):
        with self.server.lock:
            self.server.objects.pop(self.path, None)
        self.send_response(204)
        self.end_headers()

    def get(self, head=False):
        data = self.server.objects.get(self.path)
        if data is None:
            self.send_response(404)
            self.end_headers()
            return
        etag = '"' + hashlib.md5(data).hexdigest() + '"'
        selected = self.headers.get('Range')
        if selected:
            start, end = map(int, selected.removeprefix('bytes=').split('-'))
            self.send_response(206)
            self.send_header('Content-Range', f'bytes {start}-{end}/{len(data)}')
            data = data[start:end + 1]
        else:
            self.send_response(200)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('ETag', etag)
        self.end_headers()
        if not head:
            self.wfile.write(data)


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), ObjectHandler)
        self.server.objects, self.server.headers, self.server.lock = {}, {}, threading.Lock()
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = 'https://127.0.0.1:' + str(self.server.server_port)
        # Real HTTP I/O in this fixture; production always uses HTTPSConnection.
        self.factory = patch('emberbsd_sdk.release_s3.http.client.HTTPSConnection',
            side_effect=lambda host, **kwargs: http.client.HTTPConnection(host, timeout=kwargs['timeout']))
        self.factory.start()
        self.addCleanup(self.factory.stop)
        self.store = S3Store(self.url, 'ru-1', 'bucket', 'fixture-access', 'fixture-secret')
        self.public = HTTPStore(self.url + '/bucket')

    def test_conditional_put_and_streamed_read(self):
        data = b'EmberBSD' * 10000
        digest = hashlib.sha256(data).hexdigest()
        self.store.put('releases/test/file', io.BytesIO(data), len(data), digest,
                       cache_control='public, max-age=31536000, immutable')
        self.public.verify('releases/test/file', len(data), digest)
        headers = self.store.head('releases/test/file')
        with self.assertRaises(HTTPStatusError) as error:
            self.store.put('releases/test/file', b'x', 1, hashlib.sha256(b'x').hexdigest())
        self.assertEqual(error.exception.status, 412)
        self.store.put('releases/test/file', b'x', 1, hashlib.sha256(b'x').hexdigest(), etag=headers['etag'])
        self.assertEqual(self.public.read('releases/test/file', 1)[0], b'x')
        sent = self.server.headers['/bucket/releases/test/file']
        self.assertIn('AWS4-HMAC-SHA256', {key.lower(): value for key, value in sent.items()}['authorization'])
        self.assertEqual({key.lower(): value for key, value in sent.items()}['if-match'],
                         headers['etag'][1:-1])

    def test_wrong_hash_and_truncation_are_rejected(self):
        self.server.objects['/bucket/file'] = b'data'
        for size, digest in ((4, '0' * 64), (5, hashlib.sha256(b'data').hexdigest())):
            with self.subTest(size=size), self.assertRaises(SDKError):
                self.public.verify('file', size, digest)
        with self.assertRaises(SDKError):
            self.public.read('file', 3)

    def test_range_has_exact_bounds(self):
        self.server.objects['/bucket/file'] = b'0123456789'
        self.assertEqual(self.public.range('file', 3, 6, 10), b'3456')

    def test_missing_object_and_unsafe_paths(self):
        with self.assertRaises(HTTPStatusError) as error:
            self.store.head('missing')
        self.assertEqual(error.exception.status, 404)
        for key in ('../escape', 'x?token=y', '/absolute', 'bad%2fpath'):
            with self.subTest(key=key), self.assertRaises(SDKError):
                self.store.head(key)

    def test_list_current_is_paginated_and_delete_is_exact(self):
        for key in ('current/a', 'current/b', 'current/c', 'another/keep'):
            self.server.objects['/bucket/' + key] = b'x'
        self.assertEqual(list(self.store.list_keys('current/')),
                         ['current/a', 'current/b', 'current/c'])
        self.store.delete('current/b')
        self.assertNotIn('/bucket/current/b', self.server.objects)
        self.assertIn('/bucket/another/keep', self.server.objects)


class HTTPSRetryTests(unittest.TestCase):
    def setUp(self):
        self.store = HTTPStore('https://example.org')

    def response(self, *, status=200, data=b'payload'):
        response = Mock(status=status)
        response.getheader.side_effect = lambda key, default=None: (
            str(len(data)) if key == 'Content-Length' else default)
        response.getheaders.return_value = [('Content-Length', str(len(data)))]
        response.read.return_value = data
        return response

    def test_s3_retry_resigns_without_previous_authorization(self):
        store = S3Store('https://example.org', 'region-1', 'bucket', 'access', 'secret')
        failed, success = Mock(), Mock()
        failed.getresponse.side_effect = TimeoutError()
        success.getresponse.return_value = self.response()
        with patch('emberbsd_sdk.release_s3.http.client.HTTPSConnection',
                   side_effect=[failed, success]):
            with store.request('GET', 'object') as response:
                self.assertEqual(response.status, 200)
        for connection in (failed, success):
            sent = dict(connection.request.call_args.kwargs['headers'])
            authorization = sent.pop('authorization')
            self.assertNotIn('authorization', authorization.split('SignedHeaders=')[1].split(',')[0].split(';'))
            self.assertEqual(authorization, sign_request('GET', '/bucket/object', sent,
                                                          'region-1', 'access', 'secret'))
            connection.close.assert_called_once_with()

    def test_safe_reads_retry_connection_failures_and_close_connections(self):
        for method in ('GET', 'HEAD'):
            for error in (TimeoutError(), ConnectionError(), http.client.RemoteDisconnected()):
                for phase in ('request', 'getresponse'):
                    with self.subTest(method=method, error=type(error).__name__, phase=phase):
                        failed, success = Mock(), Mock()
                        getattr(failed, phase).side_effect = error
                        success.getresponse.return_value = self.response()
                        with patch('emberbsd_sdk.release_s3.http.client.HTTPSConnection',
                                   side_effect=[failed, success]) as factory:
                            with self.store.request(method, 'object') as response:
                                self.assertEqual(response.status, 200)
                        self.assertEqual(factory.call_count, 2)
                        failed.close.assert_called_once_with()
                        success.close.assert_called_once_with()

    def test_safe_read_stops_after_three_failures(self):
        connections = [Mock() for _ in range(3)]
        for connection in connections:
            connection.getresponse.side_effect = TimeoutError()
        with patch('emberbsd_sdk.release_s3.http.client.HTTPSConnection',
                   side_effect=connections) as factory:
            with self.assertRaises(SDKError):
                with self.store.request('GET', 'object'):
                    self.fail('no response expected')
        self.assertEqual(factory.call_count, 3)
        for connection in connections:
            connection.close.assert_called_once_with()

    def test_mutations_and_requests_with_body_are_never_retried(self):
        for method, body in (('PUT', b'payload'), ('DELETE', None), ('GET', b'payload')):
            with self.subTest(method=method):
                connection = Mock()
                connection.getresponse.side_effect = TimeoutError()
                with patch('emberbsd_sdk.release_s3.http.client.HTTPSConnection',
                           return_value=connection) as factory:
                    with self.assertRaises(SDKError):
                        with self.store.request(method, 'object', body=body):
                            self.fail('no response expected')
                self.assertEqual(factory.call_count, 1)
                connection.close.assert_called_once_with()

    def test_certificate_status_integrity_and_body_failures_are_not_retried(self):
        for failure in ('certificate', 'status', 'integrity', 'body'):
            with self.subTest(failure=failure):
                connection = Mock()
                response = self.response(status=503 if failure == 'status' else 200)
                connection.getresponse.return_value = response
                if failure == 'certificate':
                    connection.getresponse.side_effect = ssl.SSLCertVerificationError('invalid certificate')
                elif failure == 'body':
                    response.read.side_effect = TimeoutError()
                elif failure == 'integrity':
                    response.read.return_value = b'wrong'
                with patch('emberbsd_sdk.release_s3.http.client.HTTPSConnection',
                           return_value=connection) as factory:
                    with self.assertRaises(SDKError):
                        if failure == 'integrity':
                            self.store.verify('object', 7, hashlib.sha256(b'payload').hexdigest())
                        else:
                            self.store.read('object', 100)
                self.assertEqual(factory.call_count, 1)
                connection.close.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
