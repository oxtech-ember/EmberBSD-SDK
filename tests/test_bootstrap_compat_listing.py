# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Listing component source and data checks. No native compiler execution."""
import copy
import hashlib
import json
import os
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import test_bootstrap_program as fixtures
from emberbsd_sdk import bootstrap_compat_component as component
from emberbsd_sdk import bootstrap_program as program
from emberbsd_sdk.errors import SDKError, CommandError


def archive(rows):
    result = bytearray(b'!<arch>\n')
    for name, body in rows:
        if len(name) > 15:
            raw = name.encode('ascii')
            body = raw + body
            name = '#1/' + str(len(raw))
        else:
            name += '/'
        result.extend((f'{name:<16}{0:<12}{0:<6}{0:<6}{"100644":<8}{len(body):<10}`\n').encode())
        result.extend(body)
        if len(body) % 2:
            result.extend(b'\n')
    return bytes(result)


class ListingDispatchTests(unittest.TestCase):
    def test_listing_has_an_explicit_component_stage(self):
        self.assertIn('compat-listing-bootstrap', program.PROGRAM_STAGES)

    def test_shared_archive_accepts_exact_ordered_object_tuple(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = [('one.o', b'first'), ('two.o', b'second')]
            for name, body in rows:
                (root / name).write_bytes(body)
            (root / 'libnbcompat.a').write_bytes(archive(rows))
            try:
                result = component._archive(root, ('one.o', 'two.o'))
            except (SDKError, TypeError) as error:
                self.fail('the common archive checker lacks an ordered object tuple: ' + str(error))
            self.assertEqual(result['path'], str(root / 'libnbcompat.a'))


class ListingComponentTests(unittest.TestCase):
    source_variable = 'EMBERBSD_LISTING_SOURCE'
    member_count = 37
    manifest_version = 2

    def setUp(self):
        source = os.environ.get(self.source_variable)
        if not source:
            self.skipTest('select the canonical listing owner source')
        self.owner = Path(source)
        self.case = c = fixtures.ProgramTests(methodName='runTest')
        c.setUp()
        self.addCleanup(c.doCleanups)
        c.source = c.base / 'listing-source'
        c.inputs['root'] = str(c.source)
        c.plan['stage'] = component.LISTING_STAGE
        relative = 'tools/compat/bootstrap-listing.json'
        c.plan['program']['manifest'] = dict(path=str(c.source / relative),
            sha256=hashlib.sha256((self.owner / relative).read_bytes()).hexdigest())
        fixed = component.profile(c.plan)
        for name in {fixed['manifest'], *fixed['sources'], *fixed['headers'], *fixed['provenance']}:
            path = c.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((self.owner / name).read_bytes())
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
            if step['id'] not in ('listing-stat-flags', 'listing-acl-darwin'):
                raise CommandError(1, argv, log=log)
            Path(argv[-1]).write_bytes(struct.pack('<8I', 0xfeedfacf, 0x100000c, 0, 2, 0, 0, 0, 0))
            return subprocess.CompletedProcess(argv, 0)
        if step['phase'] == 'component-archive':
            (c.work / 'libnbcompat.a').write_bytes(archive([
                (name, (c.work / name).read_bytes()) for name in
                ('getbsize.o', 'humanize_number.o', 'stat_flags.o', 'compat_acl.o')]))
        return c.fake_command(argv, log, **kwargs)

    def run_fixture(self, child=None):
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=child or self.fake):
            return self.case.invoke()

    def completed(self):
        result = self.run_fixture()
        self.assertEqual(result['status'], 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW', result.get('error'))
        return result

    def test_real_owner_plan_has_four_canonical_units_and_two_runtime_traces(self):
        c = self.case
        with patch('subprocess.Popen', side_effect=AssertionError('no native child')):
            self.assertIs(c.validate(), c.plan)
        steps = c.plan['commands']
        self.assertEqual(len(steps), 19)
        self.assertEqual([s['id'] for s in steps[9:13]],
                         ['compile-getbsize', 'compile-humanize_number', 'compile-stat_flags', 'compile-compat_acl'])
        self.assertEqual(steps[13]['argv'], [*c.commands['ar'], 'rc', str(c.work / 'libnbcompat.a'),
            *(str(c.work / name) for name in ('getbsize.o', 'humanize_number.o', 'stat_flags.o', 'compat_acl.o'))])
        self.assertEqual([s['id'] for s in steps if 'DYLD_PRINT_LIBRARIES' in s['env']],
                         ['listing-control-run', 'runtime-observation'])
        self.assertFalse(any(s['phase'] == 'generator' for s in steps))
        value = component.manifest(c.plan)
        fixed = component.profile(c.plan)
        expected = {fixed['manifest'], value['source_makefile'], value['source_fragment'],
                    *value['sources'], *value['headers'], *value['provenance'],
                    value['control'], value['check'], *(p['source'] for p in value['probes'])}
        self.assertEqual(value['version'], self.manifest_version)
        self.assertEqual(len(expected), self.member_count)
        self.assertEqual(expected, {fixed['manifest'], *fixed['sources'], *fixed['headers'], *fixed['provenance']})

    def test_profile_selection_does_not_accept_an_unknown_manifest(self):
        c = self.case
        c.plan['program']['manifest']['sha256'] = 'f' * 64
        with self.assertRaisesRegex(SDKError, 'unsupported OS component manifest'):
            component.manifest(c.plan)

    def test_changed_version_cannot_reuse_a_pinned_source_contract(self):
        c = self.case
        path = Path(c.plan['program']['manifest']['path'])
        value = json.loads(path.read_text())
        value['version'] = 99
        path.write_text(json.dumps(value))
        with self.assertRaises(SDKError):
            component.manifest(c.plan)

    def test_actual_probe_observations_are_retained_without_native_formatter_substitution(self):
        c = self.case
        result = self.completed()
        config = (c.work / 'nbtool_config.h').read_text()
        for macro in ('HAVE_LISTING_NATIVE_GETBSIZE', 'HAVE_LISTING_NATIVE_HUMANIZE',
                      'HAVE_LISTING_NATIVE_FLAGS', 'HAVE_LISTING_ACL_BSD'):
            self.assertIn('#define ' + macro + ' 0\n', config)
        self.assertIn('#define HAVE_LISTING_ACL_DARWIN 1\n', config)
        self.assertIn('#define HAVE_LISTING_STAT_FLAGS 1\n', config)
        saved = component.candidate(result['bootstrap_plan'], c.f.document(result))
        self.assertEqual(saved['component'], 'listing')
        self.assertEqual(set(saved['exports']), {'libnbcompat.a', 'nbtool_config.h',
            'compat_listing.h', 'compat_acl.h', 'bootstrap_cdefs.h'})

    def test_required_backend_failure_stops_before_canonical_compile(self):
        calls = []
        def child(argv, log, **kwargs):
            calls.append(Path(log).stem)
            if Path(log).stem == 'listing-acl-darwin':
                Path(log).write_text('Synthetic missing native backend\n')
                raise CommandError(1, argv, log=log)
            return self.fake(argv, log, **kwargs)
        result = self.run_fixture(child)
        self.assertEqual(result['status'], 'FAILED')
        self.assertIn('required capability', result['error'])
        self.assertNotIn('compile-getbsize', calls)
        self.assertFalse((self.case.work / 'nbtool_config.h').exists())

    def test_required_stat_flags_and_failed_control_cannot_produce_configuration(self):
        c = self.case
        value = component.manifest(c.plan)
        completed = [dict(row, actual_exit=0) for row in c.plan['commands'][:3]]
        for row, probe in zip(c.plan['commands'][3:9], value['probes']):
            status = 1 if row['id'] == 'listing-stat-flags' else 0
            completed.append(dict(row, actual_exit=status,
                configuration_probe=dict(macro=probe['macro'], available=status == 0)))
        with self.assertRaisesRegex(SDKError, 'required capability'):
            component.configuration(completed, value)
        completed[2]['actual_exit'] = 1
        with self.assertRaisesRegex(SDKError, 'successful actual control'):
            component.configuration(completed, value)

    def test_changed_first_object_refuses_before_next_compile(self):
        original = component.verify
        calls = []
        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[0]['id'] == 'compile-getbsize':
                (self.case.work / 'getbsize.o').write_bytes(b'changed between children')
            return result
        def child(argv, log, **kwargs):
            calls.append(Path(log).stem)
            return self.fake(argv, log, **kwargs)
        with patch.object(component, 'verify', side_effect=changed):
            result = self.run_fixture(child)
        self.assertEqual(result['status'], 'FAILED')
        self.assertNotIn('compile-humanize_number', calls)

    def test_unknown_header_refuses_before_next_child(self):
        calls = []
        def child(argv, log, **kwargs):
            calls.append(Path(log).stem)
            result = self.fake(argv, log, **kwargs)
            if Path(log).stem == 'compile-getbsize':
                (self.case.work / 'fcntl.h').write_text('shadow')
            return result
        result = self.run_fixture(child)
        self.assertEqual(result['status'], 'FAILED')
        self.assertIn('work roster', result['error'])
        self.assertNotIn('compile-humanize_number', calls)

    def test_acl_feature_adapter_is_a_bound_build_input_not_a_public_export(self):
        c = self.case
        fixed = component.profile(c.plan)
        self.assertIn('tools/compat/compat_acl_features.h', fixed['headers'])
        self.assertNotIn('compat_acl_features.h', fixed['exports'])
        self.assertEqual(component.sources(c.plan)['compat_acl_features.h'],
            (self.owner / 'tools/compat/compat_acl_features.h').read_bytes())
        calls = []
        def child(argv, log, **kwargs):
            calls.append(Path(log).stem)
            result = self.fake(argv, log, **kwargs)
            if Path(log).stem == 'listing-stat-flags':
                (c.work / 'compat_acl_features.h').write_text('changed adapter')
            return result
        result = self.run_fixture(child)
        self.assertEqual(result['status'], 'FAILED')
        self.assertNotIn('listing-acl-bsd', calls)

    def test_retained_archive_rejects_reorder_duplicate_missing_extra_and_changed_member(self):
        c = self.case
        result = self.completed()
        names = ('getbsize.o', 'humanize_number.o', 'stat_flags.o', 'compat_acl.o')
        rows = [(name, (c.work / name).read_bytes()) for name in names]
        for bad in (rows[::-1], [rows[0], *rows], rows[:-1], [*rows, ('extra.o', b'x')],
                    [(rows[0][0], b'changed'), *rows[1:]]):
            with self.subTest(names=[r[0] for r in bad]):
                (c.work / 'libnbcompat.a').write_bytes(archive(bad))
                with self.assertRaises(SDKError):
                    component._archive(c.work, names)
        (c.work / 'libnbcompat.a').write_bytes(archive(rows))
        self.assertEqual(component.candidate(result['bootstrap_plan'], c.f.document(result))['component'], 'listing')

    def test_receipt_cannot_cross_pair_components_or_lose_source_evidence(self):
        c = self.case
        result = self.completed()
        saved = component.candidate(result['bootstrap_plan'], c.f.document(result))
        ref = c.f.document(saved)
        review = c.f.document(dict(status='REVIEWED_INITIAL_OS_COMPAT_COMPONENT', candidate=ref,
            findings=[], all_leases_closed=True, ordinary_provider_qualified=False))
        receipt = dict(format='emberbsd-os-compat-component-acceptance', version=1, scope='gcc16-bootstrap',
            status='ACCEPTED_INITIAL_OS_COMPAT_COMPONENT', component='listing', candidate=ref,
            independent_review=review, initial_only=True, ordinary_provider_qualified=False)
        for exported in saved['exports'].values():
            Path(exported['path']).chmod(0o444)
        self.assertEqual(component.validate_input(c.f.document(receipt), c.descriptor, component='listing'), saved['exports'])
        for other in ('filetime', 'parsedate'):
            with self.assertRaises(SDKError):
                component.validate_input(c.f.document(receipt), c.descriptor, component=other)
        Path(c.inputs['source_proof']['path']).unlink()
        with self.assertRaises(SDKError):
            component.validate_input(c.f.document(receipt), c.descriptor, component='listing')

    def test_missing_selected_archiver_refuses_before_children(self):
        c = self.case
        commands = copy.deepcopy(c.commands)
        commands.pop('ar')
        with patch('subprocess.Popen', side_effect=AssertionError('no native child')):
            with self.assertRaises(SDKError):
                program.program_steps(c.plan, c.descriptor, commands, c.flags)

    def test_semantic_fixture_left_after_child_refuses(self):
        def child(argv, log, **kwargs):
            result = self.fake(argv, log, **kwargs)
            if Path(log).stem == 'listing-check':
                (self.case.work / 'listing-file').write_bytes(b'not removed')
            return result
        result = self.run_fixture(child)
        self.assertEqual(result['status'], 'FAILED')
        self.assertIn('work roster', result['error'])


if __name__ == '__main__':
    unittest.main()
