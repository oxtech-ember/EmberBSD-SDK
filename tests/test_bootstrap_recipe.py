# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Bound full-plan controls. All command children and tool payloads are synthetic."""

import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_bootstrap_pilot
import test_bootstrap_producer
import test_bootstrap_seed
from emberbsd_sdk import bootstrap_binding, bootstrap_pilot, bootstrap_producer as producer
from emberbsd_sdk import bootstrap_recipe as recipe
from emberbsd_sdk.bootstrap_seed import _plan
from emberbsd_sdk.errors import CommandError, ProcessOwnershipError, SDKError


class RecipeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_bootstrap_producer.MetadataTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.seed, self.f = self.fixture.seed, self.fixture.f
        self.base, self.descriptor = self.fixture.base, self.fixture.descriptor
        self.commands = self.fixture.commands
        for role in ('pkg_add', 'pkg_create'):
            path = self.f.bundle / role
            path.write_bytes(self.f.image)
            path.chmod(0o755)
            self.descriptor['tools'][role] = dict(path=str(path), sha256=hashlib.sha256(self.f.image).hexdigest(),
                                                 kind='native', provider='ports', version='synthetic')
            self.commands[role] = (str(path),)
            (self.fixture.utility_bin / role).symlink_to('../' + role)
        self.descriptor['driver_binding']['kind'] = bootstrap_binding.DARWIN_LLVM_MC_LLD_SEED
        self.descriptor['compiler'] = dict(family='gcc', version='16.2.0', triplet='aarch64-apple-darwin25')
        self.plan = copy.deepcopy(self.fixture.fixture.recipe_plan)
        self.work = self.base / 'full-recipe-work'
        self.plan.update(host=self.descriptor['host'], output_compiler=self.descriptor['compiler'],
            sources=self.descriptor['sources'], work_root=str(self.work),
            output_bundle_root=str(self.work / 'prefix'), metadata=copy.deepcopy(self.fixture.plan['metadata']),
            resources=dict(self.fixture.plan['resources'], cpu_jobs=1, runtime_seconds=43200),
            required_roles=sorted(self.commands), seed_descriptor={})
        self.plan['metadata']['recipes'] = sorted(self.plan['recipes'])
        self.plan['metadata']['native_tools'] = sorted([*self.plan['metadata']['native_tools'],
                                                       'pkg_add', 'pkg_create'])
        self.plan['ports_export'] = dict(source=self.descriptor['sources']['ports'],
                                         **self.plan['metadata']['export'])
        self.plan['recipes']['lang/gcc16']['object_directory'] = str(self.work / 'obj/gcc16/build')
        for number, row in enumerate(self.plan['recipes'].values()):
            row['distfiles'][0]['relative'] = f'subdir/archive-{number}.tar.xz'
        self.plan['comparison_command'] = 'gcc16-compare'
        self.refresh()

    def refresh(self):
        conf = self.plan['metadata']['makeconf']
        raw = producer.metadata_makeconf(self.plan, self.descriptor, self.commands, self.descriptor['abi_flags'])
        Path(conf['path']).write_bytes(raw)
        conf['sha256'] = hashlib.sha256(raw).hexdigest()
        self.descriptor['make_binding']['makeconf'] = copy.deepcopy(conf)
        # These fixtures start after native selection. Separate binding tests
        # verify live compiler/specs admission. No fixture payload can execute.
        self.plan['commands'] = recipe.recipe_steps(self.plan, self.descriptor, self.commands,
                                                     self.descriptor['abi_flags'])

    def validate(self, *, inspect_inputs=True):
        return _plan(self.f.document(self.plan), self.descriptor, self.commands,
                      self.descriptor['abi_flags'], inspect_inputs=inspect_inputs)

    def invoke(self):
        descriptor = self.f.document(self.descriptor)
        self.plan['seed_descriptor'] = descriptor
        plan = self.f.document(self.plan)
        acceptance = self.f.document(dict(input_route_audit=self.f.raw(b'accepted synthetic route only')))
        case = self
        class Selection:
            commands = case.commands
            def as_dict(self):
                return dict(plan=case.validate())
        with patch('emberbsd_sdk.bootstrap_seed.load_bootstrap_seed', side_effect=lambda *a, **kw: Selection()):
            return bootstrap_pilot.run_bootstrap(descriptor['path'], descriptor_sha256=descriptor['sha256'],
                accepted_seed_receipt=acceptance, bootstrap_plan=plan, required_roles=self.plan['required_roles'])

    def fake_command(self, argv, log, **kwargs):
        Path(log).write_bytes(b'synthetic complete recipe command\n')
        if argv[-1] == 'install':
            (self.work / 'prefix/installed-fixture').write_bytes(b'synthetic output; not a native provider\n')
        return subprocess.CompletedProcess(argv, 0)

    def test_full_tuple_fixed_order_and_metadata_config_are_preserved(self):
        with patch('subprocess.Popen', side_effect=AssertionError('no native child')):
            self.assertEqual(self.validate(), self.plan)
        self.assertEqual([(step['recipe'], step['phase']) for step in self.plan['commands']],
            [('devel/gmp', 'build'), ('devel/gmp', 'install'), ('lang/gcc16', 'build'),
             ('lang/gcc16', 'compare'), ('lang/gcc16', 'install')])
        for step in self.plan['commands']:
            self.assertEqual(step['argv'][:len(self.commands[step['role']])], list(self.commands[step['role']]))
            self.assertEqual(step['env'], producer.metadata_environment(self.plan, self.commands))
            if step['role'] == 'make':
                self.assertFalse(any(word.startswith(('-j', '--jobs')) for word in step['argv']))
            else:
                self.assertIn('-j1', step['argv'])
        self.assertEqual(self.plan['commands'][3]['argv'][-1], 'compare')

    def test_compare_forces_only_the_stamped_target(self):
        command = self.plan['commands'][3]
        self.assertEqual(command['argv'][-2:], ['--eval=.PHONY: compare', 'compare'])
        self.assertEqual(command['argv'].count('--eval=.PHONY: compare'), 1)
        self.assertFalse(any(word in ('-B', '--always-make') for word in command['argv']))
        self.assertTrue(all('--eval=.PHONY: compare' not in step['argv']
                            for step in self.plan['commands'] if step['id'] != 'gcc16-compare'))

    def test_missing_phony_override_is_refused_before_child(self):
        self.plan['commands'][3]['argv'].remove('--eval=.PHONY: compare')
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged') as run:
            with self.assertRaisesRegex(SDKError, 'complete source-owned route'):
                self.invoke()
            run.assert_not_called()
        self.assertFalse(self.work.exists())

    def test_recipes_with_the_ports_bmake_edge_start_the_ports_bmake_and_mksh(self):
        recipes = self.plan['recipes']
        recipes['devel/bmake'] = dict(copy.deepcopy(recipes['devel/gmp']), dependencies=[])
        recipes['devel/gmp']['dependencies'] = [*recipes['devel/gmp']['dependencies'], 'devel/bmake']
        initial = list(self.commands['make'])
        self.assertEqual(initial[1], '-m')
        steps = recipe.recipe_steps(self.plan, self.descriptor, self.commands, self.descriptor['abi_flags'])
        starts = {(step['recipe'], step['phase']): step for step in steps}
        shell = str(self.work / 'prefix/bin/mksh')
        final = [str(self.work / 'prefix/bin/bmake'), 'SHELL=' + shell, initial[4]]
        seed_env = producer.metadata_environment(self.plan, self.commands)
        self.assertNotEqual(seed_env['SHELL'], shell)
        for key in (('devel/bmake', 'build'), ('lang/gcc16', 'build'), ('lang/gcc16', 'compare')):
            self.assertEqual(starts[key]['argv'][:len(self.commands[starts[key]['role']])],
                             list(self.commands[starts[key]['role']]))
            self.assertEqual(starts[key]['env'], seed_env)
        for phase in ('build', 'install'):
            step = starts[('devel/gmp', phase)]
            self.assertEqual(step['argv'][:3], final)
            self.assertNotIn('-m', step['argv'])
            self.assertEqual([w for w in step['argv'] if w.startswith('SHELL=')], ['SHELL=' + shell])
            self.assertEqual(step['argv'][3:], ['EMBERBSD_DARWIN_BOOTSTRAP=yes', '-C',
                                                self.plan['ports_export']['root'] + '/devel/gmp', phase])
            self.assertEqual(step['env'], dict(seed_env, SHELL=shell, CONFIG_SHELL=shell))
        recipes['lang/gcc16']['dependencies'] = [*recipes['lang/gcc16']['dependencies'], 'devel/bmake']
        steps = recipe.recipe_steps(self.plan, self.descriptor, self.commands, self.descriptor['abi_flags'])
        compare = [step for step in steps if step['phase'] == 'compare'][0]
        gmake = list(self.commands['gmake'])
        self.assertEqual(compare['argv'][:3], [gmake[0], 'SHELL=' + shell, gmake[2]])
        self.assertEqual(compare['env']['SHELL'], shell)
        with self.assertRaisesRegex(SDKError, 'bound initial make'):
            recipe.recipe_make(self.plan, dict(self.commands, make=tuple(initial[:1])), 'devel/gmp')
        with self.assertRaisesRegex(SDKError, 'bound initial gmake'):
            recipe.recipe_gmake(self.plan, dict(self.commands, gmake=tuple(gmake[:1])), 'lang/gcc16')

    def test_wrong_tail_bare_tuple_and_route_overrides_fail_before_child(self):
        for kind in ('bare', 'tail', 'assignment', 'env', 'compare', 'ordering'):
            with self.subTest(kind=kind):
                before = copy.deepcopy(self.plan['commands'])
                if kind == 'bare':
                    self.plan['commands'][0]['argv'] = [self.commands['make'][0], '-C', str(self.fixture.export), 'build']
                elif kind == 'tail':
                    self.plan['commands'][0]['argv'].append('all')
                elif kind == 'assignment':
                    self.plan['commands'][0]['argv'].append('MAKE?=/usr/bin/make')
                elif kind == 'env':
                    self.plan['commands'][0]['env']['BASH_ENV'] = '/ambient'
                elif kind == 'compare':
                    self.plan['commands'][3]['argv'][-1] = 'all'
                else:
                    self.plan['commands'].reverse()
                with patch('emberbsd_sdk.bootstrap_pilot.run_logged') as run:
                    with self.assertRaisesRegex(SDKError, 'complete source-owned route'):
                        self.invoke()
                    run.assert_not_called()
                self.plan['commands'] = before
        self.assertFalse(self.work.exists())

    def test_work_input_output_and_comparison_overlap_are_refused(self):
        for kind in ('work', 'output', 'object'):
            with self.subTest(kind=kind):
                before = copy.deepcopy(self.plan)
                if kind == 'work':
                    self.plan['work_root'] = str(self.f.bundle)
                elif kind == 'output':
                    self.plan['output_bundle_root'] = str(self.base / 'external-install')
                else:
                    self.plan['recipes']['lang/gcc16']['object_directory'] = str(self.base / 'outside')
                self.refresh()
                with self.assertRaises(SDKError):
                    self.validate()
                self.plan = before
                self.refresh()

    def test_distfile_names_conflicts_and_metadata_closure_are_refused(self):
        original = copy.deepcopy(self.plan)
        for change in ('escape', 'collision', 'prefix', 'closure', 'extra'):
            with self.subTest(change=change):
                self.plan = copy.deepcopy(original)
                rows = self.plan['recipes']['lang/gcc16']['distfiles']
                if change == 'escape':
                    rows[0]['relative'] = '../archive'
                elif change == 'collision':
                    row = copy.deepcopy(rows[0])
                    row['file'] = self.f.raw(b'conflicting archive bytes')
                    rows.append(row)
                elif change == 'prefix':
                    row = copy.deepcopy(rows[0])
                    row['relative'] += '/nested'
                    rows.append(row)
                elif change == 'closure':
                    self.plan['metadata']['recipes'] = ['lang/gcc16']
                else:
                    self.plan['arbitrary_command'] = ['download']
                with self.assertRaises(SDKError):
                    self.validate()
        self.plan = original

    def test_resource_budget_is_explicit_and_bounded(self):
        for key, value in [('cpu_jobs', 2), ('link_jobs', 2), ('runtime_seconds', True),
                           ('runtime_seconds', 86401), ('runtime_seconds', 0)]:
            with self.subTest(key=key, value=value):
                original = self.plan['resources'][key]
                self.plan['resources'][key] = value
                with self.assertRaises(SDKError):
                    self.validate()
                self.plan['resources'][key] = original

    def test_first_full_loader_accepts_only_exact_input_routes(self):
        fixture = test_bootstrap_pilot.PilotTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.plan['format'] = recipe.BOOTSTRAP_FORMAT
        fixture.seed.descriptor['make_binding'] = {'synthetic-bound-dispatch': True}
        fixture.seal()
        fixture.seed.acceptance['make_binding'] = fixture.seed.descriptor['make_binding']
        fixture.seed.acceptance_ref = fixture.seed.native.document(fixture.seed.acceptance)
        # The real bound validator is exercised above. Isolate only admission
        # dispatch here, without manufacturing a qualified Darwin compiler.
        with patch('emberbsd_sdk.bootstrap_seed._plan', return_value=fixture.plan), \
                patch('emberbsd_sdk.bootstrap_seed._seed_commands', return_value=fixture.seed.commands), \
                patch('emberbsd_sdk.bootstrap_seed._execution_audit') as executed:
            fixture.seed.load(required_roles=('cc', 'cxx'))
            executed.assert_not_called()
            audit = json.loads(Path(fixture.seed.acceptance['input_route_audit']['path']).read_text())
            audit['expected_commands'] = []
            fixture.seed.acceptance['input_route_audit'] = fixture.seed.native.document(audit)
            fixture.seed.acceptance_ref = fixture.seed.native.document(fixture.seed.acceptance)
            with self.assertRaisesRegex(SDKError, 'not independently accepted'):
                fixture.seed.load(required_roles=('cc', 'cxx'))

    def test_legacy_unbound_full_plan_keeps_execution_audit_gate(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as base:
            seed = test_bootstrap_seed.SeedFixture(Path(base))
            seed.acceptance.pop('execution_audit')
            seed.acceptance['evidence_level'] = 'input-and-route-only'
            seed.acceptance_ref = seed.native.document(seed.acceptance)
            with self.assertRaisesRegex(SDKError, 'execution_audit'):
                seed.load()

    def test_controller_reuses_fences_deadline_and_pending_output_result(self):
        observed = []
        def inspect(argv, log, **kwargs):
            for context in (str(self.work), str(self.f.bundle), self.plan['resources']['heavy_link_context']):
                lock = self.base / ('.prebuild-sdk-lock-' + hashlib.sha256(context.encode()).hexdigest())
                with lock.open('rb') as handle:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            observed.append(argv)
            self.assertLessEqual(kwargs['timeout_seconds'], 43200)
            self.assertGreater(kwargs['timeout_seconds'], 0)
            return self.fake_command(argv, log, **kwargs)
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=inspect):
            result = self.invoke()
        self.assertEqual(len(observed), 5)
        self.assertEqual(result['status'], 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW')
        self.assertFalse(result['ordinary_provider_qualified'])
        self.assertTrue(result['all_commands_drained'] and result['inputs_revalidated_after_drain'] and
                         result['all_leases_closed'])
        self.assertEqual(result['stage'], 'full-recipe-bootstrap')
        self.assertNotIn('stage_comparison', result)
        self.assertNotIn('all_leases_closed', json.loads((self.work / 'result.json').read_text()))
        self.assertEqual(len(json.loads(Path(result['output_inventory']['path']).read_text())), 1)
        for relative, ref in recipe.recipe_distfiles(self.plan).items():
            self.assertEqual((self.work / 'distfiles' / relative).read_bytes(), Path(ref['path']).read_bytes())

    def test_failed_recipe_does_not_retry_or_report_completion(self):
        def fail(argv, log, **kwargs):
            Path(log).write_bytes(b'synthetic real child failure\n')
            raise CommandError(2, argv, log=log)
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=fail) as run:
            result = self.invoke()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(result['commands'][0]['actual_exit'], 2)
        self.assertTrue(result['all_commands_drained'])

    def test_failed_drain_holds_whole_recipe_and_input_fences(self):
        observed = []
        class EndFixture(BaseException):
            pass
        def fail(argv, log, **kwargs):
            Path(log).write_bytes(b'unstable synthetic log\n')
            raise ProcessOwnershipError(1234567, 'synthetic unproven drain', log=log)
        def inspect_hold():
            data = json.loads((self.work / 'result.json').read_text())
            self.assertEqual(data['status'], 'HOLD_UNPROVEN_DRAIN')
            self.assertNotIn('log', data['commands'][-1])
            for context in (str(self.work), str(self.f.bundle), self.plan['resources']['heavy_link_context']):
                lock = self.base / ('.prebuild-sdk-lock-' + hashlib.sha256(context.encode()).hexdigest())
                with lock.open('rb') as handle:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            observed.append('all fences held')
            raise EndFixture()
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=fail), \
                patch('emberbsd_sdk.bootstrap_pilot._hold', side_effect=inspect_hold):
            self.invoke()
        self.assertEqual(observed, ['all fences held'])

    def test_historical_plan_keeps_pins_without_reopening_retired_payloads(self):
        self.assertEqual(self.validate(inspect_inputs=False), self.plan)
        with patch.object(producer, '_current_host_facts', side_effect=AssertionError('no retired host read')), \
                patch.object(producer, '_utility_directory', side_effect=AssertionError('no retired payload read')), \
                patch.object(producer, 'snapshot_tree', side_effect=AssertionError('no retired export read')):
            self.assertEqual(self.validate(inspect_inputs=False), self.plan)




