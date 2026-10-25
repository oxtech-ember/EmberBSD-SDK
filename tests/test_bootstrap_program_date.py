# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Fixed native date tool mode. No C compiler or date child runs here."""

import copy
import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
import test_bootstrap_program_native as fixtures
import test_bootstrap_program as runner_fixtures
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk import bootstrap_program as program


class DateProgramTests(unittest.TestCase):
    def test_date_selects_the_complete_existing_host_source(self):
        self.assertIn('date-bootstrap', program.PROGRAM_STAGES)
        relative, _, sources, provenance = program._profile({'stage': 'date-bootstrap'})
        self.assertEqual(relative, 'tools/date/bootstrap.json')
        self.assertEqual(sources, ('bin/date/date.c',))
        self.assertIn('tools/date/Makefile', provenance)
        self.assertIn('bin/date/Makefile', provenance)

    def fixture(self):
        native = fixtures.NativeProgramTests(methodName='runTest')
        native.setUp()
        self.addCleanup(native.doCleanups)
        native.configure('date')
        return native

    def test_fixed_tool_mode_runs_without_compat_probes_or_private_sources(self):
        native = self.fixture()
        case = native.case
        self.assertIs(case.validate(), case.plan)
        self.assertEqual(len(case.plan['commands']), 44)
        self.assertEqual(program.program_sources(case.plan), {})
        compile_step = case.plan['commands'][0]
        self.assertIn('-DHAVE_NBTOOL_CONFIG_H=1', compile_step['argv'])
        self.assertIn(str(case.source / 'tools/compat/bootstrap'), compile_step['argv'])
        self.assertFalse(any(row['phase'].startswith('compat-') for row in case.plan['commands']))
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=native.fake_command):
            result = case.invoke()
        self.assertEqual(result['status'], 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW')
        self.assertFalse(result['ordinary_provider_qualified'])

    def test_mode_removal_or_changed_header_refuses_before_child(self):
        native = self.fixture()
        case = native.case
        original = copy.deepcopy(case.plan)
        case.plan['commands'][0]['argv'].remove('-DHAVE_NBTOOL_CONFIG_H=1')
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged') as run, self.assertRaises(SDKError):
            case.invoke()
        run.assert_not_called()
        case.plan.clear()
        case.plan.update(original)
        (case.source / 'tools/compat/bootstrap/nbtool_config.h').write_text('#undef HAVE_NBTOOL_CONFIG_H\n')
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged') as run, self.assertRaises(SDKError):
            case.invoke()
        run.assert_not_called()

    def test_changed_owner_semantics_refuses_under_its_pin(self):
        native = self.fixture()
        case = native.case
        path = Path(case.plan['program']['manifest']['path'])
        case.manifest['runtime_cases'][0]['args'] = ['200001010000.00']
        path.write_text(json.dumps(case.manifest))
        case.refresh_source()
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged') as run, self.assertRaises(SDKError):
            case.invoke()
        run.assert_not_called()

    @unittest.skipUnless(os.environ.get('EMBERBSD_TEST_OS_SOURCE'), 'set EMBERBSD_TEST_OS_SOURCE')
    def test_complete_real_owner_contract_builds_every_step(self):
        root = Path(os.environ['EMBERBSD_TEST_OS_SOURCE'])
        relative, digest, sources, provenance = program._profile({'stage': 'date-bootstrap'})
        raw = (root / relative).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), digest)
        manifest = json.loads(raw)
        program._native_program_contract(manifest, 'date')
        members = {relative, *sources, *manifest['headers'], *provenance}
        self.assertEqual(len(members), 9)
        for member in members:
            self.assertTrue((root / member).is_file(), member)
            self.assertFalse((root / member).is_symlink(), member)
        runner = runner_fixtures.ProgramTests(methodName='runTest')
        runner.setUp()
        self.addCleanup(runner.doCleanups)
        plan = dict(runner.plan, stage='date-bootstrap', program=dict(source=dict(root=str(root)),
            manifest=dict(path=str(root / relative), sha256=digest)))
        with patch('subprocess.Popen', side_effect=AssertionError('no native child')):
            steps = program.program_steps(plan, runner.descriptor, runner.commands, runner.flags)
        self.assertEqual(len(steps), 44)
        self.assertEqual([row['id'] for row in steps if row['phase'] == 'program-case'],
                         ['case-' + case['name'] for case in manifest['runtime_cases']])
        self.assertTrue(all('\0' not in word for row in steps for word in row['argv']))
        self.assertIn('-DHAVE_NBTOOL_CONFIG_H=1', steps[0]['argv'])


if __name__ == '__main__':
    unittest.main()
