# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Finite environment children and independent read-only uname observations."""

import copy
import ctypes
import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import test_bootstrap_program_native as fixtures
from emberbsd_sdk import bootstrap_program as program
from emberbsd_sdk.errors import SDKError


class EnvUnameProgramTests(unittest.TestCase):
    def test_profiles_exist_before_any_native_child(self):
        for name in ('env', 'uname'):
            self.assertIn(name + '-bootstrap', program.PROGRAM_STAGES)

    def test_uname_oracle_preserves_order_spaces_and_newlines(self):
        self.assertTrue(hasattr(program, '_uname_output'))
        with patch.object(program.os, 'uname', return_value=os.uname_result(
                ('system', 'node', 'release', 'version with\nnewline', 'machine'))), patch.object(
                program, '_machine_arch', return_value=b'processor'):
            self.assertEqual(program._uname_output(['version', 'machine_arch']), b'version with\nnewline processor\n')
            self.assertEqual(program._uname_output(['sysname']), b'system\n')

    def test_darwin_processor_family_uses_kernel_architecture_without_fallback(self):
        families = [('arm64', b'arm'), ('aarch64', b'arm'), ('arm', b'arm'),
                    ('x86_64', b'i386'), ('amd64', b'i386'), ('i386', b'i386'),
                    ('powerpc', b'powerpc'), ('powerpc64', b'powerpc'),
                    ('ppc', b'powerpc'), ('ppc64', b'powerpc')]
        for machine, expected in families:
            with self.subTest(machine=machine), patch.object(program.os, 'uname',
                    return_value=os.uname_result(('Darwin', 'node', 'release', 'version', machine))), patch.object(
                    ctypes, 'CDLL', side_effect=AssertionError('no FFI')), patch(
                    'subprocess.Popen', side_effect=AssertionError('no command')):
                self.assertEqual(program._machine_arch(), expected)

    def fixture(self, name):
        native = fixtures.NativeProgramTests(methodName='runTest')
        native.setUp()
        self.addCleanup(native.doCleanups)
        native.configure(name)
        return native

    def test_shared_runner_and_exact_native_oracle(self):
        for name in ('env', 'uname'):
            native = self.fixture(name)
            case = native.case
            self.assertIs(case.validate(), case.plan)
            def fake(argv, log, **kwargs):
                result = native.fake_command(argv, log, **kwargs)
                if name == 'uname' and Path(log).stem.startswith('case-'):
                    (case.work / (Path(log).stem[5:] + '.stdout')).write_bytes(b'test-system\n')
                return result
            with patch.object(program.os, 'uname', return_value=os.uname_result(
                    ('test-system', 'node', 'release', 'version', 'machine'))), patch(
                    'emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=fake):
                result = case.invoke()
            self.assertEqual(result['status'], 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW')
            self.assertFalse(result['ordinary_provider_qualified'])
            if name == 'env':
                self.assertIn('./env', case.plan['commands'][2]['argv'][-1])
                self.assertNotIn('/usr/bin', case.plan['commands'][2]['argv'][-1])

    def test_wrong_uname_output_fails(self):
        native = self.fixture('uname')
        def fake(argv, log, **kwargs):
            result = native.fake_command(argv, log, **kwargs)
            if Path(log).stem.startswith('case-'):
                (native.case.work / (Path(log).stem[5:] + '.stdout')).write_bytes(b'wrong-system\n')
            return result
        with patch.object(program.os, 'uname', return_value=os.uname_result(
                ('test-system', 'node', 'release', 'version', 'machine'))), patch(
                'emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=fake):
            result = native.case.invoke()
        self.assertEqual(result['status'], 'FAILED')
        self.assertIn('read-only controller APIs', result['error'])

    def test_changed_commands_and_missing_file_refuse_before_child(self):
        native = self.fixture('env')
        case = native.case
        old = copy.deepcopy(case.plan)
        for kind in ('argv', 'environment', 'missing-file'):
            with self.subTest(kind=kind):
                if kind == 'argv':
                    case.plan['commands'][2]['argv'][-1] = '/usr/bin/env -i sh'
                elif kind == 'environment':
                    case.plan['commands'][0]['env']['PATH'] = '/usr/bin:/bin'
                else:
                    case.work.mkdir()
                    (case.work / 'missing.txt').write_bytes(b'unexpected')
                with patch('emberbsd_sdk.bootstrap_pilot.run_logged') as run, self.assertRaises(SDKError):
                    case.invoke()
                run.assert_not_called()
                case.plan.clear()
                case.plan.update(copy.deepcopy(old))

    def test_changed_semantics_refuse_with_original_pin(self):
        for name in ('env', 'uname'):
            native = self.fixture(name)
            case = native.case
            path = Path(case.plan['program']['manifest']['path'])
            case.manifest['runtime_cases'][0]['args'] = ['/usr/bin/id']
            path.write_text(json.dumps(case.manifest))
            # Update the source snapshot, but never the reviewed manifest digest.
            case.refresh_source()
            with patch('emberbsd_sdk.bootstrap_pilot.run_logged') as run, self.assertRaises(SDKError):
                case.invoke()
            run.assert_not_called()

    def test_uname_fields_are_typed_and_confined_to_uname(self):
        native = self.fixture('uname')
        base = native.case.manifest
        for fields in ([], True, ['unknown'], ['sysname', 'sysname'], [{}]):
            manifest = copy.deepcopy(base)
            manifest['runtime_cases'][0]['stdout_uname'] = fields
            with self.subTest(fields=fields), self.assertRaises(SDKError):
                program._native_program_contract(manifest, 'uname')
        manifest = copy.deepcopy(base)
        manifest['runtime_cases'][0]['stdout'] = 'untrusted'
        with self.assertRaises(SDKError):
            program._native_program_contract(manifest, 'uname')
        with self.assertRaises(SDKError):
            program._literal_cases(base, 15)

    def test_processor_family_refuses_unknown_host_and_failed_api(self):
        for system, machine in [('Darwin', 'unknown'), ('Darwin', 'arm64-other'),
                                ('Linux', 'aarch64'), ('Darwin', '')]:
            with self.subTest(system=system, machine=machine), patch.object(program.os, 'uname',
                    return_value=os.uname_result((system, 'node', 'release', 'version', machine))), patch.object(
                    ctypes, 'CDLL', side_effect=AssertionError('no FFI')):
                with self.assertRaises(SDKError):
                    program._machine_arch()
        with patch.object(program.os, 'uname', side_effect=OSError('query failed')), patch.object(
                ctypes, 'CDLL', side_effect=AssertionError('no FFI')):
            with self.assertRaises(SDKError):
                program._machine_arch()

    @unittest.skipUnless(os.environ.get('EMBERBSD_TEST_OS_SOURCE'), 'set EMBERBSD_TEST_OS_SOURCE')
    def test_actual_owned_manifests_and_sources(self):
        root = Path(os.environ['EMBERBSD_TEST_OS_SOURCE'])
        for name in ('env', 'uname'):
            with self.subTest(name=name):
                self.assertIn(name + '-bootstrap', program.PROGRAM_STAGES)
                relative, digest, sources, provenance = program._profile({'stage': name + '-bootstrap'})
                raw = (root / relative).read_bytes()
                self.assertEqual(hashlib.sha256(raw).hexdigest(), digest)
                manifest = json.loads(raw)
                program._native_program_contract(manifest, name)
                for member in (*sources, *manifest['headers'], *provenance):
                    self.assertTrue((root / member).is_file(), member)
                    self.assertFalse((root / member).is_symlink(), member)
                if name == 'env':
                    self.assertTrue(any('./env' in row['args'] for row in manifest['runtime_cases']))
                    self.assertEqual({row['exit'] for row in manifest['runtime_cases']}, {0, 125, 126, 127})
                else:
                    self.assertTrue(any('machine_arch' in row.get('stdout_uname', []) for row in manifest['runtime_cases']))


if __name__ == '__main__':
    unittest.main()
