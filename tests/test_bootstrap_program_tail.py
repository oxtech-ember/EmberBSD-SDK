# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Native tail profile preparation. No C child is executed here."""

import copy
import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
import test_bootstrap_program_native as fixtures
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk import bootstrap_program as program
from emberbsd_sdk.bootstrap_program_history import _streams


class TailProgramTests(unittest.TestCase):
    def test_profile_uses_canonical_complete_source_set(self):
        self.assertIn('tail-bootstrap', program.PROGRAM_STAGES)
        _, _, sources, _ = program._profile({'stage': 'tail-bootstrap'})
        self.assertEqual(sources, tuple('usr.bin/tail/' + name + '.c'
            for name in ('forward', 'misc', 'read', 'reverse', 'tail')))

    def fixture(self):
        native = fixtures.NativeProgramTests(methodName='runTest')
        native.setUp()
        self.addCleanup(native.doCleanups)
        native.configure('tail')
        return native

    def test_shared_runner_creates_data_and_existing_tac_alias(self):
        native = self.fixture()
        case = native.case
        self.assertIs(case.validate(), case.plan)
        self.assertEqual(len(case.plan['commands']), 38)
        self.assertEqual(program.program_sources(case.plan), {'input.txt': b'prefix:xxx\n'})
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=native.fake_command):
            result = case.invoke()
        self.assertEqual(result['status'], 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW')
        self.assertFalse(result['ordinary_provider_qualified'])
        self.assertTrue((case.work / 'tac').is_symlink())
        self.assertEqual(os.readlink(case.work / 'tac'), 'tail')
        self.assertEqual((case.work / 'input.txt').read_bytes(), b'prefix:xxx\n')
        self.assertEqual(result['commands'][5]['invocation_aliases'], {'tac': 'tail'})

    def test_large_semantic_stream_uses_decoded_expected_size(self):
        native = self.fixture()
        case = native.case
        case.manifest['runtime_cases'][0]['stdout'] = [{'text': 'x', 'repeat': 16777217}, '\n']
        native.refresh_manifest('tail')
        case.work.mkdir()
        (case.work / 'case0.stdout').write_bytes(b'x' * 16777217 + b'\n')
        (case.work / 'case0.stderr').write_bytes(b'')
        step = case.plan['commands'][6]
        refs = program.verify_program_step(step, str(case.work), case.plan)
        self.assertIn('stdout', refs)
        outputs = {Path(ref['path']).name: ref['sha256'] for ref in refs.values()}
        _streams(refs, case.manifest['runtime_cases'][0], str(case.work), outputs)
        with open(case.work / 'case0.stdout', 'r+b') as output:
            output.write(b'y')
        with self.assertRaises(SDKError):
            program.verify_program_step(step, str(case.work), case.plan)

    def test_aggregate_fixture_limit_and_unsafe_paths_refuse_before_child(self):
        native = self.fixture()
        base = native.case.manifest
        for files in ({'escape/../file.txt': 'x'},
                      {name + '.txt': [{'text': 'x', 'repeat': 33554432}]
                       for name in ('one', 'two', 'three')}):
            manifest = copy.deepcopy(base)
            manifest['files'] = files
            with self.subTest(files=list(files)), self.assertRaises(SDKError):
                program._native_program_contract(manifest, 'tail')

    def test_changed_owner_data_and_command_refuse_before_child(self):
        native = self.fixture()
        case = native.case
        original = copy.deepcopy(case.plan)
        case.plan['commands'][6]['argv'][-1] = '/usr/bin/tail input.txt'
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged') as run, self.assertRaises(SDKError):
            case.invoke()
        run.assert_not_called()
        case.plan.clear()
        case.plan.update(original)
        manifest_path = Path(case.plan['program']['manifest']['path'])
        case.manifest['files']['input.txt'] = [{'text': 'changed', 'repeat': 2}]
        manifest_path.write_text(json.dumps(case.manifest))
        case.refresh_source()
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged') as run, self.assertRaises(SDKError):
            case.invoke()
        run.assert_not_called()

    @unittest.skipUnless(os.environ.get('EMBERBSD_TEST_OS_SOURCE'), 'set EMBERBSD_TEST_OS_SOURCE')
    def test_owner_manifest_has_bounded_large_regressions(self):
        self.assertIn('tail-bootstrap', program.PROGRAM_STAGES)
        root = Path(os.environ['EMBERBSD_TEST_OS_SOURCE'])
        relative, digest, sources, provenance = program._profile({'stage': 'tail-bootstrap'})
        raw = (root / relative).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), digest)
        manifest = json.loads(raw)
        program._native_program_contract(manifest, 'tail')
        self.assertEqual(manifest['invocations'], ['tail', 'tac'])
        self.assertTrue(any(isinstance(value, list) for value in manifest['files'].values()))
        for member in (*sources, *manifest['headers'], *provenance):
            self.assertTrue((root / member).is_file(), member)
            self.assertFalse((root / member).is_symlink(), member)


if __name__ == '__main__':
    unittest.main()
