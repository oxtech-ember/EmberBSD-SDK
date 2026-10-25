# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Fixed cut/compat contracts. Compiler children are synthetic in unit tests."""
import unittest
from emberbsd_sdk import bootstrap_program as program


class CutContractTests(unittest.TestCase):
    def test_cut_dispatch_uses_existing_program_runner(self):
        self.assertIn('cut-bootstrap', program.PROGRAM_STAGES)
        relative, digest, sources, provenance = program._profile({'stage': 'cut-bootstrap'})
        self.assertEqual(relative, 'tools/cut/bootstrap.json')
        self.assertEqual(sources, ('usr.bin/cut/cut.c', 'lib/libutil/efun.c',
            'lib/libc/stdlib/reallocarr.c', 'common/lib/libc/stdlib/strtoi.c',
            'common/lib/libc/stdlib/strtou.c'))
        self.assertIn('tools/compat/Makefile', provenance)



import copy
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
from unittest.mock import patch
import test_bootstrap_program as fixtures
from emberbsd_sdk.errors import CommandError, SDKError


class CutProbeTests(unittest.TestCase):
    def setUp(self):
        from emberbsd_sdk import bootstrap_compat as compat
        self.compat = compat
        name = getattr(self, 'program_name', 'cut')
        self.case = case = fixtures.ProgramTests(methodName='runTest')
        case.setUp()
        self.addCleanup(case.doCleanups)
        # A separate synthetic source root prevents inherited grep members.
        case.source = case.base / (name + '-source')
        case.source.mkdir()
        case.inputs['root'] = str(case.source)
        case.plan.update(stage=name + '-bootstrap')
        relative, _, sources, provenance = program._profile(case.plan)
        self.contract = dict(format='emberbsd-native-bootstrap-compat', version=1,
            name='efun', origin='Synthetic source contract', source_makefile='tools/compat/Makefile',
            source_fragment='tools/compat/efun.mk', sources=list(compat.SOURCES),
            headers=['tools/compat/compat_efun.h', 'tools/compat/namespace.h',
                     'common/lib/libc/stdlib/_strtoi.h'], control='tools/compat/probes/efun-control.c',
            probes=[dict(name=name, source='tools/compat/probes/' + name + '.c', kind=kind, macro=macro)
                for name, kind, macro in [('decl-reallocarr', 'compile', 'HAVE_DECL_REALLOCARR'),
                    ('decl-strtoi', 'compile', 'HAVE_DECL_STRTOI'),
                    ('decl-strtou', 'compile', 'HAVE_DECL_STRTOU'),
                    ('func-esetfunc', 'link', 'HAVE_ESETFUNC'),
                    ('func-reallocarr', 'link', 'HAVE_REALLOCARR')]], config_header='nbtool_config.h')
        cp = case.source / 'tools/compat/bootstrap-efun.json'
        cp.parent.mkdir(parents=True)
        cp.write_text(json.dumps(self.contract))
        digest = hashlib.sha256(cp.read_bytes()).hexdigest()
        mock = patch.object(compat, 'MANIFEST_SHA256', digest)
        mock.start(); self.addCleanup(mock.stop)
        case.manifest = dict(format='emberbsd-native-bootstrap-program', version=1, name=name,
            origin='Synthetic OS contract', canonical_host_recipe='tools/' + name + '/Makefile',
            source_makefile='usr.bin/' + name + '/Makefile', compiler=dict(family='gcc',major=16,abi='native'),
            sources=list(sources), headers=[*(['usr.bin/cut/x_cut.c'] if name == 'cut' else
                        ['usr.bin/sort/' + header for header in ('sort.h', 'fsort.h', 'pathnames.h')]),
                     'tools/compat/bootstrap_cdefs.h',
                                           *self.contract['headers']],
            include_dirs=['usr.bin/' + name, 'tools/compat', 'common/lib/libc/stdlib'],
            cppflags=['-DHAVE_NBTOOL_CONFIG_H=1', '-include', 'nbtool_config.h'],
            cflags=[], link_flags=[], libraries=[], compat=dict(path='tools/compat/bootstrap-efun.json', sha256=digest),
            invocations=[name], files={'input.txt':'a:b\n'}, runtime_cases=[dict(name='case'+str(i),
                invocation=name,args=['-b','1','input.txt'],stdin='',exit=0,stdout='a\n',stderr='') for i in range(22 if name == 'cut' else 36)])
        for member in [*sources, *case.manifest['headers'], *provenance]:
            if member == 'tools/compat/bootstrap-efun.json':
                continue
            p = case.source / member; p.parent.mkdir(parents=True,exist_ok=True)
            p.write_text('synthetic owned source '+member+'\n')
        mp = case.source / relative
        mp.write_text(json.dumps(case.manifest))
        digest = hashlib.sha256(mp.read_bytes()).hexdigest()
        case.plan['program']['manifest'] = dict(path=str(mp), sha256=digest)
        mock = patch.object(program, name.upper() + '_MANIFEST_SHA256',digest)
        mock.start(); self.addCleanup(mock.stop)
        case.refresh_source()
        case.plan['commands'] = program.program_steps(case.plan,case.descriptor,case.commands,case.flags)

    def fake(self, argv, log, **kwargs):
        step = next(s for s in self.case.plan['commands'] if s['id'] == Path(log).stem)
        if step['phase'].startswith('compat-probe-'):
            Path(log).write_text('synthetic capability command: unavailable\n')
            raise CommandError(1,argv,log=log)
        return self.case.fake_command(argv,log,**kwargs)

    def test_configuration_follows_actual_results_before_five_source_compiles(self):
        case=self.case
        self.assertIs(case.validate(),case.plan)
        steps=case.plan['commands']
        self.assertEqual([s['id'] for s in steps[:3]],
                         ['compat-control-compile','compat-control-link','compat-control-run'])
        self.assertEqual(len([s for s in steps if s['phase'].startswith('compat-probe-')]),5)
        compiles=[s for s in steps if s['phase']=='compile' and s['id'].startswith('compile-')]
        self.assertEqual([s['id'] for s in compiles],
                         ['compile-cut','compile-efun','compile-reallocarr','compile-strtoi','compile-strtou'])
        for row in compiles:
            self.assertIn(str(case.work/'nbtool_config.h'),row['argv'])
        self.assertNotIn('nbtool_config.h',program.program_sources(case.plan))
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged',side_effect=self.fake):
            result=case.invoke()
        self.assertEqual(result['status'],'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW',result.get('error'))
        header=(case.work/'nbtool_config.h').read_text()
        self.assertIn('#define HAVE_REALLOCARR 0\n',header)
        self.assertIn('#define HAVE_DECL_STRTOI 0\n',header)
        self.assertFalse(result['ordinary_provider_qualified'])
        self.assertIn('nbtool_config.h',result['outputs'])
        self.assertEqual(len([s for s in result['commands'] if 'configuration_probe' in s]),5)

    def test_failed_control_cannot_be_misread_as_absent_features(self):
        def broken(argv,log,**kwargs):
            Path(log).write_text('synthetic compiler failure\n')
            raise CommandError(1,argv,log=log)
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged',side_effect=broken):
            result=self.case.invoke()
        self.assertEqual(result['status'],'FAILED')
        self.assertEqual(len(result['commands']),1)
        self.assertFalse((self.case.work/'nbtool_config.h').exists())

    def test_unexpected_probe_exit_stops_without_configuration_or_cut(self):
        def bad(argv,log,**kwargs):
            if Path(log).stem=='compat-decl-strtoi':
                Path(log).write_text('synthetic signal or driver failure\n')
                raise CommandError(2,argv,log=log)
            return self.fake(argv,log,**kwargs)
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged',side_effect=bad):
            result=self.case.invoke()
        self.assertEqual(result['status'],'FAILED')
        self.assertFalse((self.case.work/'nbtool_config.h').exists())
        self.assertFalse((self.case.work/'cut.o').exists())

    def test_changed_probe_or_added_sdk_host_path_refuses_before_child(self):
        for mutation in ('source','argv','path','exit'):
            with self.subTest(mutation=mutation):
                original=copy.deepcopy(self.case.plan)
                p=self.case.source/'tools/compat/probes/decl-strtoi.c'; raw=p.read_bytes()
                if mutation=='source': p.write_bytes(b'changed source')
                elif mutation=='argv': self.case.plan['commands'][3]['argv'].append('-w')
                elif mutation=='path': self.case.plan['commands'][3]['env']['PATH']+=':/usr/bin'
                else: self.case.plan['commands'][3]['expected_exit']=[0,1,2]
                with patch('emberbsd_sdk.bootstrap_pilot.run_logged') as run:
                    with self.assertRaises(SDKError): self.case.invoke()
                run.assert_not_called()
                p.write_bytes(raw); self.case.plan.clear(); self.case.plan.update(original)

    def test_positive_capabilities_keep_native_outputs_and_one_facts(self):
        def available(argv, log, **kwargs):
            step = next(s for s in self.case.plan['commands'] if s['id'] == Path(log).stem)
            if step['phase'].startswith('compat-probe-'):
                Path(log).write_text('synthetic successful compiler observation\n')
                obj = step['phase'] == 'compat-probe-compile'
                Path(argv[-1]).write_bytes(struct.pack('<8I', 0xfeedfacf, 0x100000c, 0,
                                                      1 if obj else 2, 0, 0, 0, 0))
                return subprocess.CompletedProcess(argv, 0)
            return self.case.fake_command(argv, log, **kwargs)
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=available):
            result = self.case.invoke()
        self.assertEqual(result['status'], 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW', result.get('error'))
        header = (self.case.work / 'nbtool_config.h').read_text()
        self.assertIn('#define HAVE_REALLOCARR 1\n', header)
        self.assertIn('#define HAVE_DECL_STRTOI 1\n', header)
        rows = [r for r in result['commands'] if 'configuration_probe' in r]
        self.assertTrue(all(row['configuration_probe']['available'] and 'output' in row for row in rows))

    def test_changed_configuration_is_rejected_after_the_owned_child(self):
        def tamper(argv, log, **kwargs):
            returned = self.fake(argv, log, **kwargs)
            if Path(log).stem == 'compile-cut':
                (self.case.work / 'nbtool_config.h').write_text('#define FORGED 1\n')
            return returned
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=tamper):
            result = self.case.invoke()
        self.assertEqual(result['status'], 'FAILED')
        self.assertIn('configuration changed', result['error'])
        self.assertFalse((self.case.work / 'efun.o').exists())

    def test_probe_non_native_output_cannot_supply_a_capability_fact(self):
        def wrong_image(argv, log, **kwargs):
            if Path(log).stem == 'compat-decl-reallocarr':
                Path(log).write_text('synthetic reported compiler success\n')
                Path(argv[-1]).write_bytes(b'not a native object')
                return subprocess.CompletedProcess(argv, 0)
            return self.fake(argv, log, **kwargs)
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=wrong_image):
            result = self.case.invoke()
        self.assertEqual(result['status'], 'FAILED')
        self.assertFalse((self.case.work / 'nbtool_config.h').exists())

    def test_changed_manifest_cannot_change_probe_policy_under_its_pinned_digest(self):
        p = self.case.source / 'tools/compat/bootstrap-efun.json'
        value = json.loads(p.read_bytes())
        value['probes'][0]['kind'] = 'link'
        p.write_text(json.dumps(value))
        self.case.refresh_source()
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged') as run:
            with self.assertRaises(SDKError):
                self.case.invoke()
        run.assert_not_called()
        self.assertFalse(self.case.work.exists())

    def test_header_cannot_be_derived_from_an_incomplete_result_set(self):
        with self.assertRaises(SDKError):
            self.compat.configuration_bytes([], self.contract)

    def test_control_abi_and_link_flags_are_selected_without_source_flags(self):
        flags = dict(cppflags=['-DABI'], cflags=['-O1'], cxxflags=[], ldflags=['-LABI'])
        steps = program.program_steps(self.case.plan, self.case.descriptor, self.case.commands, flags)
        for row in steps[:2] + steps[3:8]:
            self.assertEqual(row['argv'][:len(self.case.commands['cc'])], list(self.case.commands['cc']))
            self.assertIn('-O1', row['argv'])
            self.assertNotIn('-DHAVE_NBTOOL_CONFIG_H=1', row['argv'])
        for row in [steps[1], *[r for r in steps if r['phase'] == 'compat-probe-link']]:
            self.assertIn('-LABI', row['argv'])
            self.assertEqual(row['argv'].count('-Wl,--threads=1'), 1)

    @unittest.skipUnless(os.environ.get('EMBERBSD_TEST_OS_SOURCE'),'set EMBERBSD_TEST_OS_SOURCE')
    def test_actual_source_roster_and_pinned_owner_semantics(self):
        # This check uses the real digests, outside synthetic fixture overrides.
        root=Path(os.environ['EMBERBSD_TEST_OS_SOURCE'])
        manifest=json.loads((root/'tools/cut/bootstrap.json').read_bytes())
        self.assertEqual(hashlib.sha256((root/'tools/cut/bootstrap.json').read_bytes()).hexdigest(),ACTUAL_CUT_SHA)
        self.assertEqual(hashlib.sha256((root/'tools/compat/bootstrap-efun.json').read_bytes()).hexdigest(),ACTUAL_COMPAT_SHA)
        program._native_program_contract(manifest,'cut')
        sources=manifest['sources']
        fragment=(root/'tools/compat/efun.mk').read_text().split('COMPAT_EFUN_SRCS=')[1].split()
        self.assertEqual(fragment,[Path(s).name for s in sources[1:]])
        for member in (*sources,*manifest['headers'],*program._profile(self.case.plan)[3]):
            self.assertTrue((root/member).is_file(),member)
        self.assertIn('#include "compat_efun.h"',(root/'tools/compat/compat_defs.h').read_text())
        self.assertEqual(len(manifest['runtime_cases']),22)

ACTUAL_CUT_SHA='169b46d73ce9b36094bd5589bc6def4a061f78fe5d9d3c2579641d41f5c181dc'
ACTUAL_COMPAT_SHA='3aa76b9c12fbb0581205ac1477b5d1f11b79cf980acffbc74e2c9972410ac7d1'


if __name__ == "__main__":
    unittest.main()
