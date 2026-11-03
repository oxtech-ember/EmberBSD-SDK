# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Only current remains; incomplete publication cannot discard its packages."""

import os
import unittest
from unittest.mock import patch

import test_release_delivery as fixture
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.jsonio import json_bytes
from emberbsd_sdk.release_catalog import parse_release
from emberbsd_sdk.release_delivery import publish_release, fetch_release


@unittest.skipUnless(os.environ.get('EMBERBSD_TEST_HOST_PREFIX'), 'select native Ports OpenSSL')
class CurrentRepositoryTests(fixture.DeliveryTests):
    # Reuse setup and fixture producer, without running the inherited test cases.
    def publish(self, release):
        return publish_release(self.crypto, self.store, self.public, release, self.signer,
                               self.ca.certificate)

    def version(self, name, filename, payload):
        envelope = self.release(name, payload)
        release = parse_release(self.crypto.verify_cms(envelope.read_bytes(), self.ca.certificate))
        (envelope.parent / 'payload').rename(envelope.parent / filename)
        release['artifacts'][0]['path'] = filename
        envelope.write_bytes(self.crypto.sign_cms(json_bytes(release),
                                                self.signer, self.ca.certificate))
        return envelope

    def test_new_version_replaces_old_without_dated_release_directories(self):
        first = self.publish(self.version('first', 'bootstrap-1.tgz', b'one'))
        second = self.publish(self.version('second', 'bootstrap-2.tgz', b'two'))
        keys = set(self.server.objects)
        self.assertIn('/bucket/current.p7m', keys)
        self.assertIn('/bucket/current/host/darwin-arm64/bootstrap-2.tgz', keys)
        self.assertNotIn('/bucket/current/host/darwin-arm64/bootstrap-1.tgz', keys)
        self.assertNotIn('/bucket/' + first['release']['path'], keys)
        self.assertIn('/bucket/' + second['release']['path'], keys)
        self.assertFalse(any('/releases/' in key or '/channels/' in key for key in keys))
        fetch_release(self.crypto, self.public, self.ca.certificate,
                      self.work / 'state.json', self.work / 'snapshot')

    def test_failed_upload_preserves_current_then_retry_removes_abandoned_files(self):
        first = self.publish(self.version('first', 'bootstrap-1.tgz', b'one'))
        pointer = self.server.objects['/bucket/current.p7m']
        new = self.version('second', 'bootstrap-2.tgz', b'two')
        put = self.store.put
        def fail(key, *args, **kwargs):
            if key.endswith('pkg_summary.gz'):
                raise SDKError('interrupted upload')
            return put(key, *args, **kwargs)
        with patch.object(self.store, 'put', side_effect=fail), self.assertRaises(SDKError):
            self.publish(new)
        self.assertEqual(self.server.objects['/bucket/current.p7m'], pointer)
        self.assertIn('/bucket/current/host/darwin-arm64/bootstrap-1.tgz', self.server.objects)
        self.server.objects['/bucket/current/abandoned'] = b'unfinished previous upload'
        self.publish(new)
        self.assertNotIn('/bucket/current/abandoned', self.server.objects)
        self.assertNotIn('/bucket/' + first['release']['path'], self.server.objects)

    def test_reusing_bytes_does_not_upload_package_again(self):
        self.publish(self.version('first', 'bootstrap-1.tgz', b'one'))
        package_key = 'current/host/darwin-arm64/bootstrap-1.tgz'
        with patch.object(self.store, 'put', wraps=self.store.put) as put:
            self.publish(self.version('second', 'bootstrap-1.tgz', b'one'))
        self.assertNotIn(package_key, [call.args[0] for call in put.call_args_list])

    def test_same_package_filename_cannot_silently_change(self):
        self.publish(self.version('first', 'bootstrap-1.tgz', b'one'))
        pointer = self.server.objects['/bucket/current.p7m']
        with self.assertRaises(SDKError):
            self.publish(self.version('second', 'bootstrap-1.tgz', b'two'))
        self.assertEqual(self.server.objects['/bucket/current.p7m'], pointer)

    def test_publisher_lock_prevents_delete_race(self):
        self.server.objects['/bucket/.publisher-lock'] = b'busy publisher'
        with self.assertRaisesRegex(SDKError, 'publisher'):
            self.publish(self.release())
        self.assertEqual(self.server.objects, {'/bucket/.publisher-lock': b'busy publisher'})

    def test_reader_restarts_if_current_changes_before_metadata_download(self):
        first = self.publish(self.version('first', 'bootstrap-1.tgz', b'one'))
        second = self.version('second', 'bootstrap-2.tgz', b'two')
        read = self.public.read
        changed = False
        def switch(key, *args, **kwargs):
            nonlocal changed
            if key == first['release']['path'] and not changed:
                changed = True
                self.publish(second)
            return read(key, *args, **kwargs)
        with patch.object(self.public, 'read', side_effect=switch):
            release = fetch_release(self.crypto, self.public, self.ca.certificate,
                                    self.work / 'state.json', self.work / 'snapshot')
        self.assertEqual(release['release_id'], 'second')

    def test_retry_finishes_cleanup_after_current_was_switched(self):
        self.publish(self.version('first', 'bootstrap-1.tgz', b'one'))
        second = self.version('second', 'bootstrap-2.tgz', b'two')
        delete = self.store.delete
        def fail(key):
            if key.endswith('bootstrap-1.tgz'):
                raise SDKError('interrupted cleanup')
            return delete(key)
        with patch.object(self.store, 'delete', side_effect=fail), self.assertRaises(SDKError):
            self.publish(second)
        self.assertNotIn('/bucket/.publisher-lock', self.server.objects)
        pointer = self.publish(second)
        self.assertEqual(pointer['generation'], 2)
        self.assertNotIn('/bucket/current/host/darwin-arm64/bootstrap-1.tgz', self.server.objects)


# Fixtures use unittest.TestCase for cleanup. Copy only their setup/producer helpers.
for name in list(dir(fixture.DeliveryTests)):
    if name.startswith('test_') and name not in CurrentRepositoryTests.__dict__:
        setattr(CurrentRepositoryTests, name, None)


if __name__ == '__main__':
    unittest.main()
