# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted). No native compiler executes in these controls.
"""Exercise the fixed format routes and common admission with synthetic inputs."""

import copy
import json
from pathlib import Path
import shutil
import struct
import subprocess
import unittest
from unittest.mock import patch

import test_bootstrap_linker_trace_pilot as trace
import test_bootstrap_pilot as pilots
from emberbsd_sdk import bootstrap_pilot as P
from emberbsd_sdk.bootstrap_seed import _plan
from emberbsd_sdk.errors import CommandError, SDKError
from emberbsd_sdk.inventory import snapshot_tree
from emberbsd_sdk.native_toolset import NATIVE_C_PROBE, _flags

BASELINE = 'native-darwin-static-format-pilot'
ICONV = 'native-darwin-static-format-iconv-pilot'
LINKER = 'native-darwin-static-format-iconv-linker-pilot'


class DarwinFormatPilotTests(unittest.TestCase):
    seal = trace.LinkerTracePilotTests.seal
    bind_audit = pilots.PilotTests.bind_audit
    invoke = pilots.PilotTests.invoke

    def setUp(self):
        trace.LinkerTracePilotTests.setUp(self)
        self.work = self.base / 'format-work'
        self.plan['work_root'] = str(self.work)
        self.alias = self.fixture.sdk / 'usr/lib/libiconv.tbd'
        self.target = self.alias.with_name('libiconv.2.tbd')
        self.alias.parent.mkdir(parents=True)
        self.target.write_bytes(b'--- !tapi-tbd\nexports: [ _iconv, _iconv_open, _iconv_close ]\n')
        self.alias.symlink_to('libiconv.2.tbd')
        inventory = self.seed.native.document(snapshot_tree(str(self.fixture.sdk)))
        for kind in ('headers', 'libraries'):
            self.seed.descriptor['host_abi'][kind][0] = dict(
                root=str(self.fixture.sdk), inventory=inventory)
        self.select(ICONV)

    def select(self, purpose):
        self.plan['purpose'] = purpose
        self.plan['commands'] = P.pilot_steps(self.seed.descriptor, self.seed.commands,
            self.seed.descriptor['abi_flags'], str(self.work),
            darwin_format={BASELINE:'baseline', ICONV:'sdk-iconv', LINKER:'sdk-iconv-linker'}[purpose])
        self.seal()

    def fake_command(self, argv, log, **kwargs):
        self.assertEqual((self.work / 'native.c').read_bytes(), NATIVE_C_PROBE)
        self.assertEqual((self.work / 'native.cc').read_bytes(), P.DARWIN_STATIC_FORMAT_CXX_PROBE)
        output = b'synthetic format route\n'
        if argv == [str(self.work / 'cxx-probe')]:
            output += b'emberbsd-native-cxx-v1\n'
        Path(log).write_bytes(output)
        if '-o' in argv:
            if '-c' not in argv and str(self.work / 'cxx.o') in argv and not any(v in argv for v in (str(self.alias), '-Wl,' + str(self.alias))):
                Path(log).write_bytes(b'undefined symbol: _iconv_open\n')
                raise CommandError(1, argv, log=log)
            image = bytearray(64)
            struct.pack_into('<4I', image, 0, 0xfeedfacf, 0x0100000c, 0,
                             1 if '-c' in argv else 2)
            path = Path(argv[-1]); path.write_bytes(image); path.chmod(0o755)
        return subprocess.CompletedProcess(argv, 0)

    def test_both_exact_purposes_load_with_only_input_route_acceptance(self):
        for purpose in (BASELINE, ICONV):
            with self.subTest(purpose=purpose):
                self.select(purpose)
                with patch('subprocess.Popen', side_effect=AssertionError('no child')):
                    selected = self.seed.load(required_roles=('cc', 'cxx'))
                self.assertEqual(selected.as_dict()['plan']['purpose'], purpose)
                self.assertNotIn('execution_audit', self.seed.acceptance)

    def test_routes_preserve_inputs_and_differ_by_only_the_one_sdk_operand(self):
        descriptor = copy.deepcopy(self.seed.descriptor)
        flags = self.seed.descriptor['abi_flags']
        base = P.pilot_steps(descriptor, self.seed.commands, flags, str(self.work),
                             darwin_format='baseline')
        fixed = P.pilot_steps(descriptor, self.seed.commands, flags, str(self.work),
                              darwin_format='sdk-iconv')
        self.assertEqual(descriptor, self.seed.descriptor)
        self.assertEqual([s['id'] for s in fixed],
            ['c-compile', 'c-link', 'c-run', 'cxx-compile', 'cxx-link', 'cxx-run'])
        for before, after in zip(base, fixed):
            expected = copy.deepcopy(before)
            if after['id'] == 'cxx-link':
                expected['argv'][-2:-2] = [str(self.alias)]
                self.assertLess(after['argv'].index(str(self.work / 'cxx.o')),
                                after['argv'].index(str(self.alias)))
            self.assertEqual(after, expected)
            if after['phase'] in ('compile', 'link'):
                self.assertIn('-v', after['argv'])
                self.assertEqual('-std=c++23' in after['argv'], after['id'].startswith('cxx-'))
            if after['phase'] == 'link':
                for flag in ('-static-libstdc++', '-static-libgcc', '-nodefaultrpaths', '-Wl,--threads=1'):
                    self.assertEqual(after['argv'].count(flag), 1)
            self.assertNotIn('DYLD_LIBRARY_PATH', after['env'])

    def test_affected_baseline_retains_failure_and_stops_before_cpp_run(self):
        self.select(BASELINE)
        with patch.object(P, 'run_logged', side_effect=self.fake_command) as run, \
                patch('subprocess.Popen', side_effect=AssertionError('no child')):
            result = self.invoke()
        self.assertEqual(run.call_count, 5)
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(result['commands'][-1]['id'], 'cxx-link')
        self.assertEqual(result['commands'][-1]['actual_exit'], 1)
        self.assertIn('_iconv_open', Path(result['commands'][-1]['log']['path']).read_text())
        self.assertTrue(result['all_commands_drained'] and result['inputs_revalidated_after_drain'])
        self.assertTrue(result['all_leases_closed'])
        self.assertFalse(result['ordinary_provider_qualified'])

    def test_corrected_six_command_route_emits_fixed_source_without_qualification(self):
        with patch.object(P, 'run_logged', side_effect=self.fake_command) as run, \
                patch('subprocess.Popen', side_effect=AssertionError('no child')):
            result = self.invoke()
        self.assertEqual(run.call_count, 6)
        self.assertEqual(result['status'], 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW')
        self.assertTrue(all(row['actual_exit'] == 0 for row in result['commands']))
        self.assertFalse(result['ordinary_provider_qualified'])
        self.assertTrue(result['all_commands_drained'] and result['inputs_revalidated_after_drain'])
        self.assertNotIn('all_leases_closed', json.loads((self.work / 'result.json').read_text()))
        self.assertIn(b'"{:L%a}", std::chrono::Monday', P.DARWIN_STATIC_FORMAT_CXX_PROBE)
        self.assertIn(b'if (value != "Mon") return 1;', P.DARWIN_STATIC_FORMAT_CXX_PROBE)

    def test_wrong_host_selector_and_mixed_purposes_refuse(self):
        d, c, f = self.seed.descriptor, self.seed.commands, self.seed.descriptor['abi_flags']
        for value in (True, False, 1, '', 'other', '/caller/libiconv.tbd', []):
            with self.subTest(selector=value), self.assertRaises(SDKError):
                P.pilot_steps(d, c, f, str(self.work), darwin_format=value)
        for option in ('static_runtime', 'linker_trace'):
            with self.subTest(option=option), self.assertRaises(SDKError):
                P.pilot_steps(d, c, f, str(self.work), darwin_format='sdk-iconv', **{option:True})
        for system in ('Linux', 'NetBSD', 'Windows'):
            changed = copy.deepcopy(d); changed['host']['os'] = system
            with self.subTest(system=system), self.assertRaises(SDKError):
                P.pilot_steps(changed, c, f, str(self.work), darwin_format='baseline')

    def test_changed_plan_purpose_args_sources_or_resources_refuse_before_child(self):
        valid = copy.deepcopy(self.plan)
        for case in ('purpose', 'missing-stub', 'other-stub', 'extra-flag', 'no-cxx23',
                     'no-static', 'no-nodefault', 'source', 'parallel', 'deadline'):
            with self.subTest(case=case):
                self.plan.clear(); self.plan.update(copy.deepcopy(valid))
                if case == 'purpose': self.plan['purpose'] = BASELINE
                elif case == 'missing-stub': self.plan['commands'][4]['argv'].remove(str(self.alias))
                elif case == 'other-stub':
                    a = self.plan['commands'][4]['argv']; a[a.index(str(self.alias))] = '/caller/libiconv.tbd'
                elif case == 'extra-flag': self.plan['commands'][4]['argv'].append('-liconv')
                elif case == 'no-cxx23': self.plan['commands'][3]['argv'].remove('-std=c++23')
                elif case == 'no-static': self.plan['commands'][4]['argv'].remove('-static-libstdc++')
                elif case == 'no-nodefault': self.plan['commands'][4]['argv'].remove('-nodefaultrpaths')
                elif case == 'source': self.plan['source'] = 'caller source'
                elif case == 'parallel': self.plan['resources']['cpu_jobs'] = 2
                else: self.plan['resources']['runtime_seconds'] = 121
                self.seal()
                with patch.object(P, 'run_logged') as run, self.assertRaises(SDKError): self.invoke()
                run.assert_not_called(); self.assertFalse(self.work.exists())

    def test_missing_changed_empty_and_redirected_live_stub_refuse_before_child(self):
        original = self.target.read_bytes()
        for case in ('missing-target', 'changed-target', 'empty-target', 'missing-link', 'redirected-link'):
            with self.subTest(case=case):
                try:
                    if case == 'missing-target': self.target.unlink()
                    elif case == 'changed-target': self.target.write_bytes(b'changed ABI bytes')
                    elif case == 'empty-target': self.target.write_bytes(b'')
                    elif case == 'missing-link': self.alias.unlink()
                    else: self.alias.unlink(); self.alias.symlink_to('../../outside.tbd')
                    with patch.object(P, 'run_logged') as run, self.assertRaises(SDKError): self.invoke()
                    run.assert_not_called(); self.assertFalse(self.work.exists())
                finally:
                    self.target.write_bytes(original)
                    if self.alias.is_symlink(): self.alias.unlink()
                    self.alias.symlink_to('libiconv.2.tbd')

    def test_inventory_cannot_redirect_or_omit_the_pinned_sdk_target(self):
        valid = self.seed.descriptor
        original = json.loads(Path(valid['host_abi']['headers'][0]['inventory']['path']).read_text())
        for case in ('absolute', 'escape', 'other-name', 'missing', 'target-link', 'empty', 'bad-sha', 'ancestor-link'):
            with self.subTest(case=case):
                d = copy.deepcopy(valid); inv = copy.deepcopy(original)
                if case in ('absolute', 'escape', 'other-name'):
                    inv['usr/lib/libiconv.tbd']['target'] = {'absolute':'/usr/lib/libiconv.2.tbd',
                        'escape':'../../../libiconv.2.tbd', 'other-name':'libother.tbd'}[case]
                elif case == 'missing': inv.pop('usr/lib/libiconv.2.tbd')
                elif case == 'target-link': inv['usr/lib/libiconv.2.tbd'] = dict(kind='link', target='elsewhere')
                elif case == 'empty': inv['usr/lib/libiconv.2.tbd']['size'] = 0
                elif case == 'bad-sha': inv['usr/lib/libiconv.2.tbd']['sha256'] = 'bad'
                else: inv['usr/lib'] = dict(kind='link', target='elsewhere')
                pinned = self.seed.native.document(inv)
                for kind in ('headers', 'libraries'): d['host_abi'][kind][0]['inventory'] = pinned
                with self.assertRaises(SDKError):
                    P.pilot_steps(d, self.seed.commands, d['abi_flags'], str(self.work), darwin_format='sdk-iconv')

    def test_one_correlated_sdk_root_is_required(self):
        for case in ('duplicate-headers', 'duplicate-libraries', 'other-library-root', 'metadata-path', 'metadata-hash'):
            with self.subTest(case=case):
                d = copy.deepcopy(self.seed.descriptor)
                if case.startswith('duplicate-'):
                    kind = case.split('-',1)[1]; d['host_abi'][kind].append(copy.deepcopy(d['host_abi'][kind][0]))
                elif case == 'other-library-root': d['host_abi']['libraries'][0]['root'] = str(self.base / 'other-sdk')
                elif case == 'metadata-path': d['driver_binding']['sdk_metadata']['path'] = str(self.base / 'SDKSettings.json')
                else: d['driver_binding']['sdk_metadata']['sha256'] = '0' * 64
                with self.assertRaises(SDKError):
                    P.pilot_steps(d, self.seed.commands, d['abi_flags'], str(self.work), darwin_format='sdk-iconv')

    def test_regular_sdk_stub_is_also_a_pinned_operand(self):
        self.alias.unlink()
        self.alias.write_bytes(self.target.read_bytes())
        pinned = self.seed.native.document(snapshot_tree(str(self.fixture.sdk)))
        for kind in ('headers', 'libraries'):
            self.seed.descriptor['host_abi'][kind][0]['inventory'] = pinned
        self.select(ICONV)
        selected = self.seed.load(required_roles=('cc', 'cxx'))
        self.assertIn(str(self.alias), selected.as_dict()['plan']['commands'][4]['argv'])

    def test_historical_plan_check_uses_inventory_without_opening_retired_sdk(self):
        shutil.rmtree(self.fixture.sdk)
        checked = _plan(self.seed.plan_ref, self.seed.descriptor,
                        self.seed.commands, self.seed.descriptor['abi_flags'], inspect_inputs=False)
        self.assertEqual(checked, self.plan)

    def test_linker_encoding_changes_only_the_exact_cxx_sdk_operand(self):
        descriptor = copy.deepcopy(self.seed.descriptor)
        flags = descriptor['abi_flags']
        old = P.pilot_steps(descriptor, self.seed.commands, flags, str(self.work),
                            darwin_format='sdk-iconv')
        encoded = P.pilot_steps(descriptor, self.seed.commands, flags, str(self.work),
                                darwin_format='sdk-iconv-linker')
        expected = copy.deepcopy(old)
        argv = expected[4]['argv']
        argv[argv.index(str(self.alias))] = '-Wl,' + str(self.alias)
        self.assertEqual(encoded, expected)
        self.assertEqual(descriptor, self.seed.descriptor)
        self.assertEqual(encoded[4]['argv'].count('-Wl,' + str(self.alias)), 1)
        self.assertNotIn(str(self.alias), encoded[4]['argv'])
        self.select(LINKER)
        selected = self.seed.load(required_roles=('cc', 'cxx'))
        self.assertEqual(selected.as_dict()['plan']['purpose'], LINKER)
        self.assertNotIn('execution_audit', self.seed.acceptance)

    def test_linker_encoding_uses_six_fixed_commands_and_retains_scope(self):
        self.select(LINKER)
        with patch.object(P, 'run_logged', side_effect=self.fake_command) as run, \
                patch('subprocess.Popen', side_effect=AssertionError('no child')):
            result = self.invoke()
        self.assertEqual(run.call_count, 6)
        self.assertEqual([row['argv'] for row in result['commands']],
                         [step['argv'] for step in self.plan['commands']])
        self.assertEqual(result['status'], 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW')
        self.assertTrue(result['all_commands_drained'] and result['inputs_revalidated_after_drain'])
        self.assertTrue(result['all_leases_closed'])
        self.assertFalse(result['ordinary_provider_qualified'])

    def test_linker_encoding_rejects_tampering_before_child(self):
        self.select(LINKER)
        valid = copy.deepcopy(self.plan)
        operand = '-Wl,' + str(self.alias)
        for case in ('direct', 'other-path', 'missing', 'duplicate', 'c-link', 'compile',
                     'old-purpose', 'unknown-purpose'):
            with self.subTest(case=case):
                self.plan.clear(); self.plan.update(copy.deepcopy(valid))
                argv = self.plan['commands'][4]['argv']
                if case == 'direct': argv[argv.index(operand)] = str(self.alias)
                elif case == 'other-path': argv[argv.index(operand)] = '-Wl,/caller/libiconv.tbd'
                elif case == 'missing': argv.remove(operand)
                elif case == 'duplicate': argv.insert(-2, operand)
                elif case == 'c-link': self.plan['commands'][1]['argv'].insert(-2, operand)
                elif case == 'compile': self.plan['commands'][3]['argv'].append(operand)
                elif case == 'old-purpose': self.plan['purpose'] = ICONV
                else: self.plan['purpose'] = LINKER + '-arbitrary'
                self.seal()
                with patch.object(P, 'run_logged') as run, self.assertRaises(SDKError):
                    self.invoke()
                run.assert_not_called(); self.assertFalse(self.work.exists())

    def test_linker_encoding_preserves_host_binding_and_live_stub_refusals(self):
        d, c, f = self.seed.descriptor, self.seed.commands, self.seed.descriptor['abi_flags']
        for option in ('static_runtime', 'linker_trace'):
            with self.subTest(option=option), self.assertRaises(SDKError):
                P.pilot_steps(d, c, f, str(self.work), darwin_format='sdk-iconv-linker', **{option:True})
        for system in ('Linux', 'NetBSD', 'Windows'):
            changed = copy.deepcopy(d); changed['host']['os'] = system
            with self.subTest(system=system), self.assertRaises(SDKError):
                P.pilot_steps(changed, c, f, str(self.work), darwin_format='sdk-iconv-linker')
        with patch.object(P, '_darwin_iconv_stub', return_value='/SDK,other/usr/lib/libiconv.tbd'), \
                self.assertRaises(SDKError):
            P.pilot_steps(d, c, f, str(self.work), darwin_format='sdk-iconv-linker')
        self.select(LINKER)
        self.target.write_bytes(b'changed SDK stub')
        with patch.object(P, 'run_logged') as run, self.assertRaises(SDKError):
            self.invoke()
        run.assert_not_called(); self.assertFalse(self.work.exists())

    def test_linker_historical_plan_uses_inventory_without_opening_retired_sdk(self):
        self.select(LINKER)
        shutil.rmtree(self.fixture.sdk)
        checked = _plan(self.seed.plan_ref, self.seed.descriptor,
                        self.seed.commands, self.seed.descriptor['abi_flags'], inspect_inputs=False)
        self.assertEqual(checked, self.plan)

    def test_diagnostic_flags_do_not_enter_the_ordinary_abi_grammar(self):
        for value in ('-static-libstdc++', '-static-libgcc', '-nodefaultrpaths', str(self.alias),
                      '-Wl,' + str(self.alias)):
            flags = {key:[] for key in ('cppflags','cflags','cxxflags','ldflags')}
            flags['ldflags'] = [value]
            with self.subTest(flag=value), self.assertRaises(SDKError): _flags(flags)


if __name__ == '__main__':
    unittest.main()
