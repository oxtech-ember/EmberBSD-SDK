# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Native tr source contract and refusal gates. No compiler is executed."""

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

_ACTUAL_TR_MANIFEST_SHA256 = program.TR_MANIFEST_SHA256


class TrProgramTests(unittest.TestCase):
    def setUp(self):
        self.native = native_fixtures.NativeProgramTests(methodName='runTest')
        self.native.setUp()
        self.addCleanup(self.native.doCleanups)
        self.native.configure('tr')
        self.case = self.native.case

    def test_two_sources_and_literal_stdin_use_the_shared_runner(self):
        case = self.case
        self.assertIs(case.validate(), case.plan)
        steps = case.plan['commands']
        self.assertEqual([row['id'] for row in steps[:3]], ['compile-str', 'compile-tr', 'link-tr'])
        self.assertEqual(len(steps), 22)
        self.assertEqual(steps[-1]['argv'], [str(case.work / 'tr'), 'a', 'b'])
        self.assertEqual(program.program_sources(case.plan), {})
        for row in steps[:2]:
            self.assertIn(str(case.source / program._BOOTSTRAP_CDEFS), row['argv'])
            self.assertIn(str(case.source / 'usr.bin/tr'), row['argv'])
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=self.native.fake_command) as run:
            result = case.invoke()
        self.assertEqual(run.call_count, 22)
        self.assertEqual(result['status'], 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW')
        self.assertFalse(result['ordinary_provider_qualified'])
        self.assertTrue(result['all_commands_drained'] and result['all_leases_closed'])
        for row in result['commands']:
            if row['phase'] == 'program-case':
                self.assertNotEqual(row['stdout']['path'], row['log']['path'])
                self.assertNotEqual(row['stderr']['path'], row['log']['path'])

    def test_changed_source_route_and_environment_refuse_before_execution(self):
        original = copy.deepcopy(self.case.plan)
        source = self.case.source / 'usr.bin/tr/str.c'
        raw = source.read_bytes()
        for change in ('source', 'argv', 'env'):
            with self.subTest(change=change):
                if change == 'source':
                    source.write_bytes(b'changed source')
                elif change == 'argv':
                    self.case.plan['commands'][0]['argv'].append('-w')
                else:
                    self.case.plan['commands'][0]['env']['PATH'] += ':/bin'
                with patch('emberbsd_sdk.bootstrap_pilot.run_logged') as run:
                    with self.assertRaises(SDKError):
                        self.case.invoke()
                run.assert_not_called()
                source.write_bytes(raw)
                self.case.plan.clear()
                self.case.plan.update(copy.deepcopy(original))

    def test_fixed_profile_refuses_extra_files_missing_headers_and_wrong_case_count(self):
        for key, value in [('files', {'unexpected.txt': 'data'}), ('headers', []),
                           ('runtime_cases', self.case.manifest['runtime_cases'][:-1])]:
            changed = copy.deepcopy(self.case.manifest)
            changed[key] = value
            with self.subTest(key=key), self.assertRaises(SDKError):
                program._native_program_contract(changed, 'tr')

    @unittest.skipUnless(os.environ.get('EMBERBSD_TEST_OS_SOURCE'), 'set EMBERBSD_TEST_OS_SOURCE for actual OS inputs')
    def test_actual_os_contract_and_guarded_array_width_expression(self):
        root = Path(os.environ['EMBERBSD_TEST_OS_SOURCE']).resolve(strict=True)
        relative, digest, sources, provenance = program._profile({'stage': 'tr-bootstrap'})
        raw = (root / relative).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), _ACTUAL_TR_MANIFEST_SHA256)
        manifest = json.loads(raw)
        program._native_program_contract(manifest, 'tr')
        self.assertTrue(any(case['stdin'] and any('[:' in arg for arg in case['args'])
                            for case in manifest['runtime_cases']))
        for member in (*sources, *manifest['headers'], *provenance):
            self.assertTrue((root / member).is_file(), member)
            self.assertFalse((root / member).is_symlink(), member)
        header = (root / program._BOOTSTRAP_CDEFS).read_text()
        # The denominator is the array element type, not a byte or fixed-width type.
        self.assertRegex(header, r'#ifndef __arraycount\s+#define __arraycount\(x\) '
                         r'\(sizeof\(x\) / sizeof\(\(x\)\[0\]\)\)\s+#endif')
        full = (root / 'tools/compat/compat_defs.h').read_text()
        self.assertRegex(full, r'#define\s+__arraycount\(__x\)\s+\(sizeof\(__x\) / sizeof\(__x\[0\]\)\)')
        self.assertIn('__arraycount(classes)', (root / 'usr.bin/tr/str.c').read_text())
        recipe = (root / 'tools/tr/Makefile').read_text()
        self.assertRegex(recipe, r'NOCOMPATLIB=\s*yes\n')
        readme = root / 'tools/tr/README.md'
        for target in re.findall(r'\]\(([^)]+)\)', readme.read_text()):
            self.assertTrue((readme.parent / target).exists(), target)


if __name__ == '__main__':
    unittest.main()
