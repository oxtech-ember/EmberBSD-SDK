# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted). No C/C++ compiler is executed.
"""Check fixed fixtures with a selected mksh and no external utilities."""

import copy
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import test_core  # Adds the SDK source to sys.path.
from emberbsd_sdk import bootstrap_make, bootstrap_program, bootstrap_shell
from emberbsd_sdk.errors import SDKError
import test_bootstrap_program as programs
import test_bootstrap_bsdinstall as bsdinstall
import test_bootstrap_program_gnu as gnu
import test_bootstrap_pkg_install as packages


class SharedStdinTests(unittest.TestCase):
    def test_all_four_profiles_share_the_literal_builtin_route(self):
        for fixture in (programs.ProgramTests, bsdinstall.BSDInstallTests,
                        gnu.GNUProgramTests, packages.PkgInstallTests):
            case = fixture(methodName='runTest')
            case.setUp()
            self.addCleanup(case.doCleanups)
            if fixture is gnu.GNUProgramTests:
                case.configure()
            plan = case.case.plan if fixture is gnu.GNUProgramTests else case.plan
            cases = [row for row in plan['commands'] if row['phase'] == 'program-case']
            self.assertTrue(cases)
            for row in cases:
                with self.subTest(profile=fixture.__name__, case=row['id']):
                    producer = row['argv'][-1].split(' | ', 1)[0]
                    self.assertIn('case "${KSH_VERSION-}" in *MIRBSD*)', producer)
                    self.assertIn('print -rn -- ', producer)
                    self.assertNotIn('printf', producer)

    def test_unrepresentable_literal_input_is_refused(self):
        for value in (None, b'bytes', 'nul\0data'):
            with self.subTest(value=value), self.assertRaises(SDKError):
                bootstrap_shell.literal_stdin(value)


class SelectedShellTests(unittest.TestCase):
    def setUp(self):
        selected = os.environ.get('EMBERBSD_TEST_MKSH')
        if selected is None:
            self.skipTest('set EMBERBSD_TEST_MKSH to an explicitly selected native mksh')
        self.shell = Path(selected)
        self.assertTrue(self.shell.is_absolute() and self.shell.is_file())
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.work = Path(temp.name).resolve()
        self.environment = dict(PATH='', LC_ALL='C', TZ='UTC', TMPDIR=str(self.work))

    def shell_output(self, command):
        return subprocess.run([str(self.shell), '-c', command], cwd=self.work,
            env=self.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)

    def test_make_shell_steps_need_no_printf_provider(self):
        descriptor = dict(make_binding=dict(makeconf=dict(path=str(self.work / 'makeconf.mk'))))
        commands = {role: (str(self.work / ('unused-' + role)),)
                    for role in ('cc', 'cxx', 'cpp', 'as', 'ld', 'make', 'gmake')}
        commands['sh'] = (str(self.shell),)
        flags = {key: [] for key in ('cppflags', 'cflags', 'cxxflags', 'ldflags')}
        steps = bootstrap_make.make_probe_steps(descriptor, commands, flags, str(self.work))
        for step, expected in ((steps[0], b'shell-positive-ok\n'),
                               (steps[3], b'expected-shell-failure\n')):
            with self.subTest(step=step['id']):
                result = self.shell_output(step['argv'][-1])
                self.assertEqual(result.returncode, step['expected_exit'], result.stderr)
                self.assertEqual(result.stdout, expected)
                self.assertEqual(result.stderr, b'')

    def test_direct_program_stdin_is_exact_without_a_printf_provider(self):
        case = programs.ProgramTests(methodName='runTest')
        case.setUp()
        self.addCleanup(case.doCleanups)
        payloads = ['', 'one\n', '\n\n', '-n', '-e', r'back\slash\n', 'literal | pipe\n',
                    "' \" $HOME $(exit 99); * ?\n", 'caf\u00e9\n']
        for payload in payloads:
            case.manifest['runtime_cases'][0]['stdin'] = payload
            # The pinned manifest parser is checked elsewhere; vary only the
            # literal bytes through the same source-owned command constructor.
            with patch.object(bootstrap_program, 'program_manifest', return_value=case.manifest):
                steps = bootstrap_program.program_steps(case.plan, case.descriptor, case.commands, case.flags)
            command = next(row for row in steps if row['phase'] == 'program-case')['argv'][-1]
            producer = bootstrap_shell.literal_stdin(payload)
            self.assertTrue(command.startswith(producer + ' | '))
            with self.subTest(payload=payload):
                result = self.shell_output(producer)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, payload.encode())
                self.assertEqual(result.stderr, b'')

    def test_foreign_shell_family_fails_before_output(self):
        command = bootstrap_shell.literal_stdin('hidden').replace('${KSH_VERSION-}', 'unsupported')
        result = self.shell_output(command)
        self.assertEqual(result.returncode, 42)
        self.assertEqual(result.stdout, b'')

    def test_real_selected_make_fixtures_use_only_builtins_and_recursion(self):
        inputs = [os.environ.get(name) for name in
                  ('EMBERBSD_TEST_BMAKE', 'EMBERBSD_TEST_GMAKE', 'EMBERBSD_TEST_MK_ROOT')]
        if not all(inputs):
            self.skipTest('set the three explicit selected make and mk-root inputs')
        bmake, gmake, mk = inputs
        self.assertTrue(all(Path(path).is_absolute() and Path(path).exists() for path in inputs))
        config = self.work / 'makeconf.mk'
        config.write_bytes(bootstrap_make.MAKECONF_SOURCE)
        descriptor = dict(make_binding=dict(shell=str(self.shell), mk_root=mk,
                          makeconf=dict(path=str(config))))
        commands = {role: (str(self.work / ('unused-' + role)),)
                    for role in ('cc', 'cxx', 'cpp', 'as', 'ld')}
        commands.update(sh=(str(self.shell),),
            make=(bmake, '-m', mk, 'SHELL=' + str(self.shell), 'MAKECONF=' + str(config)),
            gmake=(gmake, 'SHELL=' + str(self.shell), 'MAKECONF=' + str(config)))
        flags = {key: [] for key in ('cppflags', 'cflags', 'cxxflags', 'ldflags')}
        for name, data in bootstrap_make.make_probe_sources(descriptor, commands).items():
            (self.work / name).write_bytes(data)
        steps = bootstrap_make.make_probe_steps(descriptor, commands, flags, str(self.work))
        for step in steps:
            with self.subTest(step=step['id']):
                result = subprocess.run(step['argv'], cwd=self.work, env=dict(step['env'], PATH=''),
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10)
                self.assertEqual(result.returncode, step['expected_exit'], result.stdout)
                log = self.work / (step['id'] + '.log')
                log.write_bytes(result.stdout)
                bootstrap_make.verify_make_step(step, str(log), str(self.work), descriptor)


class LinkDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.case = programs.ProgramTests(methodName='runTest')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def test_fixed_darwin_link_trace_is_link_only(self):
        steps = self.case.plan['commands']
        for step in steps:
            self.assertEqual(step['argv'].count('-Wl,-v'), int(step['phase'] == 'link'))
        link = steps[5]
        self.assertEqual(link['argv'][-4:],
                         ['-Wl,--threads=1', '-Wl,-v', '-o', str(self.case.work / 'grep')])
        changed = copy.deepcopy(self.case.descriptor)
        changed['host']['os'] = 'Linux'
        other = bootstrap_program.program_steps(self.case.plan, changed, self.case.commands, self.case.flags)
        self.assertTrue(all('-Wl,-v' not in row['argv'] for row in other))

    def test_link_trace_tampering_is_refused(self):
        original = copy.deepcopy(self.case.plan['commands'])
        for kind in ('missing', 'duplicate', 'position', 'compile'):
            self.case.plan['commands'] = copy.deepcopy(original)
            steps = self.case.plan['commands']
            if kind == 'missing':
                steps[5]['argv'].remove('-Wl,-v')
            elif kind == 'duplicate':
                steps[5]['argv'].insert(-2, '-Wl,-v')
            elif kind == 'position':
                steps[5]['argv'].remove('-Wl,-v')
                steps[5]['argv'].append('-Wl,-v')
            else:
                steps[0]['argv'].insert(-2, '-Wl,-v')
            with self.subTest(kind=kind), self.assertRaises(SDKError):
                self.case.validate()

    def test_bsdinstall_uses_the_same_fixed_darwin_link_diagnostic(self):
        case = bsdinstall.BSDInstallTests(methodName='runTest')
        case.setUp()
        self.addCleanup(case.doCleanups)
        links = [row for row in case.plan['commands'] if row['phase'] == 'link']
        self.assertEqual(len(links), 1)
        link = links[0]
        self.assertEqual(link['argv'][-4:-1], ['-Wl,--threads=1', '-Wl,-v', '-o'])
        self.assertTrue(all('-Wl,-v' not in row['argv'] for row in case.plan['commands']
                            if row['phase'] != 'link'))
        link['argv'].remove('-Wl,-v')
        with self.assertRaises(SDKError):
            case.validate()


if __name__ == '__main__':
    unittest.main()
