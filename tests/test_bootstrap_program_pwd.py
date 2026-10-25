# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Native pwd uses a controller-owned directory oracle, never child output."""

import copy
import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import test_bootstrap_program_native as native_fixtures
from emberbsd_sdk import bootstrap_program as program
from emberbsd_sdk.errors import SDKError


class PwdProgramTests(unittest.TestCase):
    def setUp(self):
        self.actual_digest = getattr(program, 'PWD_MANIFEST_SHA256', None)
        self.native = native_fixtures.NativeProgramTests(methodName='runTest')
        self.native.setUp()
        self.addCleanup(self.native.doCleanups)
        self.native.configure('pwd')
        self.case = self.native.case

    def fake_command(self, argv, log, **kwargs):
        result = self.native.fake_command(argv, log, **kwargs)
        step = next(row for row in self.case.plan['commands'] if row['id'] == Path(log).stem)
        if step['phase'] == 'program-case':
            case = next(row for row in self.case.manifest['runtime_cases'] if step['id'] == 'case-' + row['name'])
            if case.get('stdout_work_root') is True:
                (self.case.work / (case['name'] + '.stdout')).write_bytes(os.fsencode(self.case.work) + b'\n')
        return result

    def test_shared_runner_checks_exact_controller_directory(self):
        case = self.case
        self.assertIs(case.validate(), case.plan)
        self.assertEqual(len(case.plan['commands']), 13)
        self.assertEqual(case.plan['commands'][-1]['argv'], [str(case.work / 'pwd')])
        for row in case.plan['commands']:
            self.assertNotIn('PWD', row['env'])
        with patch.dict(os.environ, {'PWD': '/untrusted-control-env'}), patch(
                'emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=self.fake_command) as run:
            result = case.invoke()
        self.assertEqual(run.call_count, 13)
        self.assertEqual(result['status'], 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW')
        self.assertFalse(result['ordinary_provider_qualified'])

    def test_wrong_nonempty_child_directory_fails_without_retry(self):
        def wrong(argv, log, **kwargs):
            result = self.fake_command(argv, log, **kwargs)
            if Path(log).stem == 'case-case0':
                (self.case.work / 'case0.stdout').write_bytes(b'/different/directory\n')
            return result
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=wrong) as run:
            result = self.case.invoke()
        self.assertEqual(result['status'], 'FAILED')
        self.assertIn('controller work root', result['error'])
        self.assertEqual(run.call_count, 3)

    def test_system_fallback_and_other_environment_refuse_before_execution(self):
        old = copy.deepcopy(self.case.plan)
        for field in ('command', 'env'):
            with self.subTest(field=field):
                if field == 'command':
                    self.case.plan['commands'][-1]['argv'][0] = '/bin/pwd'
                else:
                    self.case.plan['commands'][0]['env']['PWD'] = '/tmp'
                with patch('emberbsd_sdk.bootstrap_pilot.run_logged') as run, self.assertRaises(SDKError):
                    self.case.invoke()
                run.assert_not_called()
                self.case.plan.clear()
                self.case.plan.update(copy.deepcopy(old))

    def test_oracle_field_is_exact_and_cannot_replace_other_profiles(self):
        base = copy.deepcopy(self.case.manifest)
        for value in (False, 'true', 1):
            manifest = copy.deepcopy(base)
            manifest['runtime_cases'][0]['stdout_work_root'] = value
            with self.subTest(value=value), self.assertRaises(SDKError):
                program._native_program_contract(manifest, 'pwd')
        for extra in ({'stdout': 'anything'}, {'exit': 1}):
            manifest = copy.deepcopy(base)
            manifest['runtime_cases'][0].update(extra)
            with self.subTest(extra=extra), self.assertRaises(SDKError):
                program._native_program_contract(manifest, 'pwd')
        with self.assertRaises(SDKError):
            program._literal_cases(base, 10)

    @unittest.skipUnless(os.environ.get('EMBERBSD_TEST_OS_SOURCE'), 'set EMBERBSD_TEST_OS_SOURCE for actual OS inputs')
    def test_actual_os_manifest_selects_unchanged_native_libc_source(self):
        root = Path(os.environ['EMBERBSD_TEST_OS_SOURCE']).resolve(strict=True)
        relative, _, sources, provenance = program._profile({'stage': 'pwd-bootstrap'})
        raw = (root / relative).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), self.actual_digest)
        manifest = json.loads(raw)
        program._native_program_contract(manifest, 'pwd')
        self.assertEqual(sources, ('bin/pwd/pwd.c',))
        self.assertTrue(any(row['args'] == ['-L'] for row in manifest['runtime_cases']))
        self.assertTrue(any(row['args'] == ['-P'] for row in manifest['runtime_cases']))
        self.assertTrue(any(row['exit'] != 0 for row in manifest['runtime_cases']))
        for member in (*sources, *manifest['headers'], *provenance):
            self.assertTrue((root / member).is_file(), member)
            self.assertFalse((root / member).is_symlink(), member)


if __name__ == '__main__':
    unittest.main()
