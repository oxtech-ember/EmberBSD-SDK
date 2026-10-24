# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Named metadata observations are pinned data, never shell code."""

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from emberbsd_sdk import bootstrap_producer as producer
from emberbsd_sdk.errors import SDKError


class MetadataValueTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'values.log'
        self.values = {name: '' for name in producer.VARIABLES}

    def reference(self, raw):
        self.path.write_bytes(raw)
        return dict(path=str(self.path), sha256=hashlib.sha256(raw).hexdigest())

    def raw(self):
        return ''.join(name + '=' + self.values[name] + '\n'
                       for name in producer.VARIABLES).encode('utf-8')

    def test_empty_and_literal_values_are_preserved_without_execution(self):
        values = ['', 'word', 'space value', r'first\nsecond', 'double"quote\'single',
                  '-n', '  leading and trailing  ', 'x=y=z', '# not a diagnostic',
                  '`touch forbidden` $(touch forbidden)', 'snowman: \u2603']
        self.values.update(zip(producer.VARIABLES, values))
        with patch('subprocess.Popen', side_effect=AssertionError('metadata is not executable')):
            self.assertEqual(producer.metadata_values(self.reference(self.raw())), self.values)
        self.assertFalse((self.path.parent / 'forbidden').exists())

    def test_missing_duplicate_reordered_unknown_and_diagnostic_rows_are_refused(self):
        rows = self.raw().decode('utf-8').splitlines(keepends=True)
        variants = [rows[:-1], [rows[0], *rows], [rows[1], rows[0], *rows[2:]],
                    [rows[0], rows[0], *rows[2:]], ['UNEXPECTED=value\n', *rows[1:]],
                    ['bmake: warning: nested command failed\n', *rows],
                    [rows[0], 'bmake: warning: nested command failed\n', *rows[2:]],
                    [rows[0].replace('=', '', 1), *rows[1:]], [*rows, '\n']]
        for number, variant in enumerate(variants):
            with self.subTest(number=number):
                with self.assertRaises(SDKError):
                    producer.metadata_values(self.reference(''.join(variant).encode('utf-8')))

    def test_newlines_cr_nul_non_utf8_and_unterminated_output_are_refused(self):
        raw = self.raw()
        for value in (raw[:-1], raw.replace(b'PKGNAME=', b'PKGNAME=first\nsecond', 1),
                      raw.replace(b'PKGNAME=', b'PKGNAME=\r', 1),
                      raw.replace(b'PKGNAME=', b'PKGNAME=\x00', 1),
                      raw.replace(b'PKGNAME=', b'PKGNAME=\xff', 1)):
            with self.subTest(value=value[:30]):
                with self.assertRaises(SDKError):
                    producer.metadata_values(self.reference(value))

    def test_changed_log_is_refused_by_the_existing_file_reference_validator(self):
        reference = self.reference(self.raw())
        self.path.write_bytes(b'changed\n')
        with self.assertRaises(SDKError):
            producer.metadata_values(reference)


if __name__ == '__main__':
    unittest.main()
