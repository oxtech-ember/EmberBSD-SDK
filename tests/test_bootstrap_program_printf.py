# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Native printf source contract and refusal gates. No compiler is executed."""

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import unittest
from unittest.mock import patch

import test_bootstrap_program_native as native_fixtures
from emberbsd_sdk import bootstrap_program as program
from emberbsd_sdk.errors import SDKError


class PrintfProgramTests(unittest.TestCase):
    def setUp(self):
        self.actual_digest = getattr(program, 'PRINTF_MANIFEST_SHA256', None)
        self.native = native_fixtures.NativeProgramTests(methodName='runTest')
        self.native.setUp()
        self.addCleanup(self.native.doCleanups)
        self.native.configure('printf')
        self.case = self.native.case

    def test_argv_program_uses_selected_output_and_shared_runner(self):
        case = self.case
        self.assertIs(case.validate(), case.plan)
        steps = case.plan['commands']
        self.assertEqual([row['id'] for row in steps[:2]], ['compile-printf', 'link-printf'])
        self.assertEqual(len(steps), 27)
        self.assertEqual(steps[-1]['argv'], [str(case.work / 'printf'), '%s', 'synthetic result'])
        self.assertEqual(program.program_sources(case.plan), {})
        self.assertIn(str(case.source / program._BOOTSTRAP_CDEFS), steps[0]['argv'])
        self.assertIn('-Wl,-v', steps[1]['argv'])
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=self.native.fake_command) as run:
            result = case.invoke()
        self.assertEqual(run.call_count, 27)
        self.assertEqual(result['status'], 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW')
        self.assertFalse(result['ordinary_provider_qualified'])
        self.assertTrue(result['all_commands_drained'] and result['all_leases_closed'])

    def test_source_system_command_and_environment_changes_refuse_execution(self):
        original = copy.deepcopy(self.case.plan)
        source = self.case.source / 'usr.bin/printf/printf.c'
        raw = source.read_bytes()
        for change in ('source', 'argv', 'env'):
            with self.subTest(change=change):
                if change == 'source':
                    source.write_bytes(b'changed source')
                elif change == 'argv':
                    self.case.plan['commands'][-1]['argv'][0] = '/usr/bin/printf'
                else:
                    self.case.plan['commands'][0]['env']['PATH'] += ':/bin'
                with patch('emberbsd_sdk.bootstrap_pilot.run_logged') as run:
                    with self.assertRaises(SDKError):
                        self.case.invoke()
                run.assert_not_called()
                source.write_bytes(raw)
                self.case.plan.clear()
                self.case.plan.update(copy.deepcopy(original))

    def test_contract_refuses_files_libraries_missing_headers_and_case_count(self):
        for key, value in [('files', {'unexpected.txt': 'data'}), ('libraries', ['util']),
                           ('headers', []), ('runtime_cases', self.case.manifest['runtime_cases'][:-1])]:
            changed = copy.deepcopy(self.case.manifest)
            changed[key] = value
            with self.subTest(key=key), self.assertRaises(SDKError):
                program._native_program_contract(changed, 'printf')

    @unittest.skipUnless(os.environ.get('EMBERBSD_TEST_OS_SOURCE'), 'set EMBERBSD_TEST_OS_SOURCE for actual OS inputs')
    def test_actual_os_contract_keeps_semantics_at_the_source_owner(self):
        root = Path(os.environ['EMBERBSD_TEST_OS_SOURCE']).resolve(strict=True)
        relative, _, sources, provenance = program._profile({'stage': 'printf-bootstrap'})
        raw = (root / relative).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), self.actual_digest)
        manifest = json.loads(raw)
        program._native_program_contract(manifest, 'printf')
        self.assertTrue(any(case['exit'] != 0 for case in manifest['runtime_cases']))
        self.assertTrue(any('%b' in case['args'] for case in manifest['runtime_cases']))
        self.assertTrue(any('-L' in case['args'] for case in manifest['runtime_cases']))
        for member in (*sources, *manifest['headers'], *provenance):
            self.assertTrue((root / member).is_file(), member)
            self.assertFalse((root / member).is_symlink(), member)
        recipe = (root / 'tools/printf/Makefile').read_text()
        self.assertRegex(recipe, r'NOCOMPATLIB=\s*yes\n')
        readme = root / 'tools/printf/README.md'
        for target in re.findall(r'\]\(([^)]+)\)', readme.read_text()):
            self.assertTrue((readme.parent / target).exists(), target)


if __name__ == '__main__':
    unittest.main()
