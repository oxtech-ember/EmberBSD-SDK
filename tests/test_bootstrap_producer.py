# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Finite metadata controls. All producer children are synthetic."""

import copy
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_bootstrap_make
import test_bootstrap_pilot
from emberbsd_sdk import bootstrap_binding, bootstrap_pilot, bootstrap_producer as producer
from emberbsd_sdk.bootstrap_seed import _plan
from emberbsd_sdk.errors import CommandError, SDKError
from emberbsd_sdk.inventory import snapshot_tree


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_bootstrap_make.MakeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.seed = self.fixture.seed
        self.base, self.f = self.fixture.base, self.seed.native
        self.descriptor = self.seed.descriptor
        self.descriptor['host'] = dict(os='Darwin', cpu='arm64')
        self.facts = dict(os='Darwin', cpu='aarch64', kernel_version='27.0.0',
                          product_version='27.0', pkgsrc_opsys_version='270000')
        self.tools = sorted(producer.EARLY_TOOLS)
        for role in [*self.tools, 'pkg_info', 'pkg_admin', 'dsymutil']:
            path = self.f.bundle / role
            path.write_bytes(self.f.image)
            path.chmod(0o755)
            self.descriptor['tools'][role] = dict(path=str(path), sha256=hashlib.sha256(self.f.image).hexdigest(),
                                                kind='native', provider='ports', version='synthetic')
        self.utility_bin = self.f.bundle / 'utilities'
        self.utility_bin.mkdir()
        for role in self.tools:
            (self.utility_bin / role).symlink_to('../' + role)
        binding = dict(sdk_version='26.5', deployment='26.0',
                       sdk_metadata=dict(path=str(self.base / 'headers/SDKSettings.json')))
        self.export = self.seed.export
        profile = self.export / producer.PROFILE_EXPORT
        profile.write_bytes(b'# source-owner profile fixture; never executed\n')
        self.profile_sha = hashlib.sha256(profile.read_bytes()).hexdigest()
        self.profile_patch = patch.object(producer, 'PROFILE_SHA256', self.profile_sha)
        self.profile_patch.start()
        self.addCleanup(self.profile_patch.stop)
        self.host_patch = patch.object(producer, '_current_host_facts', return_value=self.facts)
        self.host_patch.start()
        self.addCleanup(self.host_patch.stop)
        config = self.f.raw(b'export configuration bytes\n')
        exporter = self.f.raw(b'# canonical producer source fixture\n')
        self.export_inventory = self.f.document(snapshot_tree(str(self.export)))
        pkgsrc = dict(repository='https://example.invalid/pkgsrc', commit='2' * 40)
        self.manifest = {'upstream/pkgsrc': dict(kind='gitlink', commit=pkgsrc['commit']),
                         'pkgsrc/export.conf': dict(kind='file', sha256=config['sha256']),
                         'scripts/prepare-pkgsrc.py': dict(kind='file', sha256=exporter['sha256']),
                         producer.PROFILE_RELATIVE: dict(kind='file', sha256=self.profile_sha)}
        self.proof = dict(format='emberbsd-ports-export-source-proof', version=1,
                          ports_source=self.descriptor['sources']['ports'], pkgsrc_source=pkgsrc,
                          ports_manifest=self.f.document(self.manifest), inventory=self.export_inventory,
                          exporter=dict(relative='scripts/prepare-pkgsrc.py', file=exporter),
                          export_config=dict(relative='pkgsrc/export.conf', file=config))
        conf = self.f.raw(b'placeholder\n')
        self.descriptor['make_binding']['makeconf'] = conf
        self.commands = bootstrap_binding.seed_commands(self.descriptor, inspect_inputs=False)
        # This fixture exercises metadata routing after selection. Existing
        # binding/native tests cover live loader qualification separately.
        self.descriptor['driver_binding'] = binding
        self.work = self.base / 'metadata-work'
        required = set(producer.EARLY_TOOLS) | {'make', 'gmake', 'sh', 'pkg_info', 'pkg_admin', 'cc', 'cxx',
                    'cpp', 'as', 'ld', 'dsymutil', 'ar', 'ranlib', 'nm', 'strip'}
        self.plan = dict(format=producer.PRODUCER_FORMAT, version=1, scope='gcc16-bootstrap', stage='metadata',
                         host=self.descriptor['host'], sources=self.descriptor['sources'], work_root=str(self.work),
                         required_roles=sorted(required), seed_descriptor={},
                         resources=dict(cpu_jobs=2, link_jobs=1, runtime_seconds=1200,
                                        lock_root=str(self.base), heavy_link_context=str(self.base / 'heavy')),
                         metadata=dict(export=dict(root=str(self.export), inventory=self.export_inventory,
                                                   source_proof=self.f.document(self.proof)),
                                       profile=dict(path=str(profile), sha256=self.profile_sha), makeconf=conf,
                                       host_facts=self.facts, utility_bin=str(self.utility_bin),
                                       native_tools=self.tools, source_tools=[], recipes=['lang/gcc16']))
        raw = producer.metadata_makeconf(self.plan, self.descriptor, self.commands, self.descriptor['abi_flags'])
        Path(conf['path']).write_bytes(raw)
        conf['sha256'] = hashlib.sha256(raw).hexdigest()
        self.plan['commands'] = producer.producer_steps(self.plan, self.descriptor, self.commands,
                                                       self.descriptor['abi_flags'])

    def validate(self):
        return producer.validate_producer_plan(self.plan, self.descriptor, self.commands,
                                               self.descriptor['abi_flags'])

    def invoke(self):
        descriptor_ref = self.f.document(self.descriptor)
        self.plan['seed_descriptor'] = descriptor_ref
        plan_ref = self.f.document(self.plan)
        acceptance = self.f.document(dict(input_route_audit=self.f.raw(b'synthetic admission fixture')))
        case = self
        class Selection:
            commands = case.commands
            def as_dict(self):
                return dict(plan=case.validate())
        with patch('emberbsd_sdk.bootstrap_seed.load_bootstrap_seed', side_effect=lambda *a, **kw: Selection()):
            return bootstrap_pilot.run_producer(descriptor_ref['path'], descriptor_sha256=descriptor_ref['sha256'],
                     accepted_seed_receipt=acceptance, bootstrap_plan=plan_ref, required_roles=self.plan['required_roles'])

    def fake_command(self, argv, log, **kwargs):
        Path(log).write_bytes(b'synthetic raw metadata observations\n')
        return subprocess.CompletedProcess(argv, 0)

    def test_admission_needs_current_inputs_but_no_future_dag_or_output(self):
        with patch('subprocess.Popen', side_effect=AssertionError('no native process')):
            self.assertIs(self.validate(), self.plan)
            self.assertEqual(_plan(self.f.document(self.plan), self.descriptor, self.commands,
                                   self.descriptor['abi_flags']), self.plan)
        self.assertNotIn('recipes', self.plan)
        self.assertNotIn('output_bundle_root', self.plan)
        for row in self.plan['commands']:
            self.assertEqual(row['argv'][:len(self.commands['make'])], list(self.commands['make']))
            self.assertLess(row['argv'].index('EMBERBSD_DARWIN_BOOTSTRAP=yes'), row['argv'].index('-C'))
            self.assertEqual(row['env']['PATH'], str(self.utility_bin))
            self.assertEqual(row['env']['TMPDIR'], str(self.work))
        variables = set(producer.VARIABLES)
        self.assertEqual(len(producer.VARIABLES), 56)
        self.assertIn('BOOTSTRAP_DEPENDS', variables)
        self.assertTrue(set('WRKDIR WRKSRC OBJDIR CONFIGURE_DIRS BUILD_DIRS MAKE_DIRS DISTFILES '
                            'EXTRACT_ONLY MASTER_SITES DIST_SUBDIR PATCHFILES PATCH_SITES '
                            'MACHINE_GNU_PLATFORM GCC_TARGET_MACHINE'.split()) <= variables)
        argv = self.plan['commands'][0]['argv']
        self.assertEqual(argv[argv.index('-C') + 2:],
                         [word for name in producer.VARIABLES for word in ('-V', name + '=${' + name + '}')])
        self.assertNotIn('show-vars', argv)
        self.assertNotIn('show-vars-eval', argv)
        self.assertEqual(argv.count('BOOTSTRAP_DEPENDS=${BOOTSTRAP_DEPENDS}'), 1)

    def test_gcc_configure_uname_is_the_declared_native_utility(self):
        raw = producer.metadata_makeconf(self.plan, self.descriptor, self.commands,
                                         self.descriptor['abi_flags'])
        expected = ('EMBERBSD_GCC16_UNAME= ' + self.commands['uname'][0] + '\n').encode()
        self.assertEqual(raw.count(expected), 1)
        self.assertLess(raw.index(expected), raw.index(b'.include '))
        self.assertIn('uname', self.plan['metadata']['native_tools'])
        self.assertIs(self.validate(), self.plan)

    def test_full_recipe_environment_keeps_sdk_diagnostics_for_reconfigure(self):
        plan = copy.deepcopy(self.plan)
        plan.update(format='emberbsd-native-gcc16-bootstrap-plan', output_bundle_root=str(self.work / 'prefix'))
        env = producer.metadata_environment(plan, self.commands)
        self.assertEqual(env['GCC_CONFIGURE_SDK'], 'yes')
        self.assertEqual(env['GCC_CONFIGURE_UNAME'], self.commands['uname'][0])
        self.assertNotIn('GCC_CONFIGURE_SDK', producer.metadata_environment(self.plan, self.commands))

    def test_outside_uname_still_refuses_after_recomputed_makeconf(self):
        self.commands['uname'] = ('/usr/bin/uname',)
        raw = producer.metadata_makeconf(self.plan, self.descriptor, self.commands,
                                         self.descriptor['abi_flags'])
        conf = self.plan['metadata']['makeconf']
        Path(conf['path']).write_bytes(raw)
        conf['sha256'] = hashlib.sha256(raw).hexdigest()
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged') as child:
            with self.assertRaisesRegex(SDKError, 'exact selected spelling'):
                self.invoke()
        child.assert_not_called()
        self.assertFalse(self.work.exists())

    def test_gcc_configure_uname_missing_tail_or_outside_refuses_before_children(self):
        for change in ('missing', 'tail', 'outside'):
            with self.subTest(change=change):
                original = self.commands['uname']
                if change == 'missing':
                    del self.commands['uname']
                elif change == 'tail':
                    self.commands['uname'] = (*original, '-s')
                else:
                    self.commands['uname'] = ('/usr/bin/uname',)
                with patch('emberbsd_sdk.bootstrap_pilot.run_logged') as child:
                    with self.assertRaises(SDKError):
                        self.invoke()
                child.assert_not_called()
                self.assertFalse(self.work.exists())
                self.commands['uname'] = original

    def test_metadata_phase_is_explicit_before_profile_include(self):
        values = producer.metadata_assignments(self.plan, self.descriptor, self.commands,
                                              self.descriptor['abi_flags'])
        self.assertEqual(values['EMBERBSD_DARWIN_METADATA'], 'yes')
        raw = producer.metadata_makeconf(self.plan, self.descriptor, self.commands,
                                         self.descriptor['abi_flags'])
        self.assertEqual(raw.splitlines().count(b'EMBERBSD_DARWIN_METADATA= yes'), 1)
        self.assertLess(raw.index(b'EMBERBSD_DARWIN_METADATA= yes\n'), raw.index(b'.include '))

    def test_metadata_phase_requires_producer_format(self):
        commands = dict(self.commands, pkg_add=('/selected/pkg_add',),
                        pkg_create=('/selected/pkg_create',))
        for kind in ('emberbsd-native-gcc16-bootstrap-plan', 'other-format', None):
            with self.subTest(format=kind):
                plan = copy.deepcopy(self.plan)
                if kind is None:
                    del plan['format']
                else:
                    plan['format'] = kind
                values = producer.metadata_assignments(plan, self.descriptor, commands,
                                                      self.descriptor['abi_flags'])
                self.assertNotIn('EMBERBSD_DARWIN_METADATA', values)
                raw = producer.metadata_makeconf(plan, self.descriptor, commands,
                                                 self.descriptor['abi_flags'])
                self.assertNotIn(b'EMBERBSD_DARWIN_METADATA=', raw)

    def test_metadata_phase_requires_metadata_stage(self):
        for stage in ('utility-relocation', 'bsdinstall-bootstrap', None):
            with self.subTest(stage=stage):
                plan = copy.deepcopy(self.plan)
                if stage is None:
                    del plan['stage']
                else:
                    plan['stage'] = stage
                values = producer.metadata_assignments(plan, self.descriptor, self.commands,
                                                      self.descriptor['abi_flags'])
                self.assertNotIn('EMBERBSD_DARWIN_METADATA', values)
                raw = producer.metadata_makeconf(plan, self.descriptor, self.commands,
                                                 self.descriptor['abi_flags'])
                self.assertNotIn(b'EMBERBSD_DARWIN_METADATA=', raw)

    def test_metadata_makeconf_restores_both_make_values_after_profile(self):
        raw = producer.metadata_makeconf(self.plan, self.descriptor, self.commands,
                                         self.descriptor['abi_flags'])
        make = shlex.join(self.commands['make'])
        expected = ('.include "' + self.plan['metadata']['profile']['path'] + '"\n'
                    '_MAKE= ' + make + '\nMAKE= ' + make + '\n').encode()
        self.assertTrue(raw.endswith(expected))
        self.assertEqual(raw.splitlines().count(('_MAKE= ' + make).encode()), 1)
        self.assertEqual(raw.splitlines().count(('MAKE= ' + make).encode()), 1)

    def test_recursive_metadata_binding_does_not_change_other_formats_or_stages(self):
        values = dict(EMBERBSD_DARWIN_MAKE=shlex.join(self.commands['make']), LDFLAGS='')
        for kind, stage in [('emberbsd-native-gcc16-bootstrap-plan', 'metadata'),
                            (producer.PRODUCER_FORMAT, 'utility-relocation'),
                            (producer.PRODUCER_FORMAT, None)]:
            with self.subTest(format=kind, stage=stage):
                plan = copy.deepcopy(self.plan)
                plan.update(format=kind, stage=stage)
                with patch.object(producer, 'metadata_assignments', return_value=values):
                    raw = producer.metadata_makeconf(plan, self.descriptor, self.commands, {})
                self.assertFalse(any(line.startswith((b'_MAKE=', b'MAKE='))
                                     for line in raw.splitlines()))
                self.assertTrue(raw.endswith(('.include "' +
                                self.plan['metadata']['profile']['path'] + '"\n').encode()))

    def test_read_only_queries_bind_the_complete_recursive_make(self):
        override = 'MAKE=' + shlex.join(self.commands['make'])
        for step in self.plan['commands']:
            self.assertEqual(step['argv'].count(override), 1)
            self.assertLess(step['argv'].index(override), step['argv'].index('-C'))
            self.assertEqual(shlex.split(override.removeprefix('MAKE=')),
                             list(self.commands['make']))

    def test_recursive_make_tuple_reaches_a_synthetic_nested_query(self):
        commands = copy.deepcopy(self.commands)
        commands['make'] = (str(self.base / 'selected make'), '-m', str(self.base / 'mk files'),
                            'SHELL=' + str(self.base / 'selected shell'),
                            'MAKECONF=' + str(self.base / 'mk config'))
        with patch.object(producer, 'metadata_environment', return_value={}):
            steps = producer.producer_steps(self.plan, self.descriptor, commands,
                                            self.descriptor['abi_flags'])
        override = next(word for word in steps[0]['argv'] if word.startswith('MAKE='))
        nested = shlex.split(override.removeprefix('MAKE=')) + ['show-var', 'VARNAME=PKGNAME']
        # The child receives argv as data. It does not execute a selected make.
        code = 'import json,sys; print(json.dumps(sys.argv[1:]))'
        result = subprocess.run([sys.executable, '-B', '-c', code, *nested],
                                check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout),
                         [*commands['make'], 'show-var', 'VARNAME=PKGNAME'])
        self.assertEqual(result.stderr, '')

    def test_query_without_recursive_make_binding_is_refused(self):
        for step in self.plan['commands']:
            step['argv'] = [word for word in step['argv'] if not word.startswith('MAKE=')]
        with self.assertRaisesRegex(SDKError, 'fixed source-owned query'):
            self.validate()

    def test_current_source_refuses_legacy_producer_commands(self):
        # Retain historical plans with their exact old SDK source. The new
        # parser can read accepted old logs without admitting old live queries.
        for step in self.plan['commands']:
            if step['id'].endswith('-vars'):
                self.assertEqual(step['argv'][-2:], ['-V', 'BOOTSTRAP_DEPENDS=${BOOTSTRAP_DEPENDS}'])
                del step['argv'][-2:]
        with self.assertRaisesRegex(SDKError, 'fixed source-owned query'):
            self.validate()

    def test_parallel_profile_histories_emit_the_same_budget_and_launcher(self):
        original = copy.deepcopy(self.plan)
        outputs = []
        for digest in (producer.PARALLEL_PROFILE_SHA256, producer.BUILTIN_PROFILE_SHA256,
                       producer.LIBTOOL_PROFILE_SHA256, producer.RANLIB_PROFILE_SHA256):
            self.plan['metadata']['profile']['sha256'] = digest
            self.plan['resources']['cpu_jobs'] = 18
            self.plan['link_control'] = dict(
                python=dict(path='/explicit/control-python', sha256='1' * 64),
                launcher=dict(path='/explicit/sdk/scripts/link-launcher.py', sha256='2' * 64))
            values = producer.metadata_assignments(self.plan, self.descriptor, self.commands,
                                                  self.descriptor['abi_flags'])
            self.assertEqual(values['EMBERBSD_DARWIN_CPU_BUDGET'], '18')
            self.assertIn('/explicit/control-python', values['EMBERBSD_GCC16_LINKER_LAUNCHER'])
            outputs.append(producer.metadata_makeconf(self.plan, self.descriptor, self.commands,
                                                     self.descriptor['abi_flags']))
        self.assertTrue(all(value == outputs[0] for value in outputs))
        self.plan = original
        values = producer.metadata_assignments(self.plan, self.descriptor, self.commands,
                                              self.descriptor['abi_flags'])
        self.assertNotIn('EMBERBSD_DARWIN_CPU_BUDGET', values)
        self.assertNotIn('EMBERBSD_GCC16_LINKER_LAUNCHER', values)

    def _check_parallel_profile_job_gate(self, constant):
        with patch.object(producer, 'PROFILE_SHA256', 'f' * 64), \
             patch.object(producer, constant, self.profile_sha):
            with self.assertRaisesRegex(SDKError, 'full-plan link control'):
                self.validate()
            self.plan['resources']['cpu_jobs'] = 1
            conf = self.plan['metadata']['makeconf']
            raw = producer.metadata_makeconf(self.plan, self.descriptor, self.commands,
                                             self.descriptor['abi_flags'])
            Path(conf['path']).write_bytes(raw)
            conf['sha256'] = hashlib.sha256(raw).hexdigest()
            self.plan['commands'] = producer.producer_steps(self.plan, self.descriptor, self.commands,
                                                           self.descriptor['abi_flags'])
            self.assertIs(self.validate(), self.plan)
            self.proof['ports_manifest'] = self.f.document({})
            self.plan['metadata']['export']['source_proof'] = self.f.document(self.proof)
            with self.assertRaises(SDKError):
                self.validate()

    def test_builtin_profile_retains_full_source_checks_and_metadata_job_gate(self):
        self._check_parallel_profile_job_gate('BUILTIN_PROFILE_SHA256')

    def test_libtool_profile_retains_full_source_checks_and_metadata_job_gate(self):
        self._check_parallel_profile_job_gate('LIBTOOL_PROFILE_SHA256')

    def test_ranlib_profile_retains_full_source_checks_and_metadata_job_gate(self):
        self._check_parallel_profile_job_gate('RANLIB_PROFILE_SHA256')

    def test_host_facts_recipe_and_resource_changes_are_refused(self):
        for kind in ('host', 'stage', 'recipe', 'jobs', 'timeout'):
            with self.subTest(kind=kind):
                original = copy.deepcopy(self.plan)
                if kind == 'host':
                    self.plan['metadata']['host_facts'] = dict(self.facts, product_version='26.5')
                elif kind == 'stage':
                    self.plan['stage'] = 'build'
                elif kind == 'recipe':
                    self.plan['metadata']['recipes'] = ['lang/gcc16', '../outside']
                elif kind == 'jobs':
                    self.plan['resources']['cpu_jobs'] = 5
                else:
                    self.plan['resources']['runtime_seconds'] = True
                with self.assertRaises(SDKError):
                    self.validate()
                self.plan = original

    def test_export_and_source_chain_changes_are_refused(self):
        for kind in ('export', 'gitlink', 'ports', 'profile', 'producer', 'config-as-producer'):
            with self.subTest(kind=kind):
                original = copy.deepcopy(self.proof)
                if kind == 'export':
                    self.proof['inventory'] = self.f.document({'fake': {}})
                elif kind == 'gitlink':
                    self.proof['pkgsrc_source']['commit'] = '3' * 40
                elif kind == 'ports':
                    self.proof['ports_source'] = dict(self.proof['ports_source'], commit='3' * 40)
                elif kind == 'profile':
                    manifest = copy.deepcopy(self.manifest)
                    manifest[producer.PROFILE_RELATIVE]['sha256'] = '0' * 64
                    self.proof['ports_manifest'] = self.f.document(manifest)
                elif kind == 'producer':
                    self.proof['exporter']['relative'] = 'scripts/arbitrary-hook.py'
                else:
                    self.proof['exporter'] = copy.deepcopy(self.proof['export_config'])
                self.plan['metadata']['export']['source_proof'] = self.f.document(self.proof)
                with self.assertRaises(SDKError):
                    self.validate()
                self.proof = original

    def test_changed_export_file_is_refused_before_child(self):
        (self.export / 'lang/gcc16/Makefile').write_bytes(b'changed source\n')
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged') as run:
            with self.assertRaisesRegex(SDKError, 'canonical export changed'):
                self.invoke()
        run.assert_not_called()
        self.assertFalse(self.work.exists())

    def test_makeconf_cannot_hide_or_duplicate_selection(self):
        conf = self.plan['metadata']['makeconf']
        Path(conf['path']).write_bytes(Path(conf['path']).read_bytes() + b'CC=/usr/bin/cc\n')
        conf['sha256'] = hashlib.sha256(Path(conf['path']).read_bytes()).hexdigest()
        with self.assertRaisesRegex(SDKError, 'MAKECONF differs'):
            self.validate()

    def _recipe_utility_plan(self, recipes, roles):
        for name in recipes:
            (self.export / name).mkdir(parents=True, exist_ok=True)
            (self.export / name / 'Makefile').write_bytes(b'# recipe fixture; never executed\n')
        for role in roles:
            path = self.f.bundle / role
            path.write_bytes(self.f.image)
            path.chmod(0o755)
            self.descriptor['tools'][role] = dict(path=str(path), sha256=hashlib.sha256(self.f.image).hexdigest(),
                                                kind='native', provider='ports', version='synthetic')
            (self.utility_bin / role).symlink_to('../' + role)
            self.commands[role] = type(self.commands['grep'])([str(path)])
        self.proof['inventory'] = self.f.document(snapshot_tree(str(self.export)))
        data = self.plan['metadata']
        data['export'] = dict(root=str(self.export), inventory=self.proof['inventory'],
                              source_proof=self.f.document(self.proof))
        data['native_tools'] = sorted([*self.tools, *roles])
        data['recipes'] = sorted(['lang/gcc16', *recipes])
        self.plan['required_roles'] = sorted({*self.plan['required_roles'], *roles})
        conf = data['makeconf']
        raw = producer.metadata_makeconf(self.plan, self.descriptor, self.commands, self.descriptor['abi_flags'])
        Path(conf['path']).write_bytes(raw)
        conf['sha256'] = hashlib.sha256(raw).hexdigest()
        self.plan['commands'] = producer.producer_steps(self.plan, self.descriptor, self.commands,
                                                       self.descriptor['abi_flags'])

    def test_meson_and_ninja_closures_need_their_bare_utilities(self):
        recipes = ['devel/meson', 'devel/ninja-build']
        for missing, recipe in (('od', 'devel/ninja-build'), ('lipo', 'devel/meson')):
            with self.subTest(missing=missing):
                self.setUp()
                self._recipe_utility_plan(recipes, [role for role in ('lipo', 'od') if role != missing])
                with self.assertRaisesRegex(SDKError, f'{recipe} needs native utility {missing}'):
                    self.validate()
        self.setUp()
        self._recipe_utility_plan(recipes, ['lipo', 'od'])
        self.assertIs(self.validate(), self.plan)
        self.descriptor['tools']['od']['kind'] = 'script'
        with self.assertRaisesRegex(SDKError, 'devel/ninja-build needs native utility od'):
            self.validate()
        # Closures without these recipes keep their existing roster.
        self.setUp()
        self._recipe_utility_plan(['devel/pcre2'], [])
        self.assertIs(self.validate(), self.plan)
        self.assertEqual(producer.RECIPE_UTILITIES, {'devel/meson': ('lipo',), 'devel/ninja-build': ('od',)})

    def test_missing_extra_or_wrong_utility_alias_is_refused(self):
        path = self.utility_bin / 'grep'
        path.unlink()
        with self.assertRaisesRegex(SDKError, 'missing or extra'):
            self.validate()
        path.symlink_to('../sed')
        with self.assertRaisesRegex(SDKError, 'exact selected spelling'):
            self.validate()
        path.unlink()
        path.symlink_to('../grep')
        (self.utility_bin / 'unselected').symlink_to('../grep')
        with self.assertRaisesRegex(SDKError, 'missing or extra'):
            self.validate()

    def test_bare_make_extra_build_target_and_env_override_are_refused(self):
        for kind in ('bare', 'build', 'env'):
            with self.subTest(kind=kind):
                original = copy.deepcopy(self.plan['commands'])
                if kind == 'bare':
                    self.plan['commands'][0]['argv'] = [self.commands['make'][0], '-C', str(self.export)]
                elif kind == 'build':
                    self.plan['commands'][0]['argv'].append('all')
                else:
                    self.plan['commands'][0]['env']['BASH_ENV'] = '/ambient'
                with self.assertRaisesRegex(SDKError, 'fixed source-owned query'):
                    self.validate()
                self.plan['commands'] = original

    def test_fake_execution_uses_existing_controller_and_retains_raw_pending_result(self):
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=self.fake_command) as run:
            result = self.invoke()
        self.assertEqual(run.call_count, 6)
        self.assertEqual(result['status'], 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW')
        self.assertTrue(result['all_commands_drained'] and result['all_leases_closed'])
        self.assertFalse(result['ordinary_provider_qualified'])
        self.assertTrue(all(row['actual_exit'] == 0 for row in result['commands']))
        self.assertNotIn('all_leases_closed', json.loads((self.work / 'result.json').read_text()))
        self.assertTrue(all(0 < call.kwargs['timeout_seconds'] <= 1200 for call in run.call_args_list))
        self.assertFalse((self.work / 'native.c').exists())

    def test_actual_metadata_failure_is_not_retried_or_accepted(self):
        def fail(argv, log, **kwargs):
            Path(log).write_bytes(b'synthetic missing source provider\n')
            raise CommandError(2, argv, log=log)
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=fail) as run:
            result = self.invoke()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(result['commands'][0]['actual_exit'], 2)
        self.assertTrue(result['all_commands_drained'])

    def test_loader_dispatch_uses_input_route_acceptance_before_future_outputs(self):
        fixture = test_bootstrap_pilot.PilotTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.plan['format'] = producer.PRODUCER_FORMAT
        fixture.plan['stage'] = 'metadata'
        fixture.seal()
        # Exercise loader dispatch separately from the metadata validator above.
        with patch.object(producer, 'validate_producer_plan', return_value=fixture.plan) as validate, \
                patch('emberbsd_sdk.bootstrap_seed._execution_audit') as executed, \
                patch('subprocess.Popen', side_effect=AssertionError('no native child')):
            selected = fixture.seed.load(required_roles=('cc', 'cxx'))
        validate.assert_called_once()
        executed.assert_not_called()
        self.assertEqual(selected.as_dict()['plan']['stage'], 'metadata')


if __name__ == '__main__':
    unittest.main()
