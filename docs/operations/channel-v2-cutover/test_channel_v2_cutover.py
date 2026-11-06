"""Local conditional-HTTP fixture tests; not proof of production S3 semantics."""
import hashlib
import importlib.util
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path[:0] = ['/tmp/kiss-sdk/src', '/tmp/kiss-sdk/tests']
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.jsonio import json_bytes, loads_json
from emberbsd_sdk.release_delivery import publish_release
from emberbsd_sdk.release_s3 import HTTPStatusError
from test_release_delivery import PublicationRetryTests, DeliveryTests

spec = importlib.util.spec_from_file_location('cutover_artifact', '/tmp/emberbsd-channel-v2-cutover.py')
cutover = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cutover)


class CutoverTests(unittest.TestCase):
    release = DeliveryTests.release

    def setUp(self):
        PublicationRetryTests.setUp(self)
        self.old_release = self.release('old')
        pointer = publish_release(self.crypto, self.store, self.public, self.old_release,
                                  self.signer, self.ca.certificate)
        pointer.update(schema='emberbsd.channel.v1', generation=42)
        self.old_channel = self.work / 'old-current.p7m'
        self.old_channel.write_bytes(self.crypto.sign_cms(json_bytes(pointer), self.signer,
                                                         self.ca.certificate))
        self.server.objects['/bucket/current.p7m'] = self.old_channel.read_bytes()
        self.expected = hashlib.sha256(self.old_channel.read_bytes()).hexdigest()
        self.candidate = self.release('candidate')
        data = loads_json(self.crypto.verify_cms(self.candidate.read_bytes(), self.ca.certificate))
        payload = b'signed-package-test-fixture'
        (self.candidate.parent / 'tool-1.0.tgz').write_bytes(payload)
        item = dict(data['artifacts'][0], id='tool', kind='host-package', path='tool-1.0.tgz',
                    size=len(payload), unpacked_size=len(payload), sha256=hashlib.sha256(payload).hexdigest())
        data['artifacts'].append(item)
        self.candidate.write_bytes(self.crypto.sign_cms(json_bytes(data), self.signer,
                                                        self.ca.certificate))
        self.crypto.verify_package.reset_mock()

    def run_cutover(self, **kwargs):
        return cutover.cutover(self.crypto, self.store, self.public, self.candidate,
                               self.old_channel, self.old_release, self.signer,
                               self.ca.certificate, expected_current=self.expected, **kwargs)

    def pointer(self):
        return self.server.objects['/bucket/current.p7m']

    def test_switch_verifies_package_and_preserves_coverage(self):
        result = self.run_cutover()
        self.assertEqual(result['schema'], 'emberbsd.channel.v2')
        self.assertNotIn('generation', result)
        self.crypto.verify_package.assert_called_once()
        self.assertIn('/bucket/current/host/darwin-arm64/payload', self.server.objects)
        self.assertIn('/bucket/current/host/darwin-arm64/tool-1.0.tgz', self.server.objects)

    def test_bad_package_signature_rejects_before_any_write(self):
        before = dict(self.server.objects)
        self.crypto.verify_package.side_effect = SDKError('package signature rejected')
        with patch.object(self.store, 'put') as put:
            with self.assertRaisesRegex(SDKError, 'signature rejected'):
                self.run_cutover()
            put.assert_not_called()
        self.assertEqual(self.server.objects, before)

    def test_missing_payload_rejects_before_write(self):
        (self.candidate.parent / 'payload').unlink()
        before = dict(self.server.objects)
        with patch.object(self.store, 'put') as put:
            with self.assertRaises(SDKError):
                self.run_cutover()
            put.assert_not_called()
        self.assertEqual(self.server.objects, before)

    def test_candidate_cannot_drop_old_artifacts(self):
        data = loads_json(self.crypto.verify_cms(self.candidate.read_bytes(), self.ca.certificate))
        data['artifacts'] = [item for item in data['artifacts'] if item['id'] != 'bootstrap']
        self.candidate.write_bytes(self.crypto.sign_cms(json_bytes(data), self.signer, self.ca.certificate))
        before = dict(self.server.objects)
        with self.assertRaisesRegex(SDKError, 'drops current artifacts'):
            self.run_cutover()
        self.assertEqual(self.server.objects, before)

    def test_wrong_expected_hash_rejects_before_write(self):
        self.expected = 'f' * 64
        with patch.object(self.store, 'put') as put:
            with self.assertRaisesRegex(SDKError, 'expected SHA256'):
                self.run_cutover()
            put.assert_not_called()

    def test_stale_current_rejects_before_payload_write(self):
        changed = loads_json(self.crypto.verify_cms(self.pointer(), self.ca.certificate))
        changed['generation'] += 1
        self.server.objects['/bucket/current.p7m'] = self.crypto.sign_cms(json_bytes(changed), self.signer,
                                                                        self.ca.certificate)
        before = dict(self.server.objects)
        with patch.object(cutover, '_immutable') as upload:
            with self.assertRaises(SDKError):
                self.run_cutover()
            upload.assert_not_called()
        self.assertEqual(self.server.objects, before)

    def test_upload_failure_preserves_current_and_old_objects(self):
        before = dict(self.server.objects)
        put = self.store.put
        def fail(key, *args, **kwargs):
            if key.endswith('/tool-1.0.tgz'):
                raise SDKError('upload interrupted')
            return put(key, *args, **kwargs)
        with patch.object(self.store, 'put', side_effect=fail):
            with self.assertRaisesRegex(SDKError, 'upload interrupted'):
                self.run_cutover()
        self.assertEqual(self.pointer(), before['/bucket/current.p7m'])
        for key, value in before.items():
            self.assertEqual(self.server.objects[key], value)
        self.run_cutover()

    def test_signing_failure_preserves_old_current(self):
        before = self.pointer()
        self.crypto.sign_cms.side_effect = SDKError('signing failed')
        with self.assertRaisesRegex(SDKError, 'signing failed'):
            self.run_cutover()
        self.assertEqual(self.pointer(), before)

    def test_conditional_switch_failure_preserves_old_current(self):
        before = self.pointer()
        put = self.store.put
        def fail(key, *args, **kwargs):
            if key == 'current.p7m':
                self.assertIn('etag', kwargs)
                raise HTTPStatusError(412, 'PUT', key)
            return put(key, *args, **kwargs)
        with patch.object(self.store, 'put', side_effect=fail):
            with self.assertRaises(HTTPStatusError):
                self.run_cutover()
        self.assertEqual(self.pointer(), before)

    def test_lost_ack_retry_keeps_exact_new_pointer(self):
        put = self.store.put
        def fail(key, *args, **kwargs):
            result = put(key, *args, **kwargs)
            if key == 'current.p7m':
                raise SDKError('lost acknowledgement')
            return result
        with patch.object(self.store, 'put', side_effect=fail):
            with self.assertRaisesRegex(SDKError, 'lost acknowledgement'):
                self.run_cutover()
        committed = self.pointer()
        self.crypto.sign_cms.reset_mock()
        self.run_cutover()
        self.assertEqual(self.pointer(), committed)
        self.crypto.sign_cms.assert_not_called()

    def test_post_switch_public_verify_failure_then_exact_retry(self):
        verify = self.public.verify
        def fail(key, *args, **kwargs):
            if key == 'current.p7m':
                raise SDKError('public verification failed')
            return verify(key, *args, **kwargs)
        with patch.object(self.public, 'verify', side_effect=fail):
            with self.assertRaisesRegex(SDKError, 'public verification failed'):
                self.run_cutover()
        committed = self.pointer()
        self.run_cutover()
        self.assertEqual(self.pointer(), committed)

    def test_cleanup_failure_then_exact_retry(self):
        with patch.object(cutover, '_clean_current', side_effect=SDKError('cleanup failed')):
            with self.assertRaisesRegex(SDKError, 'cleanup failed'):
                self.run_cutover()
        committed = self.pointer()
        self.run_cutover()
        self.assertEqual(self.pointer(), committed)

    def expire_old_after(self, seconds):
        old = loads_json(self.crypto.verify_cms(self.old_channel.read_bytes(), self.ca.certificate))
        old['expires_at'] = old['issued_at'] + seconds
        raw = self.crypto.sign_cms(json_bytes(old), self.signer, self.ca.certificate)
        self.old_channel.write_bytes(raw)
        self.server.objects['/bucket/current.p7m'] = raw
        self.expected = hashlib.sha256(raw).hexdigest()
        return old['issued_at']

    def test_exact_retry_survives_retired_v1_expiry(self):
        started = self.expire_old_after(1)
        with patch.object(cutover.time, 'time', return_value=started):
            self.run_cutover(ttl=100)
        committed = self.pointer()
        with patch.object(cutover.time, 'time', return_value=started + 2):
            self.crypto.sign_cms.reset_mock()
            self.run_cutover(ttl=100)
            self.crypto.sign_cms.assert_not_called()
        self.assertEqual(self.pointer(), committed)

    def test_expired_old_current_cannot_start_cutover(self):
        started = self.expire_old_after(1)
        before = dict(self.server.objects)
        with patch.object(cutover.time, 'time', return_value=started + 2), \
                patch.object(cutover, '_immutable') as upload:
            with self.assertRaisesRegex(SDKError, 'expired'):
                self.run_cutover()
            upload.assert_not_called()
        self.assertEqual(self.server.objects, before)

    def test_exact_retry_does_not_authenticate_retired_signer(self):
        self.run_cutover(ttl=100)
        committed = self.pointer()
        old_envelopes = {self.old_channel.read_bytes(), self.old_release.read_bytes()}
        verify = self.crypto.verify_cms.side_effect
        def expired_old_signer(envelope, trust, **kwargs):
            if envelope in old_envelopes:
                raise SDKError('retired signer expired')
            return verify(envelope, trust, **kwargs)
        with patch.object(self.crypto, 'verify_cms', side_effect=expired_old_signer):
            self.run_cutover(ttl=100)
        self.assertEqual(self.pointer(), committed)

    def test_initial_switch_still_authenticates_old_signer(self):
        before = dict(self.server.objects)
        old_envelopes = {self.old_channel.read_bytes(), self.old_release.read_bytes()}
        verify = self.crypto.verify_cms.side_effect
        def expired_old_signer(envelope, trust, **kwargs):
            if envelope in old_envelopes:
                raise SDKError('retired signer expired')
            return verify(envelope, trust, **kwargs)
        with patch.object(self.crypto, 'verify_cms', side_effect=expired_old_signer), \
                patch.object(cutover, '_immutable') as upload:
            with self.assertRaisesRegex(SDKError, 'retired signer expired'):
                self.run_cutover()
            upload.assert_not_called()
        self.assertEqual(self.server.objects, before)

    def test_retry_still_authenticates_current_v2_signer(self):
        self.run_cutover(ttl=100)
        committed = self.pointer()
        verify = self.crypto.verify_cms.side_effect
        def expired_current_signer(envelope, trust, **kwargs):
            if envelope == committed:
                raise SDKError('current signer expired')
            return verify(envelope, trust, **kwargs)
        with patch.object(self.crypto, 'verify_cms', side_effect=expired_current_signer), \
                patch.object(cutover, '_clean_current') as cleanup:
            with self.assertRaisesRegex(SDKError, 'current signer expired'):
                self.run_cutover()
            cleanup.assert_not_called()
        self.assertEqual(self.pointer(), committed)

    def test_expired_retry_never_refreshes(self):
        result = self.run_cutover(ttl=1)
        before = self.pointer()
        with patch.object(cutover.time, 'time', return_value=result['expires_at']):
            with self.assertRaisesRegex(SDKError, 'expired'):
                self.run_cutover()
        self.assertEqual(self.pointer(), before)


if __name__ == '__main__':
    unittest.main()
