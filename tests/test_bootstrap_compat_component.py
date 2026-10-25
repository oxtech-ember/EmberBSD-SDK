# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Data-only regressions for the fixed OS file-time component."""

import unittest
import copy
import hashlib
import os
from pathlib import Path
import subprocess
from unittest.mock import Mock, patch

from emberbsd_sdk import bootstrap_program as program
from emberbsd_sdk import bootstrap_compat_component as component
from emberbsd_sdk.errors import CommandError, SDKError
import test_bootstrap_program as fixtures


class ComponentDispatchTests(unittest.TestCase):
    def test_component_has_an_admitted_producer_stage(self):
        self.assertIn('compat-filetime-bootstrap', program.PROGRAM_STAGES)


class FiletimeComponentTests(unittest.TestCase):
    def setUp(self):
        root = os.environ.get('EMBERBSD_OS_SOURCE')
        if not root:
            self.skipTest('select the canonical OS source for source-contract checks')
        self.owner = Path(root)
        self.case = c = fixtures.ProgramTests(methodName='runTest')
        c.setUp()
        self.addCleanup(c.doCleanups)
        c.source = c.base / 'filetime-source'
        c.source.mkdir()
        c.inputs['root'] = str(c.source)
        c.plan['stage'] = component.STAGE
        c.work = c.base / 'filetime-work'
        c.plan['work_root'] = str(c.work)
        for name in {component.MANIFEST, *component.SOURCES, *component.HEADERS,
                     *component.PROVENANCE}:
            path = c.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((self.owner / name).read_bytes())
        c.plan['program']['manifest'] = dict(path=str(c.source / component.MANIFEST),
                                            sha256=component.DIGEST)
        c.plan['required_roles'] = sorted(set(c.plan['required_roles']) | {'ar', 'ranlib'})
        for role in ('ar', 'ranlib'):
            c.commands.setdefault(role, (str(c.base / role),))
        c.refresh_source()
        c.plan['commands'] = program.program_steps(c.plan, c.descriptor, c.commands, c.flags)

    def fake(self, argv, log, **kwargs):
        c = self.case
        step = next(row for row in c.plan['commands'] if row['id'] == Path(log).stem)
        Path(log).write_text('Synthetic data-only observation; no compiler was run.\n')
        if step['id'] in component.PROBES:
            raise CommandError(1, argv, log=log)
        if step['phase'] == 'component-archive':
            body = (c.work / 'utimens.o').read_bytes()
            header = (f'{"utimens.o/":<16}{"0":<12}{"0":<6}{"0":<6}'
                      f'{"100644":<8}{len(body):<10}`\n').encode()
            (c.work / 'libnbcompat.a').write_bytes(b'!<arch>\n' + header + body)
        return c.fake_command(argv, log, **kwargs)

    def run_fixture(self):
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=self.fake):
            return self.case.invoke()

    def test_real_owner_manifest_builds_an_exact_plan_without_children(self):
        c = self.case
        with patch('subprocess.Popen', side_effect=AssertionError('no child')):
            self.assertIs(c.validate(), c.plan)
        self.assertEqual(len(c.plan['commands']), 12)
        self.assertEqual(c.plan['commands'][6]['argv'],
            [*c.commands['ar'], 'rc', str(c.work / 'libnbcompat.a'), str(c.work / 'utimens.o')])
        self.assertEqual(c.plan['commands'][7]['argv'],
            [*c.commands['ranlib'], str(c.work / 'libnbcompat.a')])
        self.assertNotIn('yacc', c.plan['required_roles'])

    def test_missing_ar_or_changed_command_refuses(self):
        c = self.case
        commands = dict(c.commands)
        commands.pop('ar')
        with self.assertRaisesRegex(SDKError, 'selected compiler'):
            program.program_steps(c.plan, c.descriptor, commands, c.flags)
        c.plan['commands'][6]['argv'][0] = '/usr/bin/ar'
        with self.assertRaisesRegex(SDKError, 'command differs'):
            c.validate()

    def test_changed_owner_manifest_uses_real_pinned_digest(self):
        path = self.case.source / component.MANIFEST
        path.write_bytes(path.read_bytes().replace(b'"ranlib"', b'"unknown"'))
        self.case.refresh_source()
        with self.assertRaises(SDKError):
            self.case.validate()

    def test_full_fixture_generates_config_archive_and_pending_candidate(self):
        c = self.case
        result = self.run_fixture()
        self.assertEqual(result['status'], 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW',
                         result.get('error'))
        config = (c.work / 'nbtool_config.h').read_text()
        self.assertIn('#define HAVE_UTIMENSAT 1\n', config)
        self.assertIn('#define HAVE_UTIMENS 0\n', config)
        self.assertIn('#define HAVE_LUTIMENS 0\n', config)
        candidate = component.candidate(result['bootstrap_plan'], c.f.document(result))
        self.assertEqual(candidate['status'], 'PENDING_INDEPENDENT_COMPONENT_REVIEW')
        self.assertFalse(candidate['ordinary_provider_qualified'])
        self.assertEqual(set(candidate['exports']), set(component.EXPORTS))

    def test_failed_control_cannot_become_successful_config(self):
        def fail(argv, log, **kwargs):
            Path(log).write_text('synthetic required control failure\n')
            raise CommandError(1, argv, log=log)
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=fail):
            result = self.case.invoke()
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(len(result['commands']), 1)
        self.assertFalse((self.case.work / 'nbtool_config.h').exists())

    def test_changed_generated_config_refuses_before_compile(self):
        calls = []
        def change(argv, log, **kwargs):
            if Path(log).stem == 'compile-utimens':
                self.fail('changed config reached compiler')
            calls.append(Path(log).stem)
            return self.fake(argv, log, **kwargs)
        original = component.verify
        def verify(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[0]['id'] == component.PROBES[-1]:
                (self.case.work / 'nbtool_config.h').write_text('changed\n')
            return result
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=change), \
             patch.object(component, 'verify', side_effect=verify):
            result = self.case.invoke()
        self.assertEqual(result['status'], 'FAILED')
        self.assertIn('configuration changed', result['error'])
        self.assertNotIn('compile-utimens', calls)

    def test_changed_object_refuses_before_archive(self):
        original = component.verify
        def verify(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[0]['id'] == 'compile-utimens':
                (self.case.work / 'utimens.o').write_bytes(b'changed')
            return result
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=self.fake), \
             patch.object(component, 'verify', side_effect=verify):
            result = self.case.invoke()
        self.assertEqual(result['status'], 'FAILED')
        self.assertIn('dependency changed', result['error'])
        self.assertFalse((self.case.work / 'libnbcompat.a').exists())

    def test_retained_candidate_rejects_changed_archive_or_failed_result(self):
        c = self.case
        result = self.run_fixture()
        self.assertEqual(result['status'], 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW')
        failed = copy.deepcopy(result)
        failed['commands'][6]['actual_exit'] = 1
        with self.assertRaisesRegex(SDKError, 'commands differ or failed'):
            component.candidate(result['bootstrap_plan'], c.f.document(failed))
        (c.work / 'libnbcompat.a').write_bytes(b'!<arch>\n')
        with self.assertRaisesRegex(SDKError, 'dependency changed|missing, changed or extra'):
            component.candidate(result['bootstrap_plan'], c.f.document(result))

    def test_component_input_needs_independent_review_and_same_compiler(self):
        c = self.case
        result = self.run_fixture()
        candidate = component.candidate(result['bootstrap_plan'], c.f.document(result))
        candidate_ref = c.f.document(candidate)
        review = dict(status='REVIEWED_INITIAL_OS_COMPAT_COMPONENT', candidate=candidate_ref,
                      findings=[], all_leases_closed=True, ordinary_provider_qualified=False)
        receipt = dict(format='emberbsd-os-compat-component-acceptance', version=1,
            scope='gcc16-bootstrap', status='ACCEPTED_INITIAL_OS_COMPAT_COMPONENT',
            component='filetime', candidate=candidate_ref, independent_review=c.f.document(review),
            initial_only=True, ordinary_provider_qualified=False)
        with self.assertRaisesRegex(SDKError, 'read-only'):
            component.validate_input(c.f.document(receipt), c.descriptor)
        for ref in candidate['exports'].values():
            Path(ref['path']).chmod(0o444)
        self.assertEqual(component.validate_input(c.f.document(receipt), c.descriptor),
                         candidate['exports'])
        changed = copy.deepcopy(c.descriptor)
        changed['abi_flags']['cflags'].append('-DCHANGED_ABI')
        with self.assertRaisesRegex(SDKError, 'compiler or ABI differs'):
            component.validate_input(c.f.document(receipt), changed)
        review['findings'] = ['unresolved']
        receipt['independent_review'] = c.f.document(review)
        with self.assertRaisesRegex(SDKError, 'independent review'):
            component.validate_input(c.f.document(receipt), c.descriptor)

    def test_no_header_from_partial_or_unexpected_probe_observations(self):
        value = component.manifest(self.case.plan)
        with self.assertRaisesRegex(SDKError, 'successful actual control'):
            component.configuration([], value)
        completed = [dict(row, actual_exit=0) for row in self.case.plan['commands'][:3]]
        with self.assertRaisesRegex(SDKError, 'every ordered probe'):
            component.configuration(completed, value)
        for step, probe in zip(self.case.plan['commands'][3:5], value['probes']):
            completed.append(dict(step, actual_exit=2,
                configuration_probe=dict(macro=probe['macro'], available=False)))
        with self.assertRaisesRegex(SDKError, 'drained capability'):
            component.configuration(completed, value)

    def test_archive_refuses_extra_member_without_executing_ar(self):
        result = self.run_fixture()
        self.assertEqual(result['status'], 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW')
        path = self.case.work / 'libnbcompat.a'
        raw = path.read_bytes()
        path.write_bytes(raw + raw[8:])
        with self.assertRaisesRegex(SDKError, 'missing, changed or extra'):
            component._archive(str(self.case.work))

    def test_unknown_step_and_changed_probe_source_refuse_before_child(self):
        c = self.case
        c.work.mkdir()
        for name, data in component.sources(c.plan).items():
            (c.work / name).write_bytes(data)
        step = copy.deepcopy(c.plan['commands'][0])
        step['id'] = 'unknown'
        with self.assertRaisesRegex(SDKError, 'step order'):
            component.prepare(step, c.plan, [])
        with self.assertRaisesRegex(SDKError, 'step order'):
            component.prepare(c.plan['commands'][0], c.plan, c.plan['commands'])
        (c.work / 'filetime-control.c').write_text('changed')
        with self.assertRaisesRegex(SDKError, 'work source changed'):
            component.prepare(c.plan['commands'][0], c.plan, [])

    def test_new_source_header_cannot_shadow_the_owned_namespace(self):
        c = self.case
        c.work.mkdir()
        for name, data in component.sources(c.plan).items():
            (c.work / name).write_bytes(data)
        (c.source / 'lib/libc/gen/namespace.h').write_text('unexpected source header')
        with self.assertRaisesRegex(SDKError, 'source changed'):
            component.prepare(c.plan['commands'][0], c.plan, [])

    def test_extra_work_headers_refuse_before_each_dependent_compile(self):
        c = self.case
        actual = self.run_fixture()['commands']
        for step_index in (5, 8):
            for name in ('fcntl.h', 'sys'):
                with self.subTest(step=step_index, name=name):
                    path = c.work / name
                    if name == 'sys':
                        path.mkdir()
                        (path / 'stat.h').write_text('unexpected nested header')
                    else:
                        path.write_text('unexpected header')
                    try:
                        with self.assertRaisesRegex(SDKError, 'work roster'):
                            component.prepare(c.plan['commands'][step_index], c.plan,
                                              actual[:step_index])
                    finally:
                        if path.is_dir():
                            (path / 'stat.h').unlink()
                            path.rmdir()
                        else:
                            path.unlink()

    def test_work_roster_enumeration_is_bounded_and_does_not_follow_links(self):
        c = self.case
        c.work.mkdir()
        for name, data in component.sources(c.plan).items():
            (c.work / name).write_bytes(data)
        path = c.work / 'namespace.h'
        path.unlink()
        path.symlink_to(c.source / 'tools/compat/namespace.h')
        with self.assertRaisesRegex(SDKError, 'work roster'):
            component._work_inputs(c.plan)
        count = 0
        limit = len({*component.outputs(c.plan), 'result.json',
                     *(row['id'] + '.log' for row in c.plan['commands'])})
        def entries():
            nonlocal count
            while count <= limit + 1:
                count += 1
                item = Mock()
                item.name = 'result.json'
                item.is_file.return_value = True
                yield item
        class Scan:
            def __enter__(self):
                return entries()
            def __exit__(self, *args):
                return False
        with patch.object(component.os, 'scandir', return_value=Scan()):
            with self.assertRaisesRegex(SDKError, 'work roster'):
                component._work_inputs(c.plan)
        self.assertEqual(count, limit + 1)

    def test_extra_work_header_between_steps_never_reaches_compiler(self):
        calls = []
        original = component.verify
        def verify(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[0]['id'] == component.PROBES[-1]:
                (self.case.work / 'fcntl.h').write_text('unexpected header')
            return result
        def observe(argv, log, **kwargs):
            calls.append(Path(log).stem)
            return self.fake(argv, log, **kwargs)
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=observe), \
             patch.object(component, 'verify', side_effect=verify):
            result = self.case.invoke()
        self.assertEqual(result['status'], 'FAILED')
        self.assertIn('work roster', result['error'])
        self.assertEqual(calls, [row['id'] for row in self.case.plan['commands'][:5]])

    def test_extra_work_header_after_prepare_refuses_at_drain(self):
        def inject(argv, log, **kwargs):
            if Path(log).stem == 'compile-utimens':
                (self.case.work / 'fcntl.h').write_text('unexpected header')
            return self.fake(argv, log, **kwargs)
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=inject):
            result = self.case.invoke()
        self.assertEqual(result['status'], 'FAILED')
        self.assertIn('work roster', result['error'])
        self.assertFalse((self.case.work / 'libnbcompat.a').exists())

    def test_retained_evidence_is_required_for_candidate_and_input(self):
        c = self.case
        result = self.run_fixture()
        result_ref = c.f.document(result)
        candidate = component.candidate(result['bootstrap_plan'], result_ref)
        candidate_ref = c.f.document(candidate)
        review = c.f.document(dict(status='REVIEWED_INITIAL_OS_COMPAT_COMPONENT',
            candidate=candidate_ref, findings=[], all_leases_closed=True,
            ordinary_provider_qualified=False))
        receipt = c.f.document(dict(format='emberbsd-os-compat-component-acceptance', version=1,
            scope='gcc16-bootstrap', status='ACCEPTED_INITIAL_OS_COMPAT_COMPONENT',
            component='filetime', candidate=candidate_ref, independent_review=review,
            initial_only=True, ordinary_provider_qualified=False))
        for ref in candidate['exports'].values():
            Path(ref['path']).chmod(0o444)
        self.assertEqual(component.validate_input(receipt, c.descriptor), candidate['exports'])
        refs = [c.inputs['source_proof'], c.inputs['inventory'], c.proof['members'],
                c.plan['program']['manifest']]
        for ref in refs:
            path = Path(ref['path'])
            raw = path.read_bytes()
            for change in ('missing', 'changed'):
                with self.subTest(evidence=path.name, change=change):
                    if change == 'missing':
                        path.unlink()
                    else:
                        path.write_bytes(raw + b' ')
                    try:
                        with self.assertRaises(SDKError):
                            component.candidate(result['bootstrap_plan'], result_ref)
                        with self.assertRaises(SDKError):
                            component.validate_input(receipt, c.descriptor)
                    finally:
                        path.write_bytes(raw)
        self.assertEqual(component.validate_input(receipt, c.descriptor), candidate['exports'])

    def test_normal_and_finite_source_selection_and_function_checks_agree(self):
        root = self.owner
        self.assertIn('COMPAT_FILETIME_SRCS= utimens.c',
                      (root / 'tools/compat/filetime.mk').read_text())
        self.assertIn('filetime.mk', (root / 'tools/compat/Makefile').read_text())
        ac = (root / 'tools/compat/configure.ac').read_text()
        generated = (root / 'tools/compat/configure').read_text()
        template = (root / 'tools/compat/nbtool_config.h.in').read_text()
        for name in ('utimensat', 'utimens', 'lutimens'):
            self.assertIn(name, ac)
            self.assertIn('"ac_cv_func_' + name + '"', generated)
            self.assertIn('#undef HAVE_' + name.upper(), template)
        source = (root / component.SOURCES[0]).read_text()
        self.assertIn('Copyright (c) 2012 The NetBSD Foundation', source)
        self.assertIn('!HAVE_NBTOOL_CONFIG_H || !HAVE_UTIMENS', source)


if __name__ == '__main__':
    unittest.main()
