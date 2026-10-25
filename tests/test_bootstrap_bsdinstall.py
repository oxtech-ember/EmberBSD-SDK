# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Synthetic BSD install admission and lifecycle controls. No compiler runs."""

import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_bootstrap_seed import SeedFixture
from emberbsd_sdk import bootstrap_bsdinstall as bsd, bootstrap_pilot, bootstrap_program as program
from emberbsd_sdk.bootstrap_producer import PRODUCER_FORMAT, validate_producer_plan
from emberbsd_sdk.errors import CommandError, SDKError
from emberbsd_sdk.inventory import snapshot_tree


class BSDInstallTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.addCleanup(self.make_writable)
        self.seed = SeedFixture(self.base)
        self.f, self.descriptor = self.seed.native, self.seed.descriptor
        self.descriptor['host'] = dict(os='Darwin', cpu='arm64')
        self.commands, self.flags = self.seed.commands, self.descriptor['abi_flags']
        self.commands['strip'] = (str(self.base / 'selected-strip'),)
        self.source, self.owner, self.compat = (self.base / n for n in ('source', 'owner', 'compat'))
        self.work = self.base / 'bsd-work'
        self.manifest = dict(name='bsdinstall', initial_only=True, build_system='direct',
            sources=list(bsd._SOURCES), compiler=dict(family='gcc', major=16, abi='native'),
            required_tool_roles=['cc', 'sh', 'strip'],
            selected_defines=dict(EMBERBSD_BSDINSTALL_SHELL='sh', TARGET_STRIP='strip'),
            compat=dict(recipe='pkgtools/libnbcompat', include_dir='include', static_library='lib/libnbcompat.a'),
            cppflags=['-DHAVE_NBCOMPAT_H=1', '-D_PATH_DEVNULL="/dev/null"'], files={'input.txt': 'payload\n'},
            runtime_cases=[dict(name='fixed-' + str(i), invocation='bsdinstall', args=['-U', 'input.txt', 'copied.txt'],
                                stdin='', exit=0, stdout='', stderr='', checks=[]) for i in range(19)])
        self.manifest['runtime_cases'][1].update(exit=1, stderr_nonempty=True)
        self.manifest['runtime_cases'][1].pop('stderr')
        for relative in bsd._SOURCE_FILES:
            self.put(self.source / relative, b'synthetic derived source\n')
        self.readonly(self.source)
        self.put(self.owner / bsd._PATCH, b'synthetic canonical patch\n')
        self.put(self.owner / 'pkgsrc/export.conf', b'synthetic ordered source export\n')
        manifest = self.f.document(self.manifest)
        manifest_path = self.owner / bsd.MANIFEST_RELATIVE
        self.put(manifest_path, Path(manifest['path']).read_bytes())
        self.manifest_ref = dict(path=str(manifest_path), sha256=manifest['sha256'])
        mocked = patch.object(bsd, 'MANIFEST_SHA256', manifest['sha256'])
        mocked.start()
        self.addCleanup(mocked.stop)
        for name, raw in (('include/nbcompat.h', b'synthetic header\n'),
                          ('include/nbcompat/config.h', b'synthetic config\n'),
                          ('lib/libnbcompat.a', b'!<arch>\nsynthetic archive data')):
            self.put(self.compat / name, raw)
        self.readonly(self.compat)
        self.compat_inventory = self.f.document(snapshot_tree(str(self.compat)))
        old_seed = self.f.document(self.descriptor)
        self.old_plan = dict(format='emberbsd-native-gcc16-bootstrap-plan', version=1, scope='gcc16-bootstrap',
            host=self.descriptor['host'], seed_descriptor=old_seed,
            output_bundle_root=str(self.base / 'historical-prefix'), commands=[
                dict(id='r000-' + phase, recipe='pkgtools/libnbcompat', phase=phase,
                     argv=['/synthetic/never-executed-make', phase], cwd='/synthetic/historical', env={})
                for phase in ('build', 'install')])
        self.old_result = dict(format='emberbsd-native-gcc16-producer-result', version=1, scope='gcc16-bootstrap',
            status='FAILED', stage='full-recipe-bootstrap', seed_descriptor=old_seed,
            ordinary_provider_qualified=False, all_commands_drained=True,
            inputs_revalidated_after_drain=True, all_leases_closed=True, commands=[
                dict(step, actual_exit=0, log=self.f.raw(b'synthetic drained historical log'))
                for step in self.old_plan['commands']])
        self.preparation = dict(status='PREPARED_INITIAL_LIBRARY_INPUT_ONLY',
            native_or_ordinary_qualification=False, component_recipe='pkgtools/libnbcompat',
            root=str(self.compat), inventory=self.compat_inventory, source_seed_descriptor=old_seed,
            copied_members=[dict(original=dict(path=str(self.base / 'historical-prefix' / name), sha256=row['sha256']),
                retained=dict(path=str(self.compat / name), sha256=row['sha256']), mode_before_readonly=0o644)
                for name, row in snapshot_tree(str(self.compat)).items() if row['kind'] == 'file'])
        self.plan = dict(format=PRODUCER_FORMAT, version=1, scope='gcc16-bootstrap', stage=bsd.STAGE,
            seed_descriptor={}, host=self.descriptor['host'], sources=self.descriptor['sources'], work_root=str(self.work),
            required_roles=sorted({'cc', 'sh', 'strip', *self.descriptor['driver_components'].values()}),
            resources=dict(cpu_jobs=1, link_jobs=1, runtime_seconds=120, lock_root=str(self.base),
                           heavy_link_context=str(self.base / 'heavy')),
            program=dict(source=self.tree(self.source, owner=False), owner=self.tree(self.owner, owner=True),
                         manifest=self.manifest_ref, compat={}))
        self.seal_compat()
        self.plan['commands'] = program.program_steps(self.plan, self.descriptor, self.commands, self.flags)

    @staticmethod
    def put(path, raw):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)

    def make_writable(self):
        for directory, dirs, files in os.walk(self.base):
            os.chmod(directory, 0o755)
            for name in files:
                path = Path(directory, name)
                if not path.is_symlink():
                    path.chmod(0o644)

    @staticmethod
    def readonly(root):
        for directory, dirs, files in os.walk(root, topdown=False):
            for name in files:
                Path(directory, name).chmod(0o444)
            Path(directory).chmod(0o555)

    def tree(self, root, *, owner):
        inventory = snapshot_tree(str(root))
        members = {}
        for relative, row in inventory.items():
            path = root / relative
            if owner:
                if row['kind'] != 'file':
                    continue
                raw = path.read_bytes()
                members[relative] = dict(sha256=row['sha256'], git_blob=hashlib.sha1(
                    b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest())
            else:
                members[relative] = dict(mtime_ns=path.stat().st_mtime_ns)
                if row['kind'] == 'file':
                    members[relative]['sha256'] = row['sha256']
        value = dict(root=str(root), source=self.descriptor['sources']['ports'], inventory=self.f.document(inventory))
        value['source_proof'] = self.f.document(dict(format='emberbsd-bootstrap-program-source', version=1,
            source=value['source'], inventory=value['inventory'], members=self.f.document(members)))
        return value

    def seal_compat(self):
        plan_ref = self.f.document(self.old_plan)
        self.old_result['bootstrap_plan'] = plan_ref
        result_ref = self.f.document(self.old_result)
        self.preparation.update(source_producer_result=result_ref, source_bootstrap_plan=plan_ref,
                                actual_component_commands=self.old_result['commands'][:2])
        value = dict(root=str(self.compat), inventory=self.compat_inventory,
                     producer_result=result_ref, bootstrap_plan=plan_ref)
        preparation_ref = self.f.document(self.preparation)
        value['component_receipt'] = self.f.document(dict(value, status='ACCEPTED_INITIAL_LIBNBCOMPAT_INPUT',
            scope='gcc16-bootstrap', recipe='pkgtools/libnbcompat', preparation=preparation_ref,
            independent_review=self.f.document(dict(status='REVIEWED_INITIAL_LIBNBCOMPAT_INPUT_CANDIDATE',
                findings=[], all_review_leases_closed=True, preparation=preparation_ref,
                ordinary_provider_qualified=False)),
            ordinary_provider_qualified=False, initial_only=True))
        self.plan['program']['compat'] = value

    def validate(self):
        return validate_producer_plan(self.plan, self.descriptor, self.commands, self.flags)

    def test_exact_tuple_flags_static_library_and_fixed_cases(self):
        with patch('subprocess.Popen', side_effect=AssertionError('no child is permitted')):
            self.assertIs(self.validate(), self.plan)
        steps = self.plan['commands']
        self.assertEqual(len(steps), 23)
        self.assertEqual(steps[0]['argv'][:len(self.commands['cc'])], list(self.commands['cc']))
        self.assertIn('-DEMBERBSD_BSDINSTALL_SHELL=' + json.dumps(self.commands['sh'][0]), steps[0]['argv'])
        self.assertIn('-DTARGET_STRIP=' + json.dumps(self.commands['strip'][0]), steps[0]['argv'])
        self.assertIn(str(self.compat / 'lib/libnbcompat.a'), steps[2]['argv'])
        self.assertNotIn('-lnbcompat', steps[2]['argv'])
        self.assertEqual(steps[3]['argv'][:len(self.commands['sh'])], list(self.commands['sh']))
        self.assertTrue(steps[3]['argv'][-1].startswith('umask 022; '))
        self.assertNotIn('STRIP', steps[3]['env'])
        self.assertEqual(steps[-1]['env']['DYLD_PRINT_LIBRARIES'], '1')

    def test_finished_success_is_scoped_to_the_same_initial_component(self):
        self.old_result['status'] = 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW'
        self.seal_compat()
        self.assertIs(self.validate(), self.plan)

    def test_unfinished_failed_component_and_missing_fences_refuse(self):
        for kind in ('running', 'install', 'bool', 'drain', 'inputs', 'leases', 'argv'):
            with self.subTest(kind=kind):
                saved = copy.deepcopy(self.old_result)
                if kind == 'running':
                    self.old_result['status'] = 'RUNNING'
                elif kind == 'install':
                    self.old_result['commands'][1]['actual_exit'] = 1
                elif kind == 'bool':
                    self.old_result['commands'][0]['actual_exit'] = False
                elif kind == 'argv':
                    self.old_result['commands'][0]['argv'].append('foreign')
                else:
                    key = dict(drain='all_commands_drained', inputs='inputs_revalidated_after_drain',
                               leases='all_leases_closed')[kind]
                    self.old_result[key] = False
                self.seal_compat()
                with self.assertRaises(SDKError):
                    self.validate()
                self.old_result = saved
        self.seal_compat()

    def test_copy_member_hash_origin_or_coverage_changes_refuse(self):
        for kind in ('sha', 'origin', 'omit', 'duplicate'):
            with self.subTest(kind=kind):
                saved = copy.deepcopy(self.preparation)
                rows = self.preparation['copied_members']
                if kind == 'sha':
                    rows[0]['original']['sha256'] = '0' * 64
                elif kind == 'origin':
                    rows[0]['original']['path'] = '/foreign/archive'
                elif kind == 'omit':
                    rows.pop()
                else:
                    rows.append(copy.deepcopy(rows[0]))
                self.seal_compat()
                with self.assertRaises(SDKError):
                    self.validate()
                self.preparation = saved
        self.seal_compat()

    def test_historical_abi_compiler_driver_and_binding_changes_refuse(self):
        for field in ('host_abi', 'compiler', 'abi_flags', 'driver_prefix', 'driver_binding'):
            with self.subTest(field=field):
                saved = copy.deepcopy(self.descriptor)
                self.descriptor[field] = {'changed': True} if field != 'driver_prefix' else '/foreign/prefix'
                with self.assertRaisesRegex(SDKError, 'compiler or host ABI'):
                    self.validate()
                self.descriptor.clear()
                self.descriptor.update(saved)

    def test_changed_source_and_readonly_mode_refuse(self):
        source = self.source / bsd._SOURCES[0]
        source.chmod(0o644)
        self.plan['program']['source'] = self.tree(self.source, owner=False)
        with self.assertRaisesRegex(SDKError, 'read-only derived source'):
            self.validate()
        source.chmod(0o444)
        self.plan['program']['source'] = self.tree(self.source, owner=False)
        source.chmod(0o644)
        source.write_bytes(b'changed source\n')
        source.chmod(0o444)
        with self.assertRaisesRegex(SDKError, 'source tree changed'):
            self.validate()

    def test_owner_git_proof_and_static_component_admission_are_required(self):
        source = self.plan['program']['owner']
        proof = json.loads(Path(source['source_proof']['path']).read_text())
        members = json.loads(Path(proof['members']['path']).read_text())
        members[bsd._PATCH]['git_blob'] = '0' * 40
        proof['members'] = self.f.document(members)
        source['source_proof'] = self.f.document(proof)
        with self.assertRaisesRegex(SDKError, 'Git blob'):
            self.validate()
        self.plan['program']['owner'] = self.tree(self.owner, owner=True)
        value = self.plan['program']['compat']
        receipt = json.loads(Path(value['component_receipt']['path']).read_text())
        receipt['ordinary_provider_qualified'] = True
        value['component_receipt'] = self.f.document(receipt)
        with self.assertRaisesRegex(SDKError, 'separate accepted component'):
            self.validate()

    def test_selector_tail_route_override_resources_and_compat_mutation_refuse(self):
        self.commands['strip'] = (*self.commands['strip'], '--foreign-tail')
        with self.assertRaisesRegex(SDKError, 'one exact executable'):
            self.validate()
        self.commands['strip'] = self.commands['strip'][:1]
        self.commands['strip'] = (self.commands['strip'][0] + ' spaces',)
        with self.assertRaisesRegex(SDKError, 'cannot require shell quoting'):
            self.validate()
        self.commands['strip'] = (str(self.base / 'selected-strip'),)
        self.plan['commands'][0]['env']['STRIP'] = '/foreign/strip'
        with self.assertRaisesRegex(SDKError, 'fixed selected route'):
            self.validate()
        del self.plan['commands'][0]['env']['STRIP']
        self.plan['resources']['runtime_seconds'] = 121
        with self.assertRaises(SDKError):
            self.validate()
        self.plan['resources']['runtime_seconds'] = 120
        path = self.compat / 'lib/libnbcompat.a'
        path.chmod(0o644)
        path.write_bytes(b'changed archive')
        path.chmod(0o444)
        with self.assertRaisesRegex(SDKError, 'snapshot changed'):
            self.validate()

    def test_root_modes_and_independent_review_causal_controls(self):
        for root in (self.source, self.compat):
            with self.subTest(root=root.name):
                root.chmod(0o755)
                with self.assertRaises(SDKError):
                    self.validate()
                root.chmod(0o555)
        value = self.plan['program']['compat']
        receipt = json.loads(Path(value['component_receipt']['path']).read_text())
        review = json.loads(Path(receipt['independent_review']['path']).read_text())
        for key, changed in (('findings', ['actual finding']), ('all_review_leases_closed', False),
                             ('preparation', self.f.document({})), ('ordinary_provider_qualified', True)):
            with self.subTest(field=key):
                mutant = dict(review, **{key: changed})
                value['component_receipt'] = self.f.document(dict(receipt, independent_review=self.f.document(mutant)))
                with self.assertRaisesRegex(SDKError, 'independent review differs'):
                    self.validate()
        self.seal_compat()

    def test_actual_case_content_mode_absence_and_separate_streams(self):
        self.work.mkdir()
        case = self.manifest['runtime_cases'][0]
        case['checks'] = [dict(path='output', kind='file', content='payload\n', mode=0o640),
                          dict(path='directory', kind='directory', mode=0o755),
                          dict(path='missing', kind='absent')]
        with patch.object(bsd, 'bsdinstall_manifest', return_value=self.manifest):
            for stream in ('stdout', 'stderr'):
                (self.work / ('fixed-0.' + stream)).write_bytes(b'')
            (self.work / 'output').write_bytes(b'payload\n')
            (self.work / 'output').chmod(0o640)
            (self.work / 'directory').mkdir(mode=0o755)
            step = dict(phase='program-case', id='case-fixed-0')
            result = bsd.verify_bsdinstall_step(step, str(self.work), self.plan)
            self.assertNotEqual(result['stdout']['path'], result['stderr']['path'])
            (self.work / 'output').chmod(0o600)
            with self.assertRaisesRegex(SDKError, 'type or mode'):
                bsd.verify_bsdinstall_step(step, str(self.work), self.plan)
            (self.work / 'output').chmod(0o640)
            (self.work / 'output').write_bytes(b'wrong bytes')
            with self.assertRaisesRegex(SDKError, 'copied content'):
                bsd.verify_bsdinstall_step(step, str(self.work), self.plan)
            (self.work / 'output').write_bytes(b'payload\n')
            (self.work / 'missing').symlink_to('unavailable')
            with self.assertRaisesRegex(SDKError, 'failure case created'):
                bsd.verify_bsdinstall_step(step, str(self.work), self.plan)

    def test_existing_runner_holds_compat_work_link_and_revalidates(self):
        descriptor_ref = self.f.document(self.descriptor)
        self.plan['seed_descriptor'] = descriptor_ref
        plan_ref = self.f.document(self.plan)
        accepted = self.f.document(dict(input_route_audit=self.f.raw(b'synthetic independent input audit')))
        case = self
        class Selection:
            commands = case.commands
            def as_dict(self):
                return dict(plan=case.validate())
        def locked(context):
            name = '.prebuild-sdk-lock-' + hashlib.sha256(str(context).encode()).hexdigest()
            with open(self.base / name, 'rb') as handle:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        def fake_command(argv, log, **kwargs):
            step = next(row for row in self.plan['commands'] if row['id'] == Path(log).stem)
            locked(self.compat)
            locked(self.work)
            Path(log).write_bytes(b'synthetic drained log; no native child\n')
            if step['phase'] in ('compile', 'link'):
                Path(argv[-1]).write_bytes(struct.pack('<8I', 0xfeedfacf, 0x100000c, 0,
                                          1 if step['phase'] == 'compile' else 2, 0, 0, 0, 0))
                Path(argv[-1]).chmod(0o755)
            if step['phase'] == 'link':
                locked(self.plan['resources']['heavy_link_context'])
            if step['phase'] == 'program-case':
                row = next(row for row in self.manifest['runtime_cases'] if step['id'] == 'case-' + row['name'])
                for stream in ('stdout', 'stderr'):
                    (self.work / (row['name'] + '.' + stream)).write_text(
                        'synthetic error\n' if row.get(stream + '_nonempty') else row.get(stream, ''))
            if step['phase'] == 'run':
                (self.work / 'runtime-copy.txt').write_bytes(b'payload\n')
            if step['expected_exit']:
                raise CommandError(step['expected_exit'], argv, log=log)
            return subprocess.CompletedProcess(argv, 0)
        with patch('emberbsd_sdk.bootstrap_seed.load_bootstrap_seed', side_effect=lambda *a, **kw: Selection()) as load, \
             patch.object(bootstrap_pilot, 'run_logged', side_effect=fake_command), \
             patch('subprocess.Popen', side_effect=AssertionError('no native child')):
            result = bootstrap_pilot.run_producer(descriptor_ref['path'], descriptor_sha256=descriptor_ref['sha256'],
                accepted_seed_receipt=accepted, bootstrap_plan=plan_ref, required_roles=self.plan['required_roles'])
        self.assertEqual(result['status'], 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW', result.get('error'))
        self.assertEqual(len(result['commands']), 23)
        self.assertTrue(result['all_commands_drained'] and result['inputs_revalidated_after_drain'] and
                        result['all_leases_closed'])
        self.assertEqual(load.call_count, 2)
        self.assertEqual(stat.S_IMODE((self.work / 'input.txt').stat().st_mode), 0o644)


if __name__ == '__main__':
    unittest.main()
