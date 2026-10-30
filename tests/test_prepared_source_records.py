# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Prepared sources are identified by their prepare receipts, not re-read."""
import json
import os
from unittest.mock import patch

from test_core import TemporaryCase
from emberbsd_sdk import inventory, libedit, process, sourceprep
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.sysroot_builder import SysrootBuilder


def receipt(package, tree='a' * 64):
    return {'format': 'emberbsd-prepared-source', 'version': 1, 'package': package,
            'identity': {'effective': {'api': 2, 'package': package, 'upstream': 'b' * 40}},
            'tree_sha256': tree}


class PreparedSourceRecordTests(TemporaryCase):
    def prepared(self, name, package, **kwargs):
        record = receipt(package, **kwargs)
        self.write(f'prepared/{name}/main.c', 'int main(void) { return 0; }\n')
        self.write(f'prepared/{name}/' + sourceprep.PREPARED_TREE_RECEIPT, json.dumps(record))
        return record

    def builder(self, names):
        builder = SysrootBuilder({})
        builder.paths = {name + '-source': str(self.root / 'prepared' / name) for name in names}
        return builder

    def test_records_come_from_receipts_without_owner_or_tree_reads(self):
        expected = {name: self.prepared(name, package) for name, package in SysrootBuilder.COMMON_SOURCES}
        builder = self.builder(expected)
        refuse = AssertionError('the tree or its owner must not be read again')
        with patch.object(process, 'capture', side_effect=refuse), \
                patch.object(inventory, 'snapshot_tree', side_effect=refuse), \
                patch.object(inventory, 'sha256_file', side_effect=refuse):
            self.assertEqual(builder.verify_common_sources(), expected)
        # A payload change after preparation is the preparer's concern; the
        # receipt still names the prepared identity.
        self.write('prepared/byacc/main.c', 'changed\n')
        self.assertEqual(builder.verify_common_sources()['byacc'], expected['byacc'])

    def test_source_prepared_again_during_a_run_changes_its_record(self):
        before = self.builder(['byacc'])
        self.prepared('byacc', 'devel/byacc')
        planned = before.verify_common_sources()
        self.prepared('byacc', 'devel/byacc', tree='c' * 64)
        self.assertNotEqual(before.verify_common_sources(), planned)

    def test_foreign_missing_or_malformed_receipt_fails(self):
        self.prepared('byacc', 'devel/other')
        with self.assertRaisesRegex(SDKError, 'invalid byacc prepared source receipt'):
            self.builder(['byacc']).verify_common_sources()
        os.unlink(self.root / 'prepared/byacc' / sourceprep.PREPARED_TREE_RECEIPT)
        with self.assertRaises(SDKError):
            self.builder(['byacc']).verify_common_sources()
        record = receipt('devel/byacc', tree='short')
        self.write('prepared/byacc/' + sourceprep.PREPARED_TREE_RECEIPT, json.dumps(record))
        with self.assertRaisesRegex(SDKError, 'invalid byacc prepared source receipt'):
            self.builder(['byacc']).verify_common_sources()

    def test_libedit_identity_check_skips_only_the_tree_digest(self):
        root = self.root / 'libedit'
        for name in ('src', 'doc'):
            (root / name).mkdir(parents=True)
        self.write('libedit/ports.distinfo', 'distinfo\n')
        record = {'recipe': {'distinfo_sha256': inventory.sha256_file(root / 'ports.distinfo')},
                  'tree_sha256': '0' * 64}
        with patch.object(libedit, 'validate_identity', return_value=record), \
                patch.object(libedit.jsonio, 'read_json', return_value=record):
            self.assertEqual(libedit.validate_source(root, content=False), record)
            with self.assertRaisesRegex(SDKError, 'libedit source tree changed'):
                libedit.validate_source(root)
            self.write('libedit/ports.distinfo', 'other\n')
            with self.assertRaisesRegex(SDKError, 'libedit distinfo changed'):
                libedit.validate_source(root, content=False)
