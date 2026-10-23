# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD; AI-assisted installed inventory contracts.
"""Own temporary trees exercise literal links and unchanged strict payloads."""

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk import API_VERSION, inventory
from emberbsd_sdk.errors import SDKError


class InstalledInventory(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='sdk-installed-inventory-')
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name).resolve()
        self.root = self.parent / 'installed'
        self.root.mkdir()

    def file(self, name='file', data=b'payload', mode=0o640):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)
        return path

    def test_capability_and_unchanged_strict_payload_rows(self):
        self.assertIs(type(inventory.INSTALLED_TREE_INVENTORY_API), int)
        self.assertEqual((API_VERSION, inventory.INSTALLED_TREE_INVENTORY_API), (1, 1))
        path = self.file('dir/z', b'\0\xff')
        self.file('dir/a', b'')
        (path.parent / 'link').symlink_to('z')
        strict = inventory.snapshot_tree(self.root)
        actual = inventory.snapshot_installed_tree(self.root)
        self.assertEqual(actual, strict)
        self.assertEqual(list(actual), ['dir', 'dir/a', 'dir/link', 'dir/z'])
        self.assertEqual(actual['dir/z'], dict(kind='file', mode=0o640, size=2,
                                             sha256=hashlib.sha256(b'\0\xff').hexdigest()))

    def test_existing_link_text_is_literal_and_never_resolved(self):
        outside = self.parent / 'outside'
        outside.write_bytes(b'outside canary: never hash this data')
        self.file()
        targets = {'absolute': str(outside), 'absolute-missing': str(self.parent / 'missing'),
                   'dangling': 'missing', 'escaping': '../outside', 'loop': 'loop',
                   'cycle-a': 'cycle-b', 'cycle-b': 'cycle-a', 'unicode': '../пример path'}
        for name, target in targets.items():
            (self.root / name).symlink_to(target)
        original_hash = inventory.sha256_file
        calls = []

        def only_root(path):
            self.assertEqual(path, str(self.root))
            return str(self.root)

        def only_file(path):
            calls.append(path)
            self.assertEqual(path, str(self.root / 'file'))
            return original_hash(path)

        with patch.object(inventory, 'canonical_existing', only_root), \
                patch.object(inventory, 'sha256_file', only_file):
            rows = inventory.snapshot_installed_tree(self.root)
        self.assertEqual(calls, [str(self.root / 'file')])
        self.assertEqual(set(rows), set(targets) | {'file'})
        for name, target in targets.items():
            self.assertEqual(rows[name], dict(kind='link', target=target,
                mode=stat.S_IMODE((self.root / name).lstat().st_mode)))
        self.assertEqual(outside.read_bytes(), b'outside canary: never hash this data')

    def test_default_payload_still_refuses_installed_link_forms(self):
        (self.parent / 'outside').write_bytes(b'outside')
        for target in (str(self.parent / 'outside'), str(self.parent / 'absent'),
                       '../outside', 'missing', 'link'):
            with self.subTest(target=target):
                path = self.root / 'link'
                path.symlink_to(target)
                with self.assertRaises(SDKError):
                    inventory.snapshot_tree(self.root)
                self.assertEqual(inventory.snapshot_installed_tree(self.root)['link']['target'], target)
                path.unlink()

    def test_exclusions_keep_the_same_exact_entry_contract(self):
        self.file('keep')
        self.file('skip/file')
        (self.root / 'skip/link').symlink_to('/absent')
        rows = inventory.snapshot_installed_tree(self.root, exclude=('skip',))
        self.assertEqual(set(rows), {'keep'})
        self.assertEqual(rows, inventory.snapshot_tree(self.root, exclude=('skip',)))
        rows = inventory.snapshot_installed_tree(self.root, exclude=('skip/file',))
        self.assertEqual(set(rows), {'keep', 'skip', 'skip/link'})

    def test_roots_need_real_directory_ancestors(self):
        (self.root / 'child').mkdir()
        alias = self.parent / 'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        for root in (alias, alias / 'child'):
            with self.subTest(root=root), self.assertRaises(SDKError):
                inventory.snapshot_installed_tree(root)

    def test_special_and_control_path_refusals_are_shared(self):
        for operation in (inventory.snapshot_tree, inventory.snapshot_installed_tree):
            with self.subTest(operation=operation.__name__):
                bad = self.file('bad\nname')
                with self.assertRaisesRegex(SDKError, 'control character'):
                    operation(self.root)
                bad.unlink()
                if hasattr(os, 'mkfifo'):
                    fifo = self.root / 'fifo'
                    os.mkfifo(fifo)
                    with self.assertRaisesRegex(SDKError, 'special file'):
                        operation(self.root)
                    fifo.unlink()

    def test_installed_link_target_controls_and_invalid_utf8_refuse(self):
        link = self.root / 'link'
        for target in ('bad\nname', 'bad\x7fname'):
            with self.subTest(target=target):
                link.symlink_to(target)
                with self.assertRaisesRegex(SDKError, 'control character'):
                    inventory.snapshot_installed_tree(self.root)
                link.unlink()
        link.symlink_to('literal')
        # Some filesystems cannot create invalid UTF-8 link text. Exercise the
        # native readlink boundary without depending on that host restriction.
        with patch.object(inventory.os, 'readlink', return_value='invalid\udcff'), \
                self.assertRaisesRegex(SDKError, 'invalid UTF-8'):
            inventory.snapshot_installed_tree(self.root)

    def test_changed_link_is_refused_after_readlink(self):
        link = self.root / 'link'
        link.symlink_to('old')
        original = os.readlink

        def replace(path):
            result = original(path)
            link.unlink()
            link.symlink_to('new-and-longer')
            return result

        with patch.object(inventory.os, 'readlink', replace), self.assertRaisesRegex(SDKError, 'changed'):
            inventory.snapshot_installed_tree(self.root)

    def test_changed_file_bytes_or_mode_are_refused_after_hashing(self):
        path = self.file()
        original = inventory.sha256_file
        for mutation in ('bytes', 'mode'):
            with self.subTest(mutation=mutation):
                self.file()

                def change(value):
                    result = original(value)
                    if mutation == 'bytes': path.write_bytes(b'changed payload')
                    else: path.chmod(0o600)
                    return result

                with patch.object(inventory, 'sha256_file', change), self.assertRaisesRegex(SDKError, 'changed'):
                    inventory.snapshot_installed_tree(self.root)

    def test_directory_change_during_listing_is_refused(self):
        self.file('dir/file')
        directory = self.root / 'dir'
        original = os.scandir

        @contextmanager
        def change(path):
            with original(path) as entries:
                yield entries
            if path == str(directory):
                before = directory.stat()
                (directory / 'late').write_bytes(b'not in the earlier entry list')
                os.utime(directory, ns=(before.st_atime_ns, before.st_mtime_ns + 10_000_000_000))

        with patch.object(inventory.os, 'scandir', change), self.assertRaisesRegex(SDKError, 'changed'):
            inventory.snapshot_installed_tree(self.root)


if __name__ == '__main__':
    unittest.main()
