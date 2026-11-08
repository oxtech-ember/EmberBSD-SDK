# SPDX-License-Identifier: BSD-2-Clause
import errno
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import textwrap
import unittest
from unittest.mock import patch

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

    def test_killed_owner_lock_is_reclaimed(self):
        lock = self.root / 'write.lock'
        owner = subprocess.Popen([sys.executable, '-c', textwrap.dedent(f'''
            import sys, time
            sys.path.insert(0, {str(Path(__file__).resolve().parents[1] / 'src')!r})
            from emberbsd_sdk.package_cache import directory_lock
            with directory_lock({str(lock)!r}):
                print('held', flush=True)
                time.sleep(60)
            ''')], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(owner.stdout.readline().strip(), 'held')
            with self.assertRaisesRegex(SDKError, 'busy'):
                with directory_lock(lock):
                    self.fail('entered a live owner lock')
        finally:
            owner.kill()
            owner.wait()
            owner.stdout.close()
        self.assertEqual((lock / 'pid').read_text().strip(), str(owner.pid))
        with directory_lock(lock):
            self.assertEqual((lock / 'pid').read_text().strip(), str(os.getpid()))
        self.assertFalse(lock.exists())

    def test_failed_owner_file_is_reclaimed_on_next_acquisition(self):
        lock = self.root / 'write.lock'
        with patch('emberbsd_sdk.package_cache.fcntl.flock', side_effect=OSError(errno.ENOLCK, 'no locks')):
            with self.assertRaisesRegex(SDKError, 'busy'):
                with directory_lock(lock):
                    self.fail('entered without the owner file')
        self.assertTrue((lock / 'held').is_file())
        with directory_lock(lock):
            self.assertEqual((lock / 'pid').read_text().strip(), str(os.getpid()))
        self.assertFalse(lock.exists())

    def test_losing_creator_does_not_remove_another_writers_lock(self):
        lock = self.root / 'write.lock'
        creator_at_flock = threading.Event()
        release_creator = threading.Event()
        writer_entered = threading.Event()
        release_writer = threading.Event()
        creator_errors = []
        writer_errors = []
        real_flock = __import__('emberbsd_sdk.package_cache', fromlist=['fcntl']).fcntl.flock

        def gated_flock(descriptor, operation):
            if threading.current_thread().name == 'creator-A':
                creator_at_flock.set()
                if not release_creator.wait(5):
                    raise TimeoutError('creator was not released')
            return real_flock(descriptor, operation)

        def creator():
            try:
                with directory_lock(lock):
                    creator_errors.append('creator entered')
            except SDKError as error:
                creator_errors.append(str(error))
            except BaseException as error:
                creator_errors.append(repr(error))

        def writer():
            try:
                with directory_lock(lock):
                    writer_entered.set()
                    if not release_writer.wait(5):
                        raise TimeoutError('writer was not released')
            except BaseException as error:
                writer_errors.append(repr(error))

        with patch('emberbsd_sdk.package_cache.fcntl.flock', side_effect=gated_flock):
            a = threading.Thread(target=creator, name='creator-A')
            b = threading.Thread(target=writer, name='writer-B')
            creator_c_entered = False
            a_started = b_started = False
            try:
                a.start()
                a_started = True
                self.assertTrue(creator_at_flock.wait(5), 'creator did not reach flock')
                b.start()
                b_started = True
                self.assertTrue(writer_entered.wait(5), 'writer B did not acquire the lock')
                release_creator.set()
                a.join(5)
                self.assertFalse(a.is_alive(), 'creator A did not finish')
                self.assertTrue((lock / 'held').is_file(), 'creator A removed writer B\'s lock file')
                self.assertEqual((lock / 'pid').read_text().strip(), str(os.getpid()))

                with directory_lock(lock):
                    creator_c_entered = True
            except SDKError:
                pass
            finally:
                release_creator.set()
                release_writer.set()
                if a_started:
                    a.join(5)
                if b_started:
                    b.join(5)

            if a_started:
                self.assertFalse(a.is_alive(), 'creator A did not finish')
            if b_started:
                self.assertFalse(b.is_alive(), 'writer B did not finish')

        self.assertFalse(creator_c_entered, 'creator C entered while writer B still owned the lock')
        self.assertEqual(len(creator_errors), 1, creator_errors)
        self.assertIn('busy', creator_errors[0])
        self.assertEqual(writer_errors, [])
        self.assertFalse(lock.exists())
        with directory_lock(lock):
            self.assertEqual((lock / 'pid').read_text().strip(), str(os.getpid()))

    def test_foreign_owner_lock_is_kept(self):
        lock = self.root / 'build.lock'
        lock.mkdir()
        (lock / 'pid').write_text('1\n')
        with self.assertRaisesRegex(SDKError, 'busy'):
            with directory_lock(lock):
                self.fail('entered a foreign lock')
        self.assertEqual((lock / 'pid').read_text(), '1\n')


if __name__ == '__main__':
    unittest.main()
