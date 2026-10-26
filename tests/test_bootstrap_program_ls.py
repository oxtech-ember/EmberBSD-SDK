# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Canonical ls contracts and required API controls. No native children."""
import copy
import hashlib
import json
import os
from pathlib import Path
import struct
import unittest
from unittest.mock import patch

import test_bootstrap_program as fixtures
from emberbsd_sdk import bootstrap_program as program
from emberbsd_sdk import bootstrap_program_components as components
from emberbsd_sdk.errors import CommandError, SDKError


@unittest.skipUnless(os.environ.get('EMBERBSD_LS_SOURCE'), 'select canonical ls source')
class LsProgramTests(unittest.TestCase):
    def setUp(self):
        self.owner = Path(os.environ['EMBERBSD_LS_SOURCE'])
        self.manifest = json.loads((self.owner/'tools/ls/bootstrap.json').read_text())
        self.plan = dict(stage='ls-bootstrap', work_root='/owned/work',
                         program=dict(source=dict(root=str(self.owner))))
        self.descriptor = dict(host=dict(os='Darwin'))
        self.commands = dict(cc=['/owned/bin/gcc'], sh=['/owned/bin/sh'])
        self.flags = dict(cppflags=['-isysroot', '/owned/abi'],
                          cflags=['-O2'], ldflags=['-specs=/owned/specs'])

    def steps(self):
        with patch.object(program, 'program_manifest', return_value=self.manifest), \
             patch.object(components, 'flags', return_value=(['-include', '/accepted/nbtool_config.h'], ['/accepted/libnbcompat.a'])):
            return program.program_steps(self.plan, self.descriptor, self.commands, self.flags)

    def test_exact_owner_and_full_source_roster(self):
        relative, digest, sources, provenance = program._profile(self.plan)
        self.assertEqual(hashlib.sha256((self.owner/relative).read_bytes()).hexdigest(), digest)
        program._native_program_contract(self.manifest, 'ls')
        self.assertEqual(len(sources), 5)
        self.assertEqual(len(self.manifest['runtime_cases']), 38)
        self.assertIn('tools/ls/probes/control.c', provenance)
        self.assertIn('tools/Makefile', provenance)
        self.assertEqual(components.CONSUMERS['ls-bootstrap'], ('listing',))
        for key, value in [('control', 'private.c'), ('components', ['filetime']), ('headers', [])]:
            bad = copy.deepcopy(self.manifest)
            bad[key] = value
            with self.assertRaises(SDKError):
                program._native_program_contract(bad, 'ls')

    def test_required_control_precedes_all_production_commands(self):
        steps = self.steps()
        self.assertEqual(len(steps), 48)
        self.assertEqual([row['id'] for row in steps[:3]],
                         ['compile-program-control', 'link-program-control', 'run-program-control'])
        self.assertEqual(steps[2]['expected_exit'], 0)
        self.assertEqual(steps[2]['argv'], ['/owned/work/program-control'])
        self.assertEqual(steps[2]['env']['DYLD_PRINT_LIBRARIES'], '1')
        compile_rows = [row for row in steps if row['phase'] == 'compile']
        self.assertEqual(len(compile_rows), 6)
        for row in compile_rows:
            self.assertIn('/accepted/nbtool_config.h', row['argv'])
            self.assertIn('/owned/abi', row['argv'])
            self.assertFalse(any('SMALL' in word or 'NOCOMPATLIB' in word for word in row['argv']))
        for row in steps:
            if row['phase'] == 'link':
                self.assertEqual(row['argv'].count('/accepted/libnbcompat.a'), 1)
        with patch.object(program, 'program_manifest', return_value=self.manifest):
            outputs = program.program_outputs(self.plan)
        self.assertIn('program-control', outputs)
        self.assertIn('program-control.o', outputs)

    def test_old_no_control_manifest_keeps_original_sequence(self):
        self.manifest.pop('control')
        steps = self.steps()
        self.assertEqual(len(steps), 45)
        self.assertEqual(steps[0]['id'], 'compile-cmp')
        self.assertFalse(any('program-control' in row['id'] for row in steps))

    def fixture(self):
        c = fixtures.ProgramTests(methodName='runTest')
        c.setUp()
        self.addCleanup(c.doCleanups)
        c.source, c.work = c.base/'ls-source', c.base/'ls-work'
        c.plan.update(stage='ls-bootstrap', work_root=str(c.work))
        relative, digest, sources, provenance = program._profile(c.plan)
        c.manifest = self.manifest
        for name in {relative, *sources, *provenance, *c.manifest['headers']}:
            path = c.source/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((self.owner/name).read_bytes())
        c.inputs['root'] = str(c.source)
        c.plan['program']['manifest'] = dict(path=str(c.source/relative), sha256=digest)
        c.refresh_source()
        root = c.base/'listing'
        root.mkdir()
        exports = {}
        for name in ('libnbcompat.a', 'nbtool_config.h', 'bootstrap_cdefs.h', 'compat_listing.h', 'compat_acl.h'):
            path = root/name
            path.write_bytes(('synthetic '+name).encode())
            exports[name] = dict(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        saved = dict(bootstrap_plan=c.f.document(dict(work_root=str(root))),
            source=dict(root=str(c.source), source_proof=c.f.document(dict(members=c.f.document({})))),
            producer_result=c.f.document({}), seed_descriptor=c.f.document(c.descriptor))
        receipt = c.f.document(dict(candidate=c.f.document(saved), independent_review=c.f.document({})))
        c.plan['program']['components'] = dict(listing=receipt)
        def validate(reference, descriptor, *, component):
            self.assertEqual(component, 'listing')
            self.assertEqual(reference, receipt)
            self.assertEqual(descriptor, c.descriptor)
            return exports
        validator = patch.object(components.component, 'validate_input', side_effect=validate)
        validator.start()
        self.addCleanup(validator.stop)
        c.plan['commands'] = program.program_steps(c.plan, c.descriptor, c.commands, c.flags)
        return c

    def test_complete_plan_source_and_component_selection_are_strict(self):
        c = self.fixture()
        self.assertIs(c.validate(), c.plan)
        self.assertEqual(len(json.loads(Path(c.proof['members']['path']).read_text())), 16)
        old = copy.deepcopy(c.plan['program']['components'])
        for wrong in ({}, {'filetime': old['listing']}, dict(old, parsedate=old['listing'])):
            c.plan['program']['components'] = wrong
            with self.assertRaises(SDKError):
                c.validate()
        c.plan['program']['components'] = old
        original = copy.deepcopy(c.plan['commands'])
        for mutation in ('missing', 'exit', 'trace', 'source'):
            c.plan['commands'] = copy.deepcopy(original)
            if mutation == 'missing':
                del c.plan['commands'][2]
            elif mutation == 'exit':
                c.plan['commands'][2]['expected_exit'] = 1
            elif mutation == 'trace':
                del c.plan['commands'][2]['env']['DYLD_PRINT_LIBRARIES']
            else:
                c.plan['commands'][0]['argv'][-3] = '/unselected/private.c'
            with self.subTest(mutation=mutation), self.assertRaises(SDKError):
                c.validate()

    def test_actual_failed_control_stops_before_any_ls_compile(self):
        c = self.fixture()
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
        self.assertFalse((c.work/'ls').exists())

    def test_all_read_only_fixture_effects_and_retained_reports(self):
        from emberbsd_sdk import bootstrap_program_fs as filesystem
        c = self.fixture()
        # Runtime observation also checks the selected shell's image header.
        # Use native-image test bytes; the child remains fully synthetic.
        shell = Path(c.commands['sh'][0])
        image = struct.pack('<8I', 0xfeedfacf, 0x100000c, 0, 2, 0, 0, 0, 0)
        shell.write_bytes(image)
        c.descriptor['tools']['sh']['sha256'] = hashlib.sha256(image).hexdigest()
        with patch('subprocess.Popen', side_effect=AssertionError('no native child')), \
             patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=c.fake_command):
            result = c.invoke()
        self.assertEqual(result['status'], 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW', result.get('error'))
        self.assertEqual(len(result['commands']), 48)
        checked = 0
        for row in result['commands']:
            case = filesystem.step_case(row, c.manifest)
            if case is not None:
                filesystem.verify_report(row['filesystem'], case, key=filesystem.step_key(row))
                checked += 1
        self.assertEqual(checked, 39)
        self.assertTrue(all(result[key] for key in ('all_commands_drained', 'all_leases_closed',
                                                   'inputs_revalidated_after_drain')))


if __name__ == '__main__':
    unittest.main()
