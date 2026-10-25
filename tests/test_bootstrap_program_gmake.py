# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Fixed shell-bootstrap route controls. No GNU source or native tool runs."""
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import struct
import subprocess
import unittest
from unittest.mock import patch

import test_bootstrap_program_gnu as fixtures
from emberbsd_sdk import bootstrap_gnu_program as gnu, bootstrap_program as program
from emberbsd_sdk.bootstrap_seed import _plan
from emberbsd_sdk.errors import CommandError, ProcessOwnershipError, SDKError


class GmakeBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.GNUProgramTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.case = case = self.fixture.case
        # build.sh does not reparse shell quoting from command variables.
        safe_bundle = case.base / 'native-tools'
        shutil.copytree(case.f.bundle, safe_bundle)
        case.commands = {role: tuple(word.replace(str(case.f.bundle), str(safe_bundle)) for word in argv)
                         for role, argv in case.commands.items()}
        for row in case.descriptor['tools'].values():
            row['path'] = row['path'].replace(str(case.f.bundle), str(safe_bundle))
        self.owner, self.source = case.base / 'gmake-owner', case.base / 'gmake-source'
        self.owner.mkdir()
        self.source.mkdir()
        _, recipe, version, _, patches = gnu.MANIFESTS['gmake-bootstrap']
        roles = sorted((gnu._DRIVERS - {'gmake'}) | {'sh', 'cmp', 'hostname', 'awk', 'rm'})
        for role in roles:
            if role not in case.commands:
                path = safe_bundle / ('synthetic-' + role)
                path.write_bytes(b'synthetic accepted tool; never executed\n')
                path.chmod(0o755)
                case.commands[role] = (str(path),)
                case.descriptor['tools'][role] = dict(path=str(path), kind='native', provider='ports')
            case.descriptor['tools'][role]['kind'] = 'native'
        case.descriptor['providers']['ports']['origin'] = 'source-build'
        case.descriptor['history']['ports']['kind'] = 'source-build'
        # Removing both commands makes an accidental input-make dependency causal.
        case.commands.pop('make', None)
        case.commands.pop('gmake', None)
        self.manifest = dict(format='emberbsd-native-bootstrap-program', version=1,
            origin='Synthetic source fixture. No release program is executed.',
            compiler=dict(family='gcc', major=16, abi='native'), initial_only=True,
            name='gmake', build_system='gnu-configure-bootstrap',
            configure_args=['--disable-nls', '--without-guile', '--disable-dependency-tracking'],
            canonical_host_recipe='upstream/pkgsrc/' + recipe + '/Makefile',
            source_makefile='Makefile.in', required_tool_roles=roles, make_targets=[],
            output_programs=dict(gmake='make'), invocations=['gmake'],
            files={'basic.mk': 'all:\n\t@printf fixture\\n\n'}, runtime_cases=[])
        for index in range(8):
            row = dict(name='case' + str(index), invocation='gmake', args=['-f', 'basic.mk'],
                stdin='', exit=0 if index < 5 else 1 if index == 5 else 2,
                stdout='fixture\n' if index < 5 else '')
            row.update(stderr_nonempty=True) if index > 5 else row.update(stderr='')
            self.manifest['runtime_cases'].append(row)
        relative = 'profiles/common-build-tools/bootstrap/gnu-gmake.json'
        directory = 'upstream/pkgsrc/' + recipe
        paths = [directory + '/' + name for name in ('Makefile', 'DESCR', 'PLIST', 'distinfo', 'options.mk')]
        paths += [directory + '/patches/' + name for name in patches]
        for name in paths:
            path = self.owner / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(('synthetic canonical member ' + name + '\n').encode())
        manifest_path = self.owner / relative
        manifest_path.parent.mkdir(parents=True)
        manifest_path.write_text(json.dumps(self.manifest))
        digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        control = patch.dict(gnu.MANIFESTS, {'gmake-bootstrap': ('gmake', recipe, version, digest, patches)})
        control.start()
        self.addCleanup(control.stop)
        archive = case.f.raw(b'synthetic GNU make release bytes, not an actual tar archive')
        raw = Path(archive['path']).read_bytes()
        filename = 'make-' + version + '.tar.gz'
        info = ''.join(name + ' (' + filename + ') = ' + constructor(raw).hexdigest() + '\n'
            for name, constructor in (('SHA512', hashlib.sha512), ('BLAKE2s', hashlib.blake2s)))
        info += 'Size (' + filename + ') = ' + str(len(raw)) + ' bytes\n'
        references = []
        for name in patches:
            path = self.owner / directory / 'patches' / name
            info += 'SHA1 (' + name + ') = ' + gnu._patch_sha1(path.read_bytes()) + '\n'
            references.append(dict(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
        (self.owner / directory / 'distinfo').write_text(info)
        for name in ('configure', 'Makefile.in', 'COPYING', 'README', 'build.sh', 'build.cfg.in'):
            path = self.source / name
            path.write_bytes(('synthetic release member ' + name + '\n').encode())
            path.chmod(0o555 if name in ('configure', 'build.sh') else 0o444)
        self.source.chmod(0o555)
        case.plan.update(stage='gmake-bootstrap', required_roles=sorted(set(roles) |
            set(case.descriptor['driver_components'].values())),
            resources=dict(case.plan['resources'], cpu_jobs=2, runtime_seconds=1200),
            program=dict(source=self.fixture.tree(self.source, owner=False),
                owner=self.fixture.tree(self.owner, owner=True),
                manifest=dict(path=str(manifest_path), sha256=digest), archive=archive, patches=references))
        case.plan['commands'] = program.program_steps(case.plan, case.descriptor, case.commands, case.flags)

    def test_fixed_route_dispatches_without_any_input_make(self):
        with patch('subprocess.Popen', side_effect=AssertionError('no native process')):
            self.assertIs(self.case.validate(), self.case.plan)
            self.assertEqual(_plan(self.case.f.document(self.case.plan), self.case.descriptor,
                self.case.commands, self.case.flags), self.case.plan)
        steps = self.case.plan['commands']
        self.assertEqual(len(steps), 11)
        self.assertEqual(steps[1]['argv'], [*self.case.commands['sh'], str(self.case.work / 'source/build.sh')])
        self.assertEqual(steps[1]['phase'], 'gnu-bootstrap')
        self.assertFalse(any(step['argv'][0].endswith(('bmake', 'gmake')) for step in steps))
        self.assertEqual([step['expected_exit'] for step in steps[2:-1]], [0, 0, 0, 0, 0, 1, 2, 2])

    def test_make_free_configure_requires_dependency_tracking_disabled(self):
        expected = ['--disable-nls', '--without-guile', '--disable-dependency-tracking']
        self.assertEqual(self.case.plan['commands'][0]['argv'][-3:], expected)
        for replacement in (None, '--enable-dependency-tracking'):
            bad = copy.deepcopy(self.manifest)
            bad['configure_args'].remove('--disable-dependency-tracking')
            if replacement:
                bad['configure_args'].append(replacement)
            reference = self.case.plan['program']['manifest']
            Path(reference['path']).write_text(json.dumps(bad))
            digest = hashlib.sha256(Path(reference['path']).read_bytes()).hexdigest()
            reference['sha256'] = digest
            row = gnu.MANIFESTS['gmake-bootstrap']
            with patch.dict(gnu.MANIFESTS, {'gmake-bootstrap': (*row[:3], digest, row[4])}):
                with self.assertRaisesRegex(SDKError, 'unsupported fixed GNU program contract'):
                    gnu.gnu_manifest(self.case.plan)


    def test_selected_driver_ABI_and_compiled_default_shell_are_exact(self):
        env = self.case.plan['commands'][0]['env']
        for name in ('MAKE', 'MAKEFLAGS', 'MFLAGS'):
            self.assertNotIn(name, env)
        self.assertEqual(env['PATH'], str(self.case.work / 'bin'))
        self.assertEqual(env['CONFIG_SITE'], '/dev/null')
        self.assertEqual(shlex.split(env['CC']), list(self.case.commands['cc']))
        # Match the script's unquoted parameter expansion, not shlex parsing.
        self.assertEqual(env['CPPFLAGS'].split(), [*self.case.flags['cppflags'],
            '-DPKGSRC_DEFAULT_SHELL=' + json.dumps(self.case.commands['sh'][0])])
        self.assertEqual(env['EMBERBSD_BOOTSTRAP_EXPECTED_SHELL'], self.case.commands['sh'][0])
        self.assertEqual(shlex.split(env['LDFLAGS']), [*self.case.flags['ldflags'], '-Wl,--threads=1'])

    def test_shell_bootstrap_rejects_words_that_need_shell_reparsing(self):
        for word in ('-I/path with space', '-DUNSAFE=*', "-DUNSAFE='quoted'"):
            with self.subTest(word=word), self.assertRaisesRegex(SDKError, 'literal command and ABI words'):
                gnu.gnu_environment(self.case.plan, self.case.descriptor, self.case.commands,
                                    dict(self.case.flags, cppflags=[word]))

    def test_parallel_semantic_case_requires_the_declared_two_job_budget(self):
        for jobs in (1, 3, True):
            with self.subTest(jobs=jobs):
                self.case.plan['resources']['cpu_jobs'] = jobs
                with self.assertRaises(SDKError):
                    self.case.validate()

    def test_external_make_role_or_command_is_refused(self):
        for name in ('make', 'gmake'):
            with self.subTest(role=name):
                self.case.plan['required_roles'].append(name)
                with self.assertRaisesRegex(SDKError, 'external make role'):
                    self.case.validate()
                self.case.plan['required_roles'].remove(name)
        self.case.plan['commands'][1]['argv'] = ['/external/gmake', '-j1']
        with self.assertRaisesRegex(SDKError, 'fixed source-owned configure/make route'):
            self.case.validate()

    def test_binary_release_shell_cannot_be_selected(self):
        self.case.descriptor['providers']['ports']['origin'] = 'binary-release'
        with self.assertRaisesRegex(SDKError, 'accepted source-built shell'):
            self.case.validate()

    def test_canonical_archive_patches_and_bootstrap_sources_remain_required(self):
        value = self.case.plan['program']
        value['patches'] = list(reversed(value['patches']))
        with self.assertRaisesRegex(SDKError, 'patches are missing, reordered or replaced'):
            self.case.validate()
        value['patches'].reverse()
        (self.source / 'build.cfg.in').chmod(0o644)
        (self.source / 'build.cfg.in').write_bytes(b'changed bootstrap source')
        with self.assertRaisesRegex(SDKError, 'source tree changed'):
            self.case.validate()

    def fake_command(self, argv, log, **kwargs):
        step = next(row for row in self.case.plan['commands'] if row['id'] == Path(log).stem)
        Path(log).write_bytes(b'synthetic drained command log\n')
        if step['phase'] in ('gnu-configure', 'gnu-bootstrap'):
            context = self.case.plan['resources']['heavy_link_context']
            lock = self.case.base / ('.prebuild-sdk-lock-' + hashlib.sha256(context.encode()).hexdigest())
            with lock.open('rb') as handle:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if step['phase'] == 'gnu-bootstrap':
            path = self.case.work / 'source/make'
            path.write_bytes(struct.pack('<8I', 0xfeedfacf, 0x100000c, 0, 2, 0, 0, 0, 0))
            path.chmod(0o755)
        if step['phase'] == 'program-case':
            case = next(row for row in self.manifest['runtime_cases'] if step['id'] == 'case-' + row['name'])
            for stream in ('stdout', 'stderr'):
                data = 'synthetic error\n' if stream == 'stderr' and case.get('stderr_nonempty') else case.get(stream, '')
                (self.case.work / (case['name'] + '.' + stream)).write_text(data)
        if step['expected_exit']:
            raise CommandError(step['expected_exit'], argv, log=log)
        return subprocess.CompletedProcess(argv, 0)

    def test_existing_controller_keeps_heavy_fences_streams_and_initial_scope(self):
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=self.fake_command) as run:
            result = self.case.invoke()
        self.assertEqual(run.call_count, 11)
        self.assertEqual(result['status'], 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW')
        self.assertTrue(all(result[name] for name in
            ('all_commands_drained', 'inputs_revalidated_after_drain', 'all_leases_closed')))
        self.assertFalse(result['ordinary_provider_qualified'])
        self.assertEqual(set(os.listdir(self.case.work / 'bin')),
            set(self.manifest['required_tool_roles']) - gnu._DRIVERS)
        self.assertFalse({'make', 'gmake'} & set(os.listdir(self.case.work / 'bin')))
        self.assertEqual(result['commands'][1]['program_outputs']['gmake']['path'], str(self.case.work / 'source/make'))

    def test_invalid_bootstrap_output_stops_before_semantic_cases(self):
        def wrong(argv, log, **kwargs):
            result = self.fake_command(argv, log, **kwargs)
            if Path(log).stem == 'bootstrap':
                (self.case.work / 'source/make').write_bytes(b'not Mach-O')
            return result
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=wrong) as run:
            result = self.case.invoke()
        self.assertEqual(run.call_count, 2)
        self.assertEqual(result['status'], 'FAILED')
        self.assertTrue(result['all_commands_drained'] and result['inputs_revalidated_after_drain'])

    def test_unproven_bootstrap_drain_retains_all_existing_fences(self):
        observed = []
        class EndFixture(BaseException):
            pass
        def fail(argv, log, **kwargs):
            if Path(log).stem == 'bootstrap':
                Path(log).write_bytes(b'undrained synthetic bootstrap')
                raise ProcessOwnershipError(1234567, 'synthetic shell bootstrap drain', log=log)
            return self.fake_command(argv, log, **kwargs)
        def hold():
            for context in (str(self.owner), str(self.source), str(self.case.work),
                    self.case.plan['program']['archive']['path'], self.case.plan['resources']['heavy_link_context']):
                lock = self.case.base / ('.prebuild-sdk-lock-' + hashlib.sha256(context.encode()).hexdigest())
                with lock.open('rb') as handle:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = json.loads((self.case.work / 'result.json').read_text())
            self.assertEqual(result['status'], 'HOLD_UNPROVEN_DRAIN')
            self.assertNotIn('log', result['commands'][-1])
            observed.append(True)
            raise EndFixture()
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=fail), \
                patch('emberbsd_sdk.bootstrap_pilot._hold', side_effect=hold):
            self.case.invoke()
        self.assertEqual(observed, [True])


if __name__ == '__main__':
    unittest.main()
