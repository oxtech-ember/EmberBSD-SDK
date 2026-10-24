# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Finite source and refusal controls. No native tool or compiler executes."""

import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_bootstrap_seed import SeedFixture
from emberbsd_sdk import bootstrap_binding, bootstrap_pilot
from emberbsd_sdk.bootstrap_seed import _dynamic_closure, load_bootstrap_seed
from emberbsd_sdk.errors import CommandError, ProcessOwnershipError, SDKError
from emberbsd_sdk.inventory import snapshot_tree


class PilotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.seed = SeedFixture(self.base)
        self.work = self.base / 'pilot-work'
        self.plan = dict(format=bootstrap_pilot.PILOT_FORMAT, version=1, scope='gcc16-bootstrap',
                         purpose='native-c-and-cxx-pilot', host=self.seed.descriptor['host'],
                         sources=self.seed.descriptor['sources'], work_root=str(self.work),
                         required_roles=['cc', 'cxx', 'cpp', *self.seed.descriptor['driver_components']],
                         resources=dict(cpu_jobs=1, link_jobs=1, runtime_seconds=120,
                                        lock_root=str(self.base), heavy_link_context=str(self.base / 'heavy-link')))
        self.plan['commands'] = bootstrap_pilot.pilot_steps(
            self.seed.descriptor, self.seed.commands, self.seed.descriptor['abi_flags'], str(self.work))
        self.seed.plan = self.plan
        for name, value in (('system', 'Linux'), ('machine', 'x86_64')):
            mock = patch('emberbsd_sdk.native_toolset.platform.' + name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)
        self.seal()

    def seal(self):
        self.seed.seal()
        self.seed.acceptance['evidence_level'] = 'input-and-route-only'
        self.audit = dict(format='emberbsd-bootstrap-seed-input-route-audit', version=1,
                          scope='gcc16-bootstrap', status='PASS',
                          descriptor_sha256=self.seed.descriptor_ref['sha256'],
                          bootstrap_plan=self.seed.plan_ref, tools=self.seed.descriptor['tools'],
                          dynamic_dependencies=self.seed.descriptor['dynamic_dependencies'],
                          expected_commands=self.seed.plan['commands'], execution_observed=False,
                          unresolved_tool_routes=[], unresolved_abi_inputs=[], unresolved_dsos=[])
        self.bind_audit()

    def bind_audit(self):
        self.seed.acceptance['input_route_audit'] = self.seed.native.document(self.audit)
        self.seed.acceptance.pop('execution_audit', None)
        self.seed.acceptance_ref = self.seed.native.document(self.seed.acceptance)

    def invoke(self):
        return bootstrap_pilot.run_pilot(
            self.seed.descriptor_ref['path'], descriptor_sha256=self.seed.descriptor_ref['sha256'],
            accepted_seed_receipt=self.seed.acceptance_ref, bootstrap_plan=self.seed.plan_ref,
            required_roles=('cc', 'cxx'))

    def fake_command(self, argv, log, **kwargs):
        Path(log).write_bytes(b'synthetic combined route evidence\n')
        if '-o' in argv:
            image = bytearray(self.seed.native.image)
            if '-c' in argv:
                struct.pack_into('<H', image, 16, 1)
            output = Path(argv[-1])
            output.write_bytes(image)
            output.chmod(0o755)
        return subprocess.CompletedProcess(argv, 0)

    def test_pilot_does_not_require_prior_execution_or_make_export(self):
        self.assertNotIn('execution_audit', self.seed.acceptance)
        self.seed.export.rename(self.base / 'unavailable-export')
        self.seed.descriptor['tools'].pop('sh')
        self.seed.descriptor['tools'].pop('make')
        self.seed.descriptor['dynamic_dependencies'] = self.seed.dynamic(self.seed.descriptor)
        self.seal()
        with patch('subprocess.Popen', side_effect=AssertionError('no native process')):
            selected = self.seed.load(required_roles=('cc', 'cxx'))
        self.assertEqual(selected.as_dict()['plan']['purpose'], 'native-c-and-cxx-pilot')
        self.assertNotIn('sh', selected.commands)

    def test_wrong_input_route_level_or_unresolved_route_refuses_before_child(self):
        for field, value in (('execution_observed', True), ('unresolved_dsos', ['/ambient/lib.dylib']),
                             ('expected_commands', [])):
            with self.subTest(field=field):
                original = copy.deepcopy(self.audit)
                self.audit[field] = value
                self.bind_audit()
                with patch('emberbsd_sdk.bootstrap_pilot.run_logged') as run:
                    with self.assertRaises(SDKError):
                        self.invoke()
                    run.assert_not_called()
                    self.assertFalse(self.work.exists())
                self.audit = original

    def test_altered_command_or_resource_refuses_before_child(self):
        for kind in ('command', 'parallel', 'deadline', 'ambient'):
            with self.subTest(kind=kind):
                original = copy.deepcopy(self.plan)
                if kind == 'command':
                    self.plan['commands'][0]['argv'].append('-specs=/ambient')
                elif kind == 'parallel':
                    self.plan['resources']['cpu_jobs'] = 2
                elif kind == 'deadline':
                    self.plan['resources']['runtime_seconds'] = True
                else:
                    self.plan['commands'][0]['env']['DYLD_LIBRARY_PATH'] = '/ambient'
                self.seal()
                with patch('emberbsd_sdk.bootstrap_pilot.run_logged') as run:
                    with self.assertRaises(SDKError):
                        self.invoke()
                    run.assert_not_called()
                    self.assertFalse(self.work.exists())
                self.plan.clear()
                self.plan.update(original)

    def test_serial_fake_completion_preserves_six_logs_without_qualification(self):
        original_write = bootstrap_pilot.write_json
        def fenced_write(path, value, **kwargs):
            name = '.prebuild-sdk-lock-' + hashlib.sha256(str(self.work).encode()).hexdigest()
            with open(self.base / name, 'rb') as handle:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return original_write(path, value, **kwargs)
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=self.fake_command) as run, \
                patch('emberbsd_sdk.bootstrap_pilot.write_json', side_effect=fenced_write):
            result = self.invoke()
        self.assertEqual(run.call_count, 6)
        self.assertEqual(result['status'], 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW')
        self.assertTrue(result['all_commands_drained'])
        self.assertTrue(result['all_leases_closed'])
        self.assertFalse(result['ordinary_provider_qualified'])
        self.assertNotIn('all_leases_closed', json.loads((self.work / 'result.json').read_text()))
        self.assertEqual(result['evidence_level'], 'command-reported-routes-and-actual-exits')
        self.assertTrue(all(row['actual_exit'] == 0 and set(row['log']) == {'path', 'sha256'}
                            for row in result['commands']))
        self.assertTrue(all(0 < call.kwargs['timeout_seconds'] <= 120 for call in run.call_args_list))

    def test_exact_actual_failure_is_retained_and_never_retried(self):
        def fail(argv, log, **kwargs):
            Path(log).write_bytes(b'actual fake failure log\n')
            raise CommandError(7, argv, log=log)
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=fail) as run:
            result = self.invoke()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(result['commands'][0]['actual_exit'], 7)
        self.assertTrue(result['all_commands_drained'])
        self.assertTrue(result['all_leases_closed'])
        self.assertIn('actual fake failure', Path(result['commands'][0]['log']['path']).read_text())

    def test_bad_native_output_keeps_observed_zero_but_refuses(self):
        def bad_output(argv, log, **kwargs):
            Path(log).write_bytes(b'fake completed command\n')
            Path(argv[-1]).write_bytes(b'not a native object')
            return subprocess.CompletedProcess(argv, 0)
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=bad_output) as run:
            result = self.invoke()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(result['commands'][0]['actual_exit'], 0)
        self.assertIn('native object', result['error'])

    def test_expired_budget_refuses_before_child_and_preserves_unknown_exit(self):
        with patch('emberbsd_sdk.bootstrap_pilot.time.monotonic', side_effect=[10.0, 131.0]), \
                patch('emberbsd_sdk.bootstrap_pilot.run_logged') as run:
            result = self.invoke()
        run.assert_not_called()
        self.assertEqual(result['status'], 'FAILED')
        self.assertIsNone(result['commands'][0]['actual_exit'])
        self.assertTrue(result['all_commands_drained'])

    def test_failed_link_drain_enters_hold_while_all_resource_fences_are_open(self):
        observed = []
        unstable_hashes = []
        original_sha = bootstrap_pilot.sha256_file
        class FiniteHoldControl(BaseException):
            pass
        def fail_link(argv, log, **kwargs):
            if '-c' in argv:
                return self.fake_command(argv, log, **kwargs)
            Path(log).write_bytes(b'combined failed-drain fixture\n')
            raise ProcessOwnershipError(1234567, 'finite unproven fixture', log=log)
        def inspect_hold():
            receipt = json.loads((self.work / 'result.json').read_text())
            self.assertEqual(receipt['status'], 'HOLD_UNPROVEN_DRAIN')
            self.assertEqual(receipt['owned_sid'], 1234567)
            self.assertFalse(receipt['all_commands_drained'])
            held_command = receipt['commands'][-1]
            self.assertEqual(held_command['unfinalized_log_path'], str(self.work / 'c-link.log'))
            self.assertNotIn('log', held_command)
            for context in (str(self.work), self.plan['resources']['heavy_link_context'],
                            self.seed.descriptor['bundle_root']):
                name = '.prebuild-sdk-lock-' + hashlib.sha256(context.encode()).hexdigest()
                with open(self.base / name, 'rb') as handle:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            observed.append('work, heavy-link and input leases held')
            # Production _hold never returns. End this pure fixture here.
            raise FiniteHoldControl()
        def unstable_log(path):
            if Path(path).name == 'c-link.log':
                unstable_hashes.append(path)
                raise SDKError('live log changes during hashing')
            return original_sha(path)
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=fail_link) as run, \
                patch('emberbsd_sdk.bootstrap_pilot._hold', side_effect=inspect_hold), \
                patch('emberbsd_sdk.bootstrap_pilot.sha256_file', side_effect=unstable_log):
            self.invoke()
        self.assertEqual(run.call_count, 2)
        self.assertEqual(observed, ['work, heavy-link and input leases held'])
        self.assertEqual(unstable_hashes, [])

    def test_second_run_cannot_reuse_the_pilot_work(self):
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=self.fake_command):
            self.invoke()
        before = (self.work / 'result.json').read_bytes()
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged') as run:
            with self.assertRaisesRegex(SDKError, 'fresh, unused'):
                self.invoke()
            run.assert_not_called()
        self.assertEqual((self.work / 'result.json').read_bytes(), before)

    def test_input_drift_after_drained_commands_cannot_report_completion(self):
        calls = []
        def drift(argv, log, **kwargs):
            returned = self.fake_command(argv, log, **kwargs)
            calls.append(argv)
            if len(calls) == 6:
                Path(self.seed.descriptor['tools']['cc']['path']).write_bytes(b'changed input bytes')
            return returned
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=drift):
            result = self.invoke()
        self.assertEqual(len(result['commands']), 6)
        self.assertEqual(result['status'], 'FAILED')
        self.assertTrue(result['all_commands_drained'])
        self.assertFalse(result['inputs_revalidated_after_drain'])
        self.assertFalse(result['ordinary_provider_qualified'])

    def test_darwin_route_has_verbose_single_link_thread_and_runtime_dso_trace(self):
        descriptor = copy.deepcopy(self.seed.descriptor)
        descriptor['host'] = {'os': 'Darwin', 'cpu': 'arm64'}
        steps = bootstrap_pilot.pilot_steps(descriptor, self.seed.commands,
                                            descriptor['abi_flags'], str(self.work))
        for step in steps:
            self.assertEqual(step['env']['TMPDIR'], str(self.work))
            if step['phase'] == 'run':
                self.assertEqual(step['env']['DYLD_PRINT_LIBRARIES'], '1')
            else:
                self.assertIn('-v', step['argv'])
                self.assertNotIn('DYLD_PRINT_LIBRARIES', step['env'])
            self.assertNotIn('DYLD_LIBRARY_PATH', step['env'])
            if step['phase'] == 'link':
                self.assertEqual(step['argv'].count('-Wl,--threads=1'), 1)

    def test_explicit_cache_dispatch_preserves_file_only_branch(self):
        with patch('emberbsd_sdk.darwin_runtime.validate_darwin_runtime_closure') as check:
            _dynamic_closure(self.seed.descriptor['dynamic_dependencies'], self.seed.descriptor)
            check.assert_not_called()
            graph = {'/system/cached': {'cache_image': {'path': '/audit', 'sha256': '0' * 64},
                                         'dependencies': []}}
            _dynamic_closure(graph, self.seed.descriptor)
            check.assert_called_once_with(graph, self.seed.descriptor)


class FixedBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.prefix = self.root / 'prefix'
        self.prefix.mkdir()
        (self.prefix / 'ld64.lld').write_bytes(b'fixed fixture executable bytes')
        (self.prefix / 'ld').symlink_to('ld64.lld')
        template = self.root / 'ports-template'
        template.write_bytes(b'source-owned @DEPLOYMENT@ fixture\n')
        specs = self.root / 'seed.specs'
        specs.write_bytes(b'source-owned 26.0 fixture\n')
        ref = lambda p: dict(path=str(p), sha256=hashlib.sha256(p.read_bytes()).hexdigest())
        source = dict(repository='https://example.invalid/ports', commit='1' * 40)
        self.descriptor = dict(host=dict(os='Darwin', cpu='arm64'), sources=dict(ports=source),
                               driver_prefix=str(self.prefix), bundle_root=str(self.root),
                               tools={role: dict(path=str(self.prefix / role)) for role in ('cc', 'cxx', 'cpp')})
        self.descriptor['tools']['ld'] = dict(path=str(self.prefix / 'ld64.lld'))
        self.binding = dict(kind=bootstrap_binding.DARWIN_LLVM_MC_SEED, ports_source=source,
                            template_relative=bootstrap_binding._TEMPLATE_RELATIVE,
                            template=ref(template), instantiated=ref(specs), deployment='26.0',
                            reported_programs=dict(ld=str(self.prefix / 'ld')))
        self.descriptor['driver_binding'] = self.binding
        for name, value in (('_TEMPLATE_SHA256', ref(template)['sha256']),
                             ('_INSTANTIATED_SHA256', ref(specs)['sha256'])):
            mock = patch.object(bootstrap_binding, name, value)
            mock.start()
            self.addCleanup(mock.stop)

    def test_fixed_tuple_and_nominal_query_do_not_reselect_execution_ld(self):
        commands = bootstrap_binding.seed_commands(self.descriptor)
        self.assertEqual(commands['cc'], (str(self.prefix / 'cc'), '-B' + str(self.prefix) + '/',
                                          '-specs=' + self.binding['instantiated']['path'],
                                          '-mmacosx-version-min=26.0'))
        tools = {role: row['path'] for role, row in self.descriptor['tools'].items()}
        reported = bootstrap_binding.seed_reported_tools(self.descriptor, tools)
        self.assertEqual(reported['ld'], str(self.prefix / 'ld'))
        self.assertEqual(tools['ld'], str(self.prefix / 'ld64.lld'))
        self.assertEqual(commands['ld'], (tools['ld'],))

    def test_wrong_template_specs_deployment_or_linker_flavor_refuses(self):
        for kind in ('template', 'specs', 'deployment', 'linker'):
            with self.subTest(kind=kind):
                descriptor = copy.deepcopy(self.descriptor)
                if kind == 'template':
                    descriptor['driver_binding']['template']['sha256'] = '0' * 64
                elif kind == 'specs':
                    descriptor['driver_binding']['instantiated']['sha256'] = '0' * 64
                elif kind == 'deployment':
                    descriptor['driver_binding']['deployment'] = '27.0'
                else:
                    descriptor['tools']['ld']['path'] = str(self.prefix / 'ld')
                with self.assertRaises(SDKError):
                    bootstrap_binding.seed_commands(descriptor)

    def test_changed_spec_bytes_refuse_even_when_filename_is_selected(self):
        Path(self.binding['instantiated']['path']).write_bytes(b'unselected arbitrary specs\n')
        with self.assertRaisesRegex(SDKError, 'SHA256 mismatch'):
            bootstrap_binding.seed_commands(self.descriptor)

    def test_metadata_only_graduation_keeps_exact_fixed_identities(self):
        Path(self.binding['instantiated']['path']).unlink()
        commands = bootstrap_binding.seed_commands(self.descriptor, inspect_inputs=False)
        self.assertEqual(commands['cc'][-1], '-mmacosx-version-min=26.0')
        self.binding['instantiated']['sha256'] = '0' * 64
        with self.assertRaises(SDKError):
            bootstrap_binding.seed_commands(self.descriptor, inspect_inputs=False)


