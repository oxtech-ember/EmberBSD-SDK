# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Prepared-source timestamp reuse preserves incremental build inputs."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_core import SDK_SOURCE  # noqa: F401  # puts SDK sources on sys.path
from emberbsd_sdk import inventory, sourceprep
from emberbsd_sdk.errors import SDKError


class PreparedReuseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.old, self.new = self.base / 'old', self.base / 'new'
        self.old.mkdir(); self.new.mkdir()
        for root in (self.old, self.new):
            (root / 'same').write_text('same')
            (root / 'changed').write_text('old' if root == self.old else 'new')
            (root / 'mode').write_text('same')
        (self.new / 'mode').chmod(0o755)
        (self.new / 'added').write_text('new')
        for root in (self.old, self.new):
            for path in root.iterdir(): os.utime(path, ns=(1_000_000_000, 1_000_000_000))
        sourceprep.write_prepared_tree_receipt(self.old, package='lang/v8', identity={'version': 1})

    def test_reuses_only_equal_rows_and_valid_receipt(self):
        original = inventory.snapshot_tree
        calls = []
        def snapshot(root, *args, **kwargs):
            calls.append(str(root))
            return original(root, *args, **kwargs)
        with patch.object(inventory, 'snapshot_tree', side_effect=snapshot):
            record = sourceprep.write_prepared_tree_receipt(self.new, package='lang/v8', identity={'version': 2}, previous_root=self.old)
        self.assertEqual(calls.count(os.path.realpath(self.new)), 1)
        self.assertEqual((self.new / 'same').stat().st_mtime_ns, 1_000_000_000)
        for name in ('changed', 'mode', 'added'):
            self.assertGreater((self.new / name).stat().st_mtime_ns, 1_000_000_000)
        self.assertEqual(record, sourceprep.validate_prepared_tree(self.new, package='lang/v8', identity={'version': 2}))
        self.assertEqual((self.old / 'same').stat().st_mtime_ns, 1_000_000_000)

    def test_equal_timestamp_does_not_touch_metadata(self):
        before = (self.new / 'same').stat()
        sourceprep.write_prepared_tree_receipt(self.new, package='lang/v8', identity={'version': 2}, previous_root=self.old)
        after = (self.new / 'same').stat()
        self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(after.st_ctime_ns, before.st_ctime_ns)

    def test_previous_cache_reused_without_extra_new_tree_hash(self):
        cache = self.base / 'cache'
        now = __import__('time').time_ns()
        with patch.dict(os.environ, {'EMBER_PREPARED_SOURCE_CACHE': str(cache)}):
            with patch.object(sourceprep.time, 'time_ns', return_value=now + 3_000_000_000):
                sourceprep.validate_prepared_tree(self.old, package='lang/v8', identity={'version': 1})
            original = inventory.sha256_file
            paths = []
            def measure(path):
                paths.append(str(path))
                return original(path)
            with patch.object(sourceprep.time, 'time_ns', return_value=now + 5_000_000_000), patch.object(inventory, 'sha256_file', side_effect=measure):
                sourceprep.write_prepared_tree_receipt(self.new, package='lang/v8', identity={'version': 2}, previous_root=self.old)
            self.assertFalse(any(path.startswith(os.path.realpath(self.old) + '/') for path in paths))
            self.assertEqual(len(paths), 4)

    def test_symlink_is_not_followed_or_retimed(self):
        (self.old / 'link').symlink_to('same')
        # Reissue receipt because old payload gained a link.
        (self.old / sourceprep.PREPARED_TREE_RECEIPT).unlink()
        sourceprep.write_prepared_tree_receipt(self.old, package='lang/v8', identity={'version': 1})
        (self.new / 'link').symlink_to('same')
        sourceprep.write_prepared_tree_receipt(self.new, package='lang/v8', identity={'version': 2}, previous_root=self.old)
        self.assertTrue((self.new / 'link').is_symlink())
        self.assertEqual((self.new / 'same').stat().st_mtime_ns, 1_000_000_000)

    def test_existing_receipt_refuses_before_retime(self):
        (self.new / sourceprep.PREPARED_TREE_RECEIPT).write_text('{}')
        before = (self.new / 'changed').stat().st_mtime_ns
        with self.assertRaises(SDKError):
            sourceprep.write_prepared_tree_receipt(self.new, package='lang/v8', identity={'v': 2}, previous_root=self.old)
        self.assertEqual((self.new / 'changed').stat().st_mtime_ns, before)

    def test_overlap_refused(self):
        with self.assertRaises(SDKError):
            sourceprep.write_prepared_tree_receipt(self.old, package='lang/v8', identity={'v': 2}, previous_root=self.old)
        nested = self.old / 'nested'; nested.mkdir()
        with self.assertRaises(SDKError):
            sourceprep.write_prepared_tree_receipt(nested, package='lang/v8', identity={'v': 2}, previous_root=self.old)

    def test_hardlink_refuses_before_any_write(self):
        (self.new / 'added').unlink(); os.link(self.old / 'same', self.new / 'added')
        before = (self.new / 'changed').stat().st_mtime_ns
        with self.assertRaises(SDKError):
            sourceprep.write_prepared_tree_receipt(self.new, package='lang/v8', identity={'v': 2}, previous_root=self.old)
        self.assertEqual((self.new / 'changed').stat().st_mtime_ns, before)
        self.assertFalse((self.new / sourceprep.PREPARED_TREE_RECEIPT).exists())

    def test_previous_changes_after_inventory_refused_before_retime(self):
        original = inventory.snapshot_tree
        def snapshot(root, *args, **kwargs):
            rows = original(root, *args, **kwargs)
            if str(root) == os.path.realpath(self.new):
                (self.old / 'same').write_text('late mutation')
            return rows
        before = (self.new / 'changed').stat().st_mtime_ns
        with patch.object(inventory, 'snapshot_tree', side_effect=snapshot):
            with self.assertRaises(SDKError):
                sourceprep.write_prepared_tree_receipt(self.new, package='lang/v8', identity={'v': 2}, previous_root=self.old)
        self.assertEqual((self.new / 'changed').stat().st_mtime_ns, before)
        self.assertFalse((self.new / sourceprep.PREPARED_TREE_RECEIPT).exists())

    def test_changed_previous_refused(self):
        (self.old / 'same').write_text('tampered')
        with self.assertRaises(SDKError):
            sourceprep.write_prepared_tree_receipt(self.new, package='lang/v8', identity={'v': 2}, previous_root=self.old)
        self.assertFalse((self.new / sourceprep.PREPARED_TREE_RECEIPT).exists())

if __name__ == '__main__': unittest.main()
