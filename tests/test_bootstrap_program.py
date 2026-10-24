# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Causal finite-program controls. Every compiler and runtime child is synthetic."""

import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_bootstrap_seed import SeedFixture
from emberbsd_sdk import bootstrap_pilot, bootstrap_program as program, bootstrap_producer as producer
from emberbsd_sdk.bootstrap_seed import _plan
from emberbsd_sdk.errors import CommandError, SDKError
from emberbsd_sdk.inventory import snapshot_tree


class ProgramTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.seed = SeedFixture(self.base)
        self.f, self.descriptor = self.seed.native, self.seed.descriptor
        self.descriptor['host'] = dict(os='Darwin', cpu='arm64')
        self.descriptor['sources']['os'] = dict(repository='https://example.invalid/os', commit='3' * 40)
        self.work, self.source = self.base / 'grep-work', self.base / 'grep-source'
        self.source.mkdir()
        self.manifest = dict(format='emberbsd-native-bootstrap-program', version=1, name='grep',
            origin='Synthetic source-owner contract; no native source is compiled.',
            canonical_host_recipe='tools/grep/Makefile', source_makefile='usr.bin/grep/Makefile',
            compiler=dict(family='gcc', major=16, abi='native'), sources=list(program._SOURCES),
            headers=['usr.bin/grep/grep.h'], include_dirs=['usr.bin/grep'],
            cppflags=['-DWITHOUT_NLS', '-DWITHOUT_BZ2', '-DWITHOUT_GZIP'],
            cflags=[], link_flags=[], libraries=[], invocations=['grep', 'egrep', 'fgrep'],
            files={'input.txt': 'hay\nneedle\na.b\n', 'patterns.txt': 'needle\n^a.b$\n'}, runtime_cases=[])
        for index in range(14):
            status = 2 if index >= 11 else 1 if index in (1, 5) else 0
            row = dict(name='case' + str(index), invocation=('egrep' if index == 9 else
                       'fgrep' if index == 10 else 'grep'), args=['needle'],
                       stdin="needle\n' $(not-a-command)\n", exit=status, stdout='needle\n' if status == 0 else '')
            row.update(dict(stderr_nonempty=True) if status == 2 else dict(stderr=''))
            self.manifest['runtime_cases'].append(row)
        for relative in [*program._SOURCES, 'usr.bin/grep/grep.h', *program._PROVENANCE]:
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(('synthetic source member ' + relative + '\n').encode())
        manifest_path = self.source / program.MANIFEST_RELATIVE
        manifest_path.write_text(json.dumps(self.manifest), encoding='utf-8')
        manifest_ref = dict(path=str(manifest_path), sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest())
        mock = patch.object(program, 'MANIFEST_SHA256', manifest_ref['sha256'])
        mock.start()
        self.addCleanup(mock.stop)
        self.inputs = dict(root=str(self.source), source=self.descriptor['sources']['os'])
        self.plan = dict(format=producer.PRODUCER_FORMAT, version=1, scope='gcc16-bootstrap',
            stage='grep-bootstrap', seed_descriptor={}, host=self.descriptor['host'],
            sources=self.descriptor['sources'], work_root=str(self.work),
            required_roles=['cc', 'sh', *self.descriptor['driver_components'].values()],
            resources=dict(cpu_jobs=1, link_jobs=1, runtime_seconds=120,
                           lock_root=str(self.base), heavy_link_context=str(self.base / 'heavy')),
            program=dict(source=self.inputs, manifest=manifest_ref))
        self.commands, self.flags = self.seed.commands, self.descriptor['abi_flags']
        self.refresh_source()
        self.plan['commands'] = program.program_steps(self.plan, self.descriptor, self.commands, self.flags)

    def refresh_source(self):
        inventory = snapshot_tree(str(self.source))
        members = {}
        for name, row in inventory.items():
            if row['kind'] == 'file':
                raw = (self.source / name).read_bytes()
                members[name] = dict(sha256=row['sha256'], git_blob=hashlib.sha1(
                    b'blob ' + str(len(raw)).encode('ascii') + b'\0' + raw).hexdigest())
        self.inputs['inventory'] = self.f.document(inventory)
        self.proof = dict(format='emberbsd-bootstrap-program-source', version=1,
            source=self.inputs['source'], inventory=self.inputs['inventory'], members=self.f.document(members))
        self.inputs['source_proof'] = self.f.document(self.proof)

    def validate(self):
        return producer.validate_producer_plan(self.plan, self.descriptor, self.commands, self.flags)

    def invoke(self):
        descriptor_ref = self.f.document(self.descriptor)
        self.plan['seed_descriptor'] = descriptor_ref
        plan_ref = self.f.document(self.plan)
        accepted = self.f.document(dict(input_route_audit=self.f.raw(b'synthetic independent acceptance fixture')))
        case = self
        class Selection:
            commands = case.commands
            def as_dict(self):
                return dict(plan=case.validate())
        with patch('emberbsd_sdk.bootstrap_seed.load_bootstrap_seed', side_effect=lambda *a, **kw: Selection()):
            return bootstrap_pilot.run_producer(descriptor_ref['path'], descriptor_sha256=descriptor_ref['sha256'],
                accepted_seed_receipt=accepted, bootstrap_plan=plan_ref, required_roles=self.plan['required_roles'])

    def fake_command(self, argv, log, **kwargs):
        step = next(row for row in self.plan['commands'] if row['id'] == Path(log).stem)
        Path(log).write_bytes(b'synthetic raw combined command log\n')
        if step['phase'] in ('compile', 'link'):
            # A minimal native Mach-O header is test data, not compiler output.
            image = struct.pack('<8I', 0xfeedfacf, 0x100000c, 0,
                                1 if step['phase'] == 'compile' else 2, 0, 0, 0, 0)
            Path(argv[-1]).write_bytes(image)
            Path(argv[-1]).chmod(0o755)
        if step['phase'] == 'link':
            context = self.plan['resources']['heavy_link_context']
            name = '.prebuild-sdk-lock-' + hashlib.sha256(context.encode()).hexdigest()
            with open(self.base / name, 'rb') as handle:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if step['phase'] == 'program-case':
            case = next(row for row in self.manifest['runtime_cases'] if step['id'] == 'case-' + row['name'])
            for stream in ('stdout', 'stderr'):
                value = ('synthetic actual error\n' if stream == 'stderr' and case.get('stderr_nonempty') else
                         case.get(stream, ''))
                (self.work / (case['name'] + '.' + stream)).write_text(value, encoding='utf-8')
        if step['expected_exit']:
            raise CommandError(step['expected_exit'], argv, log=log)
        return subprocess.CompletedProcess(argv, 0)

    def test_admission_and_dispatch_need_no_future_recipe_graph(self):
        with patch('subprocess.Popen', side_effect=AssertionError('no native process')):
            self.assertIs(self.validate(), self.plan)
            self.assertEqual(_plan(self.f.document(self.plan), self.descriptor, self.commands, self.flags), self.plan)
        self.assertNotIn('metadata', self.plan)
        self.assertNotIn('output_bundle_root', self.plan)

    def test_fixed_driver_flags_and_shell_quoting_are_exact(self):
        flags = dict(cppflags=['-DABI'], cflags=['-O1'], cxxflags=[], ldflags=['-LABI'])
        steps = program.program_steps(self.plan, self.descriptor, self.commands, flags)
        self.assertEqual(len(steps), 21)
        for step in steps[:6]:
            self.assertEqual(step['argv'][:len(self.commands['cc'])], list(self.commands['cc']))
            self.assertEqual(step['argv'].count('-O1'), 1)
            self.assertEqual(step['argv'].count('-v'), 1)
            self.assertNotIn('DYLD_PRINT_LIBRARIES', step['env'])
        self.assertEqual(steps[5]['argv'].count('-Wl,--threads=1'), 1)
        self.assertEqual(steps[5]['argv'].count('-LABI'), 1)
        shell = steps[6]
        self.assertEqual(shell['argv'][:len(self.commands['sh'])], list(self.commands['sh']))
        self.assertIn(shlex.quote(self.manifest['runtime_cases'][0]['stdin']), shell['argv'][-1])
        self.assertTrue(all('DYLD_PRINT_LIBRARIES' not in row['env'] for row in steps[:20]))
        self.assertEqual(steps[20]['env']['DYLD_PRINT_LIBRARIES'], '1')
        self.assertEqual(steps[20]['argv'], [str(self.work / 'grep'), '-q', 'needle', 'input.txt'])
        self.assertTrue(all(row['env']['TMPDIR'] == str(self.work) for row in steps))

    def test_changed_source_refuses_before_work_or_child(self):
        (self.source / program._SOURCES[0]).write_bytes(b'changed source')
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged') as run:
            with self.assertRaisesRegex(SDKError, 'source changed'):
                self.invoke()
        run.assert_not_called()
        self.assertFalse(self.work.exists())

    def test_missing_extra_and_symlink_source_rosters_refuse(self):
        extra = self.source / 'usr.bin/grep/extra.h'
        extra.write_bytes(b'extra source')
        self.refresh_source()
        with self.assertRaisesRegex(SDKError, 'exact complete source roster'):
            self.validate()
        extra.unlink()
        extra.symlink_to('grep.h')
        self.refresh_source()
        with self.assertRaisesRegex(SDKError, 'without links'):
            self.validate()
        extra.unlink()
        (self.source / 'usr.bin/grep/grep.h').unlink()
        self.refresh_source()
        with self.assertRaisesRegex(SDKError, 'exact complete source roster'):
            self.validate()

    def test_wrong_git_blob_and_source_chain_refuse(self):
        members = json.loads(Path(self.proof['members']['path']).read_text())
        members[program._SOURCES[0]]['git_blob'] = '0' * 40
        self.proof['members'] = self.f.document(members)
        self.inputs['source_proof'] = self.f.document(self.proof)
        with self.assertRaisesRegex(SDKError, 'Git blob'):
            self.validate()
        self.refresh_source()
        self.inputs['source'] = dict(self.inputs['source'], commit='4' * 40)
        with self.assertRaisesRegex(SDKError, 'OS source pin'):
            self.validate()

    def test_manifest_route_resource_and_environment_overrides_refuse(self):
        for kind in ('manifest', 'argv', 'env', 'cpu', 'budget', 'role'):
            with self.subTest(kind=kind):
                old = copy.deepcopy(self.plan)
                if kind == 'manifest':
                    self.plan['program']['manifest']['sha256'] = '0' * 64
                elif kind == 'argv':
                    self.plan['commands'][0]['argv'].append('-specs=/ambient')
                elif kind == 'env':
                    self.plan['commands'][6]['env']['DYLD_LIBRARY_PATH'] = '/ambient'
                elif kind == 'cpu':
                    self.plan['resources']['cpu_jobs'] = 2
                elif kind == 'budget':
                    self.plan['resources']['runtime_seconds'] = 121
                else:
                    self.plan['required_roles'].remove('sh')
                with self.assertRaises(SDKError):
                    self.validate()
                self.plan.clear()
                self.plan.update(old)
                self.inputs = self.plan['program']['source']

    def test_missing_file_control_refuses_existing_or_dangling_member(self):
        self.work.mkdir()
        missing = self.work / 'missing.txt'
        for link in (False, True):
            if link:
                missing.symlink_to('unavailable')
            else:
                missing.write_bytes(b'not missing')
            with self.assertRaisesRegex(SDKError, 'missing-file control'):
                self.validate()
            missing.unlink()

    def test_synthetic_completion_retains_separate_streams_and_no_qualification(self):
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=self.fake_command) as run:
            result = self.invoke()
        self.assertEqual(run.call_count, 21)
        self.assertEqual(result['status'], 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW')
        self.assertTrue(result['all_commands_drained'] and result['all_leases_closed'])
        self.assertFalse(result['ordinary_provider_qualified'])
        self.assertEqual([row['actual_exit'] for row in result['commands'][6:20]],
                         [row['exit'] for row in self.manifest['runtime_cases']])
        for row in result['commands'][6:20]:
            self.assertEqual(set(row['stdout']), {'path', 'sha256'})
            self.assertEqual(set(row['stderr']), {'path', 'sha256'})
            self.assertNotEqual(row['stdout']['path'], row['log']['path'])
        self.assertEqual(os.readlink(self.work / 'egrep'), 'grep')
        self.assertEqual(os.readlink(self.work / 'fgrep'), 'grep')
        self.assertTrue(all(0 < call.kwargs['timeout_seconds'] <= 120 for call in run.call_args_list))
        self.assertNotIn('all_leases_closed', json.loads((self.work / 'result.json').read_text()))

    def test_wrong_case_stdout_fails_after_actual_exit_without_retry(self):
        def wrong(argv, log, **kwargs):
            result = self.fake_command(argv, log, **kwargs)
            if Path(log).stem == 'case-case0':
                (self.work / 'case0.stdout').write_bytes(b'wrong output\n')
            return result
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=wrong) as run:
            result = self.invoke()
        self.assertEqual(run.call_count, 7)
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(result['commands'][-1]['actual_exit'], 0)
        self.assertIn('actual stdout differs', result['error'])

    def test_unexpected_error_case_success_is_not_accepted(self):
        def wrong(argv, log, **kwargs):
            try:
                return self.fake_command(argv, log, **kwargs)
            except CommandError:
                if Path(log).stem == 'case-case1':
                    return subprocess.CompletedProcess(argv, 0)
                raise
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=wrong) as run:
            result = self.invoke()
        self.assertEqual(run.call_count, 8)
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(result['commands'][-1]['actual_exit'], 0)

    def test_non_native_link_output_cannot_run_cases(self):
        def wrong(argv, log, **kwargs):
            result = self.fake_command(argv, log, **kwargs)
            if Path(log).stem == 'link-grep':
                Path(argv[-1]).write_bytes(b'not a native executable')
            return result
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=wrong) as run:
            result = self.invoke()
        self.assertEqual(run.call_count, 6)
        self.assertEqual(result['status'], 'FAILED')
        self.assertFalse((self.work / 'egrep').exists())

    def test_empty_actual_error_stream_is_not_accepted(self):
        def wrong(argv, log, **kwargs):
            try:
                return self.fake_command(argv, log, **kwargs)
            except CommandError:
                if Path(log).stem == 'case-case11':
                    (self.work / 'case11.stderr').write_bytes(b'')
                raise
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=wrong) as run:
            result = self.invoke()
        self.assertEqual(run.call_count, 18)
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(result['commands'][-1]['actual_exit'], 2)
        self.assertIn('actual stderr', result['error'])


if __name__ == '__main__':
    unittest.main()
