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
from emberbsd_sdk.release_catalog import parse_release
import emberbsd_sdk.release_delivery as release_delivery
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
                               self.ca.certificate)

    def sparse_release(self, name, snapshot, *, extra=None):
        previous = parse_release(self.crypto.verify_cms(snapshot.read_bytes(), self.ca.certificate))
        directory = self.work / (name + '-sparse')
        directory.mkdir()
        (directory / 'host').mkdir()
        (directory / 'host/pkg_summary.gz').write_bytes(
            (snapshot.parent / 'host/pkg_summary.gz').read_bytes())
        artifacts = [dict(item) for item in previous['artifacts']]
        if extra is not None:
            path, data = extra
            (directory / path).write_bytes(data)
            artifacts.append(dict(id='new-package', path=path, kind='host-package',
                                  host=artifacts[0]['host'], target=None,
                                  unpacked_size=len(data), depends=[], licenses=['2-clause-bsd']))
        draft = dict(schema='emberbsd.release.v1', release_id=name, created_at=int(time.time()),
                     sources=previous['sources'], artifacts=artifacts)
        output = directory / 'release.p7m'
        seal_release(self.crypto, json_bytes(draft), directory, self.signer, self.ca.certificate,
                     output, reuse_current=snapshot)
        return output

    def fetch(self, directory='snapshot'):
        return fetch_release(self.crypto, self.public, self.ca.certificate,
                             self.work / 'state.json', self.work / directory)

    def test_publish_fetch_and_same_release_retry(self):
        release = self.release()
        def publish():
            return publish_release(self.crypto, self.store, self.public, release, self.signer,
                                   self.ca.certificate, expected_generation=0)
        first = publish()
        pointer = self.server.objects['/bucket/current.p7m']
        self.assertEqual(publish(), first)
        self.assertEqual(self.server.objects['/bucket/current.p7m'], pointer)
        fetched = self.fetch()
        self.assertEqual(fetched['release_id'], 'one')
        self.assertTrue((self.work / 'snapshot/host/pkg_summary.gz').is_file())
        self.assertFalse((self.work / 'snapshot/payload').exists())
        self.assertTrue((self.work / 'state.json').is_file())
        self.assertIn('immutable', self.server.headers['/bucket/current/host/darwin-arm64/payload']['cache-control'])
        self.assertIn('no-cache', self.server.headers['/bucket/current.p7m']['cache-control'])

    def test_failed_upload_does_not_advance_channel(self):
        self.publish(self.release())
        pointer = self.server.objects['/bucket/current.p7m']
        new = self.release('two')
        original = self.store.put
        def fail(key, *args, **kwargs):
            if key.endswith('/host/pkg_summary.gz'):
                raise SDKError('interrupted upload')
            return original(key, *args, **kwargs)
        with patch.object(self.store, 'put', side_effect=fail), self.assertRaises(SDKError):
            self.publish(new)
        self.assertEqual(self.server.objects['/bucket/current.p7m'], pointer)
        self.assertEqual(self.server.objects['/bucket/current.p7m'], pointer)
        self.assertEqual(self.publish(new)['generation'], 2)

    def test_stale_candidate_does_not_replace_a_newer_current(self):
        first = self.release()
        publish_release(self.crypto, self.store, self.public, first, self.signer,
                        self.ca.certificate, expected_generation=0)
        # Reuse the immutable payload key with identical bytes; this test covers
        # generation rejection rather than a changed package version.
        new = self.release('two', b'payload' * 1000)
        before = dict(self.server.objects)
        with self.assertRaisesRegex(SDKError, 'expected generation 0, found 1'):
            publish_release(self.crypto, self.store, self.public, new, self.signer,
                            self.ca.certificate, expected_generation=0)
        self.assertEqual(self.server.objects, before)
        self.assertEqual(publish_release(self.crypto, self.store, self.public, new, self.signer,
                                       self.ca.certificate, expected_generation=1)['generation'], 2)

    def test_sparse_publish_reuses_exact_current_without_reading_payloads(self):
        first = self.release()
        self.publish(first)
        sparse = self.sparse_release('sparse', first)
        original_verify = self.public.verify
        verified = []
        def track(key, *args, **kwargs):
            verified.append(key)
            return original_verify(key, *args, **kwargs)
        with patch.object(self.public, 'verify', side_effect=track):
            result = publish_release(self.crypto, self.store, self.public, sparse, self.signer,
                                     self.ca.certificate, expected_generation=1, reuse_current=True)
        self.assertEqual(result['generation'], 2)
        self.assertNotIn('current/host/darwin-arm64/payload', verified)
        self.assertEqual(publish_release(self.crypto, self.store, self.public, sparse, self.signer,
                                         self.ca.certificate, expected_generation=2,
                                         reuse_current=True), result)
        self.assertEqual(self.public.head('current/host/darwin-arm64/payload')['content-length'],
                         str(len(b'payload' * 1000)))

    def test_sparse_publish_rejects_unknown_or_changed_missing_record(self):
        first = self.release()
        self.publish(first)
        sparse = self.sparse_release('sparse-mismatch', first)
        content = self.crypto.verify_cms(sparse.read_bytes(), self.ca.certificate)
        candidate = parse_release(content)
        candidate['artifacts'][0]['unpacked_size'] += 1
        sparse.write_bytes(self.crypto.sign_cms(json_bytes(candidate), self.signer,
                                                self.ca.certificate))
        with self.assertRaisesRegex(SDKError, 'not an exact current match'):
            publish_release(self.crypto, self.store, self.public, sparse, self.signer,
                            self.ca.certificate, expected_generation=1, reuse_current=True)

    def test_sparse_publish_requires_generation_and_existing_object(self):
        first = self.release()
        self.publish(first)
        sparse = self.sparse_release('sparse', first)
        with self.assertRaisesRegex(SDKError, 'requires --expected-generation'):
            publish_release(self.crypto, self.store, self.public, sparse, self.signer,
                            self.ca.certificate, reuse_current=True)
        self.server.objects.pop('/bucket/current/host/darwin-arm64/payload')
        with self.assertRaisesRegex(SDKError, 'HTTP 404'):
            publish_release(self.crypto, self.store, self.public, sparse, self.signer,
                            self.ca.certificate, expected_generation=1, reuse_current=True)

    def test_sparse_publish_refuses_stale_generation_before_reuse(self):
        first = self.release()
        publish_release(self.crypto, self.store, self.public, first, self.signer,
                        self.ca.certificate, expected_generation=0)
        sparse = self.sparse_release('sparse-stale', first)
        publish_release(self.crypto, self.store, self.public, self.release('intervening'),
                        self.signer, self.ca.certificate, expected_generation=1)
        before = dict(self.server.objects)
        with self.assertRaisesRegex(SDKError, 'expected generation 1, found 2'):
            publish_release(self.crypto, self.store, self.public, sparse, self.signer,
                            self.ca.certificate, expected_generation=1, reuse_current=True)
        self.assertEqual(self.server.objects, before)

    def test_sparse_publish_verifies_new_local_package(self):
        first = self.release()
        self.publish(first)
        with patch.object(self.crypto, 'verify_package') as verifier:
            sparse = self.sparse_release('sparse-package', first,
                                         extra=('newpkg-1.0.tgz', b'signed package fixture'))
            publish_release(self.crypto, self.store, self.public, sparse, self.signer,
                            self.ca.certificate, expected_generation=1, reuse_current=True)
        verifier.assert_called()

    def test_sparse_publish_refuses_new_payload_disappearing_after_preflight(self):
        first = self.release()
        self.publish(first)
        payload = b'signed package fixture'
        with patch.object(self.crypto, 'verify_package'):
            sparse = self.sparse_release('sparse-race', first,
                                         extra=('newpkg-1.0.tgz', payload))
        key = '/bucket/current/host/darwin-arm64/newpkg-1.0.tgz'
        self.server.objects[key] = payload
        pointer = self.server.objects['/bucket/current.p7m']
        original = release_delivery._verify_sparse_local
        def remove_new_payload(*args):
            reusable = original(*args)
            (sparse.parent / 'newpkg-1.0.tgz').unlink()
            return reusable
        with patch.object(release_delivery, '_verify_sparse_local', side_effect=remove_new_payload), \
                patch.object(self.crypto, 'verify_package'):
            with self.assertRaisesRegex(SDKError, 'disappeared before upload: new-package'):
                publish_release(self.crypto, self.store, self.public, sparse, self.signer,
                                self.ca.certificate, expected_generation=1, reuse_current=True)
        self.assertEqual(self.server.objects['/bucket/current.p7m'], pointer)

    def test_release_identity_cannot_change(self):
        release = self.release()
        pointer = self.publish(release)
        other = self.release('other', b'different')
        # Change the signed ID but keep a complete, valid new manifest.
        from emberbsd_sdk.release_catalog import parse_release
        value = parse_release(self.crypto.verify_cms(other.read_bytes(), self.ca.certificate))
        value['release_id'] = 'one'
        other.write_bytes(self.crypto.sign_cms(json_bytes(value), self.signer, self.ca.certificate))
        with self.assertRaises(SDKError):
            self.publish(other)
        self.assertEqual(self.server.objects['/bucket/' + pointer['release']['path']], release.read_bytes())

    def test_competing_channel_writer_is_not_overwritten(self):
        self.publish(self.release())
        second = self.release('two')
        original = self.store.put
        winner = b'another writer changed the object'
        def race(key, *args, **kwargs):
            if key == 'current.p7m':
                self.server.objects['/bucket/' + key] = winner
            return original(key, *args, **kwargs)
        with patch.object(self.store, 'put', side_effect=race), self.assertRaises(SDKError):
            self.publish(second)
        self.assertEqual(self.server.objects['/bucket/current.p7m'], winner)

    def test_modified_index_fails_before_client_state(self):
        pointer = self.publish(self.release())
        base = pointer['release']['path'].removesuffix('/release.p7m')
        self.server.objects['/bucket/' + base + '/host/pkg_summary.gz'] = b'changed index'
        with self.assertRaises(SDKError):
            self.fetch()
        self.assertFalse((self.work / 'state.json').exists())
        self.assertFalse((self.work / 'snapshot').exists())

    def test_corrupt_manifest_and_old_generation_rejected(self):
        pointer = self.publish(self.release())
        old = self.server.objects['/bucket/current.p7m']
        self.fetch()
        self.publish(self.release('two'))
        self.fetch('second-snapshot')
        state = (self.work / 'state.json').read_bytes()
        self.server.objects['/bucket/current.p7m'] = old
        with self.assertRaises(SDKError):
            self.fetch('replayed')
        self.assertEqual((self.work / 'state.json').read_bytes(), state)

        self.server.objects['/bucket/' + pointer['release']['path']] = b'invalid envelope'
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
                                self.ca.certificate, ttl=60)
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
                                self.ca.certificate)
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
                self.ca.certificate, previous_channel_time=first['issued_at'])
        self.assertEqual(second['generation'], 2)


