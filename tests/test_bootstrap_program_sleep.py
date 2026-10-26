# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Canonical sleep contract with synthetic children, never native sleep."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

import test_bootstrap_program as fixtures
from emberbsd_sdk import bootstrap_program as program
from emberbsd_sdk.bootstrap_program_history import _commands
from emberbsd_sdk.errors import SDKError


class SleepProgramTests(unittest.TestCase):
    def setUp(self):
        self.assertIn('sleep-bootstrap', program.PROGRAM_STAGES)
        selected = os.environ.get('EMBERBSD_SLEEP_SOURCE')
        if not selected:
            self.skipTest('select the canonical sleep owner source')
        self.case = fixtures.ProgramTests(methodName='runTest')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        c = self.case
        c.plan['stage'] = 'sleep-bootstrap'
        relative, digest, sources, provenance = program._profile(c.plan)
        c.manifest = json.loads((Path(selected)/relative).read_text())
        shutil.rmtree(c.source)
        for name in {relative, *sources, *provenance, *c.manifest['headers']}:
            target = c.source/name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((Path(selected)/name).read_bytes())
        self.assertEqual(hashlib.sha256((c.source/relative).read_bytes()).hexdigest(), digest)
        c.plan['program']['manifest'] = dict(path=str(c.source/relative),sha256=digest)
        c.refresh_source()
        c.plan['commands'] = program.program_steps(c.plan,c.descriptor,c.commands,c.flags)

    def run_fixture(self, duration=500000000, failure=None):
        c = self.case
        timeouts = []
        def child(argv,log,**kwargs):
            if Path(log).stem == 'case-fraction-elapsed':
                timeouts.append(kwargs['timeout_seconds'])
                if failure:
                    Path(log).write_bytes(b'owned command failed and drained\n')
                    raise failure
            return c.fake_command(argv,log,**kwargs)
        with patch('subprocess.Popen',side_effect=AssertionError('no native child')), \
             patch('emberbsd_sdk.bootstrap_pilot.run_logged',side_effect=child), \
             patch('emberbsd_sdk.bootstrap_pilot.time.monotonic_ns',side_effect=[1000000000,1000000000+duration]):
            result = c.invoke()
        return result,timeouts

    def test_exact_owner_plan_and_system_math_library(self):
        c = self.case
        self.assertIs(c.validate(),c.plan)
        self.assertEqual(len(c.plan['commands']),24)
        self.assertEqual(len(c.manifest['runtime_cases']),21)
        self.assertEqual(c.plan['commands'][0]['argv'][-3],str(c.source/'bin/sleep/sleep.c'))
        self.assertEqual(c.plan['commands'][1]['argv'].count('-lm'),1)
        self.assertEqual(c.plan['commands'][-1]['argv'],[str(c.work/'sleep'),'0'])
        self.assertEqual(sum('elapsed' in c for c in c.manifest['runtime_cases']),1)

    def test_owned_call_elapsed_record_and_retained_validation(self):
        result,timeouts=self.run_fixture()
        self.assertEqual(result['status'],'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW',result.get('error'))
        self.assertEqual(timeouts,[5.0])
        timed=[row for row in result['commands'] if 'elapsed' in row]
        self.assertEqual(len(timed),1)
        self.assertEqual(timed[0]['elapsed'],dict(clock='monotonic_ns',scope='owned-command-call',
            start_ns=1000000000,end_ns=1500000000,elapsed_ns=500000000))
        self.assertEqual(_commands(self.case.plan,result,self.case.manifest)['path'],str(self.case.work/'sleep'))
        for alteration in ('missing','short','wrong-delta','bool','scope','extra','backwards'):
            changed=copy.deepcopy(result)
            row=next(r for r in changed['commands'] if r['id']=='case-fraction-elapsed')
            if alteration=='missing':del row['elapsed']
            elif alteration=='short':row['elapsed'].update(end_ns=1000000001,elapsed_ns=1)
            elif alteration=='wrong-delta':row['elapsed']['end_ns']+=1
            elif alteration=='bool':row['elapsed']['start_ns']=True
            elif alteration=='scope':row['elapsed']['scope']='nanosleep'
            elif alteration=='backwards':row['elapsed'].update(end_ns=1,elapsed_ns=-999999999)
            else:row['elapsed']['claimed_precise_sleep']=True
            with self.subTest(alteration=alteration),self.assertRaises(SDKError):
                _commands(self.case.plan,changed,self.case.manifest)

    def test_too_short_or_long_observation_fails_after_drain(self):
        result,_=self.run_fixture(duration=499999999)
        self.assertEqual(result['status'],'FAILED')
        self.assertIn('elapsed',result['error'])
        self.assertTrue(result['all_commands_drained'])
        self.assertTrue(result['all_leases_closed'])

    def test_too_long_observation_fails_even_after_successful_child(self):
        result,_=self.run_fixture(duration=5000000001)
        self.assertEqual(result['status'],'FAILED')
        self.assertEqual(result['commands'][-1]['actual_exit'],0)

    def test_timeout_keeps_elapsed_evidence_and_original_failure(self):
        result,_=self.run_fixture(duration=6000000000,failure=SDKError('owned polling timeout'))
        self.assertEqual(result['status'],'FAILED')
        self.assertIn('owned polling timeout',result['error'])
        self.assertEqual(result['commands'][-1]['elapsed']['elapsed_ns'],6000000000)
        self.assertTrue(result['all_commands_drained'])

    def test_missing_local_header_refuses_before_any_child(self):
        (self.case.source/'bin/sleep/sleep_darwin.h').unlink()
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged') as run,self.assertRaises(SDKError):
            self.case.invoke()
        run.assert_not_called()

    def test_manifest_elapsed_repin_refuses(self):
        path=Path(self.case.plan['program']['manifest']['path'])
        value=json.loads(path.read_text())
        value['runtime_cases'][7]['elapsed']['min_ns']=0
        path.write_text(json.dumps(value))
        self.case.plan['program']['manifest']['sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaises(SDKError):
            program.program_manifest(self.case.plan)

    def test_remaining_epoch_budget_still_caps_timed_polling(self):
        self.case.plan['resources']['runtime_seconds']=1
        result,timeouts=self.run_fixture()
        self.assertEqual(result['status'],'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW')
        self.assertEqual(len(timeouts),1)
        self.assertGreater(timeouts[0],0)
        self.assertLessEqual(timeouts[0],1)

    def test_unproven_drain_never_finalizes_elapsed(self):
        from emberbsd_sdk.errors import ProcessOwnershipError
        observed=[]
        class FiniteHoldControl(BaseException):
            pass
        def inspect_hold():
            receipt=json.loads((self.case.work/'result.json').read_text())
            self.assertEqual(receipt['status'],'HOLD_UNPROVEN_DRAIN')
            self.assertFalse(receipt['all_commands_drained'])
            self.assertNotIn('elapsed',receipt['commands'][-1])
            observed.append('unfinalized')
            raise FiniteHoldControl()
        with patch('emberbsd_sdk.bootstrap_pilot._hold',side_effect=inspect_hold):
            self.run_fixture(failure=ProcessOwnershipError(1234567,'synthetic unproven drain'))
        self.assertEqual(observed,['unfinalized'])


class ElapsedDataTests(unittest.TestCase):
    def test_optional_literal_case_accepts_only_bounded_integer_interval(self):
        row=dict(name='finite',invocation='x',args=[],stdin='',exit=0,stdout='',stderr='',
                 elapsed=dict(min_ns=500000000,max_ns=5000000000))
        manifest=dict(invocations=['x'],runtime_cases=[row])
        program._literal_cases(manifest,1)
        for limits in (None,{},dict(min_ns=True,max_ns=5),dict(min_ns=-1,max_ns=5),
                       dict(min_ns=6,max_ns=5),dict(min_ns=0,max_ns=0),
                       dict(min_ns=0,max_ns=120000000001),dict(min_ns=0,max_ns=1.0),
                       dict(min_ns=float('nan'),max_ns=10),
                       dict(min_ns=0,max_ns=10,hook='anything')):
            row['elapsed']=limits
            with self.subTest(limits=limits),self.assertRaises(SDKError):
                program._literal_cases(manifest,1)

    def test_old_untimed_pilot_has_no_new_records_or_clock_reads(self):
        c=fixtures.ProgramTests(methodName='runTest');c.setUp();self.addCleanup(c.doCleanups)
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged',side_effect=c.fake_command), \
             patch('emberbsd_sdk.bootstrap_pilot.time.monotonic_ns',side_effect=AssertionError('untimed')):
            result=c.invoke()
        self.assertEqual(result['status'],'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW')
        self.assertTrue(all('elapsed' not in row for row in result['commands']))

    def test_elapsed_record_on_untimed_command_refuses(self):
        from emberbsd_sdk.bootstrap_program_timing import observation, verify
        with self.assertRaises(SDKError):
            verify(None,dict(elapsed=observation(1,2)))
        with self.assertRaises(SDKError):
            verify(dict(name='untimed'),dict(elapsed=observation(1,2)))


if __name__=='__main__':
    unittest.main()
