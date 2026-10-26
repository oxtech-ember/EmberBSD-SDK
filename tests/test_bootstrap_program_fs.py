# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Data-only filesystem case checks. No native program is executed."""

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_core  # Adds the SDK source to sys.path.
from emberbsd_sdk import bootstrap_program_fs as fs
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk import bootstrap_program as program


def regular(mode='0600', data='owned\n'):
    return dict(type='file', mode=mode, data=data)


class FilesystemCases(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.case = dict(name='modes', filesystem=dict(umask='0022',
            fixtures={'file': regular(), 'peer': dict(type='hardlink', target='file')},
            expect={'file': regular('0640'), 'peer': dict(regular('0640'), same_inode_as='file')}))

    def test_actual_modes_and_inode_relation_then_retained_report(self):
        root = fs.prepare(self.work, self.case)
        os.chmod(root / 'file', 0o640)
        reference = fs.observe(self.work, self.case)
        fs.verify_report(reference, self.case)
        report = json.loads(Path(reference['path']).read_text())
        self.assertEqual(report['entries']['peer']['same_inode_as'], 'file')
        self.assertFalse(any({'inode', 'device'} & set(row) for row in report['entries'].values()))

    def test_mismatch_and_unexpected_entry_refuse(self):
        root = fs.prepare(self.work, self.case)
        with self.assertRaises(SDKError):
            fs.observe(self.work, self.case)

        os.chmod(root / 'file', 0o640)
        (root / 'extra').write_text('unexpected')
        with self.assertRaises(SDKError):
            fs.observe(self.work, self.case)

    def test_symlink_identity_never_follows_its_target(self):
        case = dict(name='links', filesystem=dict(umask='0022', fixtures={
            'one': dict(type='symlink', target='missing')}, expect={
            'one': dict(type='symlink', target='missing'),
            'two': dict(type='symlink', target='missing', same_inode_as='one'),
            'missing': dict(type='absent')}))
        root = fs.prepare(self.work, case)
        os.link(root / 'one', root / 'two', follow_symlinks=False)
        fs.verify_report(fs.observe(self.work, case), case)

    def test_schema_rejects_escape_unknowns_and_unasserted_fixtures(self):
        mutations = [lambda v: v['expect'].pop('file'),
            lambda v: v.update(hook='true'),
            lambda v: v['fixtures'].update({'../escape': regular()}),
            lambda v: v['fixtures'].update({'/absolute': regular()}),
            lambda v: v['fixtures'].update({'File': regular()}),
            lambda v: v['fixtures'].update({'file/child': regular()}),
            lambda v: v['fixtures']['peer'].update(target='peer'),
            lambda v: v['expect']['peer'].update(same_inode_as='peer'),
            lambda v: v.update(umask='0022; true')]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                case = copy.deepcopy(self.case)
                mutate(case['filesystem'])
                with self.assertRaises(SDKError):
                    fs.validate(case['filesystem'])

    def test_safe_hidden_names_preserve_no_follow_and_identity_checks(self):
        case = dict(name='hidden', filesystem=dict(umask='0022', fixtures={
            '.dir': dict(type='directory', mode='0700'),
            '.dir/.file': regular(),
            '.peer': dict(type='hardlink', target='.dir/.file'),
            '.link': dict(type='symlink', target='.dir/.file')}, expect={
            '.dir': dict(type='directory', mode='0700'),
            '.dir/.file': regular(),
            '.peer': dict(regular(), same_inode_as='.dir/.file'),
            '.link': dict(type='symlink', target='.dir/.file')}))
        fs.prepare(self.work, case)
        fs.verify_report(fs.observe(self.work, case), case)
        for name in ('.', '..', '.dir/..', '.dir/../escape', '/.hidden', '.dir//.file'):
            with self.subTest(name=name), self.assertRaises(SDKError):
                fs._path(name)
        altered = copy.deepcopy(case['filesystem'])
        altered['expect']['.PEER'] = regular()
        with self.assertRaises(SDKError):
            fs.validate(altered)

    def test_symlink_parent_is_rejected_before_setup(self):
        (self.work / 'cases').symlink_to(self.work / 'outside')
        with self.assertRaises((SDKError, OSError)):
            fs.prepare(self.work, self.case)
        self.assertFalse((self.work / 'outside').exists())

    def test_existing_case_is_never_reused(self):
        fs.prepare(self.work, self.case)
        with self.assertRaises((SDKError, OSError)):
            fs.prepare(self.work, self.case)

    def test_absent_parent_and_symlink_substitution(self):
        case = dict(name='absent', filesystem=dict(umask='0022', fixtures={}, expect={
            'dir': dict(type='absent'), 'dir/file': dict(type='absent')}))
        root = fs.prepare(self.work, case)
        fs.verify_report(fs.observe(self.work, case), case)
        (root / 'dir').symlink_to(self.work)
        with self.assertRaises(SDKError):
            fs.observe(self.work, case)

    def test_report_tamper_cannot_replace_an_actual_observation(self):
        root = fs.prepare(self.work, self.case)
        os.chmod(root / 'file', 0o640)
        reference = fs.observe(self.work, self.case)
        path = Path(reference['path'])
        value = json.loads(path.read_text())
        value['entries']['file']['mode'] = '0600'
        path.write_text(json.dumps(value))
        with self.assertRaises(SDKError):
            fs.verify_report(reference, self.case)

    def test_data_budget_special_types_and_external_targets_refuse(self):
        for row in (dict(type='fifo'), dict(type='symlink', target='../outside'),
                    regular(data=[dict(text='a', repeat=1048577)])):
            case = copy.deepcopy(self.case)
            case['filesystem']['fixtures']['file'] = row
            with self.assertRaises(SDKError):
                fs.validate(case['filesystem'])

    def test_changed_hardlink_and_symlink_target_refuse(self):
        root = fs.prepare(self.work, self.case)
        os.chmod(root / 'file', 0o640)
        (root / 'peer').unlink()
        (root / 'peer').write_text('owned\n')
        (root / 'peer').chmod(0o640)
        with self.assertRaises(SDKError):
            fs.observe(self.work, self.case)

    def test_lazy_directory_enumeration_stops_at_remaining_node_budget(self):
        from contextlib import contextmanager
        from types import SimpleNamespace
        real_scandir = os.scandir
        for nested in (False, True):
            with self.subTest(nested=nested):
                fixtures = {'dir': dict(type='directory', mode='0700')} if nested else {}
                case = dict(name='nested' if nested else 'flat', filesystem=dict(
                    umask='0022', fixtures=fixtures, expect=copy.deepcopy(fixtures)))
                root = fs.prepare(self.work, case)
                target_inode = (root / 'dir' if nested else root).stat().st_ino
                consumed, closed = [], []
                @contextmanager
                def large_iterator(fd):
                    if os.fstat(fd).st_ino != target_inode:
                        with real_scandir(fd) as iterator:
                            yield iterator
                        return
                    def entries():
                        for index in range(1000000):
                            consumed.append(index)
                            if len(consumed) > fs.MAX_NODES + 1 - int(nested):
                                self.fail('enumeration consumed more than the remaining node budget')
                            yield SimpleNamespace(name='item' + str(index))
                    try:
                        yield entries()
                    finally:
                        closed.append(True)
                with patch.object(fs.os, 'scandir', side_effect=large_iterator):
                    with self.assertRaises(SDKError):
                        fs.observe(self.work, case)
                self.assertGreater(len(consumed), 0)
                self.assertLessEqual(len(consumed), fs.MAX_NODES + 1 - int(nested))
                self.assertEqual(closed, [True])

    def test_nested_hardlink_target_is_relative_to_fixture_root(self):
        fixtures = {}
        for parts in ('one/two/three/four/five', 'src/a/b/c'):
            components = parts.split('/')
            for count in range(1, len(components) + 1):
                fixtures['/'.join(components[:count])] = dict(type='directory', mode='0700')
        fixtures['src/a/b/c/file'] = regular()
        peer = 'one/two/three/four/five/peer'
        fixtures[peer] = dict(type='hardlink', target='src/a/b/c/file')
        expected = copy.deepcopy(fixtures)
        expected[peer] = dict(regular(), same_inode_as='src/a/b/c/file')
        case = dict(name='nested-hardlink', filesystem=dict(umask='0022', fixtures=fixtures, expect=expected))
        fs.prepare(self.work, case)
        fs.verify_report(fs.observe(self.work, case), case)

class FileUtilityProfiles(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('EMBERBSD_TEST_FILEUTILS_SOURCE'), 'set EMBERBSD_TEST_FILEUTILS_SOURCE')
    def test_all_real_owner_cases_build_exact_program_steps(self):
        import hashlib
        source = Path(os.environ['EMBERBSD_TEST_FILEUTILS_SOURCE'])
        total = 0
        for name, count in dict(chmod=12, mkdir=11, ln=12, rmdir=9).items():
            with self.subTest(name=name):
                relative, digest, sources, provenance = program._profile({'stage': name + '-bootstrap'})
                raw = (source / relative).read_bytes()
                self.assertEqual(hashlib.sha256(raw).hexdigest(), digest)
                manifest = json.loads(raw)
                program._native_program_contract(manifest, name)
                self.assertEqual(len(manifest['runtime_cases']), count)
                self.assertEqual(sources, (f'bin/{name}/{name}.c',))
                self.assertNotIn('NOCOMPATLIB', (source / f'tools/{name}/Makefile').read_text())
                self.assertIn('nbtool_config.h', (source / sources[0]).read_text())
                for member in {relative, *sources, *provenance, *manifest['headers']}:
                    self.assertTrue((source / member).is_file())
                plan = dict(stage=name + '-bootstrap', work_root='/owned-work/' + name,
                            program=dict(source=dict(root=str(source))))
                descriptor = dict(host=dict(os='Darwin', cpu='arm64'))
                commands = dict(cc=('/selected/gcc',), sh=('/selected/mksh',))
                flags = dict(cppflags=[], cflags=[], ldflags=[])
                with patch.object(program, 'program_manifest', return_value=manifest):
                    steps = program.program_steps(plan, descriptor, commands, flags)
                    self.assertEqual(len(steps), count + 3)
                    for step, case in zip(steps[2:-1], manifest['runtime_cases']):
                        self.assertEqual(step['cwd'], plan['work_root'] + '/cases/' + case['name'])
                        self.assertIn('umask 0022;', step['argv'][-1])
                    self.assertEqual(steps[-1]['cwd'], plan['work_root'] + '/cases/runtime-observation')
                    self.assertNotIn('DYLD_PRINT_LIBRARIES', steps[-1]['env'])
                    import shlex
                    self.assertEqual(steps[-1]['argv'], [*commands['sh'], '-c',
                        'umask 0022; export DYLD_PRINT_LIBRARIES=1; exec ' +
                        shlex.join([plan['work_root'] + '/' + name,
                                    *manifest['runtime_cases'][0]['args']])])
                    self.assertEqual(len([x for x in program.program_outputs(plan) if x.endswith('.filesystem.json')]), count + 1)
                total += count
        self.assertEqual(total, 44)

    @unittest.skipUnless(os.environ.get('EMBERBSD_TEST_FILEUTILS_SOURCE'), 'set EMBERBSD_TEST_FILEUTILS_SOURCE')
    def test_effect_failure_stops_before_the_next_owned_child(self):
        import hashlib
        import test_bootstrap_program as fixtures
        fixture = fixtures.ProgramTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.source = fixture.base / 'chmod-source'
        source = Path(os.environ['EMBERBSD_TEST_FILEUTILS_SOURCE'])
        relative, digest, sources, provenance = program._profile({'stage': 'chmod-bootstrap'})
        fixture.manifest = json.loads((source / relative).read_text())
        for name in {relative, *sources, *provenance, *fixture.manifest['headers']}:
            target = fixture.source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((source / name).read_bytes())
        fixture.inputs['root'] = str(fixture.source)
        fixture.plan['stage'] = 'chmod-bootstrap'
        fixture.plan['program']['manifest'] = dict(path=str(fixture.source / relative), sha256=digest)
        fixture.refresh_source()
        fixture.plan['commands'] = program.program_steps(fixture.plan, fixture.descriptor,
                                                       fixture.commands, fixture.flags)
        fixture.validate()
        seen = []
        def child(argv, log, **kwargs):
            key = Path(log).stem
            seen.append(key)
            if key == 'case-numeric':
                self.assertEqual(kwargs['cwd'], str(fixture.work / 'cases/numeric'))
                self.assertEqual((fixture.work / 'cases/numeric/file').read_text(), 'owned\n')
                # Intentionally omit chmod. A zero exit and correct streams must fail.
                (fixture.work / 'numeric.stdout').write_bytes(b'')
                (fixture.work / 'numeric.stderr').write_bytes(b'')
            return fixture.fake_command(argv, log, **kwargs)
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=child):
            result = fixture.invoke()
        self.assertEqual(result['status'], 'FAILED')
        self.assertIn('filesystem actual state', result['error'])
        self.assertEqual(result['commands'][-1]['error'], result['error'])
        differences = json.loads(result['error'].split(': ', 1)[1])
        self.assertEqual(differences['file']['expected']['mode'], '0640')
        self.assertEqual(differences['file']['observed']['mode'], '0600')
        self.assertFalse((fixture.work / 'numeric.filesystem.json').exists())
        self.assertEqual(seen, ['compile-chmod', 'link-chmod', 'case-numeric'])
        self.assertFalse((fixture.work / 'cases/symbolic-add').exists())

    def test_old_literal_contract_rejects_new_fields(self):
        manifest = dict(runtime_cases=[dict(name='one', invocation='old', args=[], stdin='',
            stdout='', stderr='', exit=0, filesystem=dict(umask='0022', fixtures={}, expect={}))],
            invocations=['old'])
        with self.assertRaises(SDKError):
            program._literal_cases(manifest, 1)

    def test_source_hash_and_report_schema_tampering_refuse(self):
        import hashlib
        case = dict(name='one', filesystem=dict(umask='0022', fixtures={}, expect={}))
        with tempfile.TemporaryDirectory() as work:
            fs.prepare(work, case)
            reference = fs.observe(work, case)
            report_path = Path(reference['path'])
            report = json.loads(report_path.read_text())
            report['entries']['unexpected'] = dict(type='absent')
            report_path.write_text(json.dumps(report))
            reference['sha256'] = hashlib.sha256(report_path.read_bytes()).hexdigest()
            with self.assertRaises(SDKError):
                fs.verify_report(reference, case)

    def test_history_requires_each_exact_case_and_runtime_report(self):
        import hashlib
        from emberbsd_sdk.bootstrap_program_history import _commands
        case = dict(name='numeric', invocation='chmod', args=['640', 'file'], stdin='',
            stdout='', stderr='', exit=0, filesystem=dict(umask='0022',
                fixtures={'file': regular()}, expect={'file': regular('0640')}))
        manifest = dict(name='chmod', invocations=['chmod'], runtime_cases=[case])
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            outputs = {}
            def retained(name, data):
                (root / name).write_bytes(data)
                digest = hashlib.sha256(data).hexdigest()
                outputs[name] = digest
                return dict(path=str(root / name), sha256=digest)
            image = retained('chmod', b'synthetic image; not executable')
            steps = [dict(id='link-chmod', phase='link', argv=['selected-cc', '-o', image['path']], expected_exit=0),
                     dict(id='case-numeric', phase='program-case', expected_exit=0),
                     dict(id='runtime-observation', phase='run', expected_exit=0)]
            rows = []
            for step in steps:
                log = retained(step['id'] + '.log', b'synthetic drained log')
                del outputs[step['id'] + '.log']
                row = dict(step, actual_exit=0, log=log)
                if step['phase'] == 'link':
                    row['output'] = image
                else:
                    key = fs.step_key(step)
                    directory = fs.prepare(work, case, key=key)
                    os.chmod(directory / 'file', 0o640)
                    row['filesystem'] = fs.observe(work, case, key=key)
                    outputs[key + '.filesystem.json'] = row['filesystem']['sha256']
                    if step['phase'] == 'program-case':
                        row['stdout'] = retained('numeric.stdout', b'')
                        row['stderr'] = retained('numeric.stderr', b'')
                rows.append(row)
            plan = dict(stage='chmod-bootstrap', work_root=work, commands=steps)
            result = dict(commands=rows, outputs=outputs)
            with patch.object(program, 'program_outputs', return_value=list(outputs)), \
                 patch.object(program, 'program_sources', return_value={}):
                self.assertEqual(_commands(plan, result, manifest), image)
                for index in (1, 2):
                    altered = copy.deepcopy(result)
                    del altered['commands'][index]['filesystem']
                    with self.assertRaises(SDKError):
                        _commands(plan, altered, manifest)
                altered = copy.deepcopy(result)
                altered['commands'][2]['filesystem'] = rows[1]['filesystem']
                with self.assertRaises(SDKError):
                    _commands(plan, altered, manifest)

    @unittest.skipUnless(os.environ.get('EMBERBSD_TEST_FILEUTILS_SOURCE'), 'set EMBERBSD_TEST_FILEUTILS_SOURCE')
    def test_changed_owner_case_is_rejected_by_the_real_pinned_hash(self):
        source = Path(os.environ['EMBERBSD_TEST_FILEUTILS_SOURCE'])
        relative, digest, _, _ = program._profile(dict(stage='mkdir-bootstrap'))
        manifest = json.loads((source / relative).read_text())
        manifest['runtime_cases'][0]['args'] = ['/outside']
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / relative
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(manifest))
            plan = dict(stage='mkdir-bootstrap', program=dict(source=dict(root=work),
                        manifest=dict(path=str(path), sha256=digest)))
            with self.assertRaises(SDKError):
                program.program_manifest(plan)

if __name__ == '__main__':
    unittest.main()
