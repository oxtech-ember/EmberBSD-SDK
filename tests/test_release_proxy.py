# SPDX-License-Identifier: BSD-2-Clause
"""Real TLS through a loopback CONNECT proxy; no external network."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import select
import os
import socket
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.release_s3 import HTTPStore, S3Store
from emberbsd_sdk.release_signing import ReleaseCrypto


class Origin(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        body = b'proxied TLS payload'
        self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers()
        self.wfile.write(body)

class Proxy(socketserver.StreamRequestHandler):
    def handle(self):
        first = self.rfile.readline(4096).decode('ascii').strip()
        self.server.connects.append(first)
        while self.rfile.readline(4096) not in (b'\r\n', b'\n', b''):
            pass
        if self.server.reject:
            self.wfile.write(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n'); return
        upstream = socket.create_connection(('127.0.0.1', self.server.origin_port), timeout=5)
        self.wfile.write(b'HTTP/1.1 200 Connection Established\r\n\r\n'); self.wfile.flush()
        with upstream:
            while True:
                ready, _, _ = select.select([self.connection, upstream], [], [], 5)
                if not ready: return
                for source in ready:
                    try: data = source.recv(65536)
                    except ConnectionResetError: return
                    if not data: return
                    (upstream if source is self.connection else self.connection).sendall(data)

class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

@unittest.skipUnless(os.environ.get('EMBERBSD_TEST_HOST_PREFIX'), 'select native Ports OpenSSL')
class ProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(); cls.addClassCleanup(cls.tmp.cleanup)
        root = Path(cls.tmp.name); cls.cert = root / 'cert.pem'; key = root / 'key.pem'
        openssl = ReleaseCrypto.from_prefix(os.environ['EMBERBSD_TEST_HOST_PREFIX']).prefix.tool('openssl')
        subprocess.run([openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-keyout', str(key), '-out', str(cls.cert), '-subj', '/CN=localhost',
                        '-addext', 'subjectAltName=DNS:localhost'], check=True, capture_output=True)
        cls.sni = []
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.load_cert_chain(cls.cert, key)
        context.set_servername_callback(lambda sock, name, ctx: cls.sni.append(name))
        cls.origin = ThreadingHTTPServer(('127.0.0.1', 0), Origin)
        cls.origin.socket = context.wrap_socket(cls.origin.socket, server_side=True)
        threading.Thread(target=cls.origin.serve_forever, daemon=True).start()
        cls.addClassCleanup(cls.origin.server_close); cls.addClassCleanup(cls.origin.shutdown)
        cls.proxy = Server(('127.0.0.1', 0), Proxy); cls.proxy.origin_port = cls.origin.server_port
        cls.proxy.connects = []; cls.proxy.reject = False
        threading.Thread(target=cls.proxy.serve_forever, daemon=True).start()
        cls.addClassCleanup(cls.proxy.server_close); cls.addClassCleanup(cls.proxy.shutdown)
        cls.proxy_url = 'http://127.0.0.1:' + str(cls.proxy.server_address[1])
        cls.url = 'https://localhost:' + str(cls.origin.server_port)

    def test_connect_tls_sni_hostname_and_body(self):
        with HTTPStore(self.url, ca_file=str(self.cert), proxy_url=self.proxy_url) as store:
            self.assertEqual(store.read('object', 100)[0], b'proxied TLS payload')
        self.assertTrue(any(line.startswith('CONNECT localhost:' + str(self.origin.server_port) + ' HTTP/1.') for line in self.proxy.connects))
        self.assertIn('localhost', self.sni)
        with HTTPStore('https://127.0.0.1:' + str(self.origin.server_port), ca_file=str(self.cert),
                       proxy_url=self.proxy_url) as store:
            with self.assertRaises(SDKError): store.read('object', 100)

    def test_untrusted_certificate_and_connect_refusal(self):
        with HTTPStore(self.url, proxy_url=self.proxy_url) as store:
            with self.assertRaises(SDKError): store.read('object', 100)
        self.proxy.reject = True
        try:
            with HTTPStore(self.url, ca_file=str(self.cert), proxy_url=self.proxy_url) as store:
                with self.assertRaises(SDKError): store.read('object', 100)
        finally:
            self.proxy.reject = False

    def test_invalid_proxy_urls(self):
        for url in ('https://localhost:1', 'http://user:pass@localhost:1', 'http://localhost:1/path',
                    'http://localhost:1/?x=y', 'http://localhost:1/#x', 'http://localhost:1\r\nX: y'):
            with self.subTest(url=url), self.assertRaises(SDKError): HTTPStore(self.url, proxy_url=url)

    def test_s3_proxy_constructor(self):
        with S3Store(self.url, 'region-1', 'bucket', 'access', 'secret', ca_file=str(self.cert),
                     proxy_url=self.proxy_url) as store:
            self.assertEqual(store.read('object', 100)[0], b'proxied TLS payload')

    def test_s3_from_environment_proxy(self):
        with patch.dict('os.environ', {'EMBERBSD_S3_ENDPOINT': self.url, 'EMBERBSD_S3_REGION': 'region-1',
                                       'EMBERBSD_S3_BUCKET': 'bucket', 'EMBERBSD_S3_ACCESS_KEY': 'access',
                                       'EMBERBSD_S3_SECRET_KEY': 'secret'}):
            with S3Store.from_environment(ca_file=str(self.cert), proxy_url=self.proxy_url) as store:
                self.assertEqual(store.read('object', 100)[0], b'proxied TLS payload')

if __name__ == '__main__': unittest.main()