class FixedLLDBindingTests(FixedBindingTests):
    def setUp(self):
        super().setUp()
        template = Path(self.binding['template']['path'])
        template.write_bytes(b'source-owned @DEPLOYMENT@ @SDK_VERSION@ fixture\n')
        specs = Path(self.binding['instantiated']['path'])
        specs.write_bytes(b'source-owned 26.0 26.5 fixture\n')
        ref = lambda p: dict(path=str(p), sha256=hashlib.sha256(p.read_bytes()).hexdigest())
        self.binding.update(kind=bootstrap_binding.DARWIN_LLVM_MC_LLD_SEED,
                            template_relative=bootstrap_binding._LLD_TEMPLATE_RELATIVE,
                            template=ref(template), instantiated=ref(specs), sdk_version='26.5')
        self.sdk = self.root / 'host-sdk'
        self.sdk.mkdir()
        self.metadata = dict(Version='26.5', CanonicalName='macosx26.5', DefaultVariant='macos',
                             SupportedTargets=dict(macosx=dict(BuildVersionPlatformID='1')),
                             DefaultCompiler='historical ABI metadata only')
        self.metadata_path = self.sdk / 'SDKSettings.json'
        self.inventory_path = self.root / 'sdk-inventory.json'
        self.descriptor['host_abi'] = dict(headers=[dict(root=str(self.sdk), inventory={})])
        self.seal_sdk()
        self.recorded = {'26.5': ref(specs)['sha256']}
        for name, value in (('_LLD_TEMPLATE_SHA256', ref(template)['sha256']),
                             ('_LLD_INSTANTIATED_SHA256', self.recorded)):
            mock = patch.object(bootstrap_binding, name, value)
            mock.start()
            self.addCleanup(mock.stop)

    def seal_sdk(self):
        self.metadata_path.write_text(json.dumps(self.metadata))
        self.binding['sdk_metadata'] = dict(path=str(self.metadata_path),
                                           sha256=hashlib.sha256(self.metadata_path.read_bytes()).hexdigest())
        self.inventory_path.write_text(json.dumps(snapshot_tree(str(self.sdk))))
        self.descriptor['host_abi']['headers'][0]['inventory'] = dict(
            path=str(self.inventory_path), sha256=hashlib.sha256(self.inventory_path.read_bytes()).hexdigest())

    def test_sdk_version_and_platform_metadata_must_match_the_fixed_binding(self):
        original = copy.deepcopy(self.metadata)
        for field, value in (('Version', '26.4'), ('CanonicalName', 'iphoneos26.5'),
                             ('DefaultVariant', 'ios'), ('SupportedTargets', {})):
            with self.subTest(field=field):
                self.metadata = copy.deepcopy(original)
                self.metadata[field] = value
                self.seal_sdk()
                with self.assertRaisesRegex(SDKError, 'pinned macOS SDK metadata'):
                    bootstrap_binding.seed_commands(self.descriptor)

    def use_sdk(self, version, record=True):
        specs = Path(self.binding['instantiated']['path'])
        specs.write_bytes(('source-owned 26.0 %s fixture\n' % version).encode())
        sha = hashlib.sha256(specs.read_bytes()).hexdigest()
        if record:
            self.recorded[version] = sha
        self.binding.update(sdk_version=version, instantiated=dict(path=str(specs), sha256=sha))
        self.metadata.update(Version=version, CanonicalName='macosx' + version)
        self.seal_sdk()

    def test_recorded_current_sdk_version_is_accepted(self):
        self.use_sdk('27.0')
        commands = bootstrap_binding.seed_commands(self.descriptor)
        self.assertEqual(commands['cc'][-1], '-mmacosx-version-min=26.0')
        self.assertEqual(commands['cc'][-2], '-specs=' + self.binding['instantiated']['path'])

    def test_unrecorded_sdk_version_is_refused(self):
        self.use_sdk('27.1', record=False)
        with self.assertRaisesRegex(SDKError, 'unsupported fixed seed SDK version'):
            bootstrap_binding.seed_commands(self.descriptor)

    def test_recorded_version_needs_its_own_instantiation(self):
        self.use_sdk('27.0')
        self.binding['instantiated']['sha256'] = self.recorded['26.5']
        with self.assertRaisesRegex(SDKError, 'unsupported Ports seed template'):
            bootstrap_binding.seed_commands(self.descriptor)

    def test_sdk_metadata_cannot_use_another_root_or_inventory_hash(self):
        original = copy.deepcopy(self.binding['sdk_metadata'])
        self.binding['sdk_metadata']['path'] = str(self.root / 'outside-SDKSettings.json')
        with self.assertRaisesRegex(SDKError, 'outside its declared headers'):
            bootstrap_binding.seed_commands(self.descriptor)
        self.binding['sdk_metadata'] = original
        self.binding['sdk_metadata']['sha256'] = '0' * 64
        with self.assertRaisesRegex(SDKError, 'pinned ABI inventory'):
            bootstrap_binding.seed_commands(self.descriptor)

    def test_sdk_version_selector_is_required_and_cannot_use_an_implicit_default(self):
        self.binding['sdk_version'] = '26.4'
        with self.assertRaisesRegex(SDKError, 'unsupported fixed seed SDK version'):
            bootstrap_binding.seed_commands(self.descriptor)
        self.binding.pop('sdk_version')
        with self.assertRaisesRegex(SDKError, 'unsupported bootstrap driver binding fields'):
            bootstrap_binding.seed_commands(self.descriptor)

    def test_retired_sdk_payload_is_not_read_during_metadata_only_graduation(self):
        self.metadata_path.unlink()
        commands = bootstrap_binding.seed_commands(self.descriptor, inspect_inputs=False)
        self.assertEqual(commands['cc'][-1], '-mmacosx-version-min=26.0')
        self.binding['sdk_metadata']['sha256'] = '0' * 64
        with self.assertRaisesRegex(SDKError, 'pinned ABI inventory'):
            bootstrap_binding.seed_commands(self.descriptor, inspect_inputs=False)
