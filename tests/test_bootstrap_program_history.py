# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Historical Git-program receipts use synthetic children, never a compiler."""

import copy
import hashlib
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

import test_bootstrap_program_native as native
import test_bootstrap_program as programs
import test_bootstrap_program_cut as cut
import test_bootstrap_byacc as byacc
from emberbsd_sdk.bootstrap_seed import _history
from emberbsd_sdk.darwin_runtime import darwin_runtime_closure
from emberbsd_sdk.errors import SDKError


class ProgramHistoryTests(unittest.TestCase):
    def setUp(self):
        self.make_fixture('basename')

    def make_fixture(self, name):
        cls = byacc.ByaccContractTests if name == 'yacc' else cut.CutProbeTests if name in ('cut', 'sort') else programs.ProgramTests if name == 'grep' else native.NativeProgramTests
        self.fixture = cls(methodName='runTest')
        if name in ('cut', 'sort'):
            self.fixture.program_name = name
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        if name not in ('cut', 'sort', 'grep', 'yacc'):
            self.fixture.configure(name)
        self.case = c = self.fixture if name in ('grep', 'yacc') else self.fixture.case
        if name == 'yacc':
            c.inputs = c.plan['program']['source']
        c.descriptor['compiler'] = dict(family='gcc', version='16.2.0', triplet='aarch64-apple-darwin25')
        child = self.fixture.fake if name in ('cut', 'sort') else self.fixture.fake_command
        def fake(argv, log, **kwargs):
            returned = child(argv, log, **kwargs)
            if name == 'pwd' and Path(log).stem.startswith('case-'):
                (c.work / (Path(log).stem.removeprefix('case-') + '.stdout')).write_text(str(c.work) + '\n')
            if name == 'uname' and Path(log).stem.startswith('case-'):
                (c.work / (Path(log).stem.removeprefix('case-') + '.stdout')).write_bytes(b'Darwin\n')
            return returned
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=fake), \
             patch('emberbsd_sdk.bootstrap_program.socket.gethostname', return_value='test.host.example'), \
             patch('emberbsd_sdk.bootstrap_program.identity_output', return_value=b'synthetic identity\n'), \
             patch('emberbsd_sdk.bootstrap_program.os.uname', return_value=os.uname_result(
                 ('Darwin', 'historical.node', 'historical-release', 'historical-version', 'arm64'))):
            self.result = c.invoke()
        self.assertEqual(self.result['status'], 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW')
        self.old_seed = self.result['seed_descriptor']
        self.plan_ref = self.result['bootstrap_plan']
        accepted = dict(format='emberbsd-bootstrap-seed-acceptance', version=1,
                        scope='gcc16-bootstrap', status='PASS', accepted_input_scope='bootstrap-seed',
                        descriptor_sha256=self.old_seed['sha256'], bootstrap_plan=self.plan_ref)
        for key in ('host', 'compiler', 'sources', 'providers', 'host_abi', 'abi_flags',
                    'history', 'dynamic_dependencies', 'driver_prefix', 'driver_binding', 'make_binding'):
            accepted[key] = copy.deepcopy(c.descriptor.get(key))
        accepted['accepted_history_unknowns'] = _history(c.descriptor['history'], c.descriptor['providers'],
                                                        descriptor=c.descriptor, inspect_inputs=False)
        self.result['accepted_seed_receipt'] = c.f.document(accepted)
        self.image = copy.deepcopy(next(row['output'] for row in self.result['commands'] if row['id'] == 'link-' + name))
        self.audit = dict(format='emberbsd-darwin-output-runtime-audit', version=1,
            scope='producer-output', api=1, host=c.descriptor['host'], tools=[],
            cache=dict(uuid='a' * 32),
            nodes={self.image['path']: dict(kind='output-executable', sha256=self.image['sha256'],
                image=dict(loads=[], implicit_loads=[]))},
            output_runtime=dict(outputs=[self.image], observed_cache_roots={}))
        self.observations = dict(format='emberbsd-darwin-observed-cache-roots', version=1,
            scope='producer-output', cache_uuid='a' * 32,
            observations=[dict(output=self.image, trace=self.result['commands'][-1]['log'], roots=[])])
        self.acceptance = dict(format='emberbsd-bootstrap-program-output-acceptance', version=1,
            scope='gcc16-bootstrap', status='PASS', accepted_input_scope='bootstrap-seed',
            ordinary_provider_qualified=False, bootstrap_plan=self.plan_ref,
            source_proof=c.inputs['source_proof'], checks=[
                'ports-release-and-patch-membership' if name == 'yacc' else 'git-source-membership', 'native-compile-link', 'source-semantics',
                'reported-tool-routes', 'darwin-output-runtime'],
            semantic_review=c.f.raw(b'Synthetic independent historical semantic review; no native process ran.'),
            limitations=['Synthetic fixture only. No native process ran.'])
        self.record = dict(format='emberbsd-bootstrap-program-build-history', version=1,
            scope='gcc16-bootstrap', manifest=c.plan['program']['manifest'],
            source_proof=c.inputs['source_proof'], bootstrap_plan=self.plan_ref)
        self.target = c.base / 'next-inputs'
        (self.target / 'bin').mkdir(parents=True)
        self.selected = self.target / 'bin' / name
        shutil.copyfile(self.image['path'], self.selected)
        self.selected.chmod(0o755)
        self.provider = dict(origin='source-build', source='os_previous', recipe='pkgsrc/devel/byacc/bootstrap.json' if name == 'yacc' else 'tools/' + name + '/bootstrap.json',
                             recipe_sha256=c.plan['program']['manifest']['sha256'])
        self.descriptor = dict(bundle_root=str(self.target), host=c.descriptor['host'],
            sources=dict(os_previous=c.inputs['source'], os=dict(repository='https://example.invalid/os', commit='4' * 40)),
            providers={'os-basename': self.provider},
            tools={name: dict(path=str(self.selected), sha256=self.image['sha256'],
                                   kind='native', provider='os-basename', version='byacc-20260126' if name == 'yacc' else 'os-' + '3' * 40)})
        self.history = dict(kind='source-build', source_archives=[], patches=[],
            provisioned_members=[dict(invocation=name, original_path=self.image['path'],
                                     selected_path=str(self.selected), sha256=self.image['sha256'])])

        if name == 'yacc':
            from emberbsd_sdk.bootstrap_byacc import history_origins
            self.history['source_archives'], self.history['patches'] = history_origins(c.plan)

    def seal(self):
        f = self.case.f
        self.audit['output_runtime']['observed_cache_roots'] = f.document(self.observations)
        audit_ref = f.document(self.audit)
        self.acceptance.update(producer_result=f.document(self.result), runtime_audit=audit_ref,
            runtime_closure=f.document(darwin_runtime_closure(self.audit, audit_ref)), artifact=self.image)
        self.record.update(producer_result=self.acceptance['producer_result'],
                           output_acceptance=f.document(self.acceptance))
        self.history['program_build'] = f.document(self.record)

    def verify(self, *, inspect_inputs=True):
        self.seal()
        return _history({'os-basename': self.history}, self.descriptor['providers'],
                        descriptor=self.descriptor, inspect_inputs=inspect_inputs)

    def test_git_history_preserves_the_old_source_pin_without_archives(self):
        with patch('subprocess.Popen', side_effect=AssertionError('history cannot start a process')):
            self.assertEqual(self.verify(), {'os-basename': []})
        self.assertEqual(self.descriptor['sources']['os']['commit'], '4' * 40)

    def test_retired_seed_and_original_executable_are_not_reopened(self):
        # Read-only source and metadata evidence remain. Retire only old payloads.
        shutil.rmtree(self.case.f.bundle)
        Path(self.image['path']).unlink()
        Path(self.result['commands'][0]['output']['path']).unlink()
        self.assertEqual(self.verify(), {'os-basename': []})

    def test_original_seed_unknowns_cannot_be_omitted_or_changed(self):
        from emberbsd_sdk.native_toolset import _json
        original = _json(self.result['accepted_seed_receipt'])
        for value in (None, {}, {'ports': ['invented-unknown']}, {'wrong-provider': []}):
            accepted = copy.deepcopy(original)
            if value is None:
                del accepted['accepted_history_unknowns']
            else:
                accepted['accepted_history_unknowns'] = value
            self.result['accepted_seed_receipt'] = self.case.f.document(accepted)
            with self.subTest(value=value), self.assertRaisesRegex(SDKError, 'historical unknowns'):
                self.verify()

    def test_created_fixture_and_probe_copy_hashes_are_mandatory(self):
        for name, filename in (('grep', 'input.txt'), ('cut', 'input.txt'), ('cut', 'compat-decl-strtoi.c')):
            self.make_fixture(name)
            self.assertEqual(self.verify(), {'os-basename': []})
            sha = self.result['outputs'][filename]
            for mutation in ('missing', 'changed'):
                if mutation == 'missing':
                    del self.result['outputs'][filename]
                else:
                    self.result['outputs'][filename] = 'f' * 64
                with self.subTest(name=name, filename=filename, mutation=mutation), \
                     self.assertRaisesRegex(SDKError, 'created source or fixture'):
                    self.verify()
                self.result['outputs'][filename] = sha

    def test_retired_cut_fixture_copies_objects_and_executables_need_no_reopen(self):
        from emberbsd_sdk.bootstrap_program import program_sources
        self.make_fixture('cut')
        for name in program_sources(self.case.plan):
            (self.case.work / name).unlink()
        for row in self.result['commands']:
            if 'output' in row:
                Path(row['output']['path']).unlink()
        shutil.rmtree(self.case.f.bundle)
        self.assertEqual(self.verify(), {'os-basename': []})

    def test_missing_or_unqualified_output_acceptance_refuses(self):
        for key, value in [('status', 'PENDING'), ('scope', 'native-build'),
                           ('ordinary_provider_qualified', True), ('accepted_input_scope', 'qualified-native')]:
            old = copy.deepcopy(self.acceptance)
            self.acceptance[key] = value
            with self.subTest(key=key), self.assertRaises(SDKError):
                self.verify()
            self.acceptance = old
        self.seal()
        Path(self.record['output_acceptance']['path']).unlink()
        with self.assertRaises((SDKError, OSError)):
            _history({'os-basename': self.history}, self.descriptor['providers'], descriptor=self.descriptor)

    def test_incomplete_failed_or_undrained_production_refuses(self):
        for key, value in [('status', 'FAILED'), ('all_commands_drained', False),
                           ('all_leases_closed', False), ('inputs_revalidated_after_drain', False),
                           ('ordinary_provider_qualified', True), ('api', True),
                           ('commands', self.result['commands'][:-1])]:
            old = copy.deepcopy(self.result)
            self.result[key] = value
            with self.subTest(key=key), self.assertRaises(SDKError):
                self.verify()
            self.result = old

    def test_changed_finite_command_exit_or_output_hash_refuses(self):
        for mutation in ('argv', 'env', 'phase', 'exit', 'expected-type', 'output'):
            old = copy.deepcopy(self.result)
            row = self.result['commands'][0]
            if mutation == 'argv': row['argv'].append('-w')
            elif mutation == 'env': row['env']['PATH'] += ':/usr/bin'
            elif mutation == 'phase': row['phase'] = 'run'
            elif mutation == 'exit': row['actual_exit'] = 1
            elif mutation == 'expected-type': row['expected_exit'] = False
            else: row['output']['sha256'] = 'f' * 64
            with self.subTest(mutation=mutation), self.assertRaises(SDKError): self.verify()
            self.result = old

    def test_changed_source_proof_recipe_or_manifest_refuses(self):
        source = self.case.source / 'usr.bin/basename/basename.c'
        raw = source.read_bytes()
        source.write_bytes(b'changed source')
        with self.assertRaises(SDKError): self.verify()
        source.write_bytes(raw)
        self.provider['recipe_sha256'] = '0' * 64
        with self.assertRaises(SDKError): self.verify()
        self.provider['recipe_sha256'] = self.case.plan['program']['manifest']['sha256']
        self.record['source_proof'] = self.case.f.document(dict(self.case.proof, source={}))
        with self.assertRaises(SDKError): self.verify()

    def test_changed_stream_log_or_runtime_evidence_refuses(self):
        log = Path(self.result['commands'][0]['log']['path'])
        raw = log.read_bytes(); log.write_bytes(b'changed log')
        with self.assertRaises(SDKError): self.verify()
        log.write_bytes(raw)
        stream = Path(self.result['commands'][2]['stdout']['path'])
        raw = stream.read_bytes(); stream.write_bytes(b'wrong semantic result')
        with self.assertRaises(SDKError): self.verify()
        stream.write_bytes(raw)
        self.audit['nodes'][self.image['path']]['sha256'] = 'f' * 64
        with self.assertRaises(SDKError): self.verify()

    def test_wrong_role_outside_or_changed_selected_member_refuses(self):
        self.selected.write_bytes(b'changed selected executable')
        with self.assertRaises(SDKError): self.verify()
        shutil.copyfile(self.image['path'], self.selected)
        self.selected.chmod(0o755)
        self.history['provisioned_members'][0]['selected_path'] = '/usr/bin/basename'
        with self.assertRaises(SDKError): self.verify()
        self.history['provisioned_members'][0]['selected_path'] = str(self.selected)
        self.descriptor['tools']['unrelated'] = dict(self.descriptor['tools']['basename'])
        with self.assertRaises(SDKError): self.verify()

    def test_selected_symlink_nonexecutable_and_wrong_source_version_refuse(self):
        self.selected.chmod(0o644)
        with self.assertRaisesRegex(SDKError, 'not executable'): self.verify()
        self.selected.unlink()
        self.selected.symlink_to(self.image['path'])
        with self.assertRaises(SDKError): self.verify()
        self.selected.unlink()
        shutil.copyfile(self.image['path'], self.selected)
        self.selected.chmod(0o755)
        self.descriptor['tools']['basename']['version'] = 'unrelated version'
        with self.assertRaisesRegex(SDKError, 'selected spelling'): self.verify()

    def test_stale_acceptance_and_unrelated_runtime_trace_refuse(self):
        self.acceptance['bootstrap_plan'] = self.case.f.document(dict(self.case.plan, stage='cmp-bootstrap'))
        with self.assertRaisesRegex(SDKError, 'exact production'): self.verify()
        self.acceptance['bootstrap_plan'] = self.plan_ref
        self.observations['observations'][0]['trace'] = self.result['commands'][0]['log']
        with self.assertRaisesRegex(SDKError, 'producer trace'): self.verify()

    def test_unrelated_outputs_and_relabelled_compiler_logs_refuse(self):
        self.result['outputs']['unrelated-program'] = 'f' * 64
        with self.assertRaisesRegex(SDKError, 'finite producer roster'): self.verify()
        del self.result['outputs']['unrelated-program']
        self.result['commands'][0]['stdout'] = self.result['commands'][0]['log']
        with self.assertRaisesRegex(SDKError, 'combined log'): self.verify()

    def test_new_history_shape_does_not_admit_archives_patches_or_extra_fields(self):
        for key, value in [('source_archives', [{'url': 'https://example.invalid/unrelated',
                                               'file': self.case.f.raw(b'archive')}]),
                           ('patches', [{'url': 'https://example.invalid/unrelated',
                                        'file': self.case.f.raw(b'patch')}]),
                           ('build_record', self.case.f.document({}))]:
            old = copy.deepcopy(self.history)
            self.history[key] = value
            with self.subTest(key=key), self.assertRaisesRegex(SDKError, 'Git-program'): self.verify()
            self.history = old

    def test_source_proof_inventory_members_and_manifest_bytes_remain_required(self):
        for reference in (self.case.inputs['inventory'], self.case.proof['members'],
                          self.case.plan['program']['manifest']):
            p = Path(reference['path']); raw = p.read_bytes(); p.write_bytes(b'{}')
            with self.subTest(reference=reference), self.assertRaises(SDKError): self.verify()
            p.write_bytes(raw)

    def test_offline_history_does_not_open_retired_provisioned_payload(self):
        self.selected.unlink()
        self.assertEqual(self.verify(inspect_inputs=False), {'os-basename': []})
        with self.assertRaises(SDKError): self.verify()

    def test_all_fifteen_existing_os_profiles_have_a_historical_route(self):
        for name in ('grep', 'cmp', 'hostname', 'basename', 'dirname', 'cat', 'head',
                     'uniq', 'wc', 'tr', 'printf', 'pwd', 'cut', 'env', 'uname'):
            with self.subTest(name=name):
                self.make_fixture(name)
                with patch('socket.gethostname', side_effect=AssertionError('no current hostname oracle')), \
                     patch('os.uname', side_effect=AssertionError('no current kernel oracle')):
                    self.assertEqual(self.verify(), {'os-basename': []})

    def test_uname_history_keeps_the_accepted_stream_without_current_oracles(self):
        self.make_fixture('uname')
        with patch('socket.gethostname', side_effect=AssertionError('no current hostname oracle')), \
             patch('os.uname', side_effect=AssertionError('no current kernel oracle')), \
             patch('emberbsd_sdk.bootstrap_program._uname_output',
                   side_effect=AssertionError('no repeated uname semantics')):
            self.assertEqual(self.verify(), {'os-basename': []})
        stream = self.result['commands'][2]['stdout']
        Path(stream['path']).write_bytes(b'changed historical observation\n')
        with self.assertRaises(SDKError):
            self.verify()

    def test_only_manifest_declared_grep_names_can_use_its_accepted_image(self):
        self.make_fixture('grep')
        for name in ('egrep', 'fgrep'):
            selected = self.target / 'bin' / name
            shutil.copyfile(self.image['path'], selected)
            selected.chmod(0o755)
            self.descriptor['tools'][name] = dict(self.descriptor['tools']['grep'], path=str(selected))
            self.history['provisioned_members'].append(dict(invocation=name, original_path=self.image['path'],
                selected_path=str(selected), sha256=self.image['sha256']))
        self.assertEqual(self.verify(), {'os-basename': []})
        self.history['provisioned_members'][0]['invocation'] = 'awk'
        with self.assertRaises(SDKError): self.verify()

    def test_date_history_keeps_the_original_fixed_mode_and_source_pin(self):
        self.make_fixture('date')
        self.assertEqual(self.verify(), {'os-basename': []})
        compile_step = self.result['commands'][0]
        compile_step['argv'].remove('-DHAVE_NBTOOL_CONFIG_H=1')
        with self.assertRaises(SDKError):
            self.verify()

    def test_sort_history_binds_the_shared_observed_configuration(self):
        self.make_fixture('sort')
        self.assertEqual(self.verify(), {'os-basename': []})
        last = next(row for row in self.result['commands'] if row['id'] == 'compat-func-reallocarr')
        header = last['configuration_header']
        changed = Path(header['path']).read_bytes().replace(b'HAVE_REALLOCARR 0', b'HAVE_REALLOCARR 1')
        Path(header['path']).write_bytes(changed)
        header['sha256'] = hashlib.sha256(changed).hexdigest()
        self.result['outputs']['nbtool_config.h'] = header['sha256']
        with self.assertRaisesRegex(SDKError, 'historical efun configuration differs'):
            self.verify()

    def test_tail_history_requires_the_declared_alias_record(self):
        self.make_fixture('tail')
        self.assertEqual(self.verify(), {'os-basename': []})
        link = next(row for row in self.result['commands'] if row['phase'] == 'link')
        link['invocation_aliases'] = {'tac': 'other'}
        with self.assertRaises(SDKError):
            self.verify()

    def test_id_history_keeps_its_observed_identity_without_current_oracles(self):
        self.make_fixture('id')
        with patch('emberbsd_sdk.bootstrap_program.identity_output',
                   side_effect=AssertionError('historical identity must not be recomputed')):
            self.assertEqual(self.verify(), {'os-basename': []})
        stream = next(row for row in self.result['commands'] if row['id'] == 'case-case0')['stdout']
        Path(stream['path']).write_bytes(b'changed identity\n')
        with self.assertRaises(SDKError):
            self.verify()

    def test_cut_control_cannot_be_provisioned_as_the_program(self):
        self.make_fixture('cut')
        self.assertEqual(self.verify(), {'os-basename': []})
        control = next(row['output'] for row in self.result['commands'] if row['id'] == 'compat-control-link')
        self.history['provisioned_members'][0].update(original_path=control['path'], sha256=control['sha256'])
        with self.assertRaises(SDKError): self.verify()

    def test_legacy_archive_history_remains_required_and_valid(self):
        f = self.case.f
        old = dict(kind='source-build', source_archives=[], patches=[],
            build_record=f.document(dict(format='emberbsd-bootstrap-seed-history', version=1,
                scope='gcc16-bootstrap', historical_compiler='GCC16', build_log=f.raw(b'legacy log'))))
        with self.assertRaises(SDKError): _history({'legacy': old}, {'legacy': dict(origin='source-build')})
        old['source_archives'] = [dict(url='https://example.invalid/source.tar', file=f.raw(b'archive'))]
        self.assertEqual(_history({'legacy': old}, {'legacy': dict(origin='source-build')}), {'legacy': []})

    def test_byacc_history_binds_archive_patches_and_generated_parser(self):
        self.make_fixture('yacc')
        with patch('subprocess.Popen', side_effect=AssertionError('history starts no native child')):
            self.assertEqual(self.verify(), {'os-basename': []})
        self.history['patches'].pop()
        with self.assertRaisesRegex(SDKError, 'roster'): self.verify()

    def test_byacc_history_rejects_changed_capability_outcome(self):
        self.make_fixture('yacc')
        next(row for row in self.result['commands'] if row['id'] == 'capability-run')['actual_exit'] = 1
        with self.assertRaisesRegex(SDKError, 'permitted actual exit'): self.verify()

    def test_byacc_history_rejects_changed_generated_parser(self):
        self.make_fixture('yacc')
        (self.case.work / 'parser.tab.h').write_bytes(b'changed header')
        with self.assertRaises(SDKError): self.verify()


if __name__ == '__main__':
    unittest.main()
