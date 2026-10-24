# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Finite runner glue controls. Selection, transforms, and verifiers are synthetic.

The relocation module's separate tests cover its input and Mach-O contracts.
These controls check the shared controller's ordering, evidence, and fences.
"""

import fcntl
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from emberbsd_sdk import bootstrap_pilot, bootstrap_producer, bootstrap_relocation as relocation
from emberbsd_sdk.errors import CommandError, ProcessOwnershipError, SDKError


class RelocationRunnerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.work = self.base / 'work'
        self.originals = self.base / 'originals'
        self.originals.mkdir()
        self.serial = 0
        self.bundle = self.base / 'bundle'
        self.bundle.mkdir()
        self.descriptor = dict(bundle_root=str(self.bundle), host=dict(os='Darwin', cpu='arm64'),
            host_abi={kind: [dict(root=str(self.bundle))] for kind in ('headers', 'libraries')})
        self.selected_commands = dict(install_name_tool=(str(self.bundle / 'llvm-install-name-tool'),))
        self.outputs = tuple('run-' + run + '/bin/u' + str(index) for run in ('a', 'b') for index in range(21))
        original_refs = {}
        for index in range(21):
            path = self.originals / ('u' + str(index))
            path.write_bytes(('original ' + str(index)).encode())
            original_refs[path.name] = self.reference(path)
        self.plan = dict(format=bootstrap_producer.PRODUCER_FORMAT, version=1, scope='gcc16-bootstrap',
            stage='utility-relocation', work_root=str(self.work), required_roles=['install_name_tool'],
            resources=dict(cpu_jobs=1, link_jobs=1, runtime_seconds=120, lock_root=str(self.base),
                           heavy_link_context=str(self.base / 'heavy')),
            relocation=dict(profile=self.document(dict(fixture='fixed profile')),
                original_prefix=str(self.originals), final_prefix=str(self.base / 'future-input'),
                originals=self.document(original_refs)))
        self.plan['commands'] = [dict(id='transform-' + str(index), phase='transform', expected_exit=0,
            cwd=str(self.work), env=dict(PATH='', LC_ALL='C', TMPDIR=str(self.work)),
            argv=[*self.selected_commands['install_name_tool'], '--synthetic-output', str(self.work / relative)])
            for index, relative in enumerate(self.outputs)]
        self.prepared = False
        self.verified = []

    def reference(self, path):
        return dict(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest())

    def document(self, value):
        path = self.base / ('document-' + str(self.serial) + '.json')
        self.serial += 1
        path.write_text(json.dumps(value), encoding='utf-8')
        return self.reference(path)

    def assert_fenced(self):
        for context in (self.work, self.originals, self.originals / 'u0'):
            name = '.prebuild-sdk-lock-' + hashlib.sha256(str(context).encode()).hexdigest()
            with open(self.base / name, 'rb') as handle:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def prepare(self, plan):
        self.assertEqual(plan['stage'], self.plan['stage'])
        self.assert_fenced()
        for name in ('run-a/bin', 'run-b/bin'):
            (self.work / name).mkdir(parents=True)
        self.prepared = True

    def fake_command(self, argv, log, **kwargs):
        self.assertTrue(self.prepared)
        Path(argv[-1]).write_bytes(('synthetic transformed ' + Path(argv[-1]).name).encode())
        Path(log).write_bytes(b'actual synthetic merged transform log\n')
        return subprocess.CompletedProcess(argv, 0)

    def verify_step(self, plan, descriptor, commands, step):
        self.assert_fenced()
        self.assertEqual(commands, self.selected_commands)
        self.verified.append(step['id'])
        return self.reference(Path(step['argv'][-1]))

    def verify_outputs(self, plan, descriptor, commands):
        self.assert_fenced()
        self.assertEqual(len(self.verified), 42)
        for index in range(21):
            self.assertEqual((self.work / self.outputs[index]).read_bytes(),
                             (self.work / self.outputs[index + 21]).read_bytes())
        return dict(profile=plan['relocation']['profile'], members=['synthetic deterministic members'])

    def invoke(self, *, load_error=None):
        case = self
        class Selection:
            commands = case.selected_commands
            def as_dict(self):
                return dict(plan=case.plan)
        descriptor = self.document(self.descriptor)
        plan_ref, acceptance = self.document(self.plan), self.document(dict(fixture='accepted only by this test'))
        with patch('emberbsd_sdk.bootstrap_seed.load_bootstrap_seed',
                   side_effect=load_error, return_value=Selection()), \
                patch.multiple(relocation, relocation_read_roots=lambda plan: (str(self.originals),),
                    relocation_work_outputs=lambda plan: self.outputs,
                    prepare_relocation_work=self.prepare, verify_relocation_step=self.verify_step,
                    verify_relocation_outputs=self.verify_outputs):
            return bootstrap_pilot.run_producer(descriptor['path'], descriptor_sha256=descriptor['sha256'],
                accepted_seed_receipt=acceptance, bootstrap_plan=plan_ref, required_roles=['install_name_tool'])

    def test_dispatch_delegates_to_the_source_owner_validator(self):
        with patch.object(relocation, 'validate_relocation_plan', return_value=self.plan) as validate:
            result = bootstrap_producer.validate_producer_plan(self.plan, self.descriptor,
                                                               self.selected_commands, {})
        validate.assert_called_once_with(self.plan, self.descriptor, self.selected_commands, {})
        self.assertIs(result, self.plan)

    def test_all_42_steps_and_final_verification_keep_work_and_original_fences(self):
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=self.fake_command) as run:
            result = self.invoke()
        self.assertEqual(run.call_count, 42)
        self.assertEqual(len(self.verified), 42)
        self.assertEqual(result['stage'], 'utility-relocation')
        self.assertEqual(result['format'], 'emberbsd-native-gcc16-producer-result')
        self.assertEqual(result['status'], 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW')
        self.assertEqual(len(result['outputs']), 42)
        self.assertEqual(result['relocation']['profile'], self.plan['relocation']['profile'])
        self.assertTrue(result['all_commands_drained'] and result['inputs_revalidated_after_drain'])
        self.assertTrue(result['all_leases_closed'])
        self.assertFalse(result['ordinary_provider_qualified'])
        self.assertTrue(all(row['actual_exit'] == 0 and 'output' in row and 'log' in row for row in result['commands']))
        self.assertTrue(all(0 < call.kwargs['timeout_seconds'] <= 120 for call in run.call_args_list))
        self.assertNotIn('all_leases_closed', json.loads((self.work / 'result.json').read_bytes()))
        self.assertFalse((self.work / 'native.c').exists())

    def test_refused_selection_cannot_prepare_work_or_start_a_child(self):
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged') as run:
            with self.assertRaisesRegex(SDKError, 'unaccepted selection'):
                self.invoke(load_error=SDKError('unaccepted selection'))
        run.assert_not_called()
        self.assertFalse(self.prepared)
        self.assertFalse(self.work.exists())

    def test_failed_transform_retains_actual_exit_and_does_not_finalize_or_retry(self):
        def fail(argv, log, **kwargs):
            Path(log).write_bytes(b'actual synthetic transformation failure\n')
            raise CommandError(7, argv, log=log)
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=fail) as run:
            result = self.invoke()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(result['commands'][0]['actual_exit'], 7)
        self.assertNotIn('relocation', result)
        self.assertEqual(self.verified, [])

    def test_failed_output_check_preserves_actual_zero_and_stops(self):
        self.verify_step = lambda *args: (_ for _ in ()).throw(SDKError('bad transformed signature'))
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=self.fake_command) as run:
            result = self.invoke()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(result['commands'][0]['actual_exit'], 0)
        self.assertEqual(result['status'], 'FAILED')
        self.assertIn('bad transformed signature', result['error'])
        self.assertNotIn('relocation', result)

    def test_failed_final_comparison_cannot_report_completion(self):
        def refuse(*args):
            self.assert_fenced()
            raise SDKError('synthetic nondeterministic outputs')
        self.verify_outputs = refuse
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=self.fake_command) as run:
            result = self.invoke()
        self.assertEqual(run.call_count, 42)
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(len(result['outputs']), 42)
        self.assertTrue(all(row['actual_exit'] == 0 for row in result['commands']))
        self.assertIn('nondeterministic outputs', result['error'])
        self.assertNotIn('relocation', result)

    def test_unproven_transform_drain_holds_originals_and_work_without_live_hash(self):
        class EndFiniteControl(BaseException):
            pass
        def fail(argv, log, **kwargs):
            Path(log).write_bytes(b'live synthetic transform log\n')
            raise ProcessOwnershipError(1234567, 'synthetic unproven drain', log=log)
        def held():
            self.assert_fenced()
            result = json.loads((self.work / 'result.json').read_bytes())
            self.assertEqual(result['status'], 'HOLD_UNPROVEN_DRAIN')
            self.assertEqual(result['owned_sid'], 1234567)
            self.assertFalse(result['all_commands_drained'])
            self.assertNotIn('log', result['commands'][-1])
            self.assertIn('unfinalized_log_path', result['commands'][-1])
            # Production HOLD never returns. Stop this synthetic control here.
            raise EndFiniteControl()
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=fail) as run, \
                patch('emberbsd_sdk.bootstrap_pilot._hold', side_effect=held):
            self.invoke()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(self.verified, [])


if __name__ == '__main__':
    unittest.main()
