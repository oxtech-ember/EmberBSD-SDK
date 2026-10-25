# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Shared sort/efun source integration, without native compiler children."""

import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
import test_bootstrap_program_cut as fixtures
import test_bootstrap_program as runner_fixtures
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk import bootstrap_program as program
from emberbsd_sdk import bootstrap_compat as compat


class SortProgramTests(unittest.TestCase):
    def test_sort_uses_complete_sources_and_shared_efun(self):
        self.assertIn('sort-bootstrap', program.PROGRAM_STAGES)
        relative, _, sources, provenance = program._profile({'stage': 'sort-bootstrap'})
        self.assertEqual(relative, 'tools/sort/bootstrap.json')
        self.assertEqual(sources, tuple('usr.bin/sort/' + name + '.c' for name in
            ('append', 'fields', 'files', 'fsort', 'init', 'msort', 'sort', 'tmp', 'radix_sort')) + compat.SOURCES)
        self.assertTrue(set(compat.PROVENANCE) <= set(provenance))

    def fixture(self):
        fixture = fixtures.CutProbeTests(methodName='runTest')
        fixture.program_name = 'sort'
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def test_sort_observes_probes_before_thirteen_complete_compiles(self):
        fixture = self.fixture()
        case = fixture.case
        self.assertIs(case.validate(), case.plan)
        self.assertEqual(len(case.plan['commands']), 59)
        compiles = [row for row in case.plan['commands'] if row['id'].startswith('compile-')]
        self.assertEqual(len(compiles), 13)
        self.assertEqual([row['id'] for row in compiles[-4:]],
                         ['compile-efun', 'compile-reallocarr', 'compile-strtoi', 'compile-strtou'])
        for row in compiles:
            self.assertIn(str(case.work / 'nbtool_config.h'), row['argv'])
            self.assertEqual(row['env']['TMPDIR'], str(case.work))
            self.assertEqual(row['env']['LC_ALL'], 'C')
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=fixture.fake):
            result = case.invoke()
        self.assertEqual(result['status'], 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW')
        self.assertFalse(result['ordinary_provider_qualified'])
        self.assertIn('sort', result['outputs'])
        self.assertIn('#define HAVE_REALLOCARR 0', (case.work / 'nbtool_config.h').read_text())

    def test_missing_shared_probe_refuses_before_child(self):
        fixture = self.fixture()
        (fixture.case.source / 'tools/compat/probes/decl-strtoi.c').unlink()
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged') as run, self.assertRaises(SDKError):
            fixture.case.invoke()
        run.assert_not_called()

    def test_changed_configuration_stops_sort_compile_sequence(self):
        fixture = self.fixture()
        case = fixture.case
        def tamper(argv, log, **kwargs):
            returned = fixture.fake(argv, log, **kwargs)
            if Path(log).stem == 'compile-append':
                (case.work / 'nbtool_config.h').write_text('#define FORGED 1\n')
            return returned
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=tamper):
            result = case.invoke()
        self.assertEqual(result['status'], 'FAILED')
        self.assertIn('configuration changed', result['error'])
        self.assertFalse((case.work / 'fields.o').exists())

    def test_owner_policy_change_refuses_under_the_actual_pin(self):
        fixture = self.fixture()
        case = fixture.case
        path = Path(case.plan['program']['manifest']['path'])
        case.manifest['runtime_cases'][0]['args'] = ['-o', '/tmp/forbidden', 'input.txt']
        path.write_text(json.dumps(case.manifest))
        case.refresh_source()
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged') as run, self.assertRaises(SDKError):
            case.invoke()
        run.assert_not_called()

    @unittest.skipUnless(os.environ.get('EMBERBSD_TEST_OS_SOURCE'), 'set EMBERBSD_TEST_OS_SOURCE')
    def test_real_owner_roster_matches_the_common_sources_and_probes(self):
        root = Path(os.environ['EMBERBSD_TEST_OS_SOURCE'])
        relative, digest, sources, provenance = program._profile({'stage': 'sort-bootstrap'})
        raw = (root / relative).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), digest)
        manifest = json.loads(raw)
        program._native_program_contract(manifest, 'sort')
        self.assertEqual(manifest['sources'][-4:], list(compat.SOURCES))
        self.assertEqual(manifest['compat'], dict(path=compat.MANIFEST_RELATIVE, sha256=compat.MANIFEST_SHA256))
        self.assertEqual(len(manifest['runtime_cases']), 36)
        members = {relative, *sources, *manifest['headers'], *provenance}
        self.assertEqual(len(members), 35)
        for member in members:
            self.assertTrue((root / member).is_file(), member)
            self.assertFalse((root / member).is_symlink(), member)
        self.assertTrue(any(isinstance(value, list) for value in manifest['files'].values()))
        # Use the complete owner manifest, without synthetic semantic replacements.
        runner = runner_fixtures.ProgramTests(methodName='runTest')
        runner.setUp()
        self.addCleanup(runner.doCleanups)
        plan = dict(runner.plan, stage='sort-bootstrap', program=dict(
            source=dict(root=str(root)), manifest=dict(path=str(root / relative), sha256=digest)))
        with patch('subprocess.Popen', side_effect=AssertionError('source check cannot start a child')):
            steps = program.program_steps(plan, runner.descriptor, runner.commands, runner.flags)
        self.assertEqual(len(steps), 59)
        self.assertEqual([row['id'] for row in steps if row['phase'] == 'program-case'],
                         ['case-' + case['name'] for case in manifest['runtime_cases']])
        self.assertTrue(all('\0' not in word for row in steps for word in row['argv']))



if __name__ == '__main__':
    unittest.main()
