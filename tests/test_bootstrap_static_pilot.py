# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Developed with AI assistance. These controls execute no native compiler.

import copy
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import test_bootstrap_pilot as fixtures
from emberbsd_sdk import bootstrap_pilot as P
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.native_toolset import NATIVE_C_PROBE, NATIVE_CXX_PROBE, _flags


class StaticRuntimePilotTests(unittest.TestCase):
    setUp = fixtures.PilotTests.setUp
    seal = fixtures.PilotTests.seal
    bind_audit = fixtures.PilotTests.bind_audit
    invoke = fixtures.PilotTests.invoke
    fake_command = fixtures.PilotTests.fake_command

    def select_static(self):
        self.plan['purpose'] = 'native-c-and-cxx-static-runtime-pilot'
        self.plan['commands'] = P.pilot_steps(self.seed.descriptor, self.seed.commands,
            self.seed.descriptor['abi_flags'], str(self.work), static_runtime=True)
        self.seal()

    def test_legacy_commands_and_probe_bytes_remain_exact(self):
        expected = []
        env = dict(PATH=os.pathsep.join(sorted({os.path.dirname(row[0])
            for row in self.seed.commands.values()})), LC_ALL='C', TZ='UTC', TMPDIR=str(self.work))
        for language, role, filename in (('c', 'cc', 'native.c'), ('cxx', 'cxx', 'native.cc')):
            driver = list(self.seed.commands[role])
            obj, artifact = str(self.work / (language + '.o')), str(self.work / (language + '-probe'))
            for suffix, phase, argv in (
                ('compile', 'compile', [*driver, '-v', '-c', str(self.work / filename), '-o', obj]),
                ('link', 'link', [*driver, '-v', obj, '-o', artifact]),
                ('run', 'run', [artifact])):
                expected.append(dict(id=language + '-' + suffix, phase=phase,
                    cwd=str(self.work), argv=argv, env=dict(env)))
        self.assertEqual(self.plan['commands'], expected)
        self.assertEqual(P.pilot_steps(self.seed.descriptor, self.seed.commands,
            self.seed.descriptor['abi_flags'], str(self.work), static_runtime=False), expected)
        self.assertEqual(NATIVE_C_PROBE, b'#include <stdio.h>\n'
            b'int main(void) { return puts("emberbsd-native-c-v1") < 0; }\n')
        self.assertEqual(NATIVE_CXX_PROBE, b'#include <iostream>\n'
            b'int main() { std::cout << "emberbsd-native-cxx-v1" << std::endl; return !std::cout; }\n')
        self.assertEqual(P._pilot_sources(False),
            {'c': ('native.c', NATIVE_C_PROBE), 'cxx': ('native.cc', NATIVE_CXX_PROBE)})

    def test_static_constructor_changes_only_link_steps_and_fixed_cxx_source(self):
        descriptor = copy.deepcopy(self.seed.descriptor)
        descriptor['host'] = dict(os='Darwin', cpu='arm64')
        before = P.pilot_steps(descriptor, self.seed.commands, descriptor['abi_flags'], str(self.work))
        after = P.pilot_steps(descriptor, self.seed.commands, descriptor['abi_flags'],
                             str(self.work), static_runtime=True)
        for old, new in zip(before, after):
            expected = copy.deepcopy(old)
            if old['phase'] == 'link':
                expected['argv'][-2:-2] = ['-static-libstdc++', '-static-libgcc']
                self.assertEqual(new['argv'].count('-Wl,--threads=1'), 1)
            self.assertEqual(new, expected)
        self.assertEqual(P._pilot_sources(True)['c'], ('native.c', NATIVE_C_PROBE))
        self.assertEqual(P._pilot_sources(True)['cxx'], ('native.cc', P.STATIC_RUNTIME_CXX_PROBE))
        self.assertIn(b'throw 16;', P.STATIC_RUNTIME_CXX_PROBE)
        self.assertIn(b'catch (int value)', P.STATIC_RUNTIME_CXX_PROBE)
        self.assertIn(b'"emberbsd-native-cxx-v1" << std::endl', P.STATIC_RUNTIME_CXX_PROBE)
        self.assertNotEqual(P.STATIC_RUNTIME_CXX_PROBE, NATIVE_CXX_PROBE)
        for value in (None, 1, 'yes'):
            with self.subTest(selector=value), self.assertRaises(SDKError):
                P.pilot_steps(descriptor, self.seed.commands, descriptor['abi_flags'],
                              str(self.work), static_runtime=value)

    def test_exact_static_purpose_loads_without_prior_execution(self):
        self.select_static()
        with patch('subprocess.Popen', side_effect=AssertionError('no native process')):
            selected = self.seed.load(required_roles=('cc', 'cxx'))
        self.assertEqual(selected.as_dict()['plan']['purpose'],
                         'native-c-and-cxx-static-runtime-pilot')
        self.assertEqual(selected.abi_flags,
                         {name: tuple(value) for name, value in self.seed.descriptor['abi_flags'].items()})
        self.assertNotIn('execution_audit', self.seed.acceptance)

    def test_missing_extra_tail_compile_flags_and_purpose_mismatch_refuse_before_child(self):
        self.select_static()
        valid = copy.deepcopy(self.plan)
        for case in ('missing', 'duplicate', 'tail', 'compile', 'old-purpose',
                     'unknown-purpose', 'default-commands', 'source-override'):
            with self.subTest(case=case):
                self.plan.clear()
                self.plan.update(copy.deepcopy(valid))
                if case == 'missing':
                    self.plan['commands'][4]['argv'].remove('-static-libgcc')
                elif case == 'duplicate':
                    self.plan['commands'][1]['argv'].insert(-2, '-static-libstdc++')
                elif case == 'tail':
                    self.plan['commands'][4]['argv'].append('-static-libgcc')
                elif case == 'compile':
                    self.plan['commands'][3]['argv'].insert(-2, '-static-libgcc')
                elif case == 'old-purpose':
                    self.plan['purpose'] = 'native-c-and-cxx-pilot'
                elif case == 'unknown-purpose':
                    self.plan['purpose'] += '-arbitrary'
                elif case == 'default-commands':
                    self.plan['commands'] = P.pilot_steps(self.seed.descriptor,
                        self.seed.commands, self.seed.descriptor['abi_flags'], str(self.work))
                else:
                    self.plan['source_override'] = 'arbitrary.cc'
                self.seal()
                with patch('emberbsd_sdk.bootstrap_pilot.run_logged') as run:
                    with self.assertRaises(SDKError):
                        self.invoke()
                    run.assert_not_called()
                self.assertFalse(self.work.exists())

    def test_controller_writes_fixed_variant_and_retains_unqualified_evidence(self):
        self.select_static()
        def checked(argv, log, **kwargs):
            self.assertEqual((self.work / 'native.c').read_bytes(), NATIVE_C_PROBE)
            self.assertEqual((self.work / 'native.cc').read_bytes(), P.STATIC_RUNTIME_CXX_PROBE)
            return self.fake_command(argv, log, **kwargs)
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=checked) as run:
            result = self.invoke()
        self.assertEqual(run.call_count, 6)
        self.assertEqual(result['status'], 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW')
        self.assertTrue(result['all_commands_drained'] and result['all_leases_closed'])
        self.assertFalse(result['ordinary_provider_qualified'])
        self.assertTrue(all(row['actual_exit'] == 0 for row in result['commands']))

    def test_changed_variant_source_is_refused_after_fake_commands_drain(self):
        self.select_static()
        calls = []
        def changed(argv, log, **kwargs):
            value = self.fake_command(argv, log, **kwargs)
            calls.append(argv)
            if len(calls) == 6:
                (self.work / 'native.cc').write_bytes(NATIVE_CXX_PROBE)
            return value
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=changed):
            result = self.invoke()
        self.assertEqual(len(calls), 6)
        self.assertEqual(result['status'], 'FAILED')
        self.assertIn('pilot source changed', result['error'])
        self.assertTrue(result['all_commands_drained'] and result['all_leases_closed'])
        self.assertFalse(result['ordinary_provider_qualified'])

    def test_static_selectors_remain_outside_the_ordinary_abi_grammar(self):
        for name in ('cppflags', 'cflags', 'cxxflags', 'ldflags'):
            for flag in ('-static-libstdc++', '-static-libgcc'):
                with self.subTest(name=name, flag=flag):
                    value = copy.deepcopy(self.seed.descriptor['abi_flags'])
                    value[name] = [flag]
                    with self.assertRaises(SDKError):
                        _flags(value)


if __name__ == '__main__':
    unittest.main()