def real_compare_fixture(argv):
    """Check GNU make stamp scheduling, not GCC object equivalence."""
    import argparse
    parser = argparse.ArgumentParser(description=real_compare_fixture.__doc__)
    parser.add_argument('work', type=Path)
    parser.add_argument('--gmake-arg', action='append', required=True)
    args = parser.parse_args(argv)
    words = args.gmake_arg
    if (len(words) != 3 or not os.path.isabs(words[0]) or
            not words[1].startswith('SHELL=/') or not words[2].startswith('MAKECONF=/')):
        raise AssertionError('Pass the complete explicit selected gmake/SHELL/MAKECONF tuple')
    if not args.work.is_absolute() or args.work.exists() or args.work.is_symlink():
        raise AssertionError('Use a fresh absolute source-test work directory')
    args.work.mkdir()
    empty = args.work / 'empty-bin'
    empty.mkdir()
    for stage in ('stage2-gcc', 'stage3-gcc'):
        (args.work / stage).mkdir()
        (args.work / stage / 'fixture.o').write_text('same\n')
    (args.work / 'compare').write_text('timestamp\n')
    makefile = args.work / 'fixture.mk'
    makefile.write_text(
        '.PHONY: compile\ncompile:\n\t@echo "unexpected compile" >> compile.calls\n'
        'compare:\n'
        '\t@if test -f stage2-lean; then echo "Cannot compare object files as stage 2 was deleted."; exit 0; fi; \\\n'
        '\techo "compared" >> compare.calls; \\\n'
        '\tIFS= read -r first < stage2-gcc/fixture.o; \\\n'
        '\tIFS= read -r second < stage3-gcc/fixture.o; \\\n'
        '\ttest "$$first" = "$$second"\n')
    plan = dict(work_root=str(args.work), ports_export=dict(root=str(args.work / 'unused-export')),
                resources=dict(cpu_jobs=1), recipes={'lang/gcc16':dict(object_directory=str(args.work))})
    with patch.object(recipe, 'recipe_order', return_value=['lang/gcc16']), \
         patch.object(recipe, 'recipe_make', return_value=('not-executed',)), \
         patch.object(recipe, 'recipe_environment', return_value={}), \
         patch.object(recipe, 'recipe_gmake', return_value=tuple(words)):
        step = recipe.recipe_steps(plan, {}, {}, {})[1]
    fixed = [*step['argv'][:-1], '-rR', '-f', str(makefile), step['argv'][-1]]
    old = [word for word in fixed if word != '--eval=.PHONY: compare']
    env = dict(PATH=str(empty), LC_ALL='C', TZ='UTC', TMPDIR=str(args.work),
               SHELL=words[1].removeprefix('SHELL='))
    observations = []
    def run(name, command):
        result = subprocess.run(command, cwd=args.work, env=env, capture_output=True, timeout=60)
        stdout, stderr = args.work / (name + '.stdout'), args.work / (name + '.stderr')
        stdout.write_bytes(result.stdout)
        stderr.write_bytes(result.stderr)
        observations.append(dict(name=name, argv=command, exit=result.returncode,
                                 stdout=str(stdout), stderr=str(stderr)))
        return result
    assert run('old-stamped', old).returncode == 0
    calls = args.work / 'compare.calls'
    assert not calls.exists(), 'The baseline must reproduce the stamped no-op'
    assert run('new-equal', fixed).returncode == 0 and calls.read_bytes() == b'compared\n'
    (args.work / 'stage3-gcc/fixture.o').write_text('different\n')
    assert run('new-different', fixed).returncode == 2
    assert calls.read_bytes() == b'compared\ncompared\n'
    (args.work / 'stage2-lean').write_text('timestamp\n')
    lean = run('new-lean-control', fixed)
    assert lean.returncode == 0 and b'Cannot compare object files' in lean.stdout
    assert calls.read_bytes() == b'compared\ncompared\n'
    assert not (args.work / 'compile.calls').exists()
    result = dict(status='PASS_SOURCE_SCHEDULING_TEST', observations=observations,
                  old_stamp_skips_comparison=True, fixed_target_detects_difference=True,
                  lean_success_is_not_a_comparison=True, compilation_performed=False,
                  ordinary_provider_qualified=False)
    (args.work / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))


if __name__ == '__main__':
    if any(word.startswith('--gmake-arg') for word in sys.argv[1:]):
        real_compare_fixture(sys.argv[1:])
    else:
        unittest.main()
