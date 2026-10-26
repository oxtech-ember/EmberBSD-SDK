# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Fixed canonical cp source contracts. No native execution."""
import copy
import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
import test_bootstrap_program as fixtures
from emberbsd_sdk.errors import SDKError, CommandError
from emberbsd_sdk.bootstrap_seed import _plan
from emberbsd_sdk import bootstrap_program as program
from emberbsd_sdk import bootstrap_program_components as components


class CpProfileRegistrationTests(unittest.TestCase):
    def test_cp_uses_both_shared_components_and_existing_program_runner(self):
        self.assertIn('cp-bootstrap', program.PROGRAM_STAGES)
        self.assertEqual(components.CONSUMERS['cp-bootstrap'], ('filetime', 'filemeta'))
        _, _, sources, _ = program._profile(dict(stage='cp-bootstrap'))
        self.assertEqual(sources, ('bin/cp/cp.c', 'bin/cp/utils.c'))


if __name__ == '__main__':
    unittest.main()


@unittest.skipUnless(os.environ.get('EMBERBSD_CP_SOURCE'), 'select canonical cp source')
class CpProgramTests(unittest.TestCase):
    def setUp(self):
        owner = Path(os.environ['EMBERBSD_CP_SOURCE'])
        self.owner = owner
        self.case = c = fixtures.ProgramTests(methodName='runTest')
        c.setUp()
        self.addCleanup(c.doCleanups)
        c.source = c.base/'cp-source'
        parent = c.base/'cp.noindex'
        parent.mkdir()
        c.work = parent/'cp-work'
        c.plan.update(stage='cp-bootstrap', work_root=str(c.work))
        relative, digest, sources, provenance = program._profile(c.plan)
        c.manifest = json.loads((owner/relative).read_text())
        for name in {relative, *sources, *provenance, *c.manifest['headers']}:
            target = c.source/name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((owner/name).read_bytes())
        self.assertEqual(hashlib.sha256((c.source/relative).read_bytes()).hexdigest(), digest)
        c.inputs['root'] = str(c.source)
        c.plan['program']['manifest'] = dict(path=str(c.source/relative), sha256=digest)
        c.refresh_source()
        self.exports = {}
        c.plan['program']['components'] = {}
        for name in ('filetime', 'filemeta'):
            root = c.base/name
            root.mkdir()
            (root/'source').mkdir()
            exports = {}
            for member in ('libnbcompat.a', 'nbtool_config.h', 'bootstrap_cdefs.h', 'compat_'+name+'.h'):
                raw = b'common declarations' if member == 'bootstrap_cdefs.h' else (name+member).encode()
                path = root/member
                path.write_bytes(raw)
                path.chmod(0o444)
                exports[member] = dict(path=str(path), sha256=hashlib.sha256(raw).hexdigest())
            self.exports[name] = exports
            saved = dict(bootstrap_plan=c.f.document(dict(work_root=str(root))),
                         source=dict(root=str(root/'source'), source_proof=c.f.document(dict(members=c.f.document({})))),
                         producer_result=c.f.document({}), seed_descriptor=c.f.document(c.descriptor))
            c.plan['program']['components'][name] = c.f.document(dict(candidate=c.f.document(saved), independent_review=c.f.document({})))
        validator = patch.object(components.component, 'validate_input', side_effect=self.selected)
        validator.start()
        self.addCleanup(validator.stop)
        c.plan['commands'] = program.program_steps(c.plan, c.descriptor, c.commands, c.flags)

    def selected(self, reference, descriptor, *, component):
        self.assertEqual(reference, self.case.plan['program']['components'][component])
        self.assertEqual(descriptor, self.case.descriptor)
        for row in self.exports[component].values():
            if hashlib.sha256(Path(row['path']).read_bytes()).hexdigest() != row['sha256']:
                raise SDKError('synthetic retained component changed')
        return self.exports[component]

    def test_actual_owner_builds_all_steps_and_keeps_both_exact_components(self):
        c = self.case
        self.assertIs(c.validate(), c.plan)
        self.assertEqual(_plan(c.f.document(c.plan), c.descriptor, c.commands, c.flags), c.plan)
        self.assertEqual(len(c.manifest['runtime_cases']), 35)
        self.assertEqual(len(c.plan['commands']), 42)
        self.assertEqual([step['id'] for step in c.plan['commands'][:6]],
                         ['compile-program-control', 'link-program-control', 'run-program-control',
                          'compile-cp', 'compile-utils', 'link-cp'])
        self.assertEqual(c.plan['commands'][2]['env']['DYLD_PRINT_LIBRARIES'], '1')
        for step in c.plan['commands']:
            if step['phase'] == 'link':
                self.assertEqual([word for word in step['argv'] if word.endswith('.a')],
                                 [self.exports[name]['libnbcompat.a']['path'] for name in ('filetime', 'filemeta')])
            if step['phase'] == 'compile':
                self.assertIn(str(c.source/'tools/compat'), step['argv'])
                self.assertFalse(any('SMALL' in word or 'NOCOMPATLIB' in word for word in step['argv']))
        self.assertEqual(len(json.loads(Path(c.proof['members']['path']).read_text())), 15)
        self.assertEqual(len([x for x in program.program_outputs(c.plan) if x.endswith('.filesystem.json')]), 36)

    def test_missing_or_cross_paired_component_is_refused(self):
        c = self.case
        saved = copy.deepcopy(c.plan['program']['components'])
        for bad in ({}, {'filetime': saved['filetime']}, dict(saved, listing=saved['filemeta'])):
            c.plan['program']['components'] = bad
            with self.assertRaises(SDKError):
                c.validate()
        c.plan['program']['components'] = saved
        for name in ('filetime', 'filemeta'):
            ref = self.exports[name]['libnbcompat.a']
            path = Path(ref['path']); raw = path.read_bytes(); path.chmod(0o644); path.write_bytes(b'changed')
            with self.assertRaises(SDKError):
                c.validate()
            path.write_bytes(raw); path.chmod(0o444)

    def test_changed_shared_header_or_owner_policy_is_refused(self):
        c = self.case
        for relative in ('tools/compat/compat_stat_time.h', 'tools/cp/bootstrap.json'):
            path = c.source/relative
            raw = path.read_bytes()
            path.write_bytes(raw+b'\n')
            with self.assertRaises(SDKError):
                c.validate()
            path.write_bytes(raw)
        for key, value in [('control', 'private.c'), ('sources', ['bin/cp/cp.c']),
                           ('headers', ['bin/cp/extern.h']), ('components', ['filetime'])]:
            bad = copy.deepcopy(c.manifest); bad[key] = value
            with self.assertRaises(SDKError):
                program._native_program_contract(bad, 'cp')

    def test_required_control_and_component_routes_cannot_be_removed(self):
        c = self.case
        original = copy.deepcopy(c.plan['commands'])
        c.plan['commands'] = original[3:]
        with self.assertRaises(SDKError):
            c.validate()
        c.plan['commands'] = copy.deepcopy(original)
        link = next(step for step in c.plan['commands'] if step['id'] == 'link-cp')
        link['argv'].remove(self.exports['filemeta']['libnbcompat.a']['path'])
        with self.assertRaises(SDKError):
            c.validate()

    def test_failed_required_control_stops_before_cp_sources(self):
        c = self.case
        def child(argv, log, **kwargs):
            value = c.fake_command(argv, log, **kwargs)
            if Path(log).stem == 'run-program-control':
                raise CommandError(1, argv, log=log)
            return value
        with patch('subprocess.Popen', side_effect=AssertionError('no native child')), \
             patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=child):
            result = c.invoke()
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(len(result['commands']), 3)
        self.assertEqual(result['commands'][-1]['actual_exit'], 1)
        self.assertTrue(result['all_commands_drained'])
        self.assertFalse((c.work/'cp.o').exists())
        self.assertFalse((c.work/'cp').exists())

    def test_component_tamper_after_first_step_stops_before_next_child(self):
        c = self.case
        calls = []
        def child(argv, log, **kwargs):
            calls.append(Path(log).stem)
            value = c.fake_command(argv, log, **kwargs)
            if len(calls) == 1:
                archive = Path(self.exports['filemeta']['libnbcompat.a']['path'])
                archive.chmod(0o644)
                archive.write_bytes(b'changed after prepare')
            return value
        with patch('subprocess.Popen', side_effect=AssertionError('no native child')), \
             patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=child):
            result = c.invoke()
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(calls, ['compile-program-control'])
        self.assertIn('synthetic retained component changed', result['error'])
        self.assertTrue(result['all_commands_drained'])

    def test_preserve_existing_mode_has_a_different_initial_destination(self):
        case = next(row for row in self.case.manifest['runtime_cases'] if row['name'] == 'preserve-existing-mode')
        fs = case['filesystem']
        self.assertEqual(case['args'], ['-p', 'src', 'dst'])
        self.assertEqual(fs['fixtures']['src']['mode'], '0751')
        self.assertEqual(fs['fixtures']['dst']['mode'], '0600')
        self.assertEqual(fs['expect']['dst']['mode'], '0751')

    def test_root_directory_link_is_explicitly_followed_with_H(self):
        case = next((row for row in self.case.manifest['runtime_cases'] if row['name'] == 'recursive-command-root-link'), None)
        self.assertIsNotNone(case, 'the -H root-directory link case is absent')
        fs = case['filesystem']
        self.assertEqual(case['args'], ['-R', '-H', '-p', 'root', 'dst'])
        self.assertEqual(fs['fixtures']['root'], {'type': 'symlink', 'target': 'tree'})
        self.assertEqual(fs['fixtures']['tree']['type'], 'directory')
        self.assertEqual(fs['expect']['root'], fs['fixtures']['root'])
        self.assertEqual(fs['expect']['dst']['type'], 'directory')
        self.assertEqual(fs['expect']['dst/file']['data'], fs['fixtures']['tree/file']['data'])
        self.assertEqual(fs['expect']['dst']['mode'], fs['fixtures']['tree']['mode'])
