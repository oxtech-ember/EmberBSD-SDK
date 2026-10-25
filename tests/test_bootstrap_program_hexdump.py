# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Canonical hexdump/od integration. No native compiler child is executed."""

import copy
import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import test_bootstrap_program as runner_fixtures
import test_bootstrap_program_cut as fixtures

from emberbsd_sdk import bootstrap_compat as compat
from emberbsd_sdk import bootstrap_program as program
from emberbsd_sdk.bootstrap_program_data import decode_data
from emberbsd_sdk.errors import SDKError


class HexdumpProgramTests(unittest.TestCase):
    def test_profile_uses_complete_canonical_sources_and_common_efun(self):
        self.assertIn('hexdump-bootstrap', program.PROGRAM_STAGES)
        relative, _, sources, provenance = program._profile({'stage': 'hexdump-bootstrap'})
        self.assertEqual(relative, 'tools/hexdump/bootstrap.json')
        self.assertEqual(sources, tuple('usr.bin/hexdump/' + name + '.c' for name in
            ('conv', 'display', 'hexdump', 'hexsyntax', 'odsyntax', 'parse')) + compat.SOURCES)
        self.assertTrue(set(compat.PROVENANCE) <= set(provenance))
        self.assertIn('hexdump-bootstrap', compat.EFUN_PROGRAM_STAGES)

    def fixture(self):
        fixture = fixtures.CutProbeTests(methodName='runTest')
        fixture.program_name = 'hexdump'
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def test_ten_compiles_follow_shared_probes_and_link_creates_od(self):
        fixture = self.fixture()
        case = fixture.case
        self.assertIs(case.validate(), case.plan)
        self.assertEqual(len(case.plan['commands']), 57)
        compiles = [row for row in case.plan['commands'] if row['id'].startswith('compile-')]
        self.assertEqual(len(compiles), 10)
        self.assertEqual([row['id'] for row in compiles[-4:]],
                         ['compile-efun', 'compile-reallocarr', 'compile-strtoi', 'compile-strtou'])
        self.assertEqual(len([r for r in case.plan['commands'] if r['phase'].startswith('compat-probe-')]), 5)
        for row in compiles:
            self.assertIn(str(case.work / 'nbtool_config.h'), row['argv'])
            self.assertEqual(row['env']['TMPDIR'], str(case.work))
            self.assertEqual(row['env']['LC_ALL'], 'C')
            self.assertEqual(row['argv'][:len(case.commands['cc'])], list(case.commands['cc']))
        def child(argv, log, **kwargs):
            # The probe/control links must not create a future program alias.
            if Path(log).stem == 'compile-conv':
                self.assertFalse(os.path.lexists(case.work / 'od'))
            return fixture.fake(argv, log, **kwargs)
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=child):
            result = case.invoke()
        self.assertEqual(result['status'], 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW', result.get('error'))
        self.assertFalse(result['ordinary_provider_qualified'])
        self.assertEqual(os.readlink(case.work / 'od'), 'hexdump')
        link = next(row for row in result['commands'] if row['id'] == 'link-hexdump')
        self.assertEqual(link['invocation_aliases'], {'od': 'hexdump'})
        self.assertIn('#define HAVE_REALLOCARR 0', (case.work / 'nbtool_config.h').read_text())

    def test_changed_configuration_stops_after_the_owned_child(self):
        fixture = self.fixture()
        case = fixture.case
        def tamper(argv, log, **kwargs):
            returned = fixture.fake(argv, log, **kwargs)
            if Path(log).stem == 'compile-conv':
                (case.work / 'nbtool_config.h').write_text('#define FORGED 1\n')
            return returned
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=tamper):
            result = case.invoke()
        self.assertEqual(result['status'], 'FAILED')
        self.assertIn('configuration changed', result['error'])
        self.assertFalse((case.work / 'display.o').exists())

    def test_changed_source_and_commands_refuse_before_child(self):
        fixture = self.fixture()
        case = fixture.case
        original = copy.deepcopy(case.plan)
        case.plan['commands'][8]['argv'][0] = '/usr/bin/cc'
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged') as run, self.assertRaises(SDKError):
            case.invoke()
        run.assert_not_called()
        case.plan.clear()
        case.plan.update(original)
        (case.source / 'tools/compat/probes/decl-strtoi.c').write_text('changed probe\n')
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged') as run, self.assertRaises(SDKError):
            case.invoke()
        run.assert_not_called()

    def test_changed_owner_semantics_refuse_under_the_pinned_digest(self):
        fixture = self.fixture()
        case = fixture.case
        path = Path(case.plan['program']['manifest']['path'])
        case.manifest['runtime_cases'][0]['invocation'] = '/usr/bin/od'
        path.write_text(json.dumps(case.manifest))
        case.refresh_source()
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged') as run, self.assertRaises(SDKError):
            case.invoke()
        run.assert_not_called()

    @unittest.skipUnless(os.environ.get('EMBERBSD_TEST_HEXDUMP_SOURCE'), 'set EMBERBSD_TEST_HEXDUMP_SOURCE')
    def test_real_owner_manifest_builds_every_step_and_preserves_normal_host_alias(self):
        root = Path(os.environ['EMBERBSD_TEST_HEXDUMP_SOURCE'])
        relative, digest, sources, provenance = program._profile({'stage': 'hexdump-bootstrap'})
        raw = (root / relative).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), digest)
        manifest = json.loads(raw)
        program._native_program_contract(manifest, 'hexdump')
        self.assertEqual(manifest['sources'][-4:], list(compat.SOURCES))
        self.assertEqual(manifest['compat'], dict(path=compat.MANIFEST_RELATIVE, sha256=compat.MANIFEST_SHA256))
        self.assertEqual(len(manifest['runtime_cases']), 37)
        members = {relative, *sources, *manifest['headers'], *provenance}
        self.assertEqual(len(members), 30)
        for member in members:
            self.assertTrue((root / member).is_file(), member)
            self.assertFalse((root / member).is_symlink(), member)
        make = (root / 'tools/hexdump/Makefile').read_text()
        self.assertNotIn('NOCOMPATLIB', make)
        self.assertIn('.include "${.CURDIR}/../Makefile.host"', make)
        self.assertIn('install.host: ${HOST_BINDIR}/${_TOOL_PREFIX}od${HOSTEXEEXT}', make)
        self.assertIn('${HOST_BINDIR}/${_TOOL_PREFIX}od${HOSTEXEEXT}: ${HOST_INSTALLPROG}', make)
        self.assertIn('${HOST_INSTALL_SYMLINK} ${HOSTPROGNAME}${HOSTEXEEXT} ${.TARGET}', make)
        runner = runner_fixtures.ProgramTests(methodName='runTest')
        runner.setUp()
        self.addCleanup(runner.doCleanups)
        plan = dict(runner.plan, stage='hexdump-bootstrap', program=dict(
            source=dict(root=str(root)), manifest=dict(path=str(root / relative), sha256=digest)))
        with patch('subprocess.Popen', side_effect=AssertionError('source check cannot start a child')):
            steps = program.program_steps(plan, runner.descriptor, runner.commands, runner.flags)
            prepared = program.program_sources(plan)
        self.assertEqual(len(steps), 57)
        self.assertEqual([row['id'] for row in steps if row['phase'] == 'program-case'],
                         ['case-' + case['name'] for case in manifest['runtime_cases']])
        self.assertTrue(all('\0' not in word for row in steps for word in row['argv']))
        self.assertEqual(prepared['bytes.txt'], b'\0\x01\x7f\xc2\x80\xc3\xbf')
        cases = {row['name']: row for row in manifest['runtime_cases']}
        self.assertEqual(cases['ninja-stdin']['args'], ['-t', 'x1', '-A', 'n', '-v'])
        self.assertEqual(cases['ninja-stdin']['invocation'], 'od')
        self.assertEqual(cases['elf-first-four']['args'], ['-An', '-tx1', '-N4', 'elf.txt'])
        # Independent token/row checks supplement the exact owner literal outputs.
        self.assertEqual(decode_data(cases['ninja-bytes']['stdout']).split(),
                         [b'00', b'01', b'7f', b'c2', b'80', b'c3', b'bf'])
        self.assertEqual(decode_data(cases['elf-first-four']['stdout']).split(), [b'7f', b'45', b'4c', b'46'])
        all_rows = decode_data(cases['all-rows']['stdout']).splitlines()
        self.assertEqual(len(all_rows), 3)
        self.assertTrue(all(len(row) == 72 and len(row.split()) == 16 for row in all_rows))
        self.assertEqual(decode_data(cases['duplicate-rows']['stdout']).splitlines(), [all_rows[0], b'*'])
        self.assertEqual(steps[-1]['argv'], [str(runner.work / 'hexdump'), *cases['hexdump-file']['args']])


if __name__ == '__main__':
    unittest.main()
