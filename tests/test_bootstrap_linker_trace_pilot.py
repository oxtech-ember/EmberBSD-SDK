# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted). These fixtures execute no native compiler.
"""Check the fixed Darwin linker diagnostic without accepting observed execution."""

import copy
import struct
import subprocess
from pathlib import Path
import unittest
from unittest.mock import patch

import test_bootstrap_pilot as pilots
import test_bootstrap_recipe_routes as darwin
from emberbsd_sdk import bootstrap_pilot as P
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.native_toolset import NATIVE_C_PROBE, NATIVE_CXX_PROBE, _flags

PURPOSE = 'native-c-and-cxx-linker-trace-pilot'


class LinkerTracePilotTests(unittest.TestCase):
    bind_audit = pilots.PilotTests.bind_audit
    invoke = pilots.PilotTests.invoke

    def setUp(self):
        self.fixture = darwin.DarwinRecipeRoutesTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.seed = self.fixture.seed
        self.base = self.seed.native.base
        self.work = self.base / 'linker-trace-work'
        self.plan = dict(format=P.PILOT_FORMAT, version=1, scope='gcc16-bootstrap',
            purpose=PURPOSE, host=self.seed.descriptor['host'], sources=self.seed.descriptor['sources'],
            work_root=str(self.work), required_roles=['cc', 'cxx', 'cpp',
                *self.seed.descriptor['driver_components'].values()],
            resources=dict(cpu_jobs=1, link_jobs=1, runtime_seconds=120,
                lock_root=str(self.base), heavy_link_context=str(self.base / 'heavy-link')))
        self.plan['commands'] = P.pilot_steps(self.seed.descriptor, self.seed.commands,
            self.seed.descriptor['abi_flags'], str(self.work))
        # Literal fixed diagnostic on the two links, independent of the constructor.
        for step in self.plan['commands']:
            if step['phase'] == 'link':
                step['argv'][-2:-2] = ['-Wl,-v']
        self.seed.plan = self.plan
        self.seal()

    def seal(self):
        pilots.PilotTests.seal(self)
        self.seed.acceptance['driver_binding'] = copy.deepcopy(self.fixture.binding)
        query = self.seed.acceptance['checks']['driver_components']['ld']
        query['stdout'] = self.seed.native.raw((self.fixture.binding['reported_programs']['ld'] + '\n').encode())
        self.seed.acceptance_ref = self.seed.native.document(self.seed.acceptance)

    def test_exact_trace_purpose_loads_without_prior_execution(self):
        with patch('subprocess.Popen', side_effect=AssertionError('no native process')):
            try:
                selected = self.seed.load(required_roles=('cc', 'cxx'))
            except SDKError as error:
                self.fail('fixed Darwin linker trace pilot was refused: ' + str(error))
        self.assertEqual(selected.as_dict()['plan']['purpose'], PURPOSE)
        self.assertEqual(len(selected.as_dict()['plan']['commands']), 6)
        self.assertEqual(selected.abi_flags,
            {name: tuple(value) for name, value in self.seed.descriptor['abi_flags'].items()})
        self.assertNotIn('execution_audit', self.seed.acceptance)

    def test_constructor_changes_only_the_two_link_commands(self):
        descriptor, flags = self.seed.descriptor, self.seed.descriptor['abi_flags']
        before = P.pilot_steps(descriptor, self.seed.commands, flags, str(self.work))
        after = P.pilot_steps(descriptor, self.seed.commands, flags, str(self.work), linker_trace=True)
        self.assertEqual(after, self.plan['commands'])
        self.assertEqual([step['id'] for step in after],
            ['c-compile', 'c-link', 'c-run', 'cxx-compile', 'cxx-link', 'cxx-run'])
        for original, traced in zip(before, after):
            expected = copy.deepcopy(original)
            if original['phase'] == 'link':
                expected['argv'][-2:-2] = ['-Wl,-v']
                self.assertEqual(traced['argv'].count('-Wl,--threads=1'), 1)
            self.assertEqual(traced, expected)
        self.assertEqual(P.pilot_steps(descriptor, self.seed.commands, flags,
            str(self.work), linker_trace=False), before)
        static = P.pilot_steps(descriptor, self.seed.commands, flags, str(self.work), static_runtime=True)
        self.assertEqual(P.pilot_steps(descriptor, self.seed.commands, flags,
            str(self.work), static_runtime=True, linker_trace=False), static)
        self.assertTrue(all('-Wl,-v' not in step['argv'] for step in before + static))

    def test_trace_selector_is_boolean_darwin_only_and_not_static(self):
        descriptor, flags = self.seed.descriptor, self.seed.descriptor['abi_flags']
        for value in (None, 1, 'yes'):
            with self.subTest(value=value), self.assertRaises(SDKError):
                P.pilot_steps(descriptor, self.seed.commands, flags, str(self.work), linker_trace=value)
        with self.assertRaises(SDKError):
            P.pilot_steps(descriptor, self.seed.commands, flags, str(self.work),
                          static_runtime=True, linker_trace=True)
        for system in ('Linux', 'NetBSD', 'Windows'):
            changed = copy.deepcopy(descriptor)
            changed['host']['os'] = system
            with self.subTest(system=system), self.assertRaises(SDKError):
                P.pilot_steps(changed, self.seed.commands, flags, str(self.work), linker_trace=True)
            plan = copy.deepcopy(self.plan)
            plan['host'] = changed['host']
            with self.subTest(plan_system=system), self.assertRaises(SDKError):
                P.validate_pilot_plan(plan, changed, self.seed.commands, flags)

    def test_tampered_link_flags_or_purpose_refuse_before_child(self):
        original = copy.deepcopy(self.plan)
        for kind in ('missing-c', 'missing-cxx', 'duplicate', 'tail', 'compile', 'run',
                     'ordinary-purpose', 'static-purpose', 'unknown-purpose', 'extra-field'):
            with self.subTest(kind=kind):
                self.plan.clear()
                self.plan.update(copy.deepcopy(original))
                if kind.startswith('missing-'):
                    self.plan['commands'][1 if kind == 'missing-c' else 4]['argv'].remove('-Wl,-v')
                elif kind == 'duplicate':
                    self.plan['commands'][1]['argv'].insert(-2, '-Wl,-v')
                elif kind == 'tail':
                    self.plan['commands'][4]['argv'].append('-Wl,-v')
                elif kind in ('compile', 'run'):
                    self.plan['commands'][0 if kind == 'compile' else 2]['argv'].append('-Wl,-v')
                elif kind.endswith('-purpose'):
                    self.plan['purpose'] = {'ordinary-purpose': 'native-c-and-cxx-pilot',
                        'static-purpose': 'native-c-and-cxx-static-runtime-pilot',
                        'unknown-purpose': PURPOSE + '-arbitrary'}[kind]
                else:
                    self.plan['linker_trace'] = True
                self.seal()
                with patch('emberbsd_sdk.bootstrap_pilot.run_logged') as run:
                    with self.assertRaises(SDKError):
                        self.invoke()
                    run.assert_not_called()
                self.assertFalse(self.work.exists())

    def test_controller_keeps_original_sources_and_unqualified_result(self):
        def fake_command(argv, log, **kwargs):
            self.assertEqual((self.work / 'native.c').read_bytes(), NATIVE_C_PROBE)
            self.assertEqual((self.work / 'native.cc').read_bytes(), NATIVE_CXX_PROBE)
            Path(log).write_bytes(b'synthetic command-reported linker diagnostics\n')
            if '-o' in argv:
                image = bytearray(64)
                struct.pack_into('<4I', image, 0, 0xfeedfacf, 0x0100000c, 0, 1 if '-c' in argv else 2)
                output = Path(argv[-1])
                output.write_bytes(image)
                output.chmod(0o755)
            return subprocess.CompletedProcess(argv, 0)
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=fake_command) as run, \
             patch('subprocess.Popen', side_effect=AssertionError('no native process')):
            result = self.invoke()
        self.assertEqual(run.call_count, 6)
        self.assertEqual(result['status'], 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW')
        self.assertTrue(result['all_commands_drained'] and result['inputs_revalidated_after_drain'])
        self.assertFalse(result['ordinary_provider_qualified'])
        self.assertEqual([row['argv'] for row in result['commands']],
                         [step['argv'] for step in self.plan['commands']])
        self.assertTrue(all(row['actual_exit'] == 0 for row in result['commands']))

    def test_trace_flag_does_not_enter_the_ordinary_abi_grammar(self):
        for name in ('cppflags', 'cflags', 'cxxflags', 'ldflags'):
            flags = {key: [] for key in ('cppflags', 'cflags', 'cxxflags', 'ldflags')}
            flags[name] = ['-Wl,-v']
            with self.subTest(field=name), self.assertRaises(SDKError):
                _flags(flags)


if __name__ == '__main__':
    unittest.main()
