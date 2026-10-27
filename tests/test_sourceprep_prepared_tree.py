# SPDX-License-Identifier: BSD-2-Clause
import tempfile
import unittest
from pathlib import Path
from emberbsd_sdk import sourceprep
from emberbsd_sdk.errors import SDKError


class PreparedTreeTests(unittest.TestCase):
    def test_payload_and_owner_identity_are_bound(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work) / 'source'
            root.mkdir()
            (root / 'input.c').write_text('original\n')
            identity = {'archive_sha256': 'abc', 'patches': ['def']}
            record = sourceprep.write_prepared_tree_receipt(root, package='devel/example', identity=identity)
            self.assertEqual(record, sourceprep.validate_prepared_tree(root, package='devel/example', identity=identity))
            with self.assertRaises(SDKError):
                sourceprep.validate_prepared_tree(root, package='devel/other', identity=identity)
            with self.assertRaises(SDKError):
                sourceprep.validate_prepared_tree(root, package='devel/example', identity={'archive_sha256': 'changed'})
            (root / 'input.c').write_text('changed\n')
            with self.assertRaises(SDKError):
                sourceprep.validate_prepared_tree(root, package='devel/example', identity=identity)

    def test_receipt_tampering_and_escaping_link_are_rejected(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work) / 'source'
            root.mkdir()
            (root / 'data').write_text('ok')
            identity = {'revision': 'one'}
            sourceprep.write_prepared_tree_receipt(root, package='devel/example', identity=identity)
            receipt = root / sourceprep.PREPARED_TREE_RECEIPT
            receipt.write_text(receipt.read_text().replace('one', 'two'))
            with self.assertRaises(SDKError):
                sourceprep.validate_prepared_tree(root, package='devel/example', identity=identity)
            receipt.unlink()
            (root / 'escape').symlink_to('../outside')
            with self.assertRaises(SDKError):
                sourceprep.write_prepared_tree_receipt(root, package='devel/example', identity=identity)

    def test_refuses_invalid_identity(self):
        with tempfile.TemporaryDirectory() as work:
            with self.assertRaises(SDKError):
                sourceprep.write_prepared_tree_receipt(work, package='../bad', identity={'x': 'y'})
            with self.assertRaises(SDKError):
                sourceprep.write_prepared_tree_receipt(work, package='devel/example', identity={})
