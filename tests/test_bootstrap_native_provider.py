# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Native-provider routing controls. No compiler or build tool is executed."""

import copy
import hashlib
from pathlib import Path
import shlex
import sys
import unittest
from unittest.mock import patch

import test_bootstrap_producer
import test_bootstrap_recipe
from emberbsd_sdk import bootstrap_producer as producer, bootstrap_recipe as recipe
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.inventory import snapshot_tree


class NativeProviderTests(unittest.TestCase):
    def setUp(self):
        self.case = test_bootstrap_producer.MetadataTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def archive_role(self):
        case = self.case
        path = case.f.bundle / 'llvm-libtool-darwin'
        path.write_bytes(case.f.image)
        path.chmod(0o755)
        case.descriptor['tools']['libtool_darwin'] = dict(
            path=str(path), sha256=hashlib.sha256(case.f.image).hexdigest(),
            kind='native', provider='ports', version='synthetic')
        case.commands['libtool_darwin'] = (str(path),)
        case.plan['required_roles'].append('libtool_darwin')
        return path

    def values(self):
        case = self.case
        return producer.metadata_assignments(case.plan, case.descriptor, case.commands,
                                             case.descriptor['abi_flags'])

    def test_iconv_profile_is_admitted_by_metadata_and_refused_for_initial(self):
        case = self.case
        for constant, expected in (
                ('ICONV_PROFILE_SHA256', 'e6fa6a573e39cbe581b4fa697b4e77e213ea73b072d75acd8761c999396204d7'),
                ('LIBTOOL_ICONV_PROFILE_SHA256', '843a90ddd938c7a88a9929c452adc44f0f34b9aa1e5a667d2add5edce55dd408'),
                ('FINAL_RUNTIME_FLAGS_PROFILE_SHA256', '00142405ab9cff5e02cbd47bc9bad6906402e5d61a16cb1cc9a7837618d7fcd8')):
            with self.subTest(profile=constant):
                self.assertEqual(getattr(producer, constant), expected)
                with patch.object(producer, constant, case.profile_sha), \
                        patch.object(producer, 'PROFILE_SHA256', '0' * 64):
                    self.assertTrue(producer._recognized_profile(case.profile_sha))
                    case.plan['resources']['cpu_jobs'] = 1
                    conf = case.plan['metadata']['makeconf']
                    raw = producer.metadata_makeconf(case.plan, case.descriptor, case.commands,
                                                     case.descriptor['abi_flags'])
                    Path(conf['path']).write_bytes(raw)
                    conf['sha256'] = hashlib.sha256(raw).hexdigest()
                    case.descriptor['make_binding']['makeconf'] = copy.deepcopy(conf)
                    case.plan['commands'] = producer.producer_steps(case.plan, case.descriptor, case.commands,
                                                                    case.descriptor['abi_flags'])
                    with patch('subprocess.Popen', side_effect=AssertionError('no native child')):
                        self.assertIs(case.validate(), case.plan)

                        full = test_bootstrap_recipe.RecipeTests()
                        full.setUp()
                        self.addCleanup(full.doCleanups)
                        self.assertEqual(full.plan['metadata']['profile']['sha256'], case.profile_sha)
                        with patch.object(producer, 'PROFILE_SHA256', '0' * 64):
                            full.plan['metadata']['darwin_tool_phase'] = 'initial'
                            with self.assertRaisesRegex(SDKError, 'initial.*profile'):
                                full.validate()

    def test_archive_role_is_explicit_without_replacing_raw_libtool(self):
        self.case.plan['metadata']['profile']['sha256'] = producer.NATIVE_PROVIDER_PROFILE_SHA256
        before = self.values()
        self.assertNotIn('EMBERBSD_DARWIN_LIBTOOL', before)
        path = self.archive_role()
        self.assertEqual(self.values()['EMBERBSD_DARWIN_LIBTOOL'], str(path))
        self.assertNotIn('libtool_darwin', self.case.plan['metadata']['native_tools'])
        self.assertNotIn('EMBERBSD_DARWIN_TOOL.libtool', self.values())
        self.assertEqual({k: v for k, v in self.values().items() if k != 'EMBERBSD_DARWIN_LIBTOOL'}, before)

    def test_archive_wrapper_tail_or_relative_invocation_is_refused(self):
        self.case.plan['metadata']['profile']['sha256'] = producer.NATIVE_PROVIDER_PROFILE_SHA256
        path = self.archive_role()
        for kind in ('script', 'tail', 'relative'):
            with self.subTest(kind=kind):
                self.case.descriptor['tools']['libtool_darwin']['kind'] = 'script' if kind == 'script' else 'native'
                self.case.commands['libtool_darwin'] = (str(path), '--unexpected') if kind == 'tail' else (
                    'llvm-libtool-darwin' if kind == 'relative' else str(path),)
                with self.assertRaises(SDKError):
                    self.values()

    def test_llvm_metadata_refuses_missing_archive_before_export_or_process(self):
        self.case.plan['metadata']['recipes'] = ['lang/gcc16', 'lang/llvm']
        self.case.plan['metadata']['profile']['sha256'] = producer.NATIVE_PROVIDER_PROFILE_SHA256
        self.case.plan['resources']['cpu_jobs'] = 1
        with patch('subprocess.Popen', side_effect=AssertionError('no native process')):
            with self.assertRaisesRegex(SDKError, 'explicit native role: libtool_darwin'):
                self.case.validate()

    def test_llvm_metadata_uses_the_selected_archive_and_complete_export_proof(self):
        case = self.case
        guard = patch.object(producer, 'NATIVE_PROVIDER_PROFILE_SHA256', case.profile_sha)
        guard.start()
        self.addCleanup(guard.stop)
        case.plan['resources']['cpu_jobs'] = 1
        self.archive_role()
        for role in ('install_name_tool', 'llvm_readtapi'):
            case.descriptor['tools'][role] = dict(case.descriptor['tools']['libtool_darwin'])
            case.commands[role] = (case.descriptor['tools'][role]['path'],)
            case.plan['required_roles'].append(role)
        source = case.export / 'lang/llvm'
        source.mkdir(parents=True)
        (source / 'Makefile').write_text('# Synthetic source recipe; never executed.\n')
        case.plan['metadata']['recipes'] = ['lang/gcc16', 'lang/llvm']
        inventory = case.f.document(snapshot_tree(str(case.export)))
        case.proof['inventory'] = inventory
        case.plan['metadata']['export'].update(inventory=inventory, source_proof=case.f.document(case.proof))
        conf = case.plan['metadata']['makeconf']
        raw = producer.metadata_makeconf(case.plan, case.descriptor, case.commands, case.descriptor['abi_flags'])
        Path(conf['path']).write_bytes(raw)
        conf['sha256'] = hashlib.sha256(raw).hexdigest()
        case.plan['commands'] = producer.producer_steps(case.plan, case.descriptor, case.commands,
                                                       case.descriptor['abi_flags'])
        with patch('subprocess.Popen', side_effect=AssertionError('no native process')):
            self.assertIs(case.validate(), case.plan)
        for role in ('libtool_darwin', 'install_name_tool', 'llvm_readtapi'):
            case.plan['required_roles'].remove(role)
            with self.assertRaisesRegex(SDKError, 'explicit native role: ' + role):
                case.validate()
            case.plan['required_roles'].append(role)
        case.plan['metadata']['export']['source_proof']['sha256'] = '0' * 64
        with self.assertRaises(SDKError):
            case.validate()

    def test_cmake_and_gcc_launch_the_same_exact_service(self):
        case = self.case
        flags = copy.deepcopy(case.descriptor['abi_flags'])
        case.plan['metadata']['profile']['sha256'] = producer.NATIVE_PROVIDER_PROFILE_SHA256
        case.plan['link_control'] = dict(
            python=dict(path='/selected/control-python', sha256='1' * 64),
            launcher=dict(path='/selected/sdk/scripts/link-launcher.py', sha256='2' * 64))
        case.plan['resources']['cpu_jobs'] = 18
        values = self.values()
        words = shlex.split(values['EMBERBSD_GCC16_LINKER_LAUNCHER'])
        self.assertEqual(values['EMBERBSD_DARWIN_CMAKE_LINKER_LAUNCHER'].split(';'), words)
        self.assertEqual(words, ['/selected/control-python', '/selected/sdk/scripts/link-launcher.py',
            '--lock-root', case.plan['resources']['lock_root'], '--context',
            case.plan['resources']['heavy_link_context'], '--'])
        self.assertEqual(case.descriptor['abi_flags'], flags)
        self.assertEqual(values['LDFLAGS'], shlex.join(flags['ldflags']))
        self.assertNotIn('python', case.plan['required_roles'])

    def test_cmake_list_cannot_inject_arguments_through_path_syntax(self):
        plan = dict(link_control=dict(python=dict(path='/selected/python'),
                    launcher=dict(path='/selected/sdk/scripts/link-launcher.py')),
                    resources=dict(lock_root='/selected/obj', heavy_link_context='/selected/heavy'))
        for suffix in (';injected', '\nnext', ' two'):
            plan['link_control']['python']['path'] = '/selected/python' + suffix
            with self.assertRaises(SDKError):
                recipe.recipe_cmake_linker_launcher(plan)

    def test_historical_profiles_do_not_emit_new_cmake_commands(self):
        for digest in (producer.PROFILE_SHA256, producer.PARALLEL_PROFILE_SHA256,
                       producer.BUILTIN_PROFILE_SHA256, producer.LIBTOOL_PROFILE_SHA256,
                       producer.RANLIB_PROFILE_SHA256):
            self.case.plan['metadata']['profile']['sha256'] = digest
            self.assertNotIn('EMBERBSD_DARWIN_CMAKE_LINKER_LAUNCHER', self.values())
        self.case.plan['metadata']['profile']['sha256'] = producer.NATIVE_PROVIDER_PROFILE_SHA256
        self.assertEqual(self.values()['EMBERBSD_DARWIN_CMAKE_LINKER_LAUNCHER'], '')

    def test_complete_provider_closure_passes_the_existing_full_plan_validator(self):
        case = test_bootstrap_recipe.RecipeTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        profile = Path(case.plan['metadata']['profile']['path'])
        profile.write_bytes(b'# Synthetic native provider profile; never executed.\n')
        digest = hashlib.sha256(profile.read_bytes()).hexdigest()
        case.plan['metadata']['profile']['sha256'] = digest
        for role in ('libtool_darwin', 'install_name_tool', 'llvm_readtapi'):
            path = case.f.bundle / role
            path.write_bytes(case.f.image)
            path.chmod(0o755)
            case.descriptor['tools'][role] = dict(path=str(path), kind='native', provider='ports',
                version='synthetic', sha256=hashlib.sha256(case.f.image).hexdigest())
            case.commands[role] = (str(path),)
            if role not in case.plan['required_roles']:
                case.plan['required_roles'].append(role)
        for number, name in enumerate(('lang/llvm', 'devel/lld')):
            root = case.fixture.export / name
            root.mkdir(parents=True)
            (root / 'Makefile').write_text('# Synthetic provider recipe.\n')
            row = copy.deepcopy(case.plan['recipes']['devel/gmp'])
            row.update(directory=name, inventory=case.f.document(snapshot_tree(str(root))),
                       patches=[], dependencies=['lang/llvm'] if name == 'devel/lld' else [])
            row['distfiles'][0]['relative'] = f'provider-{number}.tar.xz'
            case.plan['recipes'][name] = row
        case.plan['recipes']['lang/gcc16']['dependencies'].append('devel/lld')
        data = case.plan['metadata']
        data['recipes'] = sorted(case.plan['recipes'])
        data['export']['inventory'] = case.f.document(snapshot_tree(str(case.fixture.export)))
        manifest = copy.deepcopy(case.fixture.manifest)
        manifest[producer.PROFILE_RELATIVE]['sha256'] = digest
        proof = copy.deepcopy(case.fixture.proof)
        proof.update(ports_manifest=case.f.document(manifest), inventory=data['export']['inventory'])
        data['export']['source_proof'] = case.f.document(proof)
        case.plan['ports_export'] = dict(source=case.descriptor['sources']['ports'], **data['export'])
        for profile_constant in ('NATIVE_PROVIDER_PROFILE_SHA256', 'ICONV_PROFILE_SHA256',
                                 'LIBTOOL_ICONV_PROFILE_SHA256', 'FINAL_RUNTIME_FLAGS_PROFILE_SHA256'):
            with self.subTest(profile=profile_constant), \
                    patch.object(producer, 'PROFILE_SHA256', '0' * 64), \
                    patch.object(producer, profile_constant, digest):
                case.refresh()
                with patch('subprocess.Popen', side_effect=AssertionError('no native process')):
                    self.assertEqual(case.validate(), case.plan)
        order = recipe.recipe_order(case.plan)
        self.assertLess(order.index('lang/llvm'), order.index('devel/lld'))
        self.assertLess(order.index('devel/lld'), order.index('lang/gcc16'))
        self.assertEqual(sum(step['phase'] == 'compare' for step in case.plan['commands']), 1)
        launcher = Path(recipe.__file__).resolve().parents[2] / 'scripts/link-launcher.py'
        case.plan['link_control'] = dict(
            python=dict(path=sys.executable, sha256=hashlib.sha256(Path(sys.executable).resolve().read_bytes()).hexdigest()),
            launcher=dict(path=str(launcher), sha256=hashlib.sha256(launcher.read_bytes()).hexdigest()))
        case.plan['resources']['cpu_jobs'] = 18
        for profile_constant in ('ICONV_PROFILE_SHA256', 'LIBTOOL_ICONV_PROFILE_SHA256',
                                 'FINAL_RUNTIME_FLAGS_PROFILE_SHA256'):
            with patch.object(producer, profile_constant, digest), \
                    patch.object(producer, 'PROFILE_SHA256', '0' * 64):
                case.refresh()
                with patch.object(recipe.os, 'cpu_count', return_value=18), \
                    patch('subprocess.Popen', side_effect=AssertionError('no native process')):
                    self.assertEqual(case.validate(), case.plan)
                values = producer.metadata_assignments(case.plan, case.descriptor, case.commands,
                                                      case.descriptor['abi_flags'])
                self.assertEqual(values['EMBERBSD_DARWIN_CMAKE_LINKER_LAUNCHER'].split(';'),
                                 shlex.split(values['EMBERBSD_GCC16_LINKER_LAUNCHER']))

    def test_new_full_profile_cannot_omit_source_built_llvm_providers(self):
        case = test_bootstrap_recipe.RecipeTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.plan['metadata']['profile']['sha256'] = producer.NATIVE_PROVIDER_PROFILE_SHA256
        with self.assertRaisesRegex(SDKError, 'source-built LLVM and LLD'):
            case.validate()

    def test_provider_profile_revisions_preserve_routes_and_historical_mapping(self):
        self.archive_role()
        case = self.case
        for role in ('install_name_tool', 'llvm_readtapi'):
            case.descriptor['tools'][role] = dict(case.descriptor['tools']['libtool_darwin'])
            case.commands[role] = (case.descriptor['tools'][role]['path'],)
            case.plan['required_roles'].append(role)
        case.plan['link_control'] = dict(
            python=dict(path='/selected/control-python', sha256='1' * 64),
            launcher=dict(path='/selected/sdk/scripts/link-launcher.py', sha256='2' * 64))
        case.plan['metadata']['profile']['sha256'] = producer.NATIVE_PROVIDER_PROFILE_SHA256
        previous = self.values()
        for digest in (producer.NO_DEFAULT_RPATH_PROFILE_SHA256, producer.UNPRIVILEGED_PROFILE_SHA256,
                       '3366a715faa4c6271d3020f484b969d552164cdfe23bf8dc08e266234d82501f',
                       '06009d0687d0553bcb2de546415caf185b9bdff372006df9147b1e92bc8a9052',
                       producer.GZIP_HANDOFF_PROFILE_SHA256, producer.SOURCE_GZIP_HANDOFF_PROFILE_SHA256,
                       producer.SOURCE_GZIP_MAKE_SPLIT_PROFILE_SHA256,
                       producer.RUNTIME_FLAGS_REAPPLY_PROFILE_SHA256,
                       producer.NATIVE_RUNTIME_SHELL_PROFILE_SHA256,
                       producer.PERL_DTRACE_PROFILE_SHA256,
                       producer.ICONV_PROFILE_SHA256, producer.LIBTOOL_ICONV_PROFILE_SHA256,
                       producer.FINAL_RUNTIME_FLAGS_PROFILE_SHA256):
            with self.subTest(profile=digest):
                case.plan['metadata']['profile']['sha256'] = digest
                self.assertTrue(producer._recognized_profile(digest))
                self.assertTrue(producer._parallel_profile(digest))
                self.assertEqual(self.values(), previous)
                # Policy remains in the pinned Ports source, not the ABI map.
                self.assertNotIn('-nodefaultrpaths', self.values()['LDFLAGS'])
                changed = ('1' if digest[0] == '0' else '0') + digest[1:]
                self.assertFalse(producer._recognized_profile(changed))

    def test_provider_profile_revisions_keep_metadata_and_full_closure_guards(self):
        for digest in (producer.NO_DEFAULT_RPATH_PROFILE_SHA256, producer.UNPRIVILEGED_PROFILE_SHA256,
                       '3366a715faa4c6271d3020f484b969d552164cdfe23bf8dc08e266234d82501f',
                       '06009d0687d0553bcb2de546415caf185b9bdff372006df9147b1e92bc8a9052',
                       producer.GZIP_HANDOFF_PROFILE_SHA256, producer.SOURCE_GZIP_HANDOFF_PROFILE_SHA256,
                       producer.SOURCE_GZIP_MAKE_SPLIT_PROFILE_SHA256,
                       producer.RUNTIME_FLAGS_REAPPLY_PROFILE_SHA256,
                       producer.NATIVE_RUNTIME_SHELL_PROFILE_SHA256,
                       producer.PERL_DTRACE_PROFILE_SHA256,
                       producer.ICONV_PROFILE_SHA256, producer.LIBTOOL_ICONV_PROFILE_SHA256,
                       producer.FINAL_RUNTIME_FLAGS_PROFILE_SHA256):
            with self.subTest(profile=digest):
                self.case.plan['metadata']['recipes'] = ['lang/gcc16', 'lang/llvm']
                self.case.plan['metadata']['profile']['sha256'] = digest
                self.case.plan['resources']['cpu_jobs'] = 1
                with patch('subprocess.Popen', side_effect=AssertionError('no native process')):
                    with self.assertRaisesRegex(SDKError, 'explicit native role: libtool_darwin'):
                        self.case.validate()
                case = test_bootstrap_recipe.RecipeTests()
                case.setUp()
                self.addCleanup(case.doCleanups)
                case.plan['metadata']['profile']['sha256'] = digest
                with self.assertRaisesRegex(SDKError, 'source-built LLVM and LLD'):
                    case.validate()


if __name__ == '__main__':
    unittest.main()
