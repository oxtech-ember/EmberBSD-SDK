# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Selected grammar generation, with synthetic children and real owner data."""
import copy
import hashlib
import json
import os
import shlex
from pathlib import Path
import unittest
from unittest.mock import patch
import test_bootstrap_program_native as fixtures
from emberbsd_sdk import bootstrap_program as program
from emberbsd_sdk.bootstrap_generated import verify_history
from emberbsd_sdk.errors import SDKError, CommandError


class ExprProgramTests(unittest.TestCase):
    def fixture(self):
        native = fixtures.NativeProgramTests(methodName='runTest')
        native.setUp()
        self.addCleanup(native.doCleanups)
        native.configure('expr')
        return native.case

    @staticmethod
    def fake(case, argv, log, **kwargs):
        if Path(log).stem == 'generate-expr':
            (case.work / 'expr.c').write_bytes(b'synthetic generated parser\n')
        return case.fake_command(argv, log, **kwargs)

    def test_generator_inputs_have_a_before_child_guard(self):
        self.assertTrue(callable(getattr(program, 'prepare_program_step', None)))

    def test_expr_selects_the_canonical_grammar(self):
        relative, _, sources, provenance = program._profile({'stage': 'expr-bootstrap'})
        self.assertEqual(relative, 'tools/expr/bootstrap.json')
        self.assertEqual(sources, ('bin/expr/expr.y',))
        self.assertIn('bin/expr/Makefile', provenance)
        self.assertIn('tools/expr/Makefile', provenance)

    def test_selected_generator_precedes_compile_and_retains_evidence(self):
        case = self.fixture()
        self.assertIs(case.validate(), case.plan)
        steps = case.plan['commands']
        self.assertEqual(len(steps), 56)
        self.assertEqual(steps[0]['argv'], [*case.commands['yacc'], '-o', str(case.work / 'expr.c'),
                                           str(case.source / 'bin/expr/expr.y')])
        self.assertEqual(steps[1]['argv'][-3], str(case.work / 'expr.c'))
        self.assertIn('-fwrapv', steps[1]['argv'])
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=lambda *a, **kw: self.fake(case, *a, **kw)):
            result = case.invoke()
        self.assertEqual(result['status'], 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW')
        self.assertEqual(result['commands'][0]['generated']['expr.c']['sha256'], result['outputs']['expr.c'])
        verify_history(case.plan, result)
        result['commands'][0]['generated']['expr.c']['sha256'] = '0' * 64
        with self.assertRaises(SDKError):
            verify_history(case.plan, result)

    def test_missing_relative_or_changed_yacc_refuses_before_children(self):
        for change in ('absent', 'relative', 'outside', 'arguments', 'different', 'role'):
            with self.subTest(change=change):
                case = self.fixture()
                if change == 'absent':
                    del case.commands['yacc']
                elif change == 'relative':
                    case.descriptor['tools']['yacc']['path'] = 'yacc'
                    case.commands['yacc'] = ('yacc',)
                elif change == 'outside':
                    case.descriptor['tools']['yacc']['path'] = str(case.base / 'outside-yacc')
                    case.commands['yacc'] = (str(case.base / 'outside-yacc'),)
                elif change == 'arguments':
                    case.commands['yacc'] += ('-d',)
                elif change == 'different':
                    case.commands['yacc'] = (str(case.f.bundle / 'other-yacc'),)
                else:
                    case.plan['required_roles'].remove('yacc')
                with patch('emberbsd_sdk.bootstrap_pilot.run_logged') as run, self.assertRaises(SDKError):
                    case.invoke()
                run.assert_not_called()

    def test_generator_failure_missing_empty_or_link_output_blocks_compiler(self):
        for change in ('failed', 'missing', 'empty', 'link'):
            with self.subTest(change=change):
                case = self.fixture()
                seen = []
                def child(argv, log, **kwargs):
                    seen.append(Path(log).stem)
                    Path(log).write_text('synthetic generator log\n')
                    if change == 'failed':
                        raise CommandError(1, argv, log=log)
                    if change == 'empty':
                        (case.work / 'expr.c').write_bytes(b'')
                    if change == 'link':
                        (case.work / 'expr.c').symlink_to(case.source / 'bin/expr/expr.y')
                with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=child):
                    result = case.invoke()
                self.assertEqual(result['status'], 'FAILED')
                self.assertEqual(seen, ['generate-expr'])

    def test_changed_generated_input_is_refused_before_compiler_child(self):
        case = self.fixture()
        seen = []
        real_verify = program.verify_program_step
        def verify(step, work, plan, **kwargs):
            result = real_verify(step, work, plan, **kwargs)
            if step['id'] == 'generate-expr':
                (case.work / 'expr.c').write_bytes(b'changed after recorded generator\n')
            return result
        def child(*args, **kwargs):
            seen.append(Path(args[1]).stem)
            return self.fake(case, *args, **kwargs)
        with patch('emberbsd_sdk.bootstrap_program.verify_program_step', side_effect=verify), \
             patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=child):
            result = case.invoke()
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(result['commands'][-1]['id'], 'compile-expr')
        self.assertIsNone(result['commands'][-1]['actual_exit'])
        self.assertEqual(seen, ['generate-expr'])

    def test_changed_grammar_is_refused_before_generator(self):
        case = self.fixture()
        (case.source / 'bin/expr/expr.y').write_bytes(b'changed grammar\n')
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged') as run, self.assertRaises(SDKError):
            case.invoke()
        run.assert_not_called()

    def test_stale_output_unknown_step_and_reordered_compile_are_refused(self):
        case = self.fixture()
        case.work.mkdir()
        (case.work / 'expr.c').write_bytes(b'stale')
        with self.assertRaises(SDKError):
            program.prepare_program_step(case.plan['commands'][0], case.plan)
        (case.work / 'expr.c').unlink()
        for step in (dict(case.plan['commands'][0], id='unknown'), case.plan['commands'][1]):
            with self.assertRaises(SDKError):
                program.prepare_program_step(step, case.plan)

    @unittest.skipUnless(os.environ.get('EMBERBSD_TEST_EXPR_SOURCE'), 'select the expr owner candidate')
    def test_complete_real_owner_manifest_builds_every_step(self):
        root = Path(os.environ['EMBERBSD_TEST_EXPR_SOURCE'])
        relative, digest, sources, provenance = program._profile({'stage': 'expr-bootstrap'})
        raw = (root / relative).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), digest)
        manifest = json.loads(raw)
        program._native_program_contract(manifest, 'expr')
        for member in {relative, *sources, *manifest['headers'], *provenance}:
            self.assertTrue((root / member).is_file(), member)
        case = self.fixture()
        # The real owner digest is tested without a synthetic digest substitution.
        with patch.object(program, 'EXPR_MANIFEST_SHA256', digest):
            plan = dict(case.plan, program=dict(source=dict(root=str(root)),
                manifest=dict(path=str(root / relative), sha256=digest)))
            with patch('subprocess.Popen', side_effect=AssertionError('no native child')):
                steps = program.program_steps(plan, case.descriptor, case.commands, case.flags)
        self.assertEqual(len(steps), 56)
        self.assertEqual([row['id'] for row in steps if row['phase'] == 'program-case'],
                         ['case-' + row['name'] for row in manifest['runtime_cases']])
        self.assertTrue(all('\0' not in word for row in steps for word in row['argv']))
        # Test the literal shell argument boundary, not only JSON string spelling.
        expected = {
            'match-capture': ['abc', ':', r'a\(b\)c'],
            'capture-none': ['abc', ':', r'z\(x\)'],
            'slash-operand': ['/usr/bin', ':', r'.*/\(.*\)'],
            'nested-matches': ['(', 'abc', ':', r'a\(bc\)', ')', ':', 'b.*'],
        }
        for name, arguments in expected.items():
            step = next(row for row in steps if row['id'] == 'case-' + name)
            words = shlex.split(step['argv'][-1])
            start = words.index('|') + 2
            self.assertEqual(words[start:start + len(arguments)], arguments, name)
