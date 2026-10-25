# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Source-only parsedate checks. Synthetic children never compile C."""

import copy
import os
from pathlib import Path
import struct
import subprocess
import unittest
from unittest.mock import patch

from emberbsd_sdk import bootstrap_program as program
from emberbsd_sdk import bootstrap_compat_component as component
from emberbsd_sdk.errors import SDKError, CommandError
import test_bootstrap_program as fixtures


class ParsedateDispatchTests(unittest.TestCase):
    def test_parsedate_is_an_explicit_component_stage(self):
        self.assertIn('compat-parsedate-bootstrap', program.PROGRAM_STAGES)


class ParsedateComponentTests(unittest.TestCase):
    def setUp(self):
        source = os.environ.get('EMBERBSD_PARSEDATE_SOURCE')
        if not source:
            self.skipTest('select the canonical parsedate owner source')
        self.owner = Path(source)
        self.case = c = fixtures.ProgramTests(methodName='runTest')
        c.setUp()
        self.addCleanup(c.doCleanups)
        c.source = c.base / 'parsedate-source'
        c.inputs['root'] = str(c.source)
        c.plan['stage'] = component.PARSEDATE_STAGE
        fixed = component.profile(c.plan)
        for name in {fixed['manifest'], *fixed['sources'], *fixed['headers'], *fixed['provenance']}:
            path = c.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((self.owner / name).read_bytes())
        c.plan['program']['manifest'] = dict(path=str(c.source / fixed['manifest']), sha256=fixed['digest'])
        c.plan['required_roles'] = sorted(set(c.plan['required_roles']) | {'ar', 'ranlib', 'yacc'})
        for role in ('ar', 'ranlib', 'yacc'):
            c.commands[role] = (str(c.f.bundle / role),)
        c.descriptor['tools']['yacc'] = dict(kind='native', path=c.commands['yacc'][0])
        c.refresh_source()
        c.plan['commands'] = program.program_steps(c.plan, c.descriptor, c.commands, c.flags)

    def fake(self, argv, log, **kwargs):
        c = self.case
        step = next(row for row in c.plan['commands'] if row['id'] == Path(log).stem)
        Path(log).write_text('Synthetic data only. No compiler or generator was run.\n')
        if step['id'] == 'func-mktime_z':
            raise CommandError(1, argv, log=log)
        if step['id'] == 'func-timegm':
            Path(argv[-1]).write_bytes(struct.pack('<8I', 0xfeedfacf, 0x100000c, 0, 2, 0, 0, 0, 0))
            return subprocess.CompletedProcess(argv, 0)
        if step['phase'] == 'generator':
            (c.work / 'parsedate.c').write_bytes(b'synthetic generated parser\n')
        if step['phase'] == 'component-archive':
            body = (c.work / 'parsedate.o').read_bytes()
            header = (f'{"parsedate.o/":<16}{"0":<12}{"0":<6}{"0":<6}'
                      f'{"100644":<8}{len(body):<10}`\n').encode()
            (c.work / 'libnbcompat.a').write_bytes(b'!<arch>\n' + header + body)
        return c.fake_command(argv, log, **kwargs)

    def run_fixture(self, child=None):
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=child or self.fake):
            return self.case.invoke()

    def test_real_owner_plan_has_thirteen_commands_and_five_exports(self):
        c = self.case
        with patch('subprocess.Popen', side_effect=AssertionError('no child')):
            self.assertIs(c.validate(), c.plan)
        steps = c.plan['commands']
        self.assertEqual(len(steps), 13)
        self.assertEqual(steps[5]['argv'], [*c.commands['yacc'], '-p', '__pd', '-o',
            str(c.work / 'parsedate.c'), str(c.source / 'lib/libutil/parsedate.y')])
        self.assertEqual(steps[6]['argv'][-3], str(c.work / 'parsedate.c'))
        self.assertEqual([s['id'] for s in steps if 'DYLD_PRINT_LIBRARIES' in s['env']],
                         ['parsedate-control-run', 'runtime-observation'])
        self.assertEqual(set(component.profile(c.plan)['exports']), {'libnbcompat.a', 'nbtool_config.h',
            'compat_parsedate.h', 'compat_parsedate_features.h', 'bootstrap_cdefs.h'})
        for index in (0, 3, 4):
            self.assertIn(str(c.work / 'compat_parsedate_features.h'), steps[index]['argv'])

    def test_pending_candidate_retains_probes_and_generated_source(self):
        c = self.case
        result = self.run_fixture()
        self.assertEqual(result['status'], 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW', result.get('error'))
        config = (c.work / 'nbtool_config.h').read_text()
        self.assertIn('#define HAVE_MKTIME_Z 0\n', config)
        self.assertIn('#define HAVE_TIMEGM 1\n', config)
        candidate = component.candidate(result['bootstrap_plan'], c.f.document(result))
        self.assertEqual(candidate['component'], 'parsedate')
        self.assertEqual(len(candidate['exports']), 5)
        self.assertEqual(result['commands'][5]['generated']['parsedate.c']['sha256'], result['outputs']['parsedate.c'])
        (c.work / 'parsedate.c').write_bytes(b'changed generated parser')
        with self.assertRaises(SDKError):
            component.candidate(result['bootstrap_plan'], c.f.document(result))

    def test_generated_mode_change_refuses_before_compile(self):
        original = component.verify
        calls = []
        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[0]['id'] == 'generate-parsedate':
                (self.case.work / 'parsedate.c').chmod(0o400)
            return result
        def child(argv, log, **kwargs):
            calls.append(Path(log).stem)
            return self.fake(argv, log, **kwargs)
        with patch.object(component, 'verify', side_effect=changed):
            result = self.run_fixture(child)
        self.assertEqual(result['status'], 'FAILED')
        self.assertNotIn('compile-parsedate', calls)

    def test_missing_outside_or_extended_yacc_and_missing_role_refuse(self):
        c = self.case
        for change in ('absent', 'outside', 'arguments', 'role'):
            with self.subTest(change=change):
                commands, descriptor = copy.deepcopy(c.commands), copy.deepcopy(c.descriptor)
                plan = copy.deepcopy(c.plan)
                if change == 'absent':
                    commands.pop('yacc')
                elif change == 'outside':
                    descriptor['tools']['yacc']['path'] = str(c.base / 'outside-yacc')
                    commands['yacc'] = (descriptor['tools']['yacc']['path'],)
                elif change == 'arguments':
                    commands['yacc'] += ('-d',)
                else:
                    plan['required_roles'].remove('yacc')
                with patch('subprocess.Popen', side_effect=AssertionError('no child')):
                    with self.assertRaises(SDKError):
                        program.validate_program_plan(plan, descriptor, commands, c.flags)

    def test_required_timegm_probe_failure_cannot_generate_configuration(self):
        c = self.case
        def fail(argv, log, **kwargs):
            if Path(log).stem == 'func-timegm':
                Path(log).write_text('Synthetic failed required capability\n')
                raise CommandError(1, argv, log=log)
            return self.fake(argv, log, **kwargs)
        result = self.run_fixture(fail)
        self.assertEqual(result['status'], 'FAILED')
        self.assertIn('required capability', result['error'])
        self.assertFalse((c.work / 'nbtool_config.h').exists())
        self.assertFalse((c.work / 'parsedate.c').exists())

    def test_failed_control_and_unexpected_probe_exit_stop_before_generator(self):
        c = self.case
        # The first actual control must complete; linkability is insufficient.
        def fail(argv, log, **kwargs):
            if Path(log).stem == 'parsedate-control-run':
                Path(log).write_text('Synthetic failed timegm control\n')
                raise CommandError(1, argv, log=log)
            return self.fake(argv, log, **kwargs)
        result = self.run_fixture(fail)
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(len(result['commands']), 3)
        self.assertFalse((c.work / 'nbtool_config.h').exists())
        value = component.manifest(c.plan)
        completed = [dict(row, actual_exit=0) for row in c.plan['commands'][:3]]
        for row, probe in zip(c.plan['commands'][3:5], value['probes']):
            completed.append(dict(row, actual_exit=2,
                configuration_probe=dict(macro=probe['macro'], available=False)))
        with self.assertRaisesRegex(SDKError, 'drained capability'):
            component.configuration(completed, value)

    def test_generated_tamper_refuses_before_compile(self):
        c = self.case
        original = component.verify
        calls = []
        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[0]['id'] == 'generate-parsedate':
                (c.work / 'parsedate.c').write_bytes(b'changed after capture')
            return result
        def child(argv, log, **kwargs):
            calls.append(Path(log).stem)
            return self.fake(argv, log, **kwargs)
        with patch.object(component, 'verify', side_effect=changed):
            result = self.run_fixture(child)
        self.assertEqual(result['status'], 'FAILED')
        self.assertNotIn('compile-parsedate', calls)

    def test_generated_tamper_during_compile_refuses_after_drain(self):
        c = self.case
        def child(argv, log, **kwargs):
            if Path(log).stem == 'compile-parsedate':
                (c.work / 'parsedate.c').write_bytes(b'changed during compile')
            return self.fake(argv, log, **kwargs)
        result = self.run_fixture(child)
        self.assertEqual(result['status'], 'FAILED')
        self.assertFalse((c.work / 'libnbcompat.a').exists())

    def test_extra_work_header_is_refused_after_generator(self):
        def child(argv, log, **kwargs):
            result = self.fake(argv, log, **kwargs)
            if Path(log).stem == 'generate-parsedate':
                (self.case.work / 'fcntl.h').write_text('unexpected header')
            return result
        result = self.run_fixture(child)
        self.assertEqual(result['status'], 'FAILED')
        self.assertIn('work roster', result['error'])
        self.assertFalse((self.case.work / 'parsedate.o').exists())

    def test_wrong_archive_member_and_receipt_component_pair_refuse(self):
        c = self.case
        result = self.run_fixture()
        self.assertEqual(result['status'], 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW', result.get('error'))
        saved = component.candidate(result['bootstrap_plan'], c.f.document(result))
        ref = c.f.document(saved)
        review = c.f.document(dict(status='REVIEWED_INITIAL_OS_COMPAT_COMPONENT', candidate=ref,
            findings=[], all_leases_closed=True, ordinary_provider_qualified=False))
        receipt = dict(format='emberbsd-os-compat-component-acceptance', version=1, scope='gcc16-bootstrap',
            status='ACCEPTED_INITIAL_OS_COMPAT_COMPONENT', component='parsedate', candidate=ref,
            independent_review=review, initial_only=True, ordinary_provider_qualified=False)
        for exported in saved['exports'].values():
            Path(exported['path']).chmod(0o444)
        self.assertEqual(component.validate_input(c.f.document(receipt), c.descriptor, component='parsedate'), saved['exports'])
        with self.assertRaises(SDKError):
            component.validate_input(c.f.document(receipt), c.descriptor)
        receipt['component'] = 'filetime'
        with self.assertRaisesRegex(SDKError, 'candidate changed'):
            component.validate_input(c.f.document(receipt), c.descriptor)
        path = c.work / 'libnbcompat.a'
        path.chmod(0o644)
        path.write_bytes(path.read_bytes().replace(b'parsedate.o/', b'utimens.o/  '))
        with self.assertRaisesRegex(SDKError, 'missing, changed or extra'):
            component._archive(c.work, 'parsedate.o')

    def test_retained_generated_mode_and_source_proof_remain_required(self):
        c = self.case
        result = self.run_fixture()
        self.assertEqual(result['status'], 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW', result.get('error'))
        changed = copy.deepcopy(result)
        changed['commands'][5].pop('generated_modes')
        with self.assertRaisesRegex(SDKError, 'mode'):
            component.candidate(result['bootstrap_plan'], c.f.document(changed))
        (c.work / 'parsedate.c').chmod(0o400)
        with self.assertRaisesRegex(SDKError, 'mode'):
            component.candidate(result['bootstrap_plan'], c.f.document(result))
        (c.work / 'parsedate.c').chmod(result['commands'][5]['generated_modes']['parsedate.c'])
        Path(c.inputs['source_proof']['path']).unlink()
        with self.assertRaises(SDKError):
            component.candidate(result['bootstrap_plan'], c.f.document(result))

    def test_preexisting_generated_source_and_unknown_step_refuse(self):
        c = self.case
        result = self.run_fixture()
        self.assertEqual(result['status'], 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW', result.get('error'))
        with self.assertRaisesRegex(SDKError, 'exists before'):
            component.prepare(c.plan['commands'][5], c.plan, result['commands'][:5])
        step = dict(c.plan['commands'][5], id='unknown-generator')
        with self.assertRaisesRegex(SDKError, 'step order'):
            component.prepare(step, c.plan, result['commands'][:5])

    def test_generated_mode_change_during_compile_refuses_after_drain(self):
        def child(argv, log, **kwargs):
            result = self.fake(argv, log, **kwargs)
            if Path(log).stem == 'compile-parsedate':
                (self.case.work / 'parsedate.c').chmod(0o400)
            return result
        result = self.run_fixture(child)
        self.assertEqual(result['status'], 'FAILED')
        self.assertIn('mode', result['error'])
        self.assertFalse((self.case.work / 'libnbcompat.a').exists())

    def test_missing_generated_output_never_reaches_compile(self):
        calls = []
        def child(argv, log, **kwargs):
            calls.append(Path(log).stem)
            if Path(log).stem == 'generate-parsedate':
                Path(log).write_text('Synthetic generator returned without output\n')
                return subprocess.CompletedProcess(argv, 0)
            return self.fake(argv, log, **kwargs)
        result = self.run_fixture(child)
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(calls[-1], 'generate-parsedate')
        self.assertNotIn('compile-parsedate', calls)

    def test_source_roster_matches_the_complete_owner_contract(self):
        value = component.manifest(self.case.plan)
        fixed = component.profile(self.case.plan)
        expected = {fixed['manifest'], value['source_makefile'], value['source_fragment'],
                    *value['sources'], *value['headers'], *value['provenance'],
                    value['control'], value['check'], *(p['source'] for p in value['probes'])}
        self.assertEqual(len(expected), 17)
        self.assertEqual(expected, {fixed['manifest'], *fixed['sources'], *fixed['headers'], *fixed['provenance']})


if __name__ == '__main__':
    unittest.main()
