# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Synthetic full-plan parallel routing. Do not execute native build tools."""

import copy
import fcntl
import hashlib
from pathlib import Path
import shlex
import sys
import unittest
from unittest.mock import patch

import test_bootstrap_recipe
from emberbsd_sdk import bootstrap_producer as producer, bootstrap_recipe as recipe
from emberbsd_sdk.errors import ProcessOwnershipError, SDKError
from emberbsd_sdk.inventory import sha256_file, snapshot_tree
from emberbsd_sdk.process import ContextLocks


class ParallelRecipeTests(unittest.TestCase):
    def setUp(self):
        self.case = test_bootstrap_recipe.RecipeTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        case = self.case
        # The real first-native seed has no Python provider. The orchestration
        # service must not acquire one through this generic native fixture.
        case.descriptor['tools'].pop('python')
        case.commands = {key: value for key, value in case.commands.items() if key != 'python'}
        case.plan['required_roles'].remove('python')
        case.refresh()
        self.original_plan = copy.deepcopy(case.plan)
        self.original_conf = Path(case.plan['metadata']['makeconf']['path']).read_bytes()
        profile = Path(case.plan['metadata']['profile']['path'])
        self.original_profile = profile.read_bytes()
        profile.write_bytes(b'# synthetic recognized parallel source-owner profile\n')
        self.parallel_sha = sha256_file(str(profile))
        guard = patch.object(producer, 'PARALLEL_PROFILE_SHA256', self.parallel_sha)
        guard.start()
        self.addCleanup(guard.stop)
        self.host = patch.object(recipe.os, 'cpu_count', return_value=18)
        self.host.start()
        self.addCleanup(self.host.stop)
        metadata = case.plan['metadata']
        metadata['profile']['sha256'] = self.parallel_sha
        metadata['export']['inventory'] = case.f.document(snapshot_tree(str(case.fixture.export)))
        manifest = copy.deepcopy(case.fixture.manifest)
        manifest[producer.PROFILE_RELATIVE]['sha256'] = self.parallel_sha
        proof = copy.deepcopy(case.fixture.proof)
        proof.update(ports_manifest=case.f.document(manifest), inventory=metadata['export']['inventory'])
        metadata['export']['source_proof'] = case.f.document(proof)
        case.plan['ports_export'] = dict(source=case.descriptor['sources']['ports'], **metadata['export'])
        launcher = str(Path(recipe.__file__).resolve().parents[2] / 'scripts/link-launcher.py')
        case.plan['link_control'] = {
            'python': dict(path=sys.executable, sha256=sha256_file(sys.executable)),
            'launcher': dict(path=launcher, sha256=sha256_file(launcher))}
        case.plan['resources']['cpu_jobs'] = 18
        case.refresh()

    def test_parallel_prefix_budget_and_complete_tuples_are_source_owned(self):
        case = self.case
        with patch('subprocess.Popen', side_effect=AssertionError('no native process')):
            self.assertEqual(case.validate(), case.plan)
        data = producer.metadata_assignments(case.plan, case.descriptor, case.commands,
                                             case.descriptor['abi_flags'])
        control, resources = case.plan['link_control'], case.plan['resources']
        self.assertEqual(shlex.split(data['EMBERBSD_GCC16_LINKER_LAUNCHER']),
            [control['python']['path'], control['launcher']['path'], '--lock-root', resources['lock_root'],
             '--context', resources['heavy_link_context'], '--'])
        self.assertEqual(data['EMBERBSD_DARWIN_CPU_BUDGET'], '18')
        self.assertEqual(data['EMBERBSD_DARWIN_JOBS'], '18')
        self.assertNotIn('python', case.descriptor['tools'])
        for step in case.plan['commands']:
            self.assertIn('-j18', step['argv'])
            self.assertEqual(step['argv'][:len(case.commands[step['role']])], list(case.commands[step['role']]))
            self.assertEqual(step['env']['GCC_HOST_LINKER_LAUNCHER'],
                             data['EMBERBSD_GCC16_LINKER_LAUNCHER'])
        self.assertEqual(case.plan['commands'][3]['phase'], 'compare')
        expected = producer.metadata_assignments(self.original_plan, case.descriptor, case.commands,
                                                 case.descriptor['abi_flags'])
        for name, value in data.items():
            if name.startswith('EMBERBSD_GCC16_SEED_') or name in ('LDFLAGS', 'OSX_SDK_PATH'):
                self.assertEqual(value, expected[name])

    def test_control_plane_alias_foreign_script_changed_bytes_and_extra_flags_refuse(self):
        case = self.case
        original = copy.deepcopy(case.plan['link_control'])
        for change in ('python-alias', 'foreign-script', 'python-bytes', 'script-bytes', 'extra-args', 'null'):
            with self.subTest(change=change):
                case.plan['link_control'] = copy.deepcopy(original)
                value = case.plan['link_control']
                if change == 'python-alias':
                    alias = case.base / 'alias-python'
                    alias.symlink_to(sys.executable)
                    value['python']['path'] = str(alias)
                elif change == 'foreign-script':
                    value['launcher']['path'] = str(case.base / 'foreign/scripts/link-launcher.py')
                elif change == 'python-bytes':
                    value['python']['sha256'] = '0' * 64
                elif change == 'script-bytes':
                    value['launcher']['sha256'] = '0' * 64
                elif change == 'extra-args':
                    value['args'] = ['--arbitrary-policy']
                else:
                    case.plan['link_control'] = None
                with self.assertRaises(SDKError):
                    case.validate()
        case.plan['link_control'] = original

    def test_missing_control_or_old_profile_cannot_admit_parallel_build(self):
        case = self.case
        control = case.plan.pop('link_control')
        case.refresh()
        with self.assertRaises(SDKError):
            case.validate()
        case.plan['link_control'] = control
        case.plan['metadata']['profile']['sha256'] = producer.PROFILE_SHA256
        case.refresh()
        with self.assertRaisesRegex(SDKError, 'recognized parallel full-plan profile'):
            case.validate()

    def test_compare_cannot_drop_or_override_the_launcher_environment(self):
        case = self.case
        for change in ('drop', 'override'):
            with self.subTest(change=change):
                original = copy.deepcopy(case.plan['commands'][3]['env'])
                if change == 'drop':
                    case.plan['commands'][3]['env'].pop('GCC_HOST_LINKER_LAUNCHER')
                else:
                    case.plan['commands'][3]['env']['GCC_HOST_LINKER_LAUNCHER'] = '/foreign/launcher'
                with self.assertRaisesRegex(SDKError, 'complete source-owned route'):
                    case.validate()
                case.plan['commands'][3]['env'] = original

    def test_new_profile_metadata_uses_one_job_and_no_fake_control_provider(self):
        case = self.case
        case.plan.pop('link_control')
        case.plan['resources']['cpu_jobs'] = 1
        case.refresh()
        data = producer.metadata_assignments(case.plan, case.descriptor, case.commands,
                                             case.descriptor['abi_flags'])
        self.assertEqual(data['EMBERBSD_DARWIN_CPU_BUDGET'], '1')
        self.assertEqual(data['EMBERBSD_GCC16_LINKER_LAUNCHER'], '')
        self.assertNotIn('GCC_HOST_LINKER_LAUNCHER', case.plan['commands'][0]['env'])
        self.assertEqual(case.validate(), case.plan)

    def test_jobs_cannot_exceed_actual_host_or_bypass_integer_and_link_limits(self):
        case = self.case
        for key, value in [('cpu_jobs', 19), ('cpu_jobs', True), ('cpu_jobs', 0), ('link_jobs', 2)]:
            with self.subTest(key=key, value=value):
                original = case.plan['resources'][key]
                case.plan['resources'][key] = value
                with self.assertRaises(SDKError):
                    case.validate()
                case.plan['resources'][key] = original
        with patch.object(recipe.os, 'cpu_count', return_value=None):
            with self.assertRaisesRegex(SDKError, 'CPU budget is unavailable'):
                case.validate()

    def test_outer_controller_does_not_deadlock_nested_link_fence(self):
        case, observed = self.case, []
        def inspect(argv, log, **kwargs):
            controls = case.plan['link_control']
            for context in (str(case.work), str(case.f.bundle), controls['python']['path'],
                            controls['launcher']['path']):
                lock = case.base / ('.prebuild-sdk-lock-' + hashlib.sha256(context.encode()).hexdigest())
                with lock.open('rb') as handle:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Model a nested launcher lock. A parent heavy EX would fail here.
            with ContextLocks([case.plan['resources']['heavy_link_context']], str(case.base)):
                observed.append(argv)
                return case.fake_command(argv, log, **kwargs)
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=inspect):
            result = case.invoke()
        self.assertEqual(result['status'], 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW')
        self.assertEqual(len(observed), 5)
        self.assertTrue(result['all_commands_drained'] and result['all_leases_closed'])
        self.assertFalse(result['ordinary_provider_qualified'])

    def test_unproven_parallel_drain_retains_input_control_and_work_leases(self):
        case, observed = self.case, []
        class EndFixture(BaseException):
            pass
        def fail(argv, log, **kwargs):
            Path(log).write_bytes(b'synthetic unproven nested build drain\n')
            raise ProcessOwnershipError(1234567, 'synthetic parallel drain', log=log)
        def hold():
            for context in (str(case.work), str(case.f.bundle), case.plan['link_control']['python']['path']):
                lock = case.base / ('.prebuild-sdk-lock-' + hashlib.sha256(context.encode()).hexdigest())
                with lock.open('rb') as handle:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            observed.append('fences held; no caller heavy lock')
            raise EndFixture()
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=fail), \
                patch('emberbsd_sdk.bootstrap_pilot._hold', side_effect=hold):
            case.invoke()
        self.assertEqual(observed, ['fences held; no caller heavy lock'])

    def test_historical_graduation_replays_pins_without_retired_control_or_host_reads(self):
        case = self.case
        case.plan['link_control']['python']['path'] = str(case.base / 'retired/python')
        case.plan['link_control']['launcher']['path'] = str(case.base / 'retired/sdk/scripts/link-launcher.py')
        case.refresh()
        with patch.object(recipe, '_file', side_effect=AssertionError('no retired control read')), \
                patch.object(recipe.os, 'cpu_count', side_effect=AssertionError('no current host CPU query')):
            self.assertEqual(case.validate(inspect_inputs=False), case.plan)

    def test_legacy_makeconf_remains_byte_identical(self):
        case = self.case
        Path(case.plan['metadata']['profile']['path']).write_bytes(self.original_profile)
        case.plan = self.original_plan
        case.refresh()
        self.assertEqual(Path(case.plan['metadata']['makeconf']['path']).read_bytes(), self.original_conf)
        self.assertNotIn(b'CPU_BUDGET', self.original_conf)
        self.assertNotIn(b'LINKER_LAUNCHER', self.original_conf)
        self.assertEqual(case.validate(), case.plan)


if __name__ == '__main__':
    unittest.main()
