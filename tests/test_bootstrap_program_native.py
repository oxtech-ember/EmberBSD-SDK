# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Fixed cmp/hostname controls. All child processes and outputs are synthetic."""

import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import re
import shutil
import unittest
from unittest.mock import patch

import test_bootstrap_program as fixtures
from emberbsd_sdk import bootstrap_program as program
from emberbsd_sdk.bootstrap_seed import _plan
from emberbsd_sdk.errors import ProcessOwnershipError, SDKError


def path_cases(name):
    """Independent semantic expectations; no basename or dirname is executed."""
    normal = 'file' if name == 'basename' else '/usr'
    tails = [
        ('normal', ['/usr/file'], normal + '\n'),
        ('empty', [''], '\n' if name == 'basename' else '.\n'),
        ('root', ['/'], '/\n'),
        ('slashes', ['///'], '/\n'),
        ('trailing-slash', ['/usr/bin/'], 'bin\n' if name == 'basename' else '/usr\n'),
        ('no-directory', ['file'], 'file\n' if name == 'basename' else '.\n'),
        ('option-terminator', ['--', '-file'], '-file\n' if name == 'basename' else '.\n')]
    if name == 'basename':
        tails += [('suffix', ['/usr/file.txt', '.txt'], 'file\n'),
                  ('other-suffix', ['file.txt', '.c'], 'file.txt\n'),
                  ('whole-suffix', ['file', 'file'], 'file\n'),
                  ('empty-suffix', ['file', ''], 'file\n')]
    else:
        tails += [('dot', ['.'], '.\n')]
    result = [dict(name=case, invocation=name, args=args, stdin='', exit=0, stdout=out, stderr='')
              for case, args, out in tails]
    for case, args in [('missing-operand', []),
                       ('extra-operands', ['one', 'two', 'three'] if name == 'basename' else ['one', 'two']),
                       ('invalid-option', ['-z'])]:
        result.append(dict(name=case, invocation=name, args=args, stdin='', exit=1,
                           stdout='', stderr_nonempty=True))
    return result


class NativeProgramTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.ProgramTests(methodName='runTest')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def configure(self, name):
        case = self.case
        shutil.rmtree(case.source)
        case.source.mkdir()
        case.plan['stage'] = name + '-bootstrap'
        relative, _, sources, provenance = program._profile(case.plan)
        case.manifest.update(name=name, canonical_host_recipe='tools/' + name + '/Makefile',
            source_makefile=('bin/hostname' if name == 'hostname' else 'usr.bin/' + name) + '/Makefile',
            sources=list(sources),
            headers=(['usr.bin/cmp/extern.h'] if name == 'cmp' else []) + [program._BOOTSTRAP_CDEFS],
            include_dirs=['usr.bin/cmp'] if name == 'cmp' else [],
            cppflags=['-include', program._BOOTSTRAP_CDEFS], invocations=[name], files={}, runtime_cases=[])
        if name == 'cmp':
            # Empty literal data is intentional. No real cmp binary is run.
            case.manifest['files'] = dict.fromkeys(
                ('same.txt', 'different.txt', 'short.txt', 'skip-left.txt', 'skip-right.txt'), '')
            for index in range(18):
                row = dict(name='case' + str(index), invocation='cmp', args=['same.txt', 'same.txt'],
                           stdin='', exit=0 if index == 0 else 1 if index == 1 else 2, stdout='')
                row.update(dict(stderr='') if index < 2 else dict(stderr_nonempty=True))
                case.manifest['runtime_cases'].append(row)
        elif name in ('basename', 'dirname'):
            case.manifest['runtime_cases'] = path_cases(name)
        else:
            case.manifest['runtime_cases'] = [
                dict(name='read-hostname', invocation='hostname', args=[], stdin='', exit=0,
                     stdout_nonempty=True, stderr=''),
                dict(name='short-hostname', invocation='hostname', args=['-s'], stdin='', exit=0,
                     stdout_nonempty=True, stderr=''),
                dict(name='invalid-option', invocation='hostname', args=['-z'], stdin='', exit=1,
                     stdout='', stderr_nonempty=True)]
        for member in [*sources, *case.manifest['headers'], *provenance]:
            path = case.source / member
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(('synthetic native source ' + member + '\n').encode())
        case.plan['program']['manifest']['path'] = str(case.source / relative)
        self.refresh_manifest(name)

    def refresh_manifest(self, name):
        case = self.case
        path = Path(case.plan['program']['manifest']['path'])
        path.write_text(json.dumps(case.manifest), encoding='utf-8')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        case.plan['program']['manifest']['sha256'] = digest
        mock = patch.object(program, name.upper() + '_MANIFEST_SHA256', digest)
        mock.start()
        self.addCleanup(mock.stop)
        case.refresh_source()
        case.plan['commands'] = program.program_steps(case.plan, case.descriptor, case.commands, case.flags)

    def fake_command(self, argv, log, **kwargs):
        case = self.case
        returned = case.fake_command(argv, log, **kwargs)
        if Path(log).stem in ('case-read-hostname', 'case-short-hostname'):
            name = Path(log).stem.removeprefix('case-')
            value = 'test.host.example\n' if name == 'read-hostname' else 'test\n'
            (case.work / (name + '.stdout')).write_text(value, encoding='utf-8')
        return returned

    def test_both_fixed_stages_dispatch_without_cxx_or_a_recipe_graph(self):
        for name, count in (('cmp', 24), ('hostname', 6)):
            with self.subTest(name=name):
                self.configure(name)
                with patch('subprocess.Popen', side_effect=AssertionError('no native child')):
                    self.assertIs(self.case.validate(), self.case.plan)
                    self.assertEqual(_plan(self.case.f.document(self.case.plan), self.case.descriptor,
                                           self.case.commands, self.case.flags), self.case.plan)
                self.assertEqual(len(self.case.plan['commands']), count)
                self.assertNotIn('cxx', self.case.plan['required_roles'])
                self.assertNotIn('metadata', self.case.plan)

    def test_source_order_forced_header_driver_and_abi_are_exact(self):
        for name in ('cmp', 'hostname'):
            with self.subTest(name=name):
                self.configure(name)
                case = self.case
                flags = dict(cppflags=['-DABI'], cflags=['-O1'], cxxflags=[], ldflags=['-LABI'])
                steps = program.program_steps(case.plan, case.descriptor, case.commands, flags)
                count = len(case.manifest['sources'])
                for step, source in zip(steps[:count], case.manifest['sources']):
                    argv = step['argv']
                    self.assertEqual(argv[:len(case.commands['cc'])], list(case.commands['cc']))
                    self.assertEqual(argv[len(case.commands['cc']):len(case.commands['cc']) + 3],
                                     ['-DABI', '-O1', '-v'])
                    self.assertEqual(argv.count('-include'), 1)
                    self.assertEqual(argv[argv.index('-include') + 1],
                                     str(case.source / program._BOOTSTRAP_CDEFS))
                    self.assertEqual(argv[-4], '-c')
                    self.assertEqual(argv[-3], str(case.source / source))
                    self.assertEqual(argv.count('-I'), int(name == 'cmp'))
                link = steps[count]
                self.assertEqual(link['id'], 'link-' + name)
                self.assertEqual(link['argv'].count('-LABI'), 1)
                self.assertEqual(link['argv'].count('-Wl,--threads=1'), 1)
                self.assertNotIn('-include', link['argv'])
                self.assertEqual(steps[-1]['argv'], [str(case.work / name)] +
                                 (['-s', 'same.txt', 'same.txt'] if name == 'cmp' else []))
                self.assertTrue(all('DYLD_PRINT_LIBRARIES' not in row['env'] for row in steps[:-1]))

    def test_synthetic_cases_complete_with_empty_literal_files_and_separate_streams(self):
        for name, count in (('cmp', 24), ('hostname', 6)):
            with self.subTest(name=name):
                self.configure(name)
                with patch.object(program.socket, 'gethostname', return_value='test.host.example'), \
                        patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=self.fake_command) as run:
                    result = self.case.invoke()
                self.assertEqual(run.call_count, count)
                self.assertEqual(result['status'], 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW')
                self.assertTrue(result['all_commands_drained'] and result['all_leases_closed'] and
                                result['inputs_revalidated_after_drain'])
                self.assertFalse(result['ordinary_provider_qualified'])
                self.assertFalse((self.case.work / 'egrep').exists())
                for row in result['commands']:
                    if row['phase'] == 'program-case':
                        self.assertNotEqual(row['stdout']['path'], row['log']['path'])
                        self.assertNotEqual(row['stderr']['path'], row['log']['path'])
                if name == 'cmp':
                    self.assertEqual((self.case.work / 'same.txt').read_bytes(), b'')
                # Each subtest needs a fresh work path.
                self.case.work = self.case.base / 'hostname-work'
                self.case.plan['work_root'] = str(self.case.work)

    @unittest.skipUnless(os.environ.get('EMBERBSD_TEST_OS_SOURCE'),
                         'set EMBERBSD_TEST_OS_SOURCE for source-owner consistency')
    def test_path_manifests_match_explicit_os_source(self):
        root = Path(os.environ['EMBERBSD_TEST_OS_SOURCE']).resolve(strict=True)
        for name in ('basename', 'dirname'):
            with self.subTest(name=name):
                relative, digest, sources, provenance = program._profile({'stage': name + '-bootstrap'})
                raw = (root / relative).read_bytes()
                self.assertEqual(hashlib.sha256(raw).hexdigest(), digest)
                manifest = json.loads(raw)
                program._native_program_contract(manifest, name)
                self.assertEqual(manifest['runtime_cases'], path_cases(name))
                for member in (*sources, *manifest['headers'], *provenance):
                    self.assertTrue((root / member).is_file(), member)
                    self.assertFalse((root / member).is_symlink(), member)
                recipe = (root / manifest['canonical_host_recipe']).read_text()
                self.assertRegex(recipe, r'HOST_SRCDIR=\s*usr.bin/' + name + r'\n')
                self.assertRegex(recipe, r'NOCOMPATLIB=\s*yes\n')
                self.assertIn('-include ${NETBSDSRCDIR}/' + program._BOOTSTRAP_CDEFS, recipe)
                readme = root / ('tools/' + name + '/README.md')
                for target in re.findall(r'\]\(([^)]+)\)', readme.read_text()):
                    self.assertTrue((readme.parent / target).exists(), target)

    def test_path_programs_use_existing_plan_and_runner(self):
        for name in ('basename', 'dirname'):
            with self.subTest(name=name):
                self.assertIn(name + '-bootstrap', program.PROGRAM_STAGES)
                self.configure(name)
                case = self.case
                self.assertIs(case.validate(), case.plan)
                self.assertEqual(_plan(case.f.document(case.plan), case.descriptor,
                                       case.commands, case.flags), case.plan)
                steps = case.plan['commands']
                self.assertEqual(len(steps), len(path_cases(name)) + 3)
                self.assertEqual(steps[0]['argv'][-4:],
                    ['-c', str(case.source / ('usr.bin/' + name + '/' + name + '.c')),
                     '-o', str(case.work / (name + '.o'))])
                self.assertEqual(steps[-1]['argv'], [str(case.work / name), '/usr/file'])
                self.assertEqual(program.program_sources(case.plan), {})
                with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=self.fake_command) as run:
                    result = case.invoke()
                self.assertEqual(run.call_count, len(steps))
                self.assertEqual(result['status'], 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW')
                self.assertFalse(result['ordinary_provider_qualified'])
                self.assertTrue(result['inputs_revalidated_after_drain'] and result['all_leases_closed'])
                case.work = case.base / (name + '-next-work')
                case.plan['work_root'] = str(case.work)

    def test_path_program_contract_and_output_changes_refuse(self):
        for name in ('basename', 'dirname'):
            self.configure(name)
            original = copy.deepcopy(self.case.manifest)
            for field in ('args', 'stdout', 'exit', 'cases', 'flags', 'sources', 'files'):
                with self.subTest(name=name, field=field):
                    self.case.manifest = copy.deepcopy(original)
                    if field in ('args', 'stdout', 'exit'):
                        self.case.manifest['runtime_cases'][0][field] = {'args': ['unexpected'],
                            'stdout': 'wrong\n', 'exit': 1}[field]
                    elif field == 'cases':
                        self.case.manifest['runtime_cases'].pop()
                    elif field == 'flags':
                        self.case.manifest['cflags'] = ['-w']
                    elif field == 'sources':
                        self.case.manifest['sources'] += ['unowned.c']
                    else:
                        self.case.manifest['files'] = {'unneeded': 'data'}
                    with self.assertRaises(SDKError):
                        self.refresh_manifest(name)
            self.case.manifest = copy.deepcopy(original)
            self.refresh_manifest(name)
            self.case.work.mkdir(exist_ok=True)
            row = original['runtime_cases'][0]
            step = next(s for s in self.case.plan['commands'] if s['id'] == 'case-' + row['name'])
            (self.case.work / (row['name'] + '.stdout')).write_text('wrong\n')
            (self.case.work / (row['name'] + '.stderr')).write_text('')
            with self.assertRaisesRegex(SDKError, 'actual stdout'):
                program.verify_program_step(step, str(self.case.work), self.case.plan)

    def test_path_source_and_route_changes_refuse_before_child(self):
        for name in ('basename', 'dirname'):
            self.configure(name)
            for change in ('header', 'argv', 'env'):
                with self.subTest(name=name, change=change):
                    original = copy.deepcopy(self.case.plan)
                    header = self.case.source / program._BOOTSTRAP_CDEFS
                    raw = header.read_bytes()
                    if change == 'header':
                        header.write_bytes(b'changed')
                    elif change == 'argv':
                        self.case.plan['commands'][-1]['argv'].append('extra')
                    else:
                        self.case.plan['commands'][0]['env']['PATH'] += ':/bin'
                    with patch('emberbsd_sdk.bootstrap_pilot.run_logged') as run:
                        with self.assertRaises(SDKError):
                            self.case.invoke()
                    run.assert_not_called()
                    self.assertFalse(self.case.work.exists())
                    header.write_bytes(raw)
                    self.case.plan.clear()
                    self.case.plan.update(original)
                    self.case.inputs = self.case.plan['program']['source']

    def test_hostname_setter_tails_and_nonfixed_sentinels_refuse(self):
        self.configure('hostname')
        original = copy.deepcopy(self.case.manifest)
        for args in (['new-name'], ['-s', 'new-name'], ['--', 'new-name'], ['-z', 'new-name']):
            with self.subTest(args=args):
                self.case.manifest = copy.deepcopy(original)
                self.case.manifest['runtime_cases'][0]['args'] = args
                with self.assertRaisesRegex(SDKError, 'read-only cases'):
                    self.refresh_manifest('hostname')
        self.case.manifest = copy.deepcopy(original)
        self.case.manifest['runtime_cases'][2]['stdout_nonempty'] = True
        with self.assertRaisesRegex(SDKError, 'read-only cases'):
            self.refresh_manifest('hostname')

    def test_hostname_direct_runtime_tail_cannot_be_overridden(self):
        self.configure('hostname')
        self.case.plan['commands'][-1]['argv'].append('new-name')
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged') as run:
            with self.assertRaisesRegex(SDKError, 'finite route'):
                self.case.invoke()
        run.assert_not_called()
        self.assertFalse(self.case.work.exists())

    def test_hostname_full_and_short_stdout_use_exact_control_plane_oracle(self):
        self.configure('hostname')
        self.case.work.mkdir()
        for name, expected in (('read-hostname', b'test.host.example\n'), ('short-hostname', b'test\n')):
            step = next(row for row in self.case.plan['commands'] if row['id'] == 'case-' + name)
            (self.case.work / (name + '.stderr')).write_bytes(b'')
            with patch.object(program.socket, 'gethostname', return_value='test.host.example'):
                for wrong in (b'', b'another.host\n', expected.rstrip(b'\n')):
                    (self.case.work / (name + '.stdout')).write_bytes(wrong)
                    with self.assertRaisesRegex(SDKError, 'control-plane hostname'):
                        program.verify_program_step(step, str(self.case.work), self.case.plan)
                (self.case.work / (name + '.stdout')).write_bytes(expected)
                self.assertEqual(set(program.verify_program_step(step, str(self.case.work), self.case.plan)),
                                 {'stdout', 'stderr'})

    def test_new_profile_error_stderr_must_be_nonempty(self):
        for name in ('cmp', 'hostname'):
            with self.subTest(name=name):
                self.configure(name)
                self.case.work.mkdir(exist_ok=True)
                row = next(row for row in self.case.manifest['runtime_cases'] if row.get('stderr_nonempty'))
                step = next(step for step in self.case.plan['commands'] if step['id'] == 'case-' + row['name'])
                for stream in ('stdout', 'stderr'):
                    (self.case.work / (row['name'] + '.' + stream)).write_bytes(b'')
                with self.assertRaisesRegex(SDKError, 'actual stderr'):
                    program.verify_program_step(step, str(self.case.work), self.case.plan)

    def test_source_header_and_git_blob_changes_refuse_before_child(self):
        self.configure('cmp')
        (self.case.source / program._BOOTSTRAP_CDEFS).write_bytes(b'changed header')
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged') as run:
            with self.assertRaisesRegex(SDKError, 'source changed'):
                self.case.invoke()
        run.assert_not_called()
        self.case.refresh_source()
        members = json.loads(Path(self.case.proof['members']['path']).read_text())
        members[program._BOOTSTRAP_CDEFS]['git_blob'] = '0' * 40
        self.case.proof['members'] = self.case.f.document(members)
        self.case.inputs['source_proof'] = self.case.f.document(self.case.proof)
        with self.assertRaisesRegex(SDKError, 'Git blob'):
            self.case.validate()

    def test_extra_member_and_source_header_link_refuse(self):
        self.configure('hostname')
        extra = self.case.source / 'extra.c'
        extra.write_bytes(b'extra source')
        self.case.refresh_source()
        with self.assertRaisesRegex(SDKError, 'complete source roster'):
            self.case.validate()
        extra.unlink()
        header = self.case.source / program._BOOTSTRAP_CDEFS
        header.unlink()
        header.symlink_to('../../bin/hostname/hostname.c')
        self.case.refresh_source()
        with self.assertRaisesRegex(SDKError, 'without links'):
            self.case.validate()

    def test_source_flags_and_semantic_counts_cannot_expand(self):
        self.configure('cmp')
        self.case.manifest['cflags'] = ['-w']
        with self.assertRaisesRegex(SDKError, 'direct-source cmp contract'):
            self.refresh_manifest('cmp')
        self.case.manifest['cflags'] = []
        self.case.manifest['runtime_cases'].pop()
        with self.assertRaisesRegex(SDKError, 'fixed cmp semantic cases'):
            self.refresh_manifest('cmp')

    def test_hostname_failed_drain_holds_source_work_and_link_leases(self):
        self.configure('hostname')
        observed = []
        class EndFixture(BaseException):
            pass
        def fail(argv, log, **kwargs):
            if Path(log).stem != 'link-hostname':
                return self.fake_command(argv, log, **kwargs)
            Path(log).write_bytes(b'synthetic unproven link drainage\n')
            raise ProcessOwnershipError(1234567, 'synthetic hostname drain', log=log)
        def hold():
            for context in (str(self.case.source), str(self.case.work),
                            self.case.plan['resources']['heavy_link_context']):
                lock = self.case.base / ('.prebuild-sdk-lock-' + hashlib.sha256(context.encode()).hexdigest())
                with lock.open('rb') as handle:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = json.loads((self.case.work / 'result.json').read_text())
            self.assertEqual(result['status'], 'HOLD_UNPROVEN_DRAIN')
            self.assertNotIn('log', result['commands'][-1])
            self.assertEqual(result['commands'][-1]['actual_exit'], None)
            observed.append('held')
            raise EndFixture()
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=fail), \
                patch('emberbsd_sdk.bootstrap_pilot._hold', side_effect=hold):
            self.case.invoke()
        self.assertEqual(observed, ['held'])

    def test_grep_existing_complete_route_and_source_bytes_remain_exact(self):
        case = self.case
        steps = case.plan['commands']
        self.assertEqual(program.program_sources(case.plan),
                         {name: data.encode() for name, data in case.manifest['files'].items()})
        objects = [str(case.work / (Path(source).stem + '.o')) for source in program._SOURCES]
        for step, source, output in zip(steps[:5], program._SOURCES, objects):
            self.assertEqual(step['argv'], [*case.commands['cc'], *case.flags['cppflags'],
                *case.flags['cflags'], '-v', '-DWITHOUT_NLS', '-DWITHOUT_BZ2', '-DWITHOUT_GZIP',
                '-I', str(case.source / 'usr.bin/grep'), '-c', str(case.source / source), '-o', output])
        self.assertEqual(steps[5]['argv'], [*case.commands['cc'], *case.flags['cflags'], '-v',
            *objects, *case.flags['ldflags'], '-Wl,--threads=1', '-o', str(case.work / 'grep')])
        for case_row, step in zip(case.manifest['runtime_cases'], steps[6:20]):
            command = "printf '%s' " + shlex.quote(case_row['stdin']) + ' | ' + shlex.join(
                [str(case.work / case_row['invocation']), *case_row['args']])
            for stream, redirection in (('stdout', ' >'), ('stderr', ' 2>')):
                command += redirection + shlex.quote(str(case.work / (case_row['name'] + '.' + stream)))
            self.assertEqual(step['argv'], [*case.commands['sh'], '-c', command])
        self.assertEqual(steps[-1]['argv'], [str(case.work / 'grep'), '-q', 'needle', 'input.txt'])
        self.assertEqual(program.program_outputs(case.plan), [*case.manifest['files'],
            *(Path(source).stem + '.o' for source in program._SOURCES), 'grep',
            *(row['name'] + suffix for row in case.manifest['runtime_cases'] for suffix in ('.stdout', '.stderr'))])


if __name__ == '__main__':
    unittest.main()