class PublicationRetryTests(unittest.TestCase):
    """Exercise recovery in ordinary CI; CMS is an explicit test double.

    The conditional HTTP store, inventory hashes, publication lock and channel
    state machine are real. DeliveryTests separately covers native CMS/tools.
    """
    release = DeliveryTests.release
    sparse_release = DeliveryTests.sparse_release

    def setUp(self):
        http_fixture.HTTPTests.setUp(self)
        self.temporary = tempfile.TemporaryDirectory(prefix='publication-retry-')
        self.addCleanup(self.temporary.cleanup)
        self.root = self.work = Path(self.temporary.name)
        from types import SimpleNamespace
        from unittest.mock import Mock
        marker = b'fixture-cms\0'
        def verify(envelope, trust, **kwargs):
            if not envelope.startswith(marker):
                raise SDKError('unauthenticated fixture envelope')
            return envelope[len(marker):]
        self.crypto = Mock()
        self.crypto.sign_cms.side_effect = lambda data, *_: marker + data
        self.crypto.verify_cms.side_effect = verify
        self.signer = object()
        self.ca = SimpleNamespace(certificate=object())

    def publish(self, release, generation=0, **kwargs):
        return publish_release(self.crypto, self.store, self.public, release, self.signer,
                               self.ca.certificate, expected_generation=generation, **kwargs)

    def test_exact_retry_keeps_pointer_and_does_not_sign(self):
        release = self.release()
        first = self.publish(release)
        before = dict(self.server.objects)
        self.crypto.sign_cms.reset_mock()
        self.assertEqual(self.publish(release), first)
        self.assertEqual(self.server.objects, before)
        self.crypto.sign_cms.assert_not_called()

    def test_retry_after_lost_switch_acknowledgement(self):
        release = self.release()
        put = self.store.put
        def lose_ack(key, *args, **kwargs):
            put(key, *args, **kwargs)
            if key == 'current.p7m':
                raise SDKError('lost switch acknowledgement')
        with patch.object(self.store, 'put', side_effect=lose_ack):
            with self.assertRaisesRegex(SDKError, 'lost switch acknowledgement'):
                self.publish(release)
        before = dict(self.server.objects)
        self.assertEqual(self.publish(release)['generation'], 1)
        self.assertEqual(self.server.objects, before)

    def test_retry_after_upload_failure_keeps_original_generation_expectation(self):
        release = self.release()
        put = self.store.put
        def fail_index(key, *args, **kwargs):
            if key.endswith('/host/pkg_summary.gz'):
                raise SDKError('index upload interrupted')
            return put(key, *args, **kwargs)
        with patch.object(self.store, 'put', side_effect=fail_index):
            with self.assertRaisesRegex(SDKError, 'index upload interrupted'):
                self.publish(release)
        self.assertNotIn('/bucket/current.p7m', self.server.objects)
        self.assertEqual(self.publish(release)['generation'], 1)

    def test_retry_after_public_channel_verification_failure(self):
        release = self.release()
        verify = self.public.verify
        def fail(key, *args, **kwargs):
            if key == 'current.p7m':
                raise SDKError('public verification interrupted')
            return verify(key, *args, **kwargs)
        with patch.object(self.public, 'verify', side_effect=fail):
            with self.assertRaisesRegex(SDKError, 'verification interrupted'):
                self.publish(release)
        pointer = self.server.objects['/bucket/current.p7m']
        self.assertEqual(self.publish(release)['generation'], 1)
        self.assertEqual(self.server.objects['/bucket/current.p7m'], pointer)

    def test_sparse_retry_finishes_interrupted_cleanup(self):
        first = self.release()
        self.publish(first)
        sparse = self.sparse_release('addition', first)
        obsolete = ('/bucket/current/abandoned-a', '/bucket/current/abandoned-b')
        for key in obsolete:
            self.server.objects[key] = b'abandoned upload'
        delete = self.store.delete
        def interrupt_cleanup(key):
            if key == 'current/abandoned-b':
                raise SDKError('cleanup interrupted')
            return delete(key)
        with patch.object(self.store, 'delete', side_effect=interrupt_cleanup):
            with self.assertRaisesRegex(SDKError, 'cleanup interrupted'):
                self.publish(sparse, 1, reuse_current=True)
        self.assertNotIn(obsolete[0], self.server.objects)
        self.assertIn(obsolete[1], self.server.objects)
        pointer = self.server.objects['/bucket/current.p7m']
        self.crypto.sign_cms.reset_mock()
        self.assertEqual(self.publish(sparse, 1, reuse_current=True)['generation'], 2)
        self.assertEqual(self.server.objects['/bucket/current.p7m'], pointer)
        for key in obsolete:
            self.assertNotIn(key, self.server.objects)
        self.crypto.sign_cms.assert_not_called()

    def test_foreign_stale_release_stops_before_upload_or_cleanup(self):
        self.publish(self.release())
        other = self.release('other')
        before = dict(self.server.objects)
        with self.assertRaisesRegex(SDKError, 'expected generation 0, found 1'):
            self.publish(other)
        self.assertEqual(self.server.objects, before)

    def test_equal_content_with_different_envelope_is_not_a_retry(self):
        release = self.release()
        self.publish(release)
        release.write_bytes(release.read_bytes() + b'\n')
        before = dict(self.server.objects)
        with self.assertRaisesRegex(SDKError, 'different metadata'):
            self.publish(release)
        self.assertEqual(self.server.objects, before)

    def test_future_generation_and_refresh_still_require_current_generation(self):
        release = self.release()
        self.publish(release)
        before = dict(self.server.objects)
        for generation, refresh in ((2, False), (0, True)):
            with self.subTest(generation=generation, refresh=refresh):
                with self.assertRaisesRegex(SDKError, 'current changed'):
                    self.publish(release, generation, refresh=refresh)
                self.assertEqual(self.server.objects, before)
        self.assertEqual(self.publish(release, 1, refresh=True)['generation'], 2)

    def test_expired_pointer_requires_a_new_generation_check(self):
        release = self.release()
        first = self.publish(release, ttl=10)
        with patch.object(release_delivery.time, 'time', return_value=first['expires_at']):
            with self.assertRaisesRegex(SDKError, 'current changed'):
                self.publish(release)
            self.assertEqual(self.publish(release, 1)['generation'], 2)

    def test_pointer_expiring_during_retry_does_not_advance_generation(self):
        release = self.release()
        first = self.publish(release, ttl=10)
        now = [first['issued_at']]
        verify = self.public.verify
        def expire(*args, **kwargs):
            now[0] = first['expires_at']
            return verify(*args, **kwargs)
        before = dict(self.server.objects)
        with patch.object(release_delivery.time, 'time', side_effect=lambda: now[0]), \
                patch.object(self.public, 'verify', side_effect=expire):
            with self.assertRaisesRegex(SDKError, 'expired during retry'):
                self.publish(release)
        self.assertEqual(self.server.objects, before)

    def test_retry_still_authenticates_candidate_and_checks_payload(self):
        release = self.release()
        self.publish(release)
        before = dict(self.server.objects)
        envelope = release.read_bytes()
        release.write_bytes(envelope.removeprefix(b'fixture-cms\0'))
        with self.assertRaisesRegex(SDKError, 'unauthenticated'):
            self.publish(release)
        release.write_bytes(envelope)
        (release.parent / 'payload').write_bytes(b'corrupt payload')
        with self.assertRaisesRegex(SDKError, 'size or SHA256 mismatch'):
            self.publish(release)
        self.assertEqual(self.server.objects, before)

    def test_retry_checks_public_bytes_before_cleanup(self):
        release = self.release()
        self.publish(release)
        key = '/bucket/current/host/darwin-arm64/payload'
        self.server.objects[key] = b'corrupt public payload'
        self.server.objects['/bucket/current/abandoned'] = b'keep until verified'
        before = dict(self.server.objects)
        with self.assertRaises(SDKError):
            self.publish(release)
        self.assertEqual(self.server.objects, before)

    def test_retry_respects_publisher_lock(self):
        release = self.release()
        self.publish(release)
        self.server.objects['/bucket/.publisher-lock'] = b'another publisher'
        before = dict(self.server.objects)
        with self.assertRaisesRegex(SDKError, 'publisher is busy'):
            self.publish(release)
        self.assertEqual(self.server.objects, before)


if __name__ == '__main__':
    unittest.main()
