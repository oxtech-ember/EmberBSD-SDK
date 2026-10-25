# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Fixed GNU producer controls. No configure, make, compiler, or utility runs."""

import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import struct
import subprocess
import unittest
from unittest.mock import patch

import test_bootstrap_program as fixtures
from emberbsd_sdk import bootstrap_gnu_program as gnu, bootstrap_program as program
from emberbsd_sdk.bootstrap_seed import _plan
from emberbsd_sdk.errors import CommandError, ProcessOwnershipError, SDKError
from emberbsd_sdk.inventory import snapshot_tree


class GNUProgramTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.ProgramTests(methodName='runTest')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

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
        value = dict(root=str(root), source=self.case.descriptor['sources']['ports'],
                     inventory=self.case.f.document(inventory))
        proof = dict(format='emberbsd-bootstrap-program-source', version=1, source=value['source'],
                     inventory=value['inventory'], members=self.case.f.document(members))
        value['source_proof'] = self.case.f.document(proof)
        return value

    def configure(self, name='diffutils'):
        case = self.case
        stage = name + '-bootstrap'
        _, recipe, version, _, patches = gnu.MANIFESTS[stage]
        owner, source = case.base / (name + '-owner'), case.base / (name + '-source')
        owner.mkdir()
        source.mkdir()
        raw_roles = {'sh', 'awk', 'cmp', 'hostname', 'grep', 'rm'} | ({'diff'} if name == 'findutils' else set())
        roles = sorted(gnu._DRIVERS | raw_roles)
        for role in roles:
            if role not in case.commands:
                path = case.f.bundle / ('synthetic-' + role)
                path.write_bytes(b'synthetic accepted native tool; never executed\n')
                path.chmod(0o755)
                case.commands[role] = (str(path),)
                case.descriptor['tools'][role] = dict(path=str(path), kind='native', provider='ports')
            case.descriptor['tools'][role]['kind'] = 'native'
        case.commands['gmake'] = (case.commands['gmake'][0], 'SHELL=' + case.commands['sh'][0],
                                  'MAKECONF=' + str(case.base / 'synthetic-makeconf'))
        targets = ([dict(directory='lib', targets=['all']),
                    dict(directory='src', targets=['paths.h', 'version.c', 'version.h']),
                    dict(directory='src', targets=['diff'])] if name == 'diffutils' else
                   [dict(directory='gl/lib', targets=['all']), dict(directory='lib', targets=['libfind.a']),
                    dict(directory='find', targets=['find']), dict(directory='xargs', targets=['xargs'])])
        manifest = dict(format='emberbsd-native-bootstrap-program', version=1, origin='Synthetic source fixture.',
            compiler=dict(family='gcc', major=16, abi='native'), build_system='gnu-configure',
            configure_args=['--disable-nls'], initial_only=True, name=name,
            canonical_host_recipe='pkgsrc/' + recipe + '/Makefile',
            source_makefile='src/Makefile.in' if name == 'diffutils' else 'Makefile.in',
            required_tool_roles=roles, make_targets=targets,
            output_programs=dict(diff='src/diff') if name == 'diffutils' else dict(find='find/find', xargs='xargs/xargs'),
            invocations=['diff'] if name == 'diffutils' else ['find', 'xargs'],
            files={'empty.txt': ''} if name == 'diffutils' else {'tree/sub/a.txt': 'alpha\n'}, runtime_cases=[])
        for index in range(7 if name == 'diffutils' else 15):
            invocation = 'diff' if name == 'diffutils' else 'find' if index < 2 else 'xargs'
            row = dict(name='case' + str(index), invocation=invocation, args=[], stdin="a 'b c'\n",
                       exit=0 if index == 0 else 1 if index == 1 else 2 if name == 'diffutils' else 123,
                       stdout='')
            row.update(dict(stderr_nonempty=True) if index == 2 else dict(stderr=''))
            if invocation == 'xargs' or name == 'findutils' and index == 1:
                row.update(command_role='sh', command_args=['-c', "printf '<%s>\\n' \"$@\"", 'sh'])
            manifest['runtime_cases'].append(row)
        relative = 'profiles/common-build-tools/bootstrap/gnu-' + name + '.json'
        paths = ['pkgsrc/' + recipe + '/' + leaf for leaf in ('Makefile', 'DESCR', 'PLIST', 'distinfo')]
        paths += ['pkgsrc/' + recipe + '/patches/' + leaf for leaf in patches]
        if name == 'diffutils':
            paths.append('pkgsrc/' + recipe + '/options.mk')
        for path in [*paths, relative]:
            (owner / path).parent.mkdir(parents=True, exist_ok=True)
            (owner / path).write_bytes(('synthetic owner source ' + path + '\n').encode())
        (owner / relative).write_text(json.dumps(manifest), encoding='utf-8')
        digest = hashlib.sha256((owner / relative).read_bytes()).hexdigest()
        mocked = patch.dict(gnu.MANIFESTS, {stage: (name, recipe, version, digest, patches)})
        mocked.start()
        self.addCleanup(mocked.stop)
        archive = case.f.raw(b'synthetic original GNU archive; source data only')
        raw = Path(archive['path']).read_bytes()
        filename = name + '-' + version + '.tar.xz'
        info = '\n'.join(algorithm + ' (' + filename + ') = ' + constructor(raw).hexdigest()
            for algorithm, constructor in (('SHA256', hashlib.sha256), ('SHA512', hashlib.sha512),
                                            ('BLAKE2s', hashlib.blake2s)))
        info += '\nSize (' + filename + ') = ' + str(len(raw)) + ' bytes\n'
        patch_refs = []
        for leaf in patches:
            path = owner / ('pkgsrc/' + recipe + '/patches/' + leaf)
            info += 'SHA1 (' + leaf + ') = ' + hashlib.sha1(path.read_bytes()).hexdigest() + '\n'
            patch_refs.append(dict(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
        (owner / ('pkgsrc/' + recipe + '/distinfo')).write_text(info, encoding='utf-8')
        for path in {'configure', 'Makefile.in', 'COPYING', manifest['source_makefile']}:
            (source / path).parent.mkdir(parents=True, exist_ok=True)
            (source / path).write_bytes(('synthetic GNU source ' + path + '\n').encode())
            (source / path).chmod(0o555 if path == 'configure' else 0o444)
        for directory, _, _ in os.walk(source, topdown=False):
            Path(directory).chmod(0o555)
        case.plan.update(stage=stage, resources=dict(case.plan['resources'], runtime_seconds=1200),
            required_roles=sorted(set(roles) | set(case.descriptor['driver_components'].values())),
            program=dict(source=self.tree(source, owner=False), owner=self.tree(owner, owner=True),
                         manifest=dict(path=str(owner / relative), sha256=digest), archive=archive, patches=patch_refs))
        case.manifest = manifest
        case.plan['commands'] = program.program_steps(case.plan, case.descriptor, case.commands, case.flags)
        self.owner, self.source = owner, source

    def fake_command(self, argv, log, **kwargs):
        case = self.case
        step = next(row for row in case.plan['commands'] if row['id'] == Path(log).stem)
        Path(log).write_bytes(b'synthetic GNU command log; no actual source program\n')
        if step['phase'] in ('gnu-configure', 'gnu-build'):
            context = case.plan['resources']['heavy_link_context']
            lock = case.base / ('.prebuild-sdk-lock-' + hashlib.sha256(context.encode()).hexdigest())
            with lock.open('rb') as handle:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if step['phase'] == 'gnu-build':
            row = case.manifest['make_targets'][int(step['id'].removeprefix('build-'))]
            for relative in case.manifest['output_programs'].values():
                if str(Path(relative).parent) == row['directory'] and Path(relative).name in row['targets']:
                    path = case.work / 'source' / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(struct.pack('<8I', 0xfeedfacf, 0x100000c, 0, 2, 0, 0, 0, 0))
                    path.chmod(0o755)
        if step['phase'] == 'program-case':
            row = next(row for row in case.manifest['runtime_cases'] if step['id'] == 'case-' + row['name'])
            for stream in ('stdout', 'stderr'):
                data = 'synthetic actual error\n' if stream == 'stderr' and row.get('stderr_nonempty') else row.get(stream, '')
                (case.work / (row['name'] + '.' + stream)).write_text(data, encoding='utf-8')
        if step['expected_exit']:
            raise CommandError(step['expected_exit'], argv, log=log)
        return subprocess.CompletedProcess(argv, 0)

    def test_both_variants_dispatch_without_any_recipe_graph(self):
        for name, count in (('diffutils', 12), ('findutils', 22)):
            with self.subTest(name=name):
                self.configure(name)
                with patch('subprocess.Popen', side_effect=AssertionError('no native child')):
                    self.assertIs(self.case.validate(), self.case.plan)
                    self.assertEqual(_plan(self.case.f.document(self.case.plan), self.case.descriptor,
                        self.case.commands, self.case.flags), self.case.plan)
                self.assertEqual(len(self.case.plan['commands']), count)
                self.assertNotIn('metadata', self.case.plan)
                self.assertNotIn('recipes', self.case.plan)

    def test_selected_gcc_abi_make_shell_and_triplets_remain_exact(self):
        self.configure()
        case = self.case
        flags = dict(cppflags=['-DABI'], cflags=['-O1'], cxxflags=['-O2'], ldflags=['-LABI'])
        steps = program.program_steps(case.plan, case.descriptor, case.commands, flags)
        env = steps[0]['env']
        for variable, role in (('CC', 'cc'), ('CXX', 'cxx'), ('CPP', 'cpp'), ('AS', 'as'), ('LD', 'ld'),
                ('AR', 'ar'), ('NM', 'nm'), ('RANLIB', 'ranlib'), ('STRIP', 'strip'),
                ('SHELL', 'sh'), ('CONFIG_SHELL', 'sh'), ('MAKE', 'gmake')):
            self.assertEqual(shlex.split(env[variable]), list(case.commands[role]))
        self.assertEqual(env['CPPFLAGS'], '-DABI')
        self.assertEqual(env['CFLAGS'], '-O1')
        self.assertEqual(env['CXXFLAGS'], '-O2')
        self.assertEqual(shlex.split(env['LDFLAGS']), ['-LABI', '-Wl,--threads=1'])
        self.assertEqual(env['CONFIG_SITE'], '/dev/null')
        self.assertEqual(env['PATH'], str(case.work / 'bin'))
        self.assertFalse(any(key.startswith(('ac_cv_', 'gl_cv_', 'gt_cv_')) for key in env))
        for kind in ('build', 'host'):
            self.assertIn('--' + kind + '=' + case.descriptor['compiler']['triplet'], steps[0]['argv'])
        self.assertEqual(steps[0]['argv'][-1], '--disable-nls')
        for step in steps[1:4]:
            self.assertEqual(step['argv'][:len(case.commands['gmake'])], list(case.commands['gmake']))
            self.assertEqual(step['argv'].count('-j1'), 1)

    def test_synthetic_completion_and_path_exclude_failed_apple_tools(self):
        for name, count in (('diffutils', 12), ('findutils', 22)):
            with self.subTest(name=name):
                self.configure(name)
                with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=self.fake_command) as run:
                    result = self.case.invoke()
                self.assertEqual(run.call_count, count)
                self.assertEqual(result['status'], 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW')
                self.assertTrue(result['all_commands_drained'] and result['all_leases_closed'] and
                                result['inputs_revalidated_after_drain'])
                self.assertFalse(result['ordinary_provider_qualified'])
                aliases = set(os.listdir(self.case.work / 'bin'))
                self.assertEqual(aliases, set(self.case.manifest['required_tool_roles']) - gnu._DRIVERS)
                self.assertFalse(aliases & {'find', 'xargs', 'cc', 'cxx', 'cpp', 'make', 'gmake'})
                self.assertEqual('diff' in aliases, name == 'findutils')
                for alias in aliases:
                    self.assertEqual(os.readlink(self.case.work / 'bin' / alias), self.case.commands[alias][0])
                if name == 'diffutils':
                    self.assertEqual((self.case.work / 'empty.txt').read_bytes(), b'')
                else:
                    self.assertEqual((self.case.work / 'tree/sub/a.txt').read_bytes(), b'alpha\n')
                self.case.work = self.case.base / 'findutils-work'
                self.case.plan['work_root'] = str(self.case.work)

    def test_selected_shell_children_never_use_an_implicit_xargs_command(self):
        self.configure('findutils')
        steps = self.case.plan['commands']
        for row, step in zip(self.case.manifest['runtime_cases'], steps[5:20]):
            text = step['argv'][-1]
            self.assertNotIn('/bin/echo', text)
            if row['invocation'] == 'xargs' or 'command_role' in row:
                self.assertIn(shlex.join([*self.case.commands['sh'], *row['command_args']]), text)
        self.case.manifest['runtime_cases'][2].pop('command_role')
        self.case.manifest['runtime_cases'][2].pop('command_args')
        self.refresh_manifest()
        with self.assertRaisesRegex(SDKError, 'implicit xargs'):
            self.case.validate()

    def refresh_manifest(self):
        value = self.case.plan['program']
        path = Path(value['manifest']['path'])
        path.write_text(json.dumps(self.case.manifest), encoding='utf-8')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        value['manifest']['sha256'] = digest
        old = gnu.MANIFESTS[self.case.plan['stage']]
        mocked = patch.dict(gnu.MANIFESTS, {self.case.plan['stage']: (*old[:3], digest, old[4])})
        mocked.start()
        self.addCleanup(mocked.stop)
        value['owner'] = self.tree(self.owner, owner=True)

    def test_custom_child_role_and_unrecognized_manifest_refuse(self):
        self.configure('findutils')
        self.case.manifest['runtime_cases'][2]['command_role'] = 'env'
        self.refresh_manifest()
        with self.assertRaisesRegex(SDKError, 'exact selected shell'):
            self.case.validate()
        self.case.plan['program']['manifest']['sha256'] = '0' * 64
        with self.assertRaisesRegex(SDKError, 'source-owner manifest'):
            self.case.validate()

    def test_omitted_reordered_or_changed_canonical_patches_refuse(self):
        self.configure()
        value = self.case.plan['program']
        original = copy.deepcopy(value['patches'])
        for changed in (original[:-1], list(reversed(original)), [dict(original[0], sha256='0' * 64), *original[1:]]):
            value['patches'] = changed
            with self.assertRaisesRegex(SDKError, 'patches are missing, reordered or replaced'):
                self.case.validate()
        value['patches'] = original
        Path(original[0]['path']).write_bytes(b'changed canonical patch')
        with self.assertRaisesRegex(SDKError, 'source tree changed'):
            self.case.validate()

    def test_patch_digest_filters_only_complete_lf_rcs_records(self):
        self.assertEqual(gnu._patch_sha1(b'first\r\n$NetBSD: fixture $\r\n\xff\rbare\nlast'),
                         hashlib.sha1(b'first\r\n\xff\rbare\nlast').hexdigest())
        self.assertEqual(gnu._patch_sha1(b'body\n$NetBSD: final unterminated record'),
                         hashlib.sha1(b'body\n').hexdigest())

    def test_canonical_patch_digest_excludes_rcs_but_keeps_raw_sha256_and_body_checks(self):
        self.configure()
        value = self.case.plan['program']
        reference = value['patches'][0]
        path = Path(reference['path'])
        info = self.owner / 'pkgsrc/devel/diffutils/distinfo'
        before_digest = hashlib.sha1(path.read_bytes()).hexdigest()
        body = b'\n--- value.orig\n+++ value\n@@ -1 +1 @@\n-before\n+after\n'
        digest = hashlib.sha1(body).hexdigest()
        canonical = info.read_text().replace(' = ' + before_digest + '\n', ' = ' + digest + '\n')

        def refresh(data, distinfo):
            path.write_bytes(data)
            info.write_text(distinfo)
            reference['sha256'] = hashlib.sha256(data).hexdigest()
            value['owner'] = self.tree(self.owner, owner=True)

        data = b'$NetBSD: first RCS record $\n' + body
        refresh(data, canonical)
        self.case.validate()
        raw_digest = hashlib.sha1(data).hexdigest()
        refresh(data, canonical.replace(' = ' + digest + '\n', ' = ' + raw_digest + '\n'))
        with self.assertRaisesRegex(SDKError, 'canonical patch differs from distinfo'):
            self.case.validate()
        refresh(b'$NetBSD: changed RCS record $\n' + body, canonical)
        self.case.validate()
        reference['sha256'] = '0' * 64
        with self.assertRaisesRegex(SDKError, 'patches are missing, reordered or replaced'):
            self.case.validate()
        refresh(b'$NetBSD: changed RCS record $\n' + body.replace(b'+after', b'+changed'), canonical)
        with self.assertRaisesRegex(SDKError, 'canonical patch differs from distinfo'):
            self.case.validate()

    def test_archive_and_canonical_distinfo_hashes_remain_mandatory(self):
        self.configure()
        reference = self.case.plan['program']['archive']
        Path(reference['path']).write_bytes(b'corrupt source archive')
        reference['sha256'] = hashlib.sha256(Path(reference['path']).read_bytes()).hexdigest()
        with self.assertRaisesRegex(SDKError, 'canonical SHA256'):
            self.case.validate()

    def test_owner_membership_and_release_mtime_proof_are_distinct(self):
        self.configure()
        value = self.case.plan['program']
        proof = json.loads(Path(value['owner']['source_proof']['path']).read_text())
        members = json.loads(Path(proof['members']['path']).read_text())
        next(iter(members.values()))['git_blob'] = '0' * 40
        proof['members'] = self.case.f.document(members)
        value['owner']['source_proof'] = self.case.f.document(proof)
        with self.assertRaisesRegex(SDKError, 'owner Git blob'):
            self.case.validate()
        value['owner'] = self.tree(self.owner, owner=True)
        path = self.source / 'configure'
        os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1000000))
        with self.assertRaisesRegex(SDKError, 'full mtime proof'):
            self.case.validate()

    def test_copy_preserves_full_mtimes_and_only_adds_private_write_permissions(self):
        self.configure()
        self.case.work.mkdir()
        before = snapshot_tree(str(self.source))
        gnu.prepare_gnu_work(self.case.plan, self.case.descriptor, self.case.commands)
        copied = snapshot_tree(str(self.case.work / 'source'))
        for name, row in before.items():
            self.assertEqual(copied[name], dict(row, mode=row['mode'] | (0o700 if row['kind'] == 'directory' else 0o200)))
            self.assertEqual((self.source / name).stat().st_mtime_ns,
                             (self.case.work / 'source' / name).stat().st_mtime_ns)
        self.assertEqual(snapshot_tree(str(self.source)), before)

    def test_changed_copy_mtime_refuses_before_any_child(self):
        self.configure()
        original = gnu.shutil.copy2
        def wrong(source, destination):
            result = original(source, destination)
            path = Path(result)
            os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1000000))
            return result
        with patch.object(gnu.shutil, 'copy2', side_effect=wrong), \
                patch('emberbsd_sdk.bootstrap_pilot.run_logged') as run:
            with self.assertRaisesRegex(SDKError, 'staged GNU source'):
                self.case.invoke()
        run.assert_not_called()

    def test_release_links_extra_owner_members_and_missing_roles_refuse(self):
        self.configure()
        alias = self.source / 'alias'
        self.source.chmod(0o755)  # Allow only this synthetic refusal mutation.
        alias.symlink_to('configure')
        self.case.plan['program']['source'] = self.tree(self.source, owner=False)
        with self.assertRaisesRegex(SDKError, 'contains a link'):
            self.case.validate()
        alias.unlink()
        self.case.plan['program']['source'] = self.tree(self.source, owner=False)
        extra = self.owner / 'extra.txt'
        extra.write_bytes(b'extra owner source')
        self.case.plan['program']['owner'] = self.tree(self.owner, owner=True)
        with self.assertRaisesRegex(SDKError, 'exact canonical recipe'):
            self.case.validate()
        extra.unlink()
        self.case.plan['program']['owner'] = self.tree(self.owner, owner=True)
        self.case.plan['required_roles'].remove('cmp')
        with self.assertRaisesRegex(SDKError, 'roles are incomplete'):
            self.case.validate()

    def test_binary_release_prerequisites_cannot_replace_source_built_tools(self):
        self.configure('findutils')
        for role in ('cmp', 'hostname', 'diff'):
            old = self.case.descriptor['tools'][role]['provider']
            self.case.descriptor['tools'][role]['provider'] = 'synthetic-apple'
            self.case.descriptor['history']['synthetic-apple'] = dict(kind='binary-release')
            with self.subTest(role=role), self.assertRaisesRegex(SDKError, 'accepted source builds: ' + role):
                self.case.validate()
            self.case.descriptor['tools'][role]['provider'] = old

    def test_override_env_targets_parallelism_and_nested_launcher_refuse(self):
        self.configure()
        original = copy.deepcopy(self.case.plan)
        for variant in ('env', 'target', 'parallel', 'nested', 'raw-compiler'):
            with self.subTest(variant=variant):
                if variant == 'env':
                    self.case.plan['commands'][0]['env']['PATH'] += ':/usr/bin'
                elif variant == 'target':
                    self.case.plan['commands'][1]['argv'][-1] = 'install'
                elif variant == 'parallel':
                    self.case.plan['resources']['cpu_jobs'] = 2
                elif variant == 'nested':
                    self.case.plan['link_control'] = dict(python={}, launcher={})
                else:
                    self.case.plan['commands'][0]['env']['CC'] = self.case.commands['cc'][0]
                with self.assertRaises(SDKError):
                    self.case.validate()
                self.case.plan = copy.deepcopy(original)

    def test_error_stream_and_native_executable_headers_are_checked(self):
        self.configure()
        def wrong(argv, log, **kwargs):
            returned = self.fake_command(argv, log, **kwargs)
            if Path(log).stem == 'build-02':
                (self.case.work / 'source/src/diff').write_bytes(b'not a native executable')
            return returned
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=wrong) as run:
            result = self.case.invoke()
        self.assertEqual(run.call_count, 4)
        self.assertEqual(result['status'], 'FAILED')
        self.assertTrue(result['all_commands_drained'] and result['inputs_revalidated_after_drain'])

    def test_empty_actual_error_stderr_is_rejected_after_its_expected_exit(self):
        self.configure()
        def wrong(argv, log, **kwargs):
            try:
                return self.fake_command(argv, log, **kwargs)
            except CommandError:
                if Path(log).stem == 'case-case2':
                    (self.case.work / 'case2.stderr').write_bytes(b'')
                raise
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=wrong):
            result = self.case.invoke()
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(result['commands'][-1]['actual_exit'], 2)
        self.assertIn('actual stderr', result['error'])

    def test_extra_configure_options_and_raw_shell_suffix_refuse(self):
        self.configure()
        self.case.manifest['configure_args'].append('--disable-checks')
        self.refresh_manifest()
        with self.assertRaisesRegex(SDKError, 'fixed GNU program contract'):
            self.case.validate()
        self.case.manifest['configure_args'] = ['--disable-nls']
        self.refresh_manifest()
        self.case.commands['sh'] = (*self.case.commands['sh'], '-x')
        with self.assertRaisesRegex(SDKError, 'accepted native spelling: sh'):
            self.case.validate()

    def test_unproven_configure_drain_holds_source_owner_archive_and_link_leases(self):
        self.configure()
        observed = []
        class EndFixture(BaseException):
            pass
        def fail(argv, log, **kwargs):
            Path(log).write_bytes(b'synthetic unproven configure drain')
            raise ProcessOwnershipError(1234567, 'synthetic GNU drain', log=log)
        def hold():
            contexts = (str(self.owner), str(self.source), str(self.case.work),
                        self.case.plan['program']['archive']['path'], self.case.plan['resources']['heavy_link_context'])
            for context in contexts:
                lock = self.case.base / ('.prebuild-sdk-lock-' + hashlib.sha256(context.encode()).hexdigest())
                with lock.open('rb') as handle:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = json.loads((self.case.work / 'result.json').read_text())
            self.assertEqual(result['status'], 'HOLD_UNPROVEN_DRAIN')
            self.assertNotIn('log', result['commands'][0])
            observed.append('held')
            raise EndFixture()
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=fail), \
                patch('emberbsd_sdk.bootstrap_pilot._hold', side_effect=hold):
            self.case.invoke()
        self.assertEqual(observed, ['held'])


if __name__ == '__main__':
    unittest.main()
