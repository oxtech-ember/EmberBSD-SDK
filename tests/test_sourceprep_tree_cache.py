# SPDX-License-Identifier: BSD-2-Clause
"""The prepared-tree cache may skip old bytes, never the tree audit."""

import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from test_core import SDK_SOURCE  # noqa: F401  # puts the SDK sources on sys.path
from emberbsd_sdk import inventory, sourceprep
from emberbsd_sdk.errors import SDKError


class PreparedTreeCacheTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory(prefix='sdk-prepared-cache-')
        self.addCleanup(self.work.cleanup)
        self.base = Path(self.work.name)
        self.root = self.base / 'source'
        self.root.mkdir()
        (self.root / 'old').mkdir()
        (self.root / 'old' / 'input.c').write_bytes(b'old source bytes\n')
        (self.root / 'empty').mkdir()
        (self.root / 'link').symlink_to('old/input.c')
        self.cache = self.base / 'tree-cache.json'
        self.identity = {'revision': 'fixture'}
        sourceprep.write_prepared_tree_receipt(
            self.root, package='devel/example', identity=self.identity)

    def validate(self, **kwargs):
        return sourceprep.validate_prepared_tree(
            self.root, package='devel/example', identity=self.identity,
            cache_path=self.cache, **kwargs)

    def measured_validate(self, times, *, allow_failure=False):
        original = inventory.sha256_file
        measured = {'calls': 0, 'bytes': 0}

        def hash_file(path):
            measured['calls'] += 1
            measured['bytes'] += os.stat(path).st_size
            return original(path)

        with patch.object(sourceprep.time, 'time_ns', side_effect=times), \
                patch.object(inventory, 'sha256_file', side_effect=hash_file):
            if allow_failure:
                try:
                    self.validate()
                except SDKError:
                    pass
            else:
                self.validate()
        return measured

    def test_cold_and_warm_hash_cost_and_cache_permissions(self):
        now = time.time_ns()
        payload_bytes = (self.root / 'old' / 'input.c').stat().st_size
        cold = self.measured_validate([now + 3_000_000_000])
        warm = self.measured_validate([now + 4_000_000_000])
        self.assertEqual(cold, {'calls': 1, 'bytes': payload_bytes})
        self.assertEqual(warm, {'calls': 0, 'bytes': 0})
        self.assertEqual(stat.S_IMODE(self.cache.stat().st_mode), 0o600)
        self.assertEqual(sourceprep.validate_prepared_tree(
            self.root, package='devel/example', identity=self.identity)['tree_sha256'],
            sourceprep.read_prepared_tree_receipt(
                self.root, package='devel/example')['tree_sha256'])

    def test_digest_and_stat_identity_are_both_required(self):
        now = time.time_ns()
        self.measured_validate([now + 3_000_000_000])
        cache_before = self.cache.read_bytes()
        path = self.root / 'old' / 'input.c'
        before = path.stat()
        path.write_bytes(b'new source bytes\n')
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = path.stat()
        self.assertEqual(before.st_mtime_ns, after.st_mtime_ns)
        self.assertNotEqual(before.st_ctime_ns, after.st_ctime_ns)
        measured = self.measured_validate([now + 5_000_000_000], allow_failure=True)
        self.assertEqual(measured['calls'], 1)
        self.assertGreater(measured['bytes'], 0)
        with self.assertRaisesRegex(SDKError, 'payload changed'):
            self.validate()
        self.assertEqual(self.cache.read_bytes(), cache_before)

    def test_well_formed_false_cache_digest_is_rehashed_before_acceptance(self):
        now = time.time_ns()
        self.measured_validate([now + 3_000_000_000])
        cache = json.loads(self.cache.read_text())
        cache['files']['old/input.c']['sha256'] = '0' * 64
        self.cache.write_text(json.dumps(cache))
        self.cache.chmod(0o600)
        original = inventory.sha256_file
        calls = []

        def counted(path):
            calls.append(path)
            return original(path)

        with patch.object(sourceprep.time, 'time_ns', return_value=now + 5_000_000_000), \
                patch.object(inventory, 'sha256_file', side_effect=counted):
            self.validate()
        self.assertEqual(len(calls), 1)
        repaired = json.loads(self.cache.read_text())
        self.assertNotEqual(repaired['files']['old/input.c']['sha256'], '0' * 64)

    def test_untrusted_cache_is_a_cold_miss_and_replaced_mode_0600(self):
        now = time.time_ns()
        self.measured_validate([now + 3_000_000_000])
        self.cache.chmod(0o644)
        measured = self.measured_validate([now + 5_000_000_000])
        self.assertEqual(measured['calls'], 1)
        self.assertEqual(stat.S_IMODE(self.cache.stat().st_mode), 0o600)

    def test_malformed_cache_and_clock_rollback_are_cold_misses(self):
        now = time.time_ns()
        self.cache.write_text('{broken')
        self.cache.chmod(0o600)
        cold = self.measured_validate([now + 3_000_000_000])
        self.assertEqual(cold['calls'], 1)
        # A clock rollback cannot make a previously validated digest reusable.
        rolled_back = self.measured_validate([now + 2_000_000_000])
        self.assertEqual(rolled_back['calls'], 1)

    def test_timestamp_race_window_forces_rehash(self):
        now = time.time_ns()
        self.measured_validate([now + 3_000_000_000])
        cache = json.loads(self.cache.read_text())
        cache['started_at_ns'] = now + 500_000_000
        self.cache.write_text(json.dumps(cache))
        self.cache.chmod(0o600)
        measured = self.measured_validate([now + 3_000_000_000])
        self.assertEqual(measured['calls'], 1)

    def test_cache_is_bound_to_root_path_and_inode(self):
        now = time.time_ns()
        self.measured_validate([now + 3_000_000_000])
        other = self.base / 'copied-source'
        archived = self.base / 'archived-source'
        import shutil
        shutil.copytree(self.root, other, symlinks=True)
        self.root.rename(archived)
        other.rename(self.root)
        original = inventory.sha256_file
        calls = []

        def counted(path):
            calls.append(path)
            return original(path)

        with patch.object(sourceprep.time, 'time_ns', return_value=now + 5_000_000_000), \
                patch.object(inventory, 'sha256_file', side_effect=counted):
            sourceprep.validate_prepared_tree(
                self.root, package='devel/example', identity=self.identity,
                cache_path=self.cache)
        self.assertEqual(len(calls), 1)

    def test_changed_receipt_is_rejected_without_reusing_cache(self):
        now = time.time_ns()
        self.measured_validate([now + 3_000_000_000])
        receipt = self.root / sourceprep.PREPARED_TREE_RECEIPT
        changed = json.loads(receipt.read_text())
        changed['tree_sha256'] = 'f' * 64
        receipt.write_text(json.dumps(changed))
        with self.assertRaisesRegex(SDKError, 'payload changed'):
            self.validate()

    def test_namespace_metadata_and_links_remain_part_of_validation(self):
        mutations = (
            lambda: (self.root / 'new').write_bytes(b'new'),
            lambda: (self.root / 'new-empty').mkdir(),
            lambda: (self.root / 'empty').rmdir(),
            lambda: (self.root / 'link').unlink(),
            lambda: ((self.root / 'link').unlink(),
                     (self.root / 'link').symlink_to('empty')),
            lambda: (self.root / 'old' / 'input.c').chmod(0o600),
            lambda: (self.root / 'old' / 'input.c').unlink(),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                # Each case gets an independently prepared tree/cache.
                self.setUp()
                now = time.time_ns()
                self.measured_validate([now + 3_000_000_000])
                mutate()
                with self.assertRaises(SDKError):
                    self.validate()

    def test_same_bytes_new_inode_and_changed_identity_cannot_reuse_cache(self):
        now = time.time_ns()
        self.measured_validate([now + 3_000_000_000])
        path = self.root / 'old' / 'input.c'
        before = path.stat()
        replacement = path.with_suffix('.new')
        replacement.write_bytes(path.read_bytes())
        replacement.chmod(stat.S_IMODE(before.st_mode))
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.replace(replacement, path)
        measured = self.measured_validate([now + 5_000_000_000])
        self.assertEqual(measured['calls'], 1)
        with self.assertRaises(SDKError):
            sourceprep.validate_prepared_tree(
                self.root, package='devel/example', identity={'revision': 'other'},
                cache_path=self.cache)

    def test_cache_must_be_outside_payload(self):
        with self.assertRaisesRegex(SDKError, 'outside the payload'):
            sourceprep.validate_prepared_tree(
                self.root, package='devel/example', identity=self.identity,
                cache_path=self.root / 'cache.json')

    def test_environment_path_inside_payload_is_rejected_before_mkdir(self):
        directory = self.root / 'local-cache'
        with patch.dict(os.environ, {'EMBER_PREPARED_SOURCE_CACHE': str(directory)}):
            with self.assertRaisesRegex(SDKError, 'outside the payload'):
                sourceprep.validate_prepared_tree(
                    self.root, package='devel/example', identity=self.identity)
        self.assertFalse(directory.exists())

    def test_environment_opt_in_creates_private_cache_and_reuses_it(self):
        directory = (self.base / 'private-cache').resolve()
        now = time.time_ns()
        original = inventory.sha256_file
        calls = []

        def counted(path):
            calls.append(path)
            return original(path)

        with patch.dict(os.environ, {'EMBER_PREPARED_SOURCE_CACHE': str(directory)}), \
                patch.object(sourceprep.time, 'time_ns', side_effect=[
                    now + 3_000_000_000, now + 4_000_000_000]), \
                patch.object(inventory, 'sha256_file', side_effect=counted):
            sourceprep.validate_prepared_tree(
                self.root, package='devel/example', identity=self.identity)
            sourceprep.validate_prepared_tree(
                self.root, package='devel/example', identity=self.identity)
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        self.assertEqual(len(calls), 1)
        entries = list(directory.iterdir())
        self.assertEqual(len(entries), 1)
        self.assertEqual(stat.S_IMODE(entries[0].stat().st_mode), 0o600)

    def test_explicit_cache_path_takes_precedence_over_environment(self):
        directory = self.base / 'unused-cache-dir'
        explicit = self.base / 'explicit-cache.json'
        with patch.dict(os.environ, {'EMBER_PREPARED_SOURCE_CACHE': str(directory)}):
            sourceprep.validate_prepared_tree(
                self.root, package='devel/example', identity=self.identity,
                cache_path=explicit)
        self.assertTrue(explicit.exists())
        self.assertFalse(directory.exists())

    def test_unstable_file_during_hash_is_refused(self):
        original = inventory.sha256_file
        path = self.root / 'old' / 'input.c'

        def mutate_after_hash(value):
            digest = original(value)
            path.write_bytes(b'changed during inventory')
            return digest

        with patch.object(inventory, 'sha256_file', side_effect=mutate_after_hash):
            with self.assertRaisesRegex(SDKError, 'changed during inspection'):
                self.validate()


if __name__ == '__main__':
    unittest.main()
