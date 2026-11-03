#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Exercise the bootstrap downloader against a stalled TLS handshake."""
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
    def test_handshake_timeout_retries_and_downloads_exact_bytes(self):
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

            def serve():
                try:
                    stalled, _ = listener.accept()
                    accepted.append('stalled')
                    # Hold the first TCP connection open through curl's TLS timeout.
                    with stalled:
                        second, _ = listener.accept()
                        accepted.append('retry')
                        with context.wrap_socket(second, server_side=True) as client:
                            client.recv(8192)
                            client.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: ' +
                                           str(len(PAYLOAD)).encode() +
                                           b'\r\nConnection: close\r\n\r\n' + PAYLOAD)
                except Exception as exc:
                    errors.append(repr(exc))
                finally:
                    listener.close()

            worker = threading.Thread(target=serve, daemon=True)
            worker.start()
            output = root / 'download'
            env = dict(os.environ, CURL_CA_BUNDLE=str(cert), NO_PROXY='localhost,127.0.0.1',
                       no_proxy='localhost,127.0.0.1')
            command = match.group() + '\nget "$1" "$2"\n'
            try:
                result = subprocess.run(['/bin/sh', '-c', command, 'test-get',
                                         f'https://localhost:{port}/payload', str(output)],
                                        env=env, capture_output=True, timeout=30)
            finally:
                worker.join(timeout=27)
            self.assertFalse(errors, errors)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
            self.assertEqual(accepted, ['stalled', 'retry'])
            self.assertEqual(output.read_bytes(), PAYLOAD)


if __name__ == '__main__':
    unittest.main()
