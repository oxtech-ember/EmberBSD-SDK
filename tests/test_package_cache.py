# SPDX-License-Identifier: BSD-2-Clause
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.package_cache import PackageCache, directory_lock


class Store:
    def __init__(self, payload):
        self.payload, self.calls, self.fail = payload, [], False

    def verify(self, key, size, sha256, *, sink):
        self.calls.append(('get', key))
        if self.fail:
            sink.write(self.payload[:3])
            raise OSError('connection lost')
        sink.write(self.payload)
        if len(self.payload) != size or hashlib.sha256(self.payload).hexdigest() != sha256:
            raise SDKError('invalid downloaded bytes')

    def range(self, key, start, end, total):
        self.calls.append(('range', start, end))
        return self.payload[start:end + 1]


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(b'package bytes')
        self.item = dict(size=13, sha256=hashlib.sha256(self.store.payload).hexdigest())
        self.cache = PackageCache(self.root / 'cache')

    def test_shared_archive_downloaded_once(self):
        first = self.cache.fetch(self.store, 'pkg.tgz', self.item)
        second = self.cache.fetch(self.store, 'pkg.tgz', self.item)
        self.assertEqual(first, second)
        self.assertEqual(first.read_bytes(), self.store.payload)
        self.assertEqual(len(self.store.calls), 1)

    def test_interrupted_download_resumes_then_verifies(self):
        self.store.fail = True
        with self.assertRaises(OSError):
            self.cache.fetch(self.store, 'pkg.tgz', self.item)
        self.store.fail = False
        path = self.cache.fetch(self.store, 'pkg.tgz', self.item)
        self.assertEqual(self.store.calls[-1], ('range', 3, 12))
        self.assertEqual(path.read_bytes(), self.store.payload)

    def test_sdk_transport_error_retains_partial_download(self):
        original = self.store.verify
        def interrupted(key, size, sha256, *, sink):
            sink.write(self.store.payload[:3])
            raise SDKError('HTTP transfer failed: connection reset')
        self.store.verify = interrupted
        with self.assertRaises(SDKError):
            self.cache.fetch(self.store, 'pkg.tgz', self.item)
        self.store.verify = original
        self.cache.fetch(self.store, 'pkg.tgz', self.item)
        self.assertEqual(self.store.calls, [('range', 3, 12)])

    def test_corruption_is_not_published(self):
        self.store.payload = b'wrong content'
        with self.assertRaises(SDKError):
            self.cache.fetch(self.store, 'pkg.tgz', self.item)
        self.assertFalse(self.cache.path(self.item).exists())

    def test_clean_preserves_selected_archives(self):
        path = self.cache.fetch(self.store, 'pkg.tgz', self.item)
        other = self.cache.root / ('a' * 64 + '.tgz')
        other.write_bytes(b'old package')
        self.cache.trim(0, keep={self.item['sha256']})
        self.assertTrue(path.exists())
        self.assertFalse(other.exists())

    def test_second_writer_refused_and_lock_released(self):
        lock = self.root / 'write.lock'
        with directory_lock(lock):
            with self.assertRaisesRegex(SDKError, 'busy'):
                with directory_lock(lock):
                    self.fail('second writer entered')
        self.assertFalse(lock.exists())


if __name__ == '__main__':
    unittest.main()
