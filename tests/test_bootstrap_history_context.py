# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Bounded synthetic history DAGs. No native subprocesses or acceptance."""
import unittest
from unittest.mock import patch
import test_core  # Adds the SDK source to sys.path.
from emberbsd_sdk.bootstrap_seed import _history


class HistoryContextTests(unittest.TestCase):
    def graph(self, size):
        calls = []
        def visit(level):
            providers = {str(i): dict(origin='source-build') for i in range(level)}
            histories = {str(i): dict(kind='source-build', source_archives=[], patches=[],
                                      program_build=dict(node=i)) for i in range(level)}
            return _history(histories, providers, descriptor=dict(node=level), inspect_inputs=False)
        def verify(history, name, descriptor, **kwargs):
            level = history['program_build']['node']
            calls.append(level)
            visit(level)
            return []
        with patch('emberbsd_sdk.bootstrap_program_history.verify_program_build_history', side_effect=verify):
            result = visit(size)
        self.assertEqual(result, {str(i): [] for i in range(size)})
        return calls

    def test_shared_history_dag_expands_each_node_once(self):
        self.assertEqual(len(self.graph(12)), 78)

    def test_independent_top_level_calls_are_fresh(self):
        self.assertEqual(len(self.graph(8)), 36)
        self.assertEqual(len(self.graph(8)), 36)

    def test_cycle_refuses_before_recursion_limit(self):
        from emberbsd_sdk.errors import SDKError
        histories = dict(p=dict(kind='source-build', source_archives=[], patches=[], program_build={}))
        providers = dict(p=dict(origin='source-build'))
        def visit(): return _history(histories, providers, descriptor={}, inspect_inputs=False)
        with patch('emberbsd_sdk.bootstrap_program_history.verify_program_build_history', side_effect=lambda *a, **k: visit()):
            with self.assertRaisesRegex(SDKError, 'cycle'): visit()
        self.assertEqual(self.graph(3), [0, 1, 0, 2, 0, 1])

    def test_bounds_and_failure_reset_the_scope(self):
        from emberbsd_sdk import bootstrap_history_context as context
        from emberbsd_sdk.errors import SDKError
        with patch.object(context, 'MAX_NODES', 3):
            with self.assertRaisesRegex(SDKError, 'bound'): self.graph(4)
        self.assertEqual(len(self.graph(4)), 10)
        def descend(n):
            return context.validate_history(n, lambda: descend(n - 1) if n else {})
        with patch.object(context, 'MAX_DEPTH', 3):
            with self.assertRaisesRegex(SDKError, 'bound'): descend(4)
        self.assertEqual(descend(4), {})

    def test_changed_hashed_input_refuses_a_memo_hit(self):
        import hashlib
        import tempfile
        from pathlib import Path
        from emberbsd_sdk.bootstrap_history_context import validate_history
        from emberbsd_sdk.native_toolset import _bytes
        from emberbsd_sdk.errors import SDKError
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary, 'evidence.json')
            path.write_bytes(b'original')
            reference = dict(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
            calls = []
            def leaf():
                calls.append(1)
                _bytes(reference)
                return dict(p=['named-unknown'])
            def root():
                validate_history('leaf', leaf)
                path.write_bytes(b'changed!')
                return validate_history('leaf', leaf)
            with self.assertRaisesRegex(SDKError, 'changed'):
                validate_history('root', root)
            self.assertEqual(calls, [1])
            # A later independent validation must read its new reference, not retain a failure or a cached pass.
            reference['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(validate_history('leaf', leaf), dict(p=['named-unknown']))
            self.assertEqual(calls, [1, 1])

    def test_source_tree_new_member_refuses_a_memo_hit(self):
        import tempfile
        from pathlib import Path
        from emberbsd_sdk.bootstrap_history_context import validate_history
        from emberbsd_sdk.inventory import snapshot_tree
        from emberbsd_sdk.errors import SDKError
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / 'source.c').write_bytes(b'original source')
            def leaf():
                snapshot_tree(str(root))
                return {}
            def scope():
                validate_history('leaf', leaf)
                (root / 'extra.c').write_bytes(b'unreviewed source')
                validate_history('leaf', leaf)
            with self.assertRaisesRegex(SDKError, 'changed'): validate_history('scope', scope)

    def test_retained_source_mtime_change_refuses_a_memo_hit(self):
        import os
        import tempfile
        from pathlib import Path
        from emberbsd_sdk.bootstrap_history_context import validate_history
        from emberbsd_sdk.inventory import snapshot_tree
        from emberbsd_sdk.errors import SDKError
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = root / 'source.c'
            path.write_bytes(b'same bytes')
            def leaf():
                snapshot_tree(str(root))
                return {}
            def scope():
                validate_history('leaf', leaf)
                info = path.stat()
                os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000000))
                validate_history('leaf', leaf)
            with self.assertRaisesRegex(SDKError, 'changed'): validate_history('scope', scope)

    def test_cached_results_cannot_change_unknowns_and_are_rehashed(self):
        import hashlib
        import tempfile
        from pathlib import Path
        from emberbsd_sdk.bootstrap_history_context import validate_history
        from emberbsd_sdk.native_toolset import _bytes
        from emberbsd_sdk import inventory
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary, 'receipt')
            path.write_bytes(b'original')
            reference = dict(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
            def leaf():
                _bytes(reference)
                return dict(p=['historical-toolchain-closure'])
            def root():
                first = validate_history('leaf', leaf)
                first['p'].clear()
                with patch.object(inventory, 'sha256_file', wraps=inventory.sha256_file) as hashed:
                    second = validate_history('leaf', leaf)
                self.assertEqual(hashed.call_count, 1)
                return second
            self.assertEqual(validate_history('root', root), dict(p=['historical-toolchain-closure']))

    def test_current_input_inspection_always_runs(self):
        from emberbsd_sdk.bootstrap_history_context import validate_history
        histories = dict(p=dict(kind='source-build', source_archives=[], patches=[], program_build={}))
        providers = dict(p=dict(origin='source-build'))
        def root():
            for inspect in (False, True, False, True):
                _history(histories, providers, descriptor={}, inspect_inputs=inspect)
        with patch('emberbsd_sdk.bootstrap_program_history.verify_program_build_history', return_value=[]) as checked:
            validate_history('root', root)
        self.assertEqual([call.kwargs['inspect_inputs'] for call in checked.call_args_list], [False, True, True])

    def test_guard_budget_is_bounded(self):
        import tempfile
        from pathlib import Path
        from emberbsd_sdk import bootstrap_history_context as context
        from emberbsd_sdk.inventory import snapshot_tree
        from emberbsd_sdk.errors import SDKError
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / 'file').write_bytes(b'input')
            with patch.object(context, 'MAX_GUARDS', 1):
                with self.assertRaisesRegex(SDKError, 'guard bound'):
                    context.validate_history('tree', lambda: snapshot_tree(str(root)))
