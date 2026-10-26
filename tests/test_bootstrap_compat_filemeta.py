# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Source/data controls for the common metadata component; no native children."""
import copy
import os
from pathlib import Path
import struct
import subprocess
import unittest
from unittest.mock import patch

import test_bootstrap_program as fixtures
from test_bootstrap_compat_listing import archive
from emberbsd_sdk import bootstrap_compat_component as component
from emberbsd_sdk import bootstrap_program as program
from emberbsd_sdk.errors import CommandError, SDKError


class FilemetaDispatchTests(unittest.TestCase):
    def test_metadata_component_has_an_explicit_initial_route(self):
        self.assertIn('compat-filemeta-bootstrap', program.PROGRAM_STAGES)
        fixed = component.profile({'stage': 'compat-filemeta-bootstrap'})
        self.assertEqual(fixed['name'], 'filemeta')
        self.assertEqual(fixed['manifest'], 'tools/compat/bootstrap-filemeta.json')


class FilemetaComponentTests(unittest.TestCase):
    def setUp(self):
        source = os.environ.get('EMBERBSD_FILEMETA_SOURCE')
        if not source:
            self.skipTest('select the canonical file metadata owner source')
        self.owner = Path(source)
        self.case = c = fixtures.ProgramTests(methodName='runTest')
        c.setUp()
        self.addCleanup(c.doCleanups)
        c.source = c.base / 'filemeta-source'
        c.inputs['root'] = str(c.source)
        c.plan['stage'] = component.FILEMETA_STAGE
        fixed = component.profile(c.plan)
        for name in {fixed['manifest'], *fixed['sources'], *fixed['headers'], *fixed['provenance']}:
            target = c.source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((self.owner / name).read_bytes())
        c.plan['program']['manifest'] = dict(path=str(c.source / fixed['manifest']), sha256=fixed['digest'])
        c.plan['required_roles'] = sorted(set(c.plan['required_roles']) | {'ar', 'ranlib'})
        for role in ('ar', 'ranlib'):
            c.commands[role] = (str(c.f.bundle / role),)
        c.refresh_source()
        c.plan['commands'] = program.program_steps(c.plan, c.descriptor, c.commands, c.flags)

    def fake(self, argv, log, **kwargs):
        c = self.case
        step = next(row for row in c.plan['commands'] if row['id'] == Path(log).stem)
        Path(log).write_text('Synthetic data only. No native child was run.\n')
        if step['phase'] == 'compat-probe-link':
            if step['id'] in ('filemeta-regular-open', 'filemeta-acl-bsd',
                              'filemeta-xattr-bsd-int', 'filemeta-xattr-bsd-ssize'):
                raise CommandError(1, argv, log=log)
            Path(argv[-1]).write_bytes(struct.pack('<8I', 0xfeedfacf, 0x100000c, 0, 2, 0, 0, 0, 0))
            return subprocess.CompletedProcess(argv, 0)
        if step['phase'] == 'component-archive':
            (c.work / 'libnbcompat.a').write_bytes(archive([
                ('compat_filemeta.o', (c.work / 'compat_filemeta.o').read_bytes())]))
        return c.fake_command(argv, log, **kwargs)

    def run_fixture(self, child=None):
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=child or self.fake):
            return self.case.invoke()

    def completed(self):
        result = self.run_fixture()
        self.assertEqual(result['status'], 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW', result.get('error'))
        return result

    def test_full_owner_plan_has_one_object_eight_probes_and_two_runtime_traces(self):
        c = self.case
        with patch('subprocess.Popen', side_effect=AssertionError('no native child')):
            self.assertIs(c.validate(), c.plan)
        steps = c.plan['commands']
        self.assertEqual(len(steps), 18)
        self.assertEqual(steps[11]['id'], 'compile-compat_filemeta')
        self.assertEqual(steps[12]['argv'], [*c.commands['ar'], 'rc',
            str(c.work / 'libnbcompat.a'), str(c.work / 'compat_filemeta.o')])
        self.assertEqual([s['id'] for s in steps if 'DYLD_PRINT_LIBRARIES' in s['env']],
                         ['filemeta-control-run', 'runtime-observation'])
        value = component.manifest(c.plan)
        fixed = component.profile(c.plan)
        expected = {fixed['manifest'], value['source_makefile'], value['source_fragment'],
                    *value['sources'], *value['headers'], *value['provenance'],
                    value['control'], value['check'], *(p['source'] for p in value['probes'])}
        self.assertEqual(len(expected), 32)
        self.assertEqual(expected, {fixed['manifest'], *fixed['sources'], *fixed['headers'], *fixed['provenance']})
        self.assertEqual(len(component._fixtures(fixed)), 8)
        self.assertNotIn('compat_native_acl.h', fixed['exports'])

    def test_probe_results_preserve_absent_features_and_shared_exports(self):
        c = self.case
        result = self.completed()
        config = (c.work / 'nbtool_config.h').read_text()
        self.assertIn('#define HAVE_FILEMETA_REGULAR_OPEN 0\n', config)
        self.assertIn('#define HAVE_FILEMETA_ACL_BSD 0\n', config)
        self.assertIn('#define HAVE_FILEMETA_XATTR_DARWIN 1\n', config)
        saved = component.candidate(result['bootstrap_plan'], c.f.document(result))
        self.assertEqual(saved['component'], 'filemeta')
        self.assertEqual(set(saved['exports']), {'libnbcompat.a', 'nbtool_config.h',
            'compat_filemeta.h', 'bootstrap_cdefs.h'})

    def test_required_capabilities_and_control_are_not_presets(self):
        c = self.case
        value = component.manifest(c.plan)
        for missing in value['required_capabilities']:
            with self.subTest(missing=missing):
                completed = [dict(row, actual_exit=0) for row in c.plan['commands'][:3]]
                for row, probe in zip(c.plan['commands'][3:11], value['probes']):
                    status = int(probe['macro'] == missing)
                    completed.append(dict(row, actual_exit=status,
                        configuration_probe=dict(macro=probe['macro'], available=status == 0)))
                with self.assertRaisesRegex(SDKError, 'required capability'):
                    component.configuration(completed, value)
        completed[2]['actual_exit'] = 1
        with self.assertRaisesRegex(SDKError, 'successful actual control'):
            component.configuration(completed, value)

    def test_missing_required_probe_stops_before_implementation_compile(self):
        calls = []
        def child(argv, log, **kwargs):
            calls.append(Path(log).stem)
            if Path(log).stem == 'filemeta-open':
                Path(log).write_text('Synthetic missing open capability.\n')
                raise CommandError(1, argv, log=log)
            return self.fake(argv, log, **kwargs)
        result = self.run_fixture(child)
        self.assertEqual(result['status'], 'FAILED')
        self.assertNotIn('compile-compat_filemeta', calls)
        self.assertFalse((self.case.work / 'nbtool_config.h').exists())

    def test_shared_header_tamper_refuses_before_probe(self):
        calls = []
        def child(argv, log, **kwargs):
            calls.append(Path(log).stem)
            result = self.fake(argv, log, **kwargs)
            if Path(log).stem == 'filemeta-control-run':
                (self.case.work / 'compat_native_features.h').write_text('changed')
            return result
        result = self.run_fixture(child)
        self.assertEqual(result['status'], 'FAILED')
        self.assertNotIn('filemeta-open', calls)

    def test_leftover_directory_refuses_before_runtime_observation(self):
        calls = []
        def child(argv, log, **kwargs):
            calls.append(Path(log).stem)
            result = self.fake(argv, log, **kwargs)
            if Path(log).stem == 'filemeta-check':
                (self.case.work / 'filemeta-source-directory').mkdir()
            return result
        result = self.run_fixture(child)
        self.assertEqual(result['status'], 'FAILED')
        self.assertNotIn('runtime-observation', calls)

    def test_receipt_rechecks_source_and_rejects_cross_component_pairing(self):
        c = self.case
        result = self.completed()
        saved = component.candidate(result['bootstrap_plan'], c.f.document(result))
        ref = c.f.document(saved)
        review = c.f.document(dict(status='REVIEWED_INITIAL_OS_COMPAT_COMPONENT', candidate=ref,
            findings=[], all_leases_closed=True, ordinary_provider_qualified=False))
        receipt = dict(format='emberbsd-os-compat-component-acceptance', version=1, scope='gcc16-bootstrap',
            status='ACCEPTED_INITIAL_OS_COMPAT_COMPONENT', component='filemeta', candidate=ref,
            independent_review=review, initial_only=True, ordinary_provider_qualified=False)
        for exported in saved['exports'].values():
            Path(exported['path']).chmod(0o444)
        self.assertEqual(component.validate_input(c.f.document(receipt), c.descriptor,
            component='filemeta'), saved['exports'])
        for name in ('filetime', 'parsedate', 'listing'):
            with self.assertRaises(SDKError):
                component.validate_input(c.f.document(receipt), c.descriptor, component=name)
        Path(c.inputs['source_proof']['path']).unlink()
        with self.assertRaises(SDKError):
            component.validate_input(c.f.document(receipt), c.descriptor, component='filemeta')

    def test_selected_archiver_is_required_before_any_child(self):
        c = self.case
        commands = copy.deepcopy(c.commands)
        commands.pop('ar')
        with patch('subprocess.Popen', side_effect=AssertionError('no native child')):
            with self.assertRaises(SDKError):
                program.program_steps(c.plan, c.descriptor, commands, c.flags)


if __name__ == '__main__':
    unittest.main()
