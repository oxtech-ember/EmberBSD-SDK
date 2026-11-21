#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Exercise the bootstrap downloader against interrupted HTTPS transfers."""
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest


INSTALLER = Path(__file__).resolve().parents[1] / 'scripts/install-sdk.sh'
PAYLOAD = b'EmberBSD bootstrap retry\x00payload\n'


class BootstrapDownloadTest(unittest.TestCase):
    def test_installer_uses_only_current_channel_contract(self):
        source = INSTALLER.read_text()
        self.assertIn('emberbsd.channel.v2', source)
        for retired in ('emberbsd.channel.v1', 'commit_channel', 'channel.json'):
            self.assertNotIn(retired, source)
        subprocess.run(['/bin/sh', '-n', str(INSTALLER)], check=True)

    def test_handshake_timeout_retries_and_downloads_exact_bytes(self):
        self.transfer('handshake')

    def test_partial_archive_resumes_at_the_saved_offset(self):
        self.transfer('partial')

    def test_server_ignoring_range_restarts_the_archive(self):
        self.transfer('no-range')

    def transfer(self, fault):
        source = INSTALLER.read_text()
        match = re.search(r'(?ms)^get\(\) \{.*?(?=^field\(\))', source)
        self.assertIsNotNone(match, 'installer get() not found')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key, cert = root / 'key.pem', root / 'cert.pem'
            config = root / 'openssl.cnf'
            config.write_text('[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n'
                              '[dn]\nCN=localhost\n[ext]\nsubjectAltName=DNS:localhost\n'
                              'basicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,digitalSignature,keyEncipherment\n')
            subprocess.run(['/usr/bin/openssl', 'req', '-x509', '-newkey', 'rsa:2048',
                            '-nodes', '-keyout', str(key), '-out', str(cert), '-days', '1',
                            '-config', str(config)], check=True, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            listener = socket.socket()
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(('127.0.0.1', 0))
            listener.listen(4)
            listener.settimeout(25)
            port = listener.getsockname()[1]
            errors, accepted = [], []

            def reply(status, content, size, extra=b''):
                connection, _ = listener.accept()
                with context.wrap_socket(connection, server_side=True) as client:
                    client.settimeout(5)
                    request = bytearray()
                    while b'\r\n\r\n' not in request:
                        data = client.recv(8192)
                        if not data:
                            raise EOFError('incomplete HTTP request')
                        request.extend(data)
                    client.sendall(b'HTTP/1.1 ' + status + b'\r\n' + extra +
                                   f'Content-Length: {size}\r\nConnection: close\r\n\r\n'.encode() +
                                   content)
                    accepted.append(b'range: bytes=7-' in request.lower())

            def serve():
                try:
                    if fault == 'handshake':
                        stalled, _ = listener.accept()
                        with stalled:
                            reply(b'200 OK', PAYLOAD, len(PAYLOAD))
                    else:
                        reply(b'200 OK', PAYLOAD[:7], len(PAYLOAD))
                        if fault == 'no-range':
                            reply(b'200 OK', PAYLOAD, len(PAYLOAD))
                            reply(b'200 OK', PAYLOAD, len(PAYLOAD))
                        else:
                            content_range = f'Content-Range: bytes 7-{len(PAYLOAD)-1}/{len(PAYLOAD)}\r\n'
                            reply(b'206 Partial Content', PAYLOAD[7:], len(PAYLOAD)-7, content_range.encode())
                except Exception as exc:
                    errors.append(repr(exc))
                finally:
                    listener.close()

            worker = threading.Thread(target=serve, daemon=True)
            worker.start()
            output = root / 'download'
            env = dict(os.environ, CURL_CA_BUNDLE=str(cert), NO_PROXY='localhost,127.0.0.1',
                       no_proxy='localhost,127.0.0.1')
            function = 'get' if fault == 'handshake' else 'get_archive'
            command = match.group() + f'\n{function} "$1" "$2"\n'
            try:
                result = subprocess.run(['/bin/sh', '-c', command, 'test-get',
                                         f'https://localhost:{port}/payload', str(output)],
                                        env=env, capture_output=True, timeout=30)
            finally:
                worker.join(timeout=27)
            self.assertFalse(errors, errors)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
            expected = {'handshake': [False], 'partial': [False, True], 'no-range': [False, True, False]}
            self.assertEqual(accepted, expected[fault])
            self.assertEqual(output.read_bytes(), PAYLOAD)


if __name__ == '__main__':
    unittest.main()
