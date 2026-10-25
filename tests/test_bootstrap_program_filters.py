# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Initial cat/head/uniq/wc contracts. Native children remain synthetic fixtures."""

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import unittest
from unittest.mock import patch

import test_bootstrap_program_native as native_fixtures
from emberbsd_sdk import bootstrap_program as program
from emberbsd_sdk.bootstrap_seed import _plan
from emberbsd_sdk.errors import SDKError


class FilterProgramTests(unittest.TestCase):
    def setUp(self):
        self.native = native_fixtures.NativeProgramTests(methodName='runTest')
        self.native.setUp()
        self.addCleanup(self.native.doCleanups)
        self.case = self.native.case

    def test_fixed_profiles_use_shared_sources_commands_and_runner(self):
        for name, count in (('cat', 18), ('head', 16), ('uniq', 16), ('wc', 18)):
            with self.subTest(name=name):
                self.assertIn(name + '-bootstrap', program.PROGRAM_STAGES)
                self.native.configure(name)
                case = self.case
                self.assertIs(case.validate(), case.plan)
                self.assertEqual(_plan(case.f.document(case.plan), case.descriptor,
                                       case.commands, case.flags), case.plan)
                source = ('bin/' if name == 'cat' else 'usr.bin/') + name + '/' + name + '.c'
                steps = case.plan['commands']
                self.assertEqual(len(steps), count + 3)
                self.assertEqual(steps[0]['argv'], [*case.commands['cc'], *case.flags['cppflags'],
                    *case.flags['cflags'], '-v', '-include', str(case.source / program._BOOTSTRAP_CDEFS),
                    '-c', str(case.source / source), '-o', str(case.work / (name + '.o'))])
                self.assertEqual(steps[-1]['argv'], [str(case.work / name), 'input.txt'])
                self.assertEqual(program.program_sources(case.plan),
                                 {'input.txt': b'synthetic input\n', 'empty.txt': b''})
                with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=self.native.fake_command) as run:
                    result = case.invoke()
                self.assertEqual(run.call_count, count + 3)
                self.assertEqual(result['status'], 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW')
                self.assertFalse(result['ordinary_provider_qualified'])
                self.assertTrue(result['all_commands_drained'] and result['all_leases_closed'] and
                                result['inputs_revalidated_after_drain'])
                self.assertEqual((case.work / 'input.txt').read_bytes(), b'synthetic input\n')
                for row in result['commands']:
                    if row['phase'] == 'program-case':
                        self.assertNotEqual(row['stdout']['path'], row['log']['path'])
                        self.assertNotEqual(row['stderr']['path'], row['log']['path'])
                case.work = case.base / (name + '-next')
                case.plan['work_root'] = str(case.work)

    def test_source_and_route_changes_refuse_before_a_child(self):
        for name in ('cat', 'head', 'uniq', 'wc'):
            self.native.configure(name)
            case = self.case
            original = copy.deepcopy(case.plan)
            for change in ('source', 'argv', 'env'):
                with self.subTest(name=name, change=change):
                    source = case.source / case.manifest['sources'][0]
                    raw = source.read_bytes()
                    if change == 'source':
                        source.write_bytes(b'changed native source')
                    elif change == 'argv':
                        case.plan['commands'][0]['argv'].append('-w')
                    else:
                        case.plan['commands'][0]['env']['PATH'] += ':/bin'
                    with patch('emberbsd_sdk.bootstrap_pilot.run_logged') as run:
                        with self.assertRaises(SDKError):
                            case.invoke()
                    run.assert_not_called()
                    self.assertFalse(case.work.exists())
                    source.write_bytes(raw)
                    case.plan.clear()
                    case.plan.update(copy.deepcopy(original))
                    case.inputs = case.plan['program']['source']

    def test_missing_file_control_rejects_existing_files_and_links(self):
        for name in ('cat', 'head', 'uniq', 'wc'):
            self.native.configure(name)
            self.case.work.mkdir(exist_ok=True)
            path = self.case.work / 'missing.txt'
            for link in (False, True):
                if link:
                    path.symlink_to('unavailable')
                else:
                    path.write_bytes(b'not missing')
                with self.assertRaisesRegex(SDKError, 'missing-file control'):
                    self.case.validate()
                path.unlink()

    def test_fixture_paths_and_payloads_use_the_shared_bounded_schema(self):
        for name in ('cat', 'head', 'uniq', 'wc'):
            self.native.configure(name)
            for key, value in [('../outside.txt', ''), ('/absolute.txt', ''), ('result.json', ''),
                               ('missing.txt', ''), ('input.txt', None), ('case0.stdout', '')]:
                with self.subTest(name=name, key=key):
                    manifest = copy.deepcopy(self.case.manifest)
                    manifest['files'] = {key: value}
                    with self.assertRaises(SDKError):
                        program._native_program_contract(manifest, name)
            manifest = copy.deepcopy(self.case.manifest)
            manifest['files'] = {}
            with self.assertRaises(SDKError):
                program._native_program_contract(manifest, name)

    @unittest.skipUnless(os.environ.get('EMBERBSD_TEST_OS_SOURCE'),
                         'set EMBERBSD_TEST_OS_SOURCE for actual source-owner contracts')
    def test_actual_os_manifests_sources_links_and_digest_negatives(self):
        root = Path(os.environ['EMBERBSD_TEST_OS_SOURCE']).resolve(strict=True)
        for name, count in (('cat', 18), ('head', 16), ('uniq', 16), ('wc', 18)):
            relative, digest, sources, provenance = program._profile({'stage': name + '-bootstrap'})
            raw = (root / relative).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), digest)
            manifest = json.loads(raw)
            program._native_program_contract(manifest, name)
            self.assertEqual(len(manifest['runtime_cases']), count)
            self.assertEqual(manifest['runtime_cases'][0]['stdin'], '')
            self.assertEqual(manifest['runtime_cases'][0]['exit'], 0)
            self.assertIn(manifest['runtime_cases'][0]['args'][0], manifest['files'])
            for member in (*sources, *manifest['headers'], *provenance):
                self.assertTrue((root / member).is_file(), member)
                self.assertFalse((root / member).is_symlink(), member)
            recipe = (root / manifest['canonical_host_recipe']).read_text()
            directory = 'bin/cat' if name == 'cat' else 'usr.bin/' + name
            self.assertRegex(recipe, r'HOST_SRCDIR=\s*' + directory + r'\n')
            if name == 'cat':
                self.assertNotIn('NOCOMPATLIB', recipe)
            else:
                self.assertRegex(recipe, r'NOCOMPATLIB=\s*yes\n')
            readme = root / ('tools/' + name + '/README.md')
            for target in re.findall(r'\]\(([^)]+)\)', readme.read_text()):
                self.assertTrue((readme.parent / target).exists(), target)
            source = self.case.base / (name + '-actual-owner')
            path = source / relative
            path.parent.mkdir(parents=True)
            path.write_bytes(raw)
            reference = dict(path=str(path), sha256=digest)
            plan = dict(stage=name + '-bootstrap',
                        program=dict(source=dict(root=str(source)), manifest=reference))
            program.program_manifest(plan)
            for change in ('case', 'file'):
                changed = json.loads(raw)
                if change == 'case':
                    changed['runtime_cases'][0]['stdout'] = 'changed output\n'
                else:
                    changed['files']['input.txt'] = 'changed input\n'
                path.write_text(json.dumps(changed))
                reference['sha256'] = digest
                with self.assertRaises(SDKError):
                    program.program_manifest(plan)
                reference['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
                with self.assertRaisesRegex(SDKError, 'source-owner bootstrap program manifest'):
                    program.program_manifest(plan)
