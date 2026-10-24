# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Synthetic metadata controls. No compiler or bootstrap process runs here."""

import copy
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import shutil
import struct
import tempfile
import unittest
from unittest.mock import patch

from test_native_toolset import Fixture, digest
from emberbsd_sdk.bootstrap_seed import (BOOTSTRAP_SEED_API, _environment,
                                        _history, load_bootstrap_seed)
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.inventory import snapshot_tree
from emberbsd_sdk.native_toolset import load_native_toolset


class SeedFixture:
    def __init__(self, base):
        self.native = Fixture(base)
        f = self.native
        self.prefix = f.bundle / 'gcc-driver'
        self.prefix.mkdir()
        for role in f.descriptor['driver_components'].values():
            old = Path(f.records[role]['path'])
            new = self.prefix / role
            old.rename(new)
            f.records[role]['path'] = str(new)
        self.descriptor = copy.deepcopy(f.descriptor)
        self.descriptor.update(format='emberbsd-bootstrap-seed', scope='gcc16-bootstrap',
                               driver_prefix=str(self.prefix))
        self.descriptor['dynamic_dependencies'] = self.dynamic(self.descriptor)
        self.descriptor['providers']['ports']['origin'] = 'source-build'
        self.descriptor['history'] = dict(ports=dict(kind='source-build',
            source_archives=[dict(url='https://example.invalid/gcc.tar.xz',
                                  file=f.raw(b'synthetic archive'))], patches=[],
            build_record=f.document(dict(format='emberbsd-bootstrap-seed-history', version=1,
                                         scope='gcc16-bootstrap', historical_compiler='external history only',
                                         build_log=f.raw(b'synthetic historical build log')))))
        self.export = f.base / 'ports-export'
        for recipe in ('lang/gcc16', 'devel/gmp'):
            directory = self.export / recipe
            (directory / 'patches').mkdir(parents=True)
            (directory / 'Makefile').write_text('# synthetic common recipe\n')
            (directory / 'patches' / 'patch-fixture').write_text('synthetic patch bytes\n')
        recipes = {name: dict(directory=name, inventory=f.document(snapshot_tree(str(self.export / name))),
                              patches=['patches/patch-fixture'],
                              distfiles=[dict(url='https://example.invalid/' + name + '.tar',
                                              file=f.raw(b'synthetic recipe distfile'))],
                              dependencies=['devel/gmp'] if name == 'lang/gcc16' else [])
                   for name in ('lang/gcc16', 'devel/gmp')}
        self.work = f.base / 'work'
        self.work.mkdir()
        self.commands = {role: (row['path'], '-B' + str(self.prefix) + os.sep)
                         if role in ('cc', 'cxx', 'cpp') else (row['path'],)
                         for role, row in self.descriptor['tools'].items()}
        environment = _environment({key: row['path'] for key, row in self.descriptor['tools'].items()},
                                   self.commands, self.descriptor['abi_flags'])
        steps = []
        for name, recipe, phase in (('gmp-build', 'devel/gmp', 'build'),
                                     ('gcc-build', 'lang/gcc16', 'build'),
                                     ('gcc-compare', 'lang/gcc16', 'compare'),
                                     ('gcc-install', 'lang/gcc16', 'install')):
            directory = self.work if phase == 'compare' else self.export / recipe
            steps.append(dict(id=name, recipe=recipe, phase=phase, role='make',
                              argv=[*self.commands['make'], '-C', str(directory),
                                    'compare' if phase == 'compare' else 'all' if phase == 'build' else 'install'],
                              cwd=str(self.export), env=dict(environment)))
        self.plan = dict(format='emberbsd-native-gcc16-bootstrap-plan', version=1,
                         scope='gcc16-bootstrap', purpose='first-native-gcc16', host=f.host,
                         output_compiler=f.compiler, sources=f.sources,
                         ports_export=dict(root=str(self.export), source=f.sources['ports'],
                                           inventory=f.document(snapshot_tree(str(self.export))),
                                           source_proof=f.raw(b'synthetic pinned source proof')),
                         recipes=recipes, work_root=str(self.work),
                         output_bundle_root=str(f.base / 'output-bundle'),
                         required_roles=['cc', 'cxx', 'sh', 'make'], commands=steps,
                         comparison_command='gcc-compare')
        self.acceptance = dict(format='emberbsd-bootstrap-seed-acceptance', version=1,
                               scope='gcc16-bootstrap', status='PASS',
                               accepted_input_scope='bootstrap-seed')
        self.seal()

    @staticmethod
    def dynamic(descriptor):
        return {str(Path(row['path']).resolve()): dict(sha256=row['sha256'], dependencies=[])
                for row in descriptor['tools'].values() if row['kind'] == 'native'}

    def seal(self):
        f = self.native
        self.descriptor['inventory'] = f.document(snapshot_tree(str(f.bundle)))
        self.descriptor_ref = f.document(self.descriptor)
        self.plan['seed_descriptor'] = self.descriptor_ref
        self.plan_ref = f.document(self.plan)
        self.acceptance.update(descriptor_sha256=self.descriptor_ref['sha256'], bootstrap_plan=self.plan_ref,
                               accepted_history_unknowns=_history(self.descriptor['history'],
                                                                   self.descriptor['providers']))
        for key in ('host', 'compiler', 'sources', 'providers', 'host_abi', 'abi_flags',
                    'history', 'dynamic_dependencies', 'driver_prefix'):
            self.acceptance[key] = copy.deepcopy(self.descriptor[key])
        checks = copy.deepcopy(f.receipt['checks'])
        for role in ('cc', 'cxx'):
            for option in ('-dumpmachine', '-dumpfullversion'):
                checks['identity'][role][option]['argv'] = [*self.commands[role], option]
        for name, role in self.descriptor['driver_components'].items():
            driver = 'cxx' if name == 'cc1plus' else 'cc'
            checks['driver_components'][name] = f.command(
                [*self.commands[driver], '-print-prog-name=' + name],
                (self.descriptor['tools'][role]['path'] + '\n').encode())
        self.acceptance['checks'] = checks
        self.acceptance['execution_audit'] = f.document(dict(
            format='emberbsd-bootstrap-seed-audit', version=1, scope='gcc16-bootstrap', status='PASS',
            descriptor_sha256=self.descriptor_ref['sha256'], bootstrap_plan=self.plan_ref,
            tools=self.descriptor['tools'], dynamic_dependencies=self.descriptor['dynamic_dependencies'],
            undeclared_tools=[], path_searches=[], undeclared_abi_inputs=[], undeclared_dsos=[]))
        self.acceptance_ref = f.document(self.acceptance)

    def load(self, **overrides):
        arguments = dict(descriptor_sha256=self.descriptor_ref['sha256'],
                         accepted_seed_receipt=self.acceptance_ref, bootstrap_plan=self.plan_ref,
                         required_roles=('cc', 'cxx', 'make', 'sh'))
        arguments.update(overrides)
        return load_bootstrap_seed(self.descriptor_ref['path'], **arguments)

    def graduate(self, output):
        f = self.native
        self.plan['output_bundle_root'] = str(output.bundle)
        self.seal()
        provider = output.providers['ports']
        provider.update(recipe='lang/gcc16', recipe_sha256=digest(self.export / 'lang/gcc16' / 'Makefile'))
        output.build.update(recipe=provider['recipe'], recipe_sha256=provider['recipe_sha256'],
                            accepted_input_scope='bootstrap-seed')
        output.seal()
        commands = []
        for step in self.plan['commands']:
            actual = copy.deepcopy(step)
            actual.update(actual_exit=0, log=f.raw(b'synthetic combined build log'))
            commands.append(actual)
        dynamic = self.dynamic(output.descriptor)
        self.graduation = dict(format='emberbsd-native-gcc16-bootstrap-graduation', version=1,
                               scope='native-build', status='PASS',
                               output_descriptor_sha256=output.descriptor_ref['sha256'],
                               seed_descriptor=self.descriptor_ref, accepted_seed_receipt=self.acceptance_ref,
                               bootstrap_plan=self.plan_ref, output_inventory=output.descriptor['inventory'],
                               commands=commands, dynamic_dependencies=dynamic,
                               stage_comparison=dict(method='gcc-bootstrap-compare', status='PASS',
                                                     command_id='gcc-compare',
                                                     stage2_inventory=f.document({'synthetic-stage2': {}}),
                                                     stage3_inventory=f.document({'synthetic-stage3': {}}),
                                                     comparison_log=f.raw(b'synthetic compare log')),
                               execution_audit=f.document(dict(
                                   format='emberbsd-native-gcc16-bootstrap-audit', version=1,
                                   scope='native-build', status='PASS', bootstrap_plan=self.plan_ref,
                                   output_descriptor_sha256=output.descriptor_ref['sha256'],
                                   recipe_closure=sorted(self.plan['recipes']), all_commands_drained=True,
                                   dynamic_dependencies=dynamic, undeclared_tools=[], path_searches=[],
                                   undeclared_abi_inputs=[], undeclared_dsos=[])))
        self.bind_graduation(output)

    def bind_graduation(self, output):
        output.build['bootstrap_graduation'] = self.native.document(self.graduation)
        output.receipt['provider_builds']['ports'] = output.document(output.build)
        output.accept_receipt()


class BootstrapSeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.seed = SeedFixture(self.base)
        for name, value in (('system', 'Linux'), ('machine', 'x86_64')):
            mock = patch('emberbsd_sdk.native_toolset.platform.' + name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)

    def refuse(self, expression=None):
        context = self.assertRaisesRegex(SDKError, expression) if expression else self.assertRaises(SDKError)
        with context:
            self.seed.load()

    def output(self):
        base = self.base / 'produced'
        base.mkdir()
        return Fixture(base)

    def test_scoped_immutable_commands_run_no_process(self):
        with patch('subprocess.Popen', side_effect=AssertionError('no process allowed')):
            selected = self.seed.load()
        self.assertEqual(BOOTSTRAP_SEED_API, 1)
        self.assertEqual(selected.command('cc'), self.seed.commands['cc'])
        self.assertEqual(selected.command('as'), self.seed.commands['as'])
        with self.assertRaises(TypeError):
            selected.commands['cc'] = ('/usr/bin/cc',)
        with self.assertRaises(SDKError):
            selected.command('undeclared')
        first = selected.as_dict()
        first['commands']['cc'].append('-specs=bad')
        self.assertEqual(len(selected.command('cc')), 2)

    def test_seed_cannot_be_used_as_ordinary_toolset(self):
        with self.assertRaises(SDKError):
            load_native_toolset(self.seed.descriptor_ref['path'],
                                descriptor_sha256=self.seed.descriptor_ref['sha256'],
                                accepted_producer_receipt=self.seed.acceptance_ref,
                                required_roles=('cc', 'cxx'))

    def test_missing_acceptance_or_plan_pin_fails_before_payload_read(self):
        for override in (dict(accepted_seed_receipt=None),
                         dict(bootstrap_plan=dict(path=self.seed.plan_ref['path'], sha256='0' * 64))):
            with self.subTest(override=override), patch('emberbsd_sdk.bootstrap_seed._payload') as payload:
                with self.assertRaises(SDKError):
                    self.seed.load(**override)
                payload.assert_not_called()

    def test_source_archive_and_patch_bytes_are_required(self):
        history = self.seed.descriptor['history']['ports']
        history['patches'].append(dict(url='https://example.invalid/patch',
                                      file=self.seed.native.raw(b'patch bytes')))
        self.seed.seal()
        self.seed.load()
        Path(history['patches'][0]['file']['path']).write_bytes(b'changed patch')
        self.refuse('bootstrap input changed')

    def test_external_historical_compiler_is_data_not_selected_tool(self):
        f = self.seed.native
        history = f.document(dict(format='emberbsd-bootstrap-seed-history', version=1,
                                  scope='gcc16-bootstrap', historical_compiler='clang external history',
                                  build_log=f.raw(b'not a compiler invocation')))
        self.seed.descriptor['history']['ports']['build_record'] = history
        self.seed.seal()
        selected = self.seed.load()
        self.assertEqual(selected.command('cc'), self.seed.commands['cc'])
        self.seed.descriptor['compiler']['family'] = 'clang'
        self.seed.seal()
        self.refuse('require GCC16')

    def test_driver_query_must_use_same_explicit_prefix_and_absolute_member(self):
        for change in ('bare', 'no-prefix'):
            with self.subTest(change=change):
                self.seed.seal()
                query = self.seed.acceptance['checks']['driver_components']['as']
                if change == 'bare':
                    query['stdout'] = self.seed.native.raw(b'as\n')
                else:
                    query['argv'] = [self.seed.commands['cc'][0], '-print-prog-name=as']
                self.seed.acceptance_ref = self.seed.native.document(self.seed.acceptance)
                self.refuse('undeclared or implicit|query mismatch')

    def test_seed_inherits_seven_driver_and_live_provider_root_guards(self):
        self.assertIn('lto-wrapper', self.seed.descriptor['driver_components'])
        with patch('emberbsd_sdk.native_toolset.provider_root', side_effect=SDKError('root refused')) as guard:
            self.refuse('root refused')
        guard.assert_called_once_with(str(self.seed.native.bundle), live=True)
        self.seed.descriptor['driver_components'].pop('lto-wrapper')
        self.seed.seal()
        self.refuse('incomplete GCC driver closure')

    def test_dynamic_closure_cannot_omit_or_change_members(self):
        graph = self.seed.descriptor['dynamic_dependencies']
        graph.pop(self.seed.descriptor['tools']['cc']['path'])
        self.seed.seal()
        self.refuse('omits native tools')

    def test_shared_library_graph_checks_real_header_hash_and_all_edges(self):
        f = self.seed.native
        library = f.bundle / 'libfixture.so'
        image = bytearray(f.image)
        struct.pack_into('<H', image, 16, 3)
        library.write_bytes(image)
        graph = self.seed.descriptor['dynamic_dependencies']
        graph[str(library)] = dict(sha256=digest(library), dependencies=[])
        compiler = self.seed.descriptor['tools']['cc']['path']
        graph[compiler]['dependencies'] = [str(library)]
        self.seed.seal()
        self.seed.load()
        graph.pop(str(library))
        self.seed.seal()
        self.refuse('undeclared dependency')

    def binary_release(self):
        f = self.seed.native
        self.seed.descriptor['providers']['ports'] = dict(origin='binary-release')
        self.seed.descriptor['history']['ports'] = dict(
            kind='binary-release', release_url='https://example.invalid/native-gcc16-release',
            release_version='16.2.0-binary-release', artifact=f.raw(b'synthetic binary release archive'),
            release_metadata=f.raw(b'synthetic original release metadata'),
            embedded_configuration=f.raw(b'synthetic original configure arguments'),
            source_archives=[], patches=[], source_commit=None, historical_build_log=None,
            historical_compiler=None, patches_complete=False, publisher_signature=None,
            unknowns=sorted(['source-archives', 'source-commit', 'historical-build-log',
                             'historical-compiler', 'historical-patches', 'publisher-signature']))
        self.seed.seal()

    def test_binary_release_with_named_unknowns_needs_explicit_independent_acceptance(self):
        self.binary_release()
        self.seed.load()
        self.assertIn('historical-build-log', self.seed.acceptance['accepted_history_unknowns']['ports'])
        self.seed.acceptance['accepted_history_unknowns'] = {}
        self.seed.acceptance_ref = self.seed.native.document(self.seed.acceptance)
        self.refuse('explicitly accept its historical unknowns')

    def test_binary_release_artifact_digest_is_not_implicit_approval(self):
        self.binary_release()
        self.seed.acceptance['status'] = 'UNREVIEWED'
        self.seed.acceptance_ref = self.seed.native.document(self.seed.acceptance)
        self.refuse('not accepted')
        self.seed.acceptance['status'] = 'PASS'
        self.seed.seal()
        artifact = self.seed.descriptor['history']['ports']['artifact']
        Path(artifact['path']).write_bytes(b'changed binary release')
        self.refuse('bootstrap input changed')

    def test_binary_release_graduation_keeps_historical_unknowns(self):
        self.binary_release()
        output = self.output()
        self.seed.graduate(output)
        output.load()
        self.assertIn('source-commit', self.seed.acceptance['accepted_history_unknowns']['ports'])

    def test_make_arguments_cannot_replace_pinned_recipe_or_compiler(self):
        original = copy.deepcopy(self.seed.plan['commands'][0]['argv'])
        for tail in (['-C', '/usr/src'], ['-f', '/tmp/unpinned.mk'],
                     ['CC=/usr/bin/cc'], ['CC+=/usr/bin/cc'],
                     ['PKGSRC_COMPILER+=clang'], ['GCC_EXEC_PREFIX?=/usr/bin'],
                     ['BASH_ENV?=/tmp/unpinned'], ['MAKE=/usr/bin/make'], ['MAKE?=/usr/bin/make']):
            with self.subTest(tail=tail):
                self.seed.plan['commands'][0]['argv'] = original + tail
                self.seed.seal()
                self.refuse('replaces its declared recipe|overrides an accepted native route|assignment operator|recursive make')

    def test_check_cli_emits_selection_but_never_runs_plan(self):
        path = Path(__file__).resolve().parents[1] / 'scripts' / 'bootstrap-seed.py'
        spec = importlib.util.spec_from_file_location('bootstrap_seed_cli', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        arguments = ['check', '--required-role', 'cc', '--required-role', 'cxx']
        for name, reference in (('descriptor', self.seed.descriptor_ref),
                                ('accepted-seed-receipt', self.seed.acceptance_ref),
                                ('bootstrap-plan', self.seed.plan_ref)):
            arguments += ['--' + name, reference['path'], '--' + name + '-sha256', reference['sha256']]
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch('subprocess.Popen', side_effect=AssertionError('no process')):
            self.assertEqual(module.main(arguments), 0)
        self.assertIn('emberbsd-bootstrap-seed-selection', output.getvalue())

    def test_common_recipe_export_and_ordered_patches_are_bound(self):
        self.seed.plan['recipes']['lang/gcc16']['patches'] = []
        self.seed.seal()
        self.refuse('omits common recipe patches')
        self.seed.plan['recipes']['lang/gcc16']['patches'] = ['patches/patch-fixture']
        self.seed.seal()
        (self.seed.export / 'lang/gcc16' / 'Makefile').write_text('changed common recipe')
        self.refuse('Ports export inventory changed')

    def test_unrelated_cycle_or_wrong_dependency_order_is_refused(self):
        original = copy.deepcopy(self.seed.plan)
        for kind in ('unrelated', 'cycle', 'order'):
            with self.subTest(kind=kind):
                self.seed.plan = copy.deepcopy(original)
                if kind == 'unrelated':
                    self.seed.plan['recipes']['unrelated'] = {}
                elif kind == 'cycle':
                    self.seed.plan['recipes']['devel/gmp']['dependencies'] = ['lang/gcc16']
                else:
                    self.seed.plan['commands'][:2] = list(reversed(self.seed.plan['commands'][:2]))
                self.seed.seal()
                self.refuse()

    def test_command_environment_cannot_replace_routes_or_add_searches(self):
        for key, value in (('CC', '/usr/bin/cc'), ('PATH', '/usr/bin'),
                           ('GCC_EXEC_PREFIX', '/usr/lib'), ('MAKEFLAGS', 'CC=/usr/bin/cc')):
            with self.subTest(key=key):
                old = copy.deepcopy(self.seed.plan['commands'][0]['env'])
                self.seed.plan['commands'][0]['env'][key] = value
                self.seed.seal()
                self.refuse('native routes|environment override')
                self.seed.plan['commands'][0]['env'] = old

    def test_path_delimiter_is_refused_before_plan_can_execute(self):
        record = self.seed.descriptor['tools']['make']
        new = self.seed.native.bundle / 'native:tools' / 'make'
        new.parent.mkdir()
        shutil.copy2(record['path'], new)
        record['path'] = str(new)
        self.seed.descriptor['dynamic_dependencies'] = self.seed.dynamic(self.seed.descriptor)
        self.seed.seal()
        self.refuse('cannot be encoded in PATH')

    def test_graduation_preserves_seed_scope_and_final_ordinary_probes(self):
        output = self.output()
        self.seed.graduate(output)
        selected = output.load()
        self.assertEqual(selected.tool('cc'), output.records['cc']['path'])
        self.assertEqual(output.build['accepted_input_scope'], 'bootstrap-seed')
        output.receipt['checks']['cxx']['compile']['actual_exit'] = 1
        output.accept_receipt()
        with self.assertRaisesRegex(SDKError, 'successful actual command'):
            output.load()

    def test_graduation_refuses_changed_plan_failed_command_or_missing_comparison(self):
        output = self.output()
        self.seed.graduate(output)
        original = copy.deepcopy(self.seed.graduation)
        for change in ('plan', 'failed', 'compare'):
            with self.subTest(change=change):
                self.seed.graduation = copy.deepcopy(original)
                if change == 'plan':
                    self.seed.graduation['bootstrap_plan']['sha256'] = '0' * 64
                elif change == 'failed':
                    self.seed.graduation['commands'][0]['actual_exit'] = 1
                else:
                    self.seed.graduation['stage_comparison']['status'] = 'UNKNOWN'
                self.seed.bind_graduation(output)
                with self.assertRaises(SDKError):
                    output.load()

    def test_build_result_requires_actual_integer_exit_and_unchanged_combined_log(self):
        output = self.output()
        self.seed.graduate(output)
        output.load()
        original = copy.deepcopy(self.seed.graduation)
        for change in ('failed-exit', 'boolean-exit', 'false-channel', 'changed-log'):
            with self.subTest(change=change):
                self.seed.graduation = copy.deepcopy(original)
                actual = self.seed.graduation['commands'][0]
                if change == 'failed-exit':
                    actual['actual_exit'] = 1
                elif change == 'boolean-exit':
                    actual['actual_exit'] = False
                elif change == 'false-channel':
                    actual['stdout'] = actual['log']
                else:
                    Path(actual['log']['path']).write_bytes(b'changed combined log')
                self.seed.bind_graduation(output)
                with self.assertRaises(SDKError):
                    output.load()

    def test_graduation_does_not_require_retaining_historical_seed_payload(self):
        output = self.output()
        self.seed.graduate(output)
        shutil.rmtree(self.seed.native.bundle)
        shutil.rmtree(self.seed.export)
        output.load()

    def test_seed_scope_cannot_be_rewritten_to_ordinary(self):
        output = self.output()
        self.seed.graduate(output)
        output.build['accepted_input_scope'] = 'qualified-native'
        output.receipt['provider_builds']['ports'] = output.document(output.build)
        output.accept_receipt()
        with self.assertRaisesRegex(SDKError, 'cannot be relabeled'):
            output.load()


if __name__ == '__main__':
    unittest.main()
