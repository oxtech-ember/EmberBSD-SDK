# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Synthetic make binding and controller controls. No native tool executes."""

import copy
import fcntl
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_bootstrap_seed import SeedFixture
from test_native_toolset import Fixture
from emberbsd_sdk import bootstrap_binding, bootstrap_make, bootstrap_pilot
from emberbsd_sdk.errors import CommandError, ProcessOwnershipError, SDKError
from emberbsd_sdk.inventory import snapshot_tree

TEMPLATE = b'''.SHELL: name=sh path=@QUALIFIED_SH@
# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted). This is a bounded bootstrap input.
# Keep every upstream rule in sys-upstream.mk unchanged.
SHELL= @QUALIFIED_SH@
.include "${.PARSEDIR}/sys-upstream.mk"
'''


class MakeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        with patch('test_bootstrap_seed.Fixture', side_effect=lambda base: Fixture(base, 'bundle')):
            self.seed = SeedFixture(self.base)
        self.recipe_plan = copy.deepcopy(self.seed.plan)
        f = self.seed.native
        gmake = f.bundle / 'gmake'
        gmake.write_bytes(f.image)
        gmake.chmod(0o755)
        self.seed.descriptor['tools']['gmake'] = dict(path=str(gmake), sha256=hashlib.sha256(f.image).hexdigest(),
                                                     kind='native', provider='ports', version='fixture-1')
        self.mk = f.bundle / 'mk'
        self.mk.mkdir()
        for name in ['sys.mk', 'bsd.own.mk', *('rule' + str(i) + '.mk' for i in range(19))]:
            (self.mk / name).write_bytes(b'# synthetic upstream mk file\n')
        original = f.document(snapshot_tree(str(self.mk)))
        (self.mk / 'sys.mk').rename(self.mk / 'sys-upstream.mk')
        shell = self.seed.descriptor['tools']['sh']['path']
        (self.mk / 'sys.mk').write_bytes(TEMPLATE.replace(b'@QUALIFIED_SH@', shell.encode()))
        for path in self.mk.iterdir():
            path.chmod(0o444)
        self.seed.descriptor['make_binding'] = dict(
            kind='ports-bootstrap-make-v1', mode_policy='read-only', ports_source=f.sources['ports'],
            template_relative='profiles/common-build-tools/bootstrap/sys.mk.in', template=f.raw(TEMPLATE),
            original_inventory=original, mk_root=str(self.mk), inventory=f.document(snapshot_tree(str(self.mk))),
            shell=shell, makeconf=f.raw(bootstrap_make.MAKECONF_SOURCE),
            source_correlation=f.raw(b'synthetic retained primary-source correlation'))
        self.seed.descriptor['dynamic_dependencies'] = self.seed.dynamic(self.seed.descriptor)
        self.work = self.base / 'make-work'
        self.plan = dict(format=bootstrap_make.MAKE_PROBE_FORMAT, version=1, scope='gcc16-bootstrap',
                         purpose='make-and-shell-routing-probe', host=f.host, sources=f.sources,
                         work_root=str(self.work), required_roles=['make', 'gmake', 'sh'],
                         resources=dict(cpu_jobs=1, link_jobs=1, runtime_seconds=120,
                                        lock_root=str(self.base), heavy_link_context=str(self.base / 'heavy')))
        self.seed.plan = self.plan
        for name, value in (('system', 'Linux'), ('machine', 'x86_64')):
            mocked = patch('emberbsd_sdk.native_toolset.platform.' + name, return_value=value)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.seal()

    def seal(self):
        self.seed.commands = bootstrap_binding.seed_commands(self.seed.descriptor)
        self.plan['commands'] = bootstrap_make.make_probe_steps(
            self.seed.descriptor, self.seed.commands, self.seed.descriptor['abi_flags'], str(self.work))
        self.seed.seal()
        self.seed.acceptance['make_binding'] = copy.deepcopy(self.seed.descriptor['make_binding'])
        self.seed.acceptance['evidence_level'] = 'input-and-route-only'
        self.seed.acceptance['input_route_audit'] = self.seed.native.document(dict(
            format='emberbsd-bootstrap-seed-input-route-audit', version=1, scope='gcc16-bootstrap', status='PASS',
            descriptor_sha256=self.seed.descriptor_ref['sha256'], bootstrap_plan=self.seed.plan_ref,
            tools=self.seed.descriptor['tools'], dynamic_dependencies=self.seed.descriptor['dynamic_dependencies'],
            expected_commands=self.plan['commands'], execution_observed=False,
            unresolved_tool_routes=[], unresolved_abi_inputs=[], unresolved_dsos=[]))
        self.seed.acceptance.pop('execution_audit', None)
        self.seed.acceptance_ref = self.seed.native.document(self.seed.acceptance)

    def invoke(self):
        return bootstrap_pilot.run_make_probe(
            self.seed.descriptor_ref['path'], descriptor_sha256=self.seed.descriptor_ref['sha256'],
            accepted_seed_receipt=self.seed.acceptance_ref, bootstrap_plan=self.seed.plan_ref,
            required_roles=('make', 'gmake', 'sh'))

    def fake_command(self, argv, log, **kwargs):
        name = Path(log).stem
        shell = self.seed.descriptor['make_binding']['shell']
        lines = {'shell-positive': ['shell-positive-ok'],
                 'bmake-positive': ['bmake-positive-ok', 'bmake-recursive-ok', 'bmake-assign-ok',
                                    'bmake-modifier-ok', 'upstream-suffixes-ok', 'bmake-shell ' + shell],
                 'gmake-positive': ['gmake-positive-ok', 'gmake-recursive-ok', 'gmake-shell ' + shell],
                 'shell-failure': ['expected-shell-failure'], 'bmake-failure': ['expected-bmake-failure'],
                 'gmake-failure': ['expected-gmake-failure']}[name]
        Path(log).write_text('\n'.join(lines) + '\n')
        if name == 'bmake-positive':
            (self.work / 'fixture.result').write_bytes(b'implicit-ok\n')
        status = next(row['expected_exit'] for row in self.plan['commands'] if row['id'] == name)
        if status:
            raise CommandError(status, argv, log=log)
        return subprocess.CompletedProcess(argv, status)

    def test_fixed_source_binding_preserves_all_original_mk_rows(self):
        self.assertEqual(hashlib.sha256(TEMPLATE).hexdigest(), bootstrap_make._TEMPLATE_SHA256)
        with patch('subprocess.Popen', side_effect=AssertionError('no native process')):
            selected = self.seed.load(required_roles=('make', 'gmake', 'sh'))
        binding = self.seed.descriptor['make_binding']
        self.assertEqual(selected.command('make'), (self.seed.descriptor['tools']['make']['path'], '-m',
                         str(self.mk), 'SHELL=' + binding['shell'], 'MAKECONF=' + binding['makeconf']['path']))
        self.assertEqual(selected.command('gmake')[1:], ('SHELL=' + binding['shell'],
                                                       'MAKECONF=' + binding['makeconf']['path']))
        self.assertEqual(len(snapshot_tree(str(self.mk))), 22)

    def test_changed_or_missing_upstream_file_is_refused(self):
        for action in ('change', 'remove'):
            with self.subTest(action=action):
                path = self.mk / 'rule0.mk'
                before = path.read_bytes()
                path.chmod(0o644)
                if action == 'change':
                    path.write_bytes(b'changed\n')
                else:
                    path.unlink()
                with self.assertRaises(SDKError):
                    bootstrap_binding.seed_commands(self.seed.descriptor)
                path.write_bytes(before)
                path.chmod(0o444)

    def test_repinning_omitted_or_changed_upstream_tree_is_refused(self):
        path = self.mk / 'rule0.mk'
        path.chmod(0o644)
        path.write_bytes(b'changed\n')
        path.chmod(0o444)
        self.seed.descriptor['make_binding']['inventory'] = self.seed.native.document(snapshot_tree(str(self.mk)))
        with self.assertRaisesRegex(SDKError, 'complete original roster'):
            bootstrap_binding.seed_commands(self.seed.descriptor)

    def test_changed_entry_even_with_reinventoried_tree_is_refused(self):
        (self.mk / 'sys.mk').chmod(0o644)
        (self.mk / 'sys.mk').write_bytes(b'SHELL=/bin/sh\n')
        (self.mk / 'sys.mk').chmod(0o444)
        self.seed.descriptor['make_binding']['inventory'] = self.seed.native.document(snapshot_tree(str(self.mk)))
        with self.assertRaisesRegex(SDKError, 'exact Ports instantiation'):
            bootstrap_binding.seed_commands(self.seed.descriptor)

    def test_alias_shell_and_unsafe_path_are_refused(self):
        binding = self.seed.descriptor['make_binding']
        for field, value in (('shell', '/bin/sh'), ('mk_root', str(self.mk) + ' bad'),
                             ('mk_root', str(self.mk) + ':ambient')):
            with self.subTest(field=field, value=value):
                old = binding[field]
                binding[field] = value
                with self.assertRaises(SDKError):
                    bootstrap_binding.seed_commands(self.seed.descriptor)
                binding[field] = old

    def test_read_only_mode_policy_is_explicit_and_preserves_original_metadata(self):
        binding = self.seed.descriptor['make_binding']
        from emberbsd_sdk.native_toolset import _json
        self.assertEqual(_json(binding['original_inventory'])['sys.mk']['mode'], 0o644)
        self.assertEqual(_json(binding['inventory'])['sys-upstream.mk']['mode'], 0o444)
        binding['mode_policy'] = 'ignore'
        with self.assertRaisesRegex(SDKError, 'mode policy'):
            bootstrap_binding.seed_commands(self.seed.descriptor)
        binding['mode_policy'] = 'read-only'
        (self.mk / 'rule0.mk').chmod(0o644)
        binding['inventory'] = self.seed.native.document(snapshot_tree(str(self.mk)))
        with self.assertRaisesRegex(SDKError, 'complete original roster'):
            bootstrap_binding.seed_commands(self.seed.descriptor)

    def test_make_binding_must_be_independently_accepted(self):
        self.seed.acceptance.pop('make_binding')
        self.seed.acceptance_ref = self.seed.native.document(self.seed.acceptance)
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged') as run:
            with self.assertRaisesRegex(SDKError, 'acceptance make binding'):
                self.invoke()
        run.assert_not_called()
        self.assertFalse(self.work.exists())

    def test_wrong_fixture_makeconf_refuses_before_child(self):
        binding = self.seed.descriptor['make_binding']
        binding['makeconf'] = self.seed.native.raw(b'include ambient.mk\n')
        self.seal()
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged') as run:
            with self.assertRaisesRegex(SDKError, 'exact fixture MAKECONF'):
                self.invoke()
        run.assert_not_called()
        self.assertFalse(self.work.exists())

    def test_finite_source_probes_parse_time_shell_recursion_and_implicit_rule(self):
        sources = bootstrap_make.make_probe_sources(self.seed.descriptor, self.seed.commands)
        bsd = sources['BMakefile'].decode()
        self.assertIn('ASSIGN != printf', bsd)
        self.assertIn('${MOD_COMMAND:sh}', bsd)
        self.assertIn('${.SHELL}', bsd)
        self.assertIn('.probe.result:', bsd)
        self.assertIn('.SUFFIXES:M.c', bsd)
        self.assertIn('.include <bsd.own.mk>', bsd)
        for text in (bsd, sources['GMakefile'].decode()):
            self.assertIn('SHELL=' + self.seed.descriptor['make_binding']['shell'], text)
        for row in self.plan['commands']:
            self.assertEqual(row['env']['TMPDIR'], str(self.work))
            self.assertNotIn('DYLD_LIBRARY_PATH', row['env'])
            self.assertNotIn('-c', row['argv'] if row['phase'] == 'make' else [])

    def test_six_synthetic_steps_keep_expected_nonzero_codes_and_fences(self):
        original_write = bootstrap_pilot.write_json
        def fenced_write(path, value, **kwargs):
            lock = self.base / ('.prebuild-sdk-lock-' + hashlib.sha256(str(self.work).encode()).hexdigest())
            with lock.open('rb') as handle:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return original_write(path, value, **kwargs)
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=self.fake_command) as run, \
                patch('emberbsd_sdk.bootstrap_pilot.write_json', side_effect=fenced_write):
            result = self.invoke()
        self.assertEqual(run.call_count, 6)
        self.assertEqual(result['status'], 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW')
        self.assertEqual([row['actual_exit'] for row in result['commands']], [0, 0, 0, 7, 2, 2])
        self.assertTrue(result['all_commands_drained'] and result['all_leases_closed'])
        self.assertFalse(result['ordinary_provider_qualified'])
        self.assertNotIn('all_leases_closed', json.loads((self.work / 'result.json').read_text()))
        self.assertEqual((self.work / 'fixture.result').read_bytes(), b'implicit-ok\n')
        self.assertFalse((self.work / 'native.c').exists())
        self.assertTrue(all(0 < call.kwargs['timeout_seconds'] <= 120 for call in run.call_args_list))

    def test_expected_failure_cannot_be_replaced_by_zero(self):
        def false_success(argv, log, **kwargs):
            try:
                return self.fake_command(argv, log, **kwargs)
            except CommandError:
                return subprocess.CompletedProcess(argv, 0)
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=false_success) as run:
            result = self.invoke()
        self.assertEqual(run.call_count, 4)
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(result['commands'][-1]['actual_exit'], 0)

    def test_missing_positive_observation_or_implicit_output_refuses(self):
        def missing(argv, log, **kwargs):
            returned = self.fake_command(argv, log, **kwargs)
            if Path(log).stem == 'bmake-positive':
                (self.work / 'fixture.result').write_bytes(b'wrong output\n')
            return returned
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=missing) as run:
            result = self.invoke()
        self.assertEqual(run.call_count, 2)
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(result['commands'][-1]['actual_exit'], 0)

    def test_make_failed_drain_retains_input_and_work_fences_without_live_hash(self):
        observed = []
        original_sha = bootstrap_pilot.sha256_file
        class FiniteHold(BaseException):
            pass
        def fail(argv, log, **kwargs):
            Path(log).write_bytes(b'live log\n')
            raise ProcessOwnershipError(1234567, 'synthetic undrained shell', log=log)
        def hold():
            receipt = json.loads((self.work / 'result.json').read_text())
            self.assertEqual(receipt['status'], 'HOLD_UNPROVEN_DRAIN')
            self.assertEqual(receipt['owned_sid'], 1234567)
            self.assertNotIn('log', receipt['commands'][-1])
            for context in (str(self.work), self.seed.descriptor['bundle_root']):
                path = self.base / ('.prebuild-sdk-lock-' + hashlib.sha256(context.encode()).hexdigest())
                with path.open('rb') as handle:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            observed.append('HOLD before live-log hash')
            raise FiniteHold()
        def no_live_hash(path):
            if Path(path).suffix == '.log':
                raise AssertionError('no live hash')
            return original_sha(path)
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=fail), \
                patch('emberbsd_sdk.bootstrap_pilot._hold', side_effect=hold), \
                patch('emberbsd_sdk.bootstrap_pilot.sha256_file', side_effect=no_live_hash):
            self.invoke()
        self.assertEqual(observed, ['HOLD before live-log hash'])

    def test_missing_binding_and_plan_overrides_refuse(self):
        for kind in ('binding', 'argv', 'status', 'resources'):
            with self.subTest(kind=kind):
                descriptor, plan = copy.deepcopy(self.seed.descriptor), copy.deepcopy(self.plan)
                if kind == 'binding':
                    descriptor.pop('make_binding')
                elif kind == 'argv':
                    plan['commands'][0]['argv'].append('/bin/sh')
                elif kind == 'status':
                    plan['commands'][-1]['expected_exit'] = 0
                else:
                    plan['resources']['cpu_jobs'] = 2
                with self.assertRaises(SDKError):
                    bootstrap_make.validate_make_probe_plan(plan, descriptor, self.seed.commands,
                                                             descriptor['abi_flags'])

    def test_completed_make_plan_cannot_graduate_an_ordinary_provider(self):
        from emberbsd_sdk.bootstrap_seed import _graduation
        receipt = self.seed.native.document(dict(
            format='emberbsd-native-gcc16-bootstrap-graduation', version=1, scope='native-build',
            status='PASS', output_descriptor_sha256='0' * 64, seed_descriptor=self.seed.descriptor_ref,
            accepted_seed_receipt=self.seed.acceptance_ref, bootstrap_plan=self.seed.plan_ref))
        with self.assertRaisesRegex(SDKError, 'finite pilot cannot graduate'):
            _graduation(receipt, {}, '0' * 64, {})

    def test_full_recipe_cannot_drop_the_recognized_make_tuple(self):
        from emberbsd_sdk.bootstrap_seed import _plan
        with self.assertRaisesRegex(SDKError, 'unsupported bound full-plan fields'):
            _plan(self.seed.native.document(self.recipe_plan), self.seed.descriptor)


if __name__ == '__main__':
    unittest.main()
