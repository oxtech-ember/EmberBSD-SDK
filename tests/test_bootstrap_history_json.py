# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Strict JSON reuse within one history validation; no native children."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from emberbsd_sdk import bootstrap_history_context as context
from emberbsd_sdk import jsonio, native_toolset
from emberbsd_sdk.errors import SDKError


class HistoryJSONTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name, 'metadata.json')

    def reference(self, raw):
        self.path.write_bytes(raw)
        return dict(path=str(self.path), sha256=hashlib.sha256(raw).hexdigest())

    def test_repeated_metadata_keeps_reads_but_validates_json_once(self):
        reference = self.reference(b'{"nested": [1, 2, 3]}')
        def validate():
            return [native_toolset._json(reference) for _ in range(6)]
        with patch.object(jsonio, '_object', wraps=jsonio._object) as strict, \
             patch.object(native_toolset, '_bytes', wraps=native_toolset._bytes) as read:
            result = context.validate_history('scope', validate)
        self.assertEqual(result, [{'nested': [1, 2, 3]}] * 6)
        self.assertEqual(read.call_count, 6)
        self.assertEqual(strict.call_count, 1)

    def test_changed_reference_bytes_fail_and_new_bytes_validate_strictly(self):
        reference = self.reference(b'{"value": 1}')
        def validate():
            self.assertEqual(native_toolset._json(reference), {'value': 1})
            self.path.write_bytes(b'{"value": 2}')
            with self.assertRaisesRegex(SDKError, 'SHA256 mismatch'):
                native_toolset._json(reference)
            changed = dict(path=str(self.path), sha256=hashlib.sha256(self.path.read_bytes()).hexdigest())
            with self.assertRaisesRegex(SDKError, 'input changed within validation'):
                native_toolset._json(changed)
            return changed
        with patch.object(jsonio, '_object', wraps=jsonio._object) as strict:
            changed = context.validate_history('scope', validate)
            self.assertEqual(context.validate_history('next', lambda: native_toolset._json(changed)), {'value': 2})
        self.assertEqual(strict.call_count, 2)

    def test_returned_objects_do_not_share_mutable_children(self):
        reference = self.reference(b'{"outer": {"values": [1]}}')
        def validate():
            first = native_toolset._json(reference)
            first['outer']['values'].append(2)
            first['unknown'] = ['changed']
            return native_toolset._json(reference)
        self.assertEqual(context.validate_history('scope', validate), {'outer': {'values': [1]}})

    def test_all_strict_refusals_apply_after_a_cached_valid_document(self):
        bad = [b'{"x": 1, "x": 2}', b'{"x": "\\ud800"}', b'{"\\ud800": 1}',
               b'{"x": "\xff"}', b'{"x": NaN}', b'{"x": Infinity}', b'{"x": 1e999}',
               b'[' * 33 + b'0' + b']' * 33]
        for raw in bad:
            with self.subTest(raw=raw):
                reference = self.reference(b'{"valid": true}')
                def validate():
                    native_toolset._json(reference)
                    native_toolset._json(reference)
                    other = self.path.with_name('invalid.json')
                    other.write_bytes(raw)
                    return native_toolset._json(dict(path=str(other), sha256=hashlib.sha256(raw).hexdigest()))
                with self.assertRaises(SDKError):
                    context.validate_history('scope', validate)
                self.assertIsNone(context._CURRENT.get())

    def test_independent_and_failed_scopes_start_fresh(self):
        reference = self.reference(b'{"value": 1}')
        def fail():
            native_toolset._json(reference)
            raise SDKError('fixture failure')
        with patch.object(jsonio, '_object', wraps=jsonio._object) as strict:
            for _ in range(2):
                context.validate_history('scope', lambda: native_toolset._json(reference))
            with self.assertRaisesRegex(SDKError, 'fixture failure'):
                context.validate_history('scope', fail)
            context.validate_history('scope', lambda: native_toolset._json(reference))
        self.assertEqual(strict.call_count, 4)
        self.assertIsNone(context._CURRENT.get())

    def test_reads_outside_a_history_scope_keep_strict_validation(self):
        reference = self.reference(b'{"value": 1}')
        with patch.object(jsonio, '_object', wraps=jsonio._object) as strict:
            native_toolset._json(reference)
            native_toolset._json(reference)
        self.assertEqual(strict.call_count, 2)

    def test_parser_limits_are_part_of_validation_identity(self):
        raw = b'{"nested": []}'
        def validate():
            with patch.object(jsonio, '_object', wraps=jsonio._object) as strict:
                context.load_json(raw, max_bytes=len(raw), max_depth=2)
                context.load_json(raw, max_bytes=len(raw), max_depth=2)
                self.assertEqual(strict.call_count, 1)
                context.load_json(raw, max_bytes=len(raw) + 1, max_depth=2)
                context.load_json(raw, max_bytes=len(raw) + 1, max_depth=3)
                self.assertEqual(strict.call_count, 3)
                with self.assertRaisesRegex(SDKError, 'nesting limit'):
                    context.load_json(raw, max_bytes=len(raw), max_depth=1)
                with self.assertRaisesRegex(SDKError, 'size limit'):
                    context.load_json(raw, max_bytes=len(raw) - 1, max_depth=2)
        context.validate_history('scope', validate)

    def test_document_budget_falls_back_to_full_strict_validation(self):
        first, second = b'{"value": 1}', b'{"value": 2}'
        def validate():
            with patch.object(jsonio, '_object', wraps=jsonio._object) as strict:
                context.load_json(first)
                context.load_json(second)
                context.load_json(second)
                context.load_json(first)
                self.assertEqual(strict.call_count, 3)
            current = context._CURRENT.get()
            self.assertEqual(len(current.json_validated), 1)
            self.assertEqual(current.json_weight, len(first))
            # A changed budget cannot reuse a marker from the earlier policy.
            with patch.object(context, 'MAX_JSON_DOCUMENTS', 0), \
                 patch.object(jsonio, '_object', wraps=jsonio._object) as strict:
                context.load_json(first)
                self.assertEqual(strict.call_count, 1)
        with patch.object(context, 'MAX_JSON_DOCUMENTS', 1):
            context.validate_history('scope', validate)

    def test_byte_budget_retains_no_oversized_document_marker(self):
        raw = b'{"value": [1, 2, 3]}'
        def validate():
            with patch.object(jsonio, '_object', wraps=jsonio._object) as strict:
                context.load_json(raw)
                context.load_json(raw)
                self.assertEqual(strict.call_count, 2)
            self.assertEqual(context._CURRENT.get().json_validated, set())
            self.assertEqual(context._CURRENT.get().json_weight, 0)
        with patch.object(context, 'MAX_JSON_VALIDATED_BYTES', len(raw) - 1):
            context.validate_history('scope', validate)

    def test_caught_parser_error_discards_all_json_markers(self):
        raw = b'{"value": 1}'
        def validate():
            context.load_json(raw)
            with self.assertRaisesRegex(SDKError, 'duplicate metadata key'):
                context.load_json(b'{"duplicate": 1, "duplicate": 2}')
            self.assertEqual(context._CURRENT.get().json_validated, set())
            self.assertEqual(context._CURRENT.get().json_weight, 0)
            with patch.object(jsonio, '_object', wraps=jsonio._object) as strict:
                context.load_json(raw)
                self.assertEqual(strict.call_count, 1)
        context.validate_history('scope', validate)

    def test_large_shared_document_has_one_strict_pass_and_fresh_values(self):
        raw = json.dumps({'rows': [{'path': 'source/' + str(i), 'sha256': 'a' * 64}
                                   for i in range(4096)]}).encode()
        reference = self.reference(raw)
        def validate():
            first = native_toolset._json(reference)
            first['rows'][0]['path'] = 'caller changed this object'
            for _ in range(3):
                self.assertEqual(native_toolset._json(reference)['rows'][0]['path'], 'source/0')
            current = context._CURRENT.get()
            self.assertEqual(len(current.json_validated), 1)
            self.assertEqual(current.json_weight, len(raw))
        with patch.object(jsonio, '_object', wraps=jsonio._object) as strict:
            context.validate_history('scope', validate)
        self.assertEqual(strict.call_count, 4097)


if __name__ == '__main__':
    unittest.main()
