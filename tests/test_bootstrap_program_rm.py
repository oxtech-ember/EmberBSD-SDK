# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Canonical rm source and filesystem controls. Every command child is synthetic."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_bootstrap_program as fixtures
from emberbsd_sdk import bootstrap_program as program, bootstrap_program_fs as fs
from emberbsd_sdk.bootstrap_program_data import decode_data
from emberbsd_sdk.bootstrap_program_history import _commands
from emberbsd_sdk.errors import SDKError


@unittest.skipUnless(os.environ.get('EMBERBSD_TEST_RM_SOURCE'), 'set EMBERBSD_TEST_RM_SOURCE')
class RmProfileTests(unittest.TestCase):
    def setUp(self):
        self.source = Path(os.environ['EMBERBSD_TEST_RM_SOURCE'])
        self.manifest = json.loads((self.source / 'tools/rm/bootstrap.json').read_text())

    def selected_profile(self):
        self.assertIn('rm-bootstrap', program.PROGRAM_STAGES)
        return program._profile(dict(stage='rm-bootstrap'))

    def fixture(self):
        relative, digest, sources, provenance = self.selected_profile()
        case = fixtures.ProgramTests(methodName='runTest')
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.source = case.base / 'rm-source'
        for member in {relative, *sources, *provenance, *self.manifest['headers']}:
            path = case.source / member
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((self.source / member).read_bytes())
        case.manifest = copy.deepcopy(self.manifest)
        case.inputs['root'] = str(case.source)
        case.plan['stage'] = 'rm-bootstrap'
        case.plan['program']['manifest'] = dict(path=str(case.source / relative), sha256=digest)
        case.refresh_source()
        case.plan['commands'] = program.program_steps(case.plan, case.descriptor, case.commands, case.flags)
        return case

    def test_exact_owner_profile_and_eight_source_members(self):
        relative, digest, sources, provenance = self.selected_profile()
        self.assertEqual(relative, 'tools/rm/bootstrap.json')
        self.assertEqual(hashlib.sha256((self.source / relative).read_bytes()).hexdigest(), digest)
        self.assertEqual(sources, ('bin/rm/rm.c',))
        self.assertIn('tools/rm/test-source.py', provenance)
        members = {relative, *sources, *provenance, *self.manifest['headers']}
        self.assertEqual(len(members), 8)
        self.assertTrue(all((self.source / name).is_file() for name in members))
        self.assertNotIn('NOCOMPATLIB', (self.source / 'tools/rm/Makefile').read_text())
        program._native_program_contract(self.manifest, 'rm')

    def test_fixed_plan_keeps_existing_executor_and_separate_runtime_fixture(self):
        case = self.fixture()
        with patch('subprocess.Popen', side_effect=AssertionError('no child')):
            self.assertIs(case.validate(), case.plan)
        steps = case.plan['commands']
        self.assertEqual(len(steps), 28)
        self.assertEqual([row['phase'] for row in steps[:2]], ['compile', 'link'])
        self.assertEqual(len(steps[2:-1]), 25)
        for step, row in zip(steps[2:-1], self.manifest['runtime_cases']):
            self.assertEqual(step['id'], 'case-' + row['name'])
            self.assertEqual(step['cwd'], str(case.work / 'cases' / row['name']))
            self.assertEqual(step['argv'][:len(case.commands['sh'])], list(case.commands['sh']))
        self.assertEqual(steps[-1]['cwd'], str(case.work / 'cases/runtime-observation'))
        self.assertNotIn('DYLD_PRINT_LIBRARIES', steps[-1]['env'])
        self.assertIn('export DYLD_PRINT_LIBRARIES=1; exec ', steps[-1]['argv'][-1])
        self.assertEqual(len([name for name in program.program_outputs(case.plan)
                              if name.endswith('.filesystem.json')]), 26)

    def test_overwrite_boundary_inputs_have_no_random_peer_oracle(self):
        self.selected_profile()
        cases = [row for row in self.manifest['runtime_cases'] if '-P' in row['args']]
        self.assertEqual(len(cases), 4)
        self.assertEqual([len(decode_data(row['filesystem']['fixtures']['victim']['data']))
                          for row in cases], [0, 1, 8192, 8193])
        for row in cases:
            self.assertEqual(set(row['filesystem']['fixtures']), {'victim'})
            self.assertEqual(row['filesystem']['expect'], {'victim': dict(type='absent')})
        altered = copy.deepcopy(self.manifest)
        row = next(row for row in altered['runtime_cases'] if row['name'] == 'overwrite-one')
        row['filesystem']['expect']['victim'] = dict(type='file', mode='0600', data='x', random_not_equal=True)
        with self.assertRaises(SDKError):
            program._native_program_contract(altered, 'rm')

    def test_changed_owner_case_refuses_before_any_child(self):
        case = self.fixture()
        path = case.source / 'tools/rm/bootstrap.json'
        changed = copy.deepcopy(self.manifest)
        changed['runtime_cases'][0]['args'] = ['-f', 'different']
        path.write_text(json.dumps(changed))
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged') as child:
            with self.assertRaises(SDKError):
                case.invoke()
        child.assert_not_called()
        self.assertFalse(case.work.exists())

    def test_successful_noop_stops_before_the_next_case(self):
        case = self.fixture()
        case.validate()
        seen = []
        def child(argv, log, **kwargs):
            seen.append(Path(log).stem)
            return case.fake_command(argv, log, **kwargs)
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=child):
            result = case.invoke()
        self.assertEqual(result['status'], 'FAILED')
        self.assertIn('filesystem actual state', result['error'])
        self.assertEqual(seen, ['compile-rm', 'link-rm', 'case-regular'])
        self.assertFalse((case.work / 'cases/multiple').exists())

    def test_missing_or_extra_source_member_is_not_accepted(self):
        case = self.fixture()
        extra = case.source / 'tools/rm/extra.c'
        extra.write_text('No private implementation is permitted.\n')
        case.refresh_source()
        with self.assertRaisesRegex(SDKError, 'exact complete source roster'):
            case.validate()

    def test_history_requires_case_and_runtime_filesystem_reports(self):
        self.selected_profile()
        case = self.manifest['runtime_cases'][0]
        manifest = dict(name='rm', invocations=['rm'], runtime_cases=[case])
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            outputs = {}
            def retained(name, raw):
                path = root / name
                path.write_bytes(raw)
                digest = hashlib.sha256(raw).hexdigest()
                outputs[name] = digest
                return dict(path=str(path), sha256=digest)
            image = retained('rm', b'synthetic image; never executed')
            steps = [dict(id='link-rm', phase='link', argv=['selected-cc','-o',image['path']], expected_exit=0),
                     dict(id='case-regular', phase='program-case', expected_exit=0),
                     dict(id='runtime-observation', phase='run', expected_exit=0)]
            rows = []
            for step in steps:
                log = retained(step['id'] + '.log', b'synthetic drained log')
                del outputs[step['id'] + '.log']
                row = dict(step, actual_exit=0, log=log)
                if step['phase'] == 'link':
                    row['output'] = image
                else:
                    key = fs.step_key(step)
                    directory = fs.prepare(work, case, key=key)
                    # This is a synthetic effect for the report verifier, not native rm.
                    (directory / 'victim').unlink()
                    row['filesystem'] = fs.observe(work, case, key=key)
                    outputs[key + '.filesystem.json'] = row['filesystem']['sha256']
                    if step['phase'] == 'program-case':
                        row['stdout'] = retained('regular.stdout', b'')
                        row['stderr'] = retained('regular.stderr', b'')
                rows.append(row)
            plan = dict(stage='rm-bootstrap', work_root=work, commands=steps)
            result = dict(commands=rows, outputs=outputs)
            with patch.object(program, 'program_outputs', return_value=list(outputs)), \
                 patch.object(program, 'program_sources', return_value={}):
                self.assertEqual(_commands(plan, result, manifest), image)
                for index in (1, 2):
                    changed = copy.deepcopy(result)
                    del changed['commands'][index]['filesystem']
                    with self.assertRaises(SDKError):
                        _commands(plan, changed, manifest)
                changed = copy.deepcopy(result)
                changed['commands'][2]['filesystem'] = rows[1]['filesystem']
                with self.assertRaises(SDKError):
                    _commands(plan, changed, manifest)


if __name__ == '__main__':
    unittest.main()
