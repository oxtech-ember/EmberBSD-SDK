# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Publish real signed metadata through the local conditional object server."""

import hashlib
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import test_release_s3 as http_fixture
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.jsonio import json_bytes
from emberbsd_sdk.release_cli import seal_release
from emberbsd_sdk.release_signing import ReleaseCrypto
from emberbsd_sdk.release_delivery import publish_release, fetch_release


@unittest.skipUnless(os.environ.get('EMBERBSD_TEST_HOST_PREFIX'), 'select native Ports OpenSSL')
class DeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='release-delivery-')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        cls.crypto = ReleaseCrypto.from_prefix(os.environ['EMBERBSD_TEST_HOST_PREFIX'])
        cls.ca = cls.crypto.create_root(cls.root / 'ca')
        cls.signer = cls.crypto.issue_signer(cls.ca, cls.root / 'signer')

    def setUp(self):
        http_fixture.HTTPTests.setUp(self)
        self.work = self.root / self._testMethodName
        self.work.mkdir()

    def release(self, name='one', payload=b'payload' * 1000, index=b'index fixture'):
        directory = self.work / name
        directory.mkdir()
        (directory / 'payload').write_bytes(payload)
        (directory / 'host').mkdir()
        (directory / 'host/pkg_summary.gz').write_bytes(index)
        host = dict(os='Darwin', arch='arm64', abi='macos-27', runtime='libSystem')
        artifacts = [dict(id=identity, path=path, kind=kind, host=host, target=None,
                          unpacked_size=size, depends=[], licenses=['2-clause-bsd'])
                     for identity, path, kind, size in (
                         ('bootstrap', 'payload', 'bootstrap', len(payload)),
                         ('index', 'host/pkg_summary.gz', 'package-index', len(index)))]
        draft = dict(schema='emberbsd.release.v1', release_id=name, created_at=int(time.time()),
                     sources={key: 'a' * 40 for key in ('os', 'ports', 'sdk')}, artifacts=artifacts)
        seal_release(self.crypto, json_bytes(draft), directory, self.signer, self.ca.certificate,
                     directory / 'release.p7m')
        return directory / 'release.p7m'

    def publish(self, release):
        return publish_release(self.crypto, self.store, self.public, release, self.signer,
                               self.ca.certificate, 'development')

    def fetch(self, directory='snapshot'):
        return fetch_release(self.crypto, self.public, self.ca.certificate, 'development',
                             self.work / 'state.json', self.work / directory)

    def test_publish_fetch_and_same_release_retry(self):
        release = self.release()
        first = self.publish(release)
        pointer = self.server.objects['/bucket/channels/development.p7m']
        self.assertEqual(self.publish(release), first)
        self.assertEqual(self.server.objects['/bucket/channels/development.p7m'], pointer)
        fetched = self.fetch()
        self.assertEqual(fetched['release_id'], 'one')
        self.assertTrue((self.work / 'snapshot/host/pkg_summary.gz').is_file())
        self.assertFalse((self.work / 'snapshot/payload').exists())
        self.assertTrue((self.work / 'state.json').is_file())
        self.assertIn('immutable', self.server.headers['/bucket/releases/one/payload']['cache-control'])
        self.assertIn('no-cache', self.server.headers['/bucket/channels/development.p7m']['cache-control'])

    def test_failed_upload_does_not_advance_channel(self):
        self.publish(self.release())
        pointer = self.server.objects['/bucket/channels/development.p7m']
        new = self.release('two')
        original = self.store.put
        def fail(key, *args, **kwargs):
            if key == 'releases/two/host/pkg_summary.gz':
                raise SDKError('interrupted upload')
            return original(key, *args, **kwargs)
        with patch.object(self.store, 'put', side_effect=fail), self.assertRaises(SDKError):
            self.publish(new)
        self.assertEqual(self.server.objects['/bucket/channels/development.p7m'], pointer)
        self.assertNotIn('/bucket/releases/two/release.p7m', self.server.objects)
        self.assertEqual(self.publish(new)['generation'], 2)

    def test_release_identity_cannot_change(self):
        release = self.release()
        self.publish(release)
        other = self.release('other', b'different')
        # Change the signed ID but keep a complete, valid new manifest.
        from emberbsd_sdk.release_catalog import parse_release
        value = parse_release(self.crypto.verify_cms(other.read_bytes(), self.ca.certificate))
        value['release_id'] = 'one'
        other.write_bytes(self.crypto.sign_cms(json_bytes(value), self.signer, self.ca.certificate))
        with self.assertRaises(SDKError):
            self.publish(other)
        self.assertEqual(self.server.objects['/bucket/releases/one/release.p7m'], release.read_bytes())

    def test_competing_channel_writer_is_not_overwritten(self):
        self.publish(self.release())
        second = self.release('two')
        original = self.store.put
        winner = b'another writer changed the object'
        def race(key, *args, **kwargs):
            if key == 'channels/development.p7m':
                self.server.objects['/bucket/' + key] = winner
            return original(key, *args, **kwargs)
        with patch.object(self.store, 'put', side_effect=race), self.assertRaises(SDKError):
            self.publish(second)
        self.assertEqual(self.server.objects['/bucket/channels/development.p7m'], winner)

    def test_modified_index_fails_before_client_state(self):
        self.publish(self.release())
        self.server.objects['/bucket/releases/one/host/pkg_summary.gz'] = b'changed index'
        with self.assertRaises(SDKError):
            self.fetch()
        self.assertFalse((self.work / 'state.json').exists())
        self.assertFalse((self.work / 'snapshot').exists())

    def test_corrupt_manifest_and_old_generation_rejected(self):
        self.publish(self.release())
        old = self.server.objects['/bucket/channels/development.p7m']
        self.fetch()
        self.publish(self.release('two'))
        self.fetch('second-snapshot')
        state = (self.work / 'state.json').read_bytes()
        self.server.objects['/bucket/channels/development.p7m'] = old
        with self.assertRaises(SDKError):
            self.fetch('replayed')
        self.assertEqual((self.work / 'state.json').read_bytes(), state)

        self.server.objects['/bucket/releases/one/release.p7m'] = b'invalid envelope'
        with self.assertRaises(SDKError):
            self.fetch('corrupted')
        self.assertEqual((self.work / 'state.json').read_bytes(), state)

    def test_indexes_are_streamed_without_cms_envelope_size_limit(self):
        size = 17 * 1024 * 1024
        self.publish(self.release(index=b'i' * size))
        self.fetch()
        self.assertEqual((self.work / 'snapshot/host/pkg_summary.gz').stat().st_size, size)

    def test_channel_expiring_during_verification_is_refreshed(self):
        release = self.release()
        first = publish_release(self.crypto, self.store, self.public, release, self.signer,
                                self.ca.certificate, 'development', ttl=60)
        clock = [first['issued_at']]
        verify = self.public.verify
        def advance(*args, **kwargs):
            clock[0] = first['issued_at'] + 120
            return verify(*args, **kwargs)
        with patch('emberbsd_sdk.release_delivery.time.time', side_effect=lambda: clock[0]), \
                patch.object(self.public, 'verify', side_effect=advance):
            second = self.publish(release)
        self.assertEqual(second['generation'], 2)
        self.assertEqual(second['issued_at'], clock[0])

    def test_expired_previous_signer_has_explicit_publisher_recovery(self):
        old = self.crypto.issue_signer(self.ca, self.work / 'short-signer', days=1)
        first = publish_release(self.crypto, self.store, self.public, self.release(), old,
                                self.ca.certificate, 'development')
        release = self.release('two')
        future = int(time.time()) + 2 * 86400
        verify = self.crypto.verify_cms
        def at_future(data, root, *, now=None):
            return verify(data, root, now=future if now is None else now)
        with patch.object(self.crypto, 'verify_cms', side_effect=at_future), \
                patch('emberbsd_sdk.release_delivery.time.time', return_value=future):
            with self.assertRaises(SDKError):
                self.publish(release)
            second = publish_release(self.crypto, self.store, self.public, release, self.signer,
                self.ca.certificate, 'development', previous_channel_time=first['issued_at'])
        self.assertEqual(second['generation'], 2)


if __name__ == '__main__':
    unittest.main()
