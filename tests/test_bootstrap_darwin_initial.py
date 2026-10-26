# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Initial Darwin operations retain the full source graph. No real child runs."""
import copy
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import unittest
from unittest.mock import patch

import test_bootstrap_producer
import test_bootstrap_recipe
import test_bootstrap_seed
from emberbsd_sdk import bootstrap_producer as producer, bootstrap_recipe as recipe
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.inventory import snapshot_tree


class InitialDarwinTests(unittest.TestCase):
    def metadata(self, *, profile_constant='INITIAL_DARWIN_PROFILE_SHA256'):
        case = test_bootstrap_producer.MetadataTests(methodName='runTest')
        case.setUp()
        self.addCleanup(case.doCleanups)
        guard = patch.object(producer, profile_constant, case.profile_sha, create=True)
        guard.start()
        self.addCleanup(guard.stop)
        case.plan['resources']['cpu_jobs'] = 1
        case.plan['metadata']['darwin_tool_phase'] = 'initial'
        for name in ('devel/cmake', 'lang/llvm'):
            path = case.export / name
            path.mkdir(parents=True)
            (path / 'Makefile').write_text('# Source-only fixture. Never executed.\n')
        case.plan['metadata']['recipes'] = ['devel/cmake', 'lang/gcc16', 'lang/llvm']
        inv = case.f.document(snapshot_tree(str(case.export)))
        case.proof['inventory'] = inv
        case.plan['metadata']['export'].update(inventory=inv, source_proof=case.f.document(case.proof))
        self.refresh(case)
        return case

    def refresh(self, case):
        conf = case.plan['metadata']['makeconf']
        raw = producer.metadata_makeconf(case.plan, case.descriptor, case.commands, case.descriptor['abi_flags'])
        Path(conf['path']).write_bytes(raw)
        conf['sha256'] = hashlib.sha256(raw).hexdigest()
        case.plan['commands'] = producer.producer_steps(case.plan, case.descriptor, case.commands,
                                                       case.descriptor['abi_flags'])

    def test_initial_metadata_needs_no_future_trio_for_its_fixed_queries(self):
        case = self.metadata()
        with patch('subprocess.Popen', side_effect=AssertionError('no child')):
            try:
                actual = case.validate()
            except SDKError as error:
                self.fail('Initial source-only metadata was refused: ' + str(error))
        self.assertIs(actual, case.plan)
        values = producer.metadata_assignments(case.plan, case.descriptor, case.commands,
                                              case.descriptor['abi_flags'])
        self.assertEqual(values.get('EMBERBSD_DARWIN_TOOL_PHASE'), 'initial')
        for name in ('LIBTOOL', 'INSTALL_NAME_TOOL', 'TAPI'):
            self.assertNotIn('EMBERBSD_DARWIN_' + name, values)

    def test_initial_phase_requires_its_exact_current_source_profile(self):
        case = self.metadata()
        with patch.object(producer, 'INITIAL_DARWIN_PROFILE_SHA256', '0' * 64):
            with self.assertRaisesRegex(SDKError, 'initial.*profile'):
                case.validate()

    def test_initial_phase_accepts_only_the_two_compatible_profile_identities(self):
        accepted = (producer.INITIAL_DARWIN_PROFILE_SHA256,
                    producer.PERL_DTRACE_PROFILE_SHA256)
        self.assertEqual(accepted, (
            'd54e703a263c307917f6b3d376e83bae04720aa74d1325d530faea50cfb6b765',
            'd61850c6dcd6ffa7c722a623912e00588a8de13317275ff91dea70bed300f1f9'))
        historical = {value for name, value in vars(producer).items()
                      if name.endswith('PROFILE_SHA256') and isinstance(value, str)}
        with patch('subprocess.Popen', side_effect=AssertionError('no child')):
            for digest in accepted:
                plan = dict(metadata=dict(darwin_tool_phase='initial', profile=dict(sha256=digest)))
                with self.subTest(accepted=digest):
                    self.assertEqual(producer.darwin_tool_phase(plan), 'initial')
            for digest in (historical - set(accepted)) | {'0' * 64}:
                plan = dict(metadata=dict(darwin_tool_phase='initial', profile=dict(sha256=digest)))
                with self.subTest(refused=digest), self.assertRaisesRegex(SDKError, 'initial.*profile'):
                    producer.darwin_tool_phase(plan)

    def test_perl_profile_passes_the_complete_initial_metadata_gate(self):
        with patch('subprocess.Popen', side_effect=AssertionError('no child')):
            case = self.metadata(profile_constant='PERL_DTRACE_PROFILE_SHA256')
            self.assertIs(case.validate(), case.plan)
            values = producer.metadata_assignments(case.plan, case.descriptor, case.commands,
                                                  case.descriptor['abi_flags'])
        self.assertEqual(values['EMBERBSD_DARWIN_TOOL_PHASE'], 'initial')
        for name in ('LIBTOOL', 'INSTALL_NAME_TOOL', 'TAPI'):
            self.assertNotIn('EMBERBSD_DARWIN_' + name, values)

    def test_unknown_phase_refuses_before_child(self):
        case = self.metadata()
        for value in ('', 'FULL', 'ar', True, ['initial']):
            with self.subTest(value=value):
                case.plan['metadata']['darwin_tool_phase'] = value
                with patch('subprocess.Popen', side_effect=AssertionError('no child')):
                    with self.assertRaisesRegex(SDKError, 'Darwin tool phase'):
                        producer.metadata_assignments(case.plan, case.descriptor, case.commands,
                                                      case.descriptor['abi_flags'])

    def test_full_default_still_requires_missing_future_trio(self):
        case = self.metadata()
        for value in ('full', None):
            if value is None:
                case.plan['metadata'].pop('darwin_tool_phase', None)
            else:
                case.plan['metadata']['darwin_tool_phase'] = value
            self.refresh(case)
            with self.subTest(value=value), self.assertRaisesRegex(SDKError, 'explicit native role: libtool_darwin'):
                case.validate()

    def initial_recipe(self, *, profile_constant='INITIAL_DARWIN_PROFILE_SHA256'):
        case = test_bootstrap_recipe.RecipeTests(methodName='runTest')
        case.setUp()
        self.addCleanup(case.doCleanups)
        plan = case.plan
        old = copy.deepcopy(plan['recipes']['lang/gcc16'])
        old['dependencies'] = []
        plan['recipes'] = {'lang/gcc16': old}
        for name, deps in [('devel/cmake', []), ('lang/llvm', ['devel/cmake']), ('devel/lld', ['lang/llvm'])]:
            plan['recipes'][name] = dict(old, directory=name, dependencies=deps)
        plan['recipes']['lang/gcc16']['dependencies'] = ['devel/lld']
        plan['metadata']['recipes'] = sorted(plan['recipes'])
        plan['metadata']['darwin_tool_phase'] = 'initial'
        guard = patch.object(producer, profile_constant,
                             case.plan['metadata']['profile']['sha256'])
        guard.start()
        self.addCleanup(guard.stop)
        plan['purpose'] = 'initial-darwin-tools'
        plan['comparison_command'] = 'initial-llvm-tools'
        plan['recipes']['lang/llvm']['object_directory'] = str(case.work / 'obj/llvm/build')
        return case

    def test_initial_commands_keep_dependency_prefix_and_exact_five_targets(self):
        case = self.initial_recipe()
        steps = recipe.recipe_steps(case.plan, case.descriptor, case.commands, case.descriptor['abi_flags'])
        self.assertEqual([row['recipe'] for row in steps], ['devel/cmake', 'devel/cmake', 'lang/llvm'])
        self.assertEqual([row['phase'] for row in steps], ['build', 'install', 'build-tools'])
        self.assertEqual(steps[-1]['argv'][-2:], ['BUILD_TARGET=llvm-libtool-darwin llvm-install-name-tool llvm-readtapi llvm-lipo llvm-otool', 'build'])
        self.assertNotIn('gcc16-compare', [row['id'] for row in steps])
        static_args = {'LLVM_BUILD_LLVM_DYLIB=OFF', 'LLVM_LINK_LLVM_DYLIB=OFF'}
        self.assertTrue(static_args <= set(steps[-1]['argv']))
        for row in steps[:-1]:
            self.assertFalse(static_args & set(row['argv']))
        self.assertEqual(set(case.plan['recipes']), {'lang/gcc16', 'devel/cmake', 'lang/llvm', 'devel/lld'})

    def test_full_recipe_commands_do_not_select_static_llvm(self):
        case = self.initial_recipe()
        case.plan['purpose'] = 'first-native-gcc16'
        case.plan['metadata']['darwin_tool_phase'] = 'full'
        steps = recipe.recipe_steps(case.plan, case.descriptor, case.commands, case.descriptor['abi_flags'])
        self.assertTrue(any(row['recipe'] == 'lang/llvm' for row in steps))
        for row in steps:
            self.assertNotIn('LLVM_BUILD_LLVM_DYLIB=OFF', row['argv'])
            self.assertNotIn('LLVM_LINK_LLVM_DYLIB=OFF', row['argv'])

    def test_perl_profile_keeps_initial_purpose_and_exact_target_command_gate(self):
        with patch('subprocess.Popen', side_effect=AssertionError('no child')):
            case = self.initial_recipe(profile_constant='PERL_DTRACE_PROFILE_SHA256')
            steps = recipe.recipe_steps(case.plan, case.descriptor, case.commands,
                                        case.descriptor['abi_flags'])
            self.assertEqual([(row['recipe'], row['phase']) for row in steps],
                             [('devel/cmake', 'build'), ('devel/cmake', 'install'),
                              ('lang/llvm', 'build-tools')])
            self.assertEqual(steps[-1]['argv'][-2:], [
                'BUILD_TARGET=llvm-libtool-darwin llvm-install-name-tool llvm-readtapi llvm-lipo llvm-otool',
                'build'])
            self.assertNotIn('gcc16-compare', [row['id'] for row in steps])
            for field, value in (('purpose', 'first-native-gcc16'), ('phase', 'full')):
                changed = copy.deepcopy(case.plan)
                if field == 'phase':
                    changed['metadata']['darwin_tool_phase'] = value
                else:
                    changed[field] = value
                with self.subTest(field=field), self.assertRaisesRegex(SDKError, 'initial.*(purpose|phase)'):
                    recipe.recipe_steps(changed, case.descriptor, case.commands,
                                        case.descriptor['abi_flags'])
            case.plan['commands'] = steps
            # Isolate the same complete command gate as the existing initial test.
            with patch.object(recipe, 'validate_metadata_inputs', return_value=case.plan['metadata']):
                self.assertIs(recipe.validate_bound_recipe_plan(case.plan, case.descriptor, case.commands,
                              case.descriptor['abi_flags'], inspect_inputs=False), case.plan)
                for kind in ('omit', 'reorder', 'target', 'shared-llvm'):
                    case.plan['commands'] = copy.deepcopy(steps)
                    if kind == 'omit':
                        case.plan['commands'].pop(0)
                    elif kind == 'reorder':
                        case.plan['commands'].reverse()
                    elif kind == 'shared-llvm':
                        argv = case.plan['commands'][-1]['argv']
                        argv[argv.index('LLVM_BUILD_LLVM_DYLIB=OFF')] = 'LLVM_BUILD_LLVM_DYLIB=ON'
                    else:
                        case.plan['commands'][-1]['argv'][-2] += ' clang'
                    with self.subTest(kind=kind), self.assertRaises(SDKError):
                        recipe.validate_bound_recipe_plan(case.plan, case.descriptor, case.commands,
                            case.descriptor['abi_flags'], inspect_inputs=False)

    def test_initial_plan_passes_the_existing_complete_source_and_command_gate(self):
        case = self.initial_recipe()
        export = case.fixture.export
        original = export / 'lang/gcc16'
        members = snapshot_tree(str(original))
        for name, row in case.plan['recipes'].items():
            root = export / name
            if root != original:
                for relative, item in members.items():
                    path = root / relative
                    if item['kind'] == 'directory':
                        path.mkdir(parents=True, exist_ok=True)
                    else:
                        self.assertEqual(item['kind'], 'file')
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes((original / relative).read_bytes())
            row['inventory'] = case.f.document(snapshot_tree(str(root)))
        inventory = case.f.document(snapshot_tree(str(export)))
        case.fixture.proof['inventory'] = inventory
        case.plan['metadata']['export'].update(inventory=inventory,
            source_proof=case.f.document(case.fixture.proof))
        case.plan['ports_export'] = dict(source=case.descriptor['sources']['ports'],
                                         **case.plan['metadata']['export'])
        case.refresh()
        with patch('subprocess.Popen', side_effect=AssertionError('no child')):
            self.assertEqual(case.validate(), case.plan)

    def test_initial_purpose_cannot_leak_into_full_phase(self):
        case = self.initial_recipe()
        case.plan['metadata']['darwin_tool_phase'] = 'full'
        with self.assertRaisesRegex(SDKError, 'initial.*phase'):
            recipe.recipe_steps(case.plan, case.descriptor, case.commands, case.descriptor['abi_flags'])

    def test_initial_phase_cannot_run_full_compiler_purpose(self):
        case = self.initial_recipe()
        case.plan['purpose'] = 'first-native-gcc16'
        with self.assertRaisesRegex(SDKError, 'initial.*purpose'):
            recipe.recipe_steps(case.plan, case.descriptor, case.commands, case.descriptor['abi_flags'])

    def test_bound_initial_command_reorder_omission_or_target_change_refuses(self):
        case = self.initial_recipe()
        case.plan['commands'] = recipe.recipe_steps(case.plan, case.descriptor, case.commands,
                                                   case.descriptor['abi_flags'])
        # Source-export admission is covered separately; isolate the command gate.
        with patch.object(recipe, 'validate_metadata_inputs', return_value=case.plan['metadata']):
            self.assertIs(recipe.validate_bound_recipe_plan(case.plan, case.descriptor, case.commands,
                case.descriptor['abi_flags'], inspect_inputs=False), case.plan)
            original = copy.deepcopy(case.plan['commands'])
            for kind in ('omit', 'reorder', 'target', 'shared-llvm'):
                case.plan['commands'] = copy.deepcopy(original)
                if kind == 'omit':
                    case.plan['commands'].pop(0)
                elif kind == 'reorder':
                    case.plan['commands'].reverse()
                elif kind == 'shared-llvm':
                    argv = case.plan['commands'][-1]['argv']
                    argv[argv.index('LLVM_BUILD_LLVM_DYLIB=OFF')] = 'LLVM_BUILD_LLVM_DYLIB=ON'
                else:
                    case.plan['commands'][-1]['argv'][-2] += ' clang'
                with self.subTest(kind=kind), self.assertRaises(SDKError):
                    recipe.validate_bound_recipe_plan(case.plan, case.descriptor, case.commands,
                        case.descriptor['abi_flags'], inspect_inputs=False)

    def test_initial_prefix_requires_cmake_to_be_an_llvm_dependency(self):
        case = self.initial_recipe()
        case.plan['recipes']['lang/llvm']['dependencies'] = []
        case.plan['recipes']['lang/gcc16']['dependencies'].append('devel/cmake')
        with self.assertRaisesRegex(SDKError, 'CMake.*dependency'):
            recipe.recipe_steps(case.plan, case.descriptor, case.commands, case.descriptor['abi_flags'])

    def test_unexecuted_future_meson_does_not_require_lipo(self):
        case = self.initial_recipe()
        case.plan['recipes']['devel/meson'] = dict(case.plan['recipes']['devel/cmake'], dependencies=[])
        case.plan['recipes']['devel/lld']['dependencies'] = []
        case.plan['recipes']['lang/gcc16']['dependencies'].append('devel/meson')
        case.plan['recipes']['lang/gcc16']['dependencies'].append('lang/llvm')
        case.plan['metadata']['recipes'] = sorted(case.plan['recipes'])
        self.assertNotIn('devel/meson', recipe.initial_execution_order(case.plan))
        self.assertNotIn('devel/meson', producer.required_recipe_utilities(case.plan))

    def test_executed_meson_keeps_lipo_and_ninja_keeps_od(self):
        case = self.initial_recipe()
        for name in ('devel/meson', 'devel/ninja-build'):
            case.plan['recipes'][name] = dict(case.plan['recipes']['devel/cmake'], dependencies=[])
            case.plan['recipes']['lang/llvm']['dependencies'].append(name)
        case.plan['metadata']['recipes'] = sorted(case.plan['recipes'])
        self.assertEqual(producer.required_recipe_utilities(case.plan),
                         {'devel/meson': ('lipo',), 'devel/ninja-build': ('od',)})

        case.plan['metadata']['darwin_tool_phase'] = 'full'
        self.assertEqual(producer.required_recipe_utilities(case.plan),
                         {'devel/meson': ('lipo',), 'devel/ninja-build': ('od',)})

    def outputs(self):
        case = self.initial_recipe()
        root = Path(case.plan['recipes']['lang/llvm']['object_directory']) / 'bin'
        root.mkdir(parents=True)
        image = struct.pack('<8I', 0xfeedfacf, 0x100000c, 0, 2, 0, 0, 0, 0)
        for name in ('llvm-libtool-darwin', 'llvm-objcopy', 'llvm-readtapi', 'llvm-lipo', 'llvm-objdump'):
            (root / name).write_bytes(image)
            (root / name).chmod(0o755)
        (root / 'llvm-install-name-tool').symlink_to('llvm-objcopy')
        (root / 'llvm-otool').symlink_to('llvm-objdump')
        return case, root

    def test_initial_outputs_bind_real_images_and_owner_declared_alias(self):
        self.assertTrue(callable(getattr(recipe, 'initial_tool_outputs', None)),
                        'Initial result lacks its separate output evidence')
        case, root = self.outputs()
        with patch('subprocess.Popen', side_effect=AssertionError('no child')):
            actual = recipe.initial_tool_outputs(case.plan)
        self.assertEqual(set(actual), set(recipe.INITIAL_DARWIN_TARGETS))
        self.assertEqual(actual['llvm-install-name-tool']['alias_of'], 'llvm-objcopy')
        self.assertEqual(actual['llvm-install-name-tool']['resolved_path'], str(root / 'llvm-objcopy'))
        self.assertEqual(actual['llvm-otool']['alias_of'], 'llvm-objdump')
        self.assertEqual(actual['llvm-otool']['resolved_path'], str(root / 'llvm-objdump'))
        for name, row in actual.items():
            self.assertEqual(Path(row['path']).name, name)
            self.assertEqual(row['sha256'], hashlib.sha256((root / name).read_bytes()).hexdigest())

    def test_existing_runner_records_initial_outputs_without_full_package_claim(self):
        case = self.initial_recipe()
        case.refresh()

        def child(argv, log, **kwargs):
            Path(log).write_text('Synthetic accepted command; no native execution.\n')
            if argv[-1] == 'install':
                (case.work / 'prefix/installed-fixture').write_text('dependency only\n')
            if argv[-1] == 'build' and 'BUILD_TARGET=' in argv[-2]:
                root = Path(case.plan['recipes']['lang/llvm']['object_directory']) / 'bin'
                root.mkdir(parents=True)
                image = struct.pack('<8I', 0xfeedfacf, 0x100000c, 0, 2, 0, 0, 0, 0)
                for name in ('llvm-libtool-darwin', 'llvm-objcopy', 'llvm-readtapi', 'llvm-lipo', 'llvm-objdump'):
                    (root / name).write_bytes(image)
                    (root / name).chmod(0o755)
                (root / 'llvm-install-name-tool').symlink_to('llvm-objcopy')
                (root / 'llvm-otool').symlink_to('llvm-objdump')
            return subprocess.CompletedProcess(argv, 0)

        # This isolates the unchanged executor after the separately tested plan gate.
        with patch.object(case, 'validate', return_value=case.plan), \
             patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=child):
            result = case.invoke()
        self.assertEqual(result['status'], 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW')
        self.assertEqual(result['stage'], 'initial-darwin-tools')
        data = json.loads(Path(result['initial_tool_output']['path']).read_text())
        self.assertEqual(set(data['outputs']), set(recipe.INITIAL_DARWIN_TARGETS))
        self.assertEqual(data['installed_dependencies'], ['devel/cmake'])
        self.assertEqual(data['not_installed_recipes'], ['devel/lld', 'lang/gcc16', 'lang/llvm'])
        self.assertFalse(data['gcc_comparison_performed'])
        self.assertFalse(data['full_package_qualified'])

    def test_missing_output_or_unrelated_alias_bytes_refuse(self):
        self.assertTrue(callable(getattr(recipe, 'initial_tool_outputs', None)))
        case, root = self.outputs()
        (root / 'llvm-install-name-tool').unlink()
        (root / 'llvm-install-name-tool').write_bytes((root / 'llvm-objcopy').read_bytes() + b'unrelated')
        (root / 'llvm-install-name-tool').chmod(0o755)
        with self.assertRaisesRegex(SDKError, 'alias'):
            recipe.initial_tool_outputs(case.plan)
        (root / 'llvm-lipo').unlink()
        with self.assertRaises(SDKError):
            recipe.initial_tool_outputs(case.plan)

    def test_otool_is_required_and_matches_the_upstream_objdump_alias(self):
        case, root = self.outputs()
        (root / 'llvm-otool').unlink()
        with self.assertRaises(SDKError):
            recipe.initial_tool_outputs(case.plan)
        (root / 'llvm-otool').write_bytes((root / 'llvm-objdump').read_bytes() + b'unrelated')
        (root / 'llvm-otool').chmod(0o755)
        with self.assertRaisesRegex(SDKError, 'alias'):
            recipe.initial_tool_outputs(case.plan)

    def test_output_object_directory_cannot_escape_owned_work(self):
        self.assertTrue(callable(getattr(recipe, 'initial_tool_outputs', None)))
        case = self.initial_recipe()
        case.plan['recipes']['lang/llvm']['object_directory'] = str(case.base / 'outside')
        with self.assertRaisesRegex(SDKError, 'LLVM.*object'):
            recipe.initial_tool_outputs(case.plan)

    def test_initial_plan_cannot_graduate_an_ordinary_provider(self):
        case = test_bootstrap_seed.BootstrapSeedTests(methodName='runTest')
        case.setUp()
        self.addCleanup(case.doCleanups)
        output = case.output()
        case.seed.graduate(output)
        for digest in (producer.INITIAL_DARWIN_PROFILE_SHA256,
                       producer.PERL_DTRACE_PROFILE_SHA256):
            initial = dict(case.seed.plan, purpose='initial-darwin-tools',
                           metadata=dict(darwin_tool_phase='initial', profile=dict(sha256=digest)))
            with self.subTest(profile=digest), \
                    patch('emberbsd_sdk.bootstrap_seed._plan', return_value=initial):
                with self.assertRaisesRegex(SDKError, 'initial tool phase'):
                    output.load()


if __name__ == '__main__':
    unittest.main()
