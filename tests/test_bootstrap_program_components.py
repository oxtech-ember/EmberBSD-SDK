# SPDX-License-Identifier: BSD-2-Clause
# Origin: EmberBSD (AI-assisted). No native children or real component payloads.
import importlib.util
import copy
import os
from pathlib import Path
from unittest.mock import patch
import unittest
import test_core  # Adds the SDK source to sys.path.
from emberbsd_sdk import bootstrap_program as program
from emberbsd_sdk import bootstrap_program_components as inputs
from emberbsd_sdk.errors import SDKError


class ComponentInputTests(unittest.TestCase):
    def test_shared_consumer_owner_exists(self):
        self.assertIsNotNone(importlib.util.find_spec('emberbsd_sdk.bootstrap_program_components'))

    def test_touch_routes_explicit_component_names(self):
        self.assertIn('touch-bootstrap',program.PROGRAM_STAGES)

    def fixture(self):
        plan=dict(stage='touch-bootstrap',program=dict(components={
            'filetime':dict(path='/source/filetime.json',sha256='a'*64),
            'parsedate':dict(path='/source/parsedate.json',sha256='b'*64)}))
        exports={}
        for name in ('filetime','parsedate'):
            exports[name]={file:dict(path='/source/'+name+'/'+file,sha256='c'*64)
                           for file in ('nbtool_config.h','libnbcompat.a','bootstrap_cdefs.h')}
        exports['parsedate']['compat_parsedate_features.h']=dict(path='/source/parsedate/compat_parsedate_features.h',sha256='d'*64)
        return plan,exports

    def test_exact_two_configs_and_archives_without_combining(self):
        plan,exports=self.fixture();descriptor={'exact':'selected'}
        def validate(ref,selected,*,component):
            self.assertIs(selected,descriptor);self.assertEqual(ref,plan['program']['components'][component])
            return exports[component]
        with patch.object(inputs.component,'validate_input',side_effect=validate) as validator:
            cpp,libs=inputs.flags(plan,descriptor)
        self.assertEqual(validator.call_count,2)
        self.assertEqual(cpp,['-include','/source/parsedate/compat_parsedate_features.h',
            '-include','/source/filetime/nbtool_config.h','-I','/source/filetime',
            '-include','/source/parsedate/nbtool_config.h','-I','/source/parsedate'])
        self.assertEqual(libs,['/source/filetime/libnbcompat.a','/source/parsedate/libnbcompat.a'])

    def test_unknown_missing_cross_pair_and_shared_header_conflict_refuse(self):
        plan,exports=self.fixture()
        for mutation in ('missing','unknown','headers','same-root'):
            changed=copy.deepcopy(plan);selected=copy.deepcopy(exports)
            if mutation=='missing':del changed['program']['components']['parsedate']
            elif mutation=='unknown':changed['program']['components']['ports']={}
            elif mutation=='headers':selected['parsedate']['bootstrap_cdefs.h']['sha256']='e'*64
            else:selected['parsedate']['libnbcompat.a']=selected['filetime']['libnbcompat.a']
            with self.subTest(mutation=mutation),patch.object(inputs.component,'validate_input',
                    side_effect=lambda ref,descriptor,component:selected[component]),self.assertRaises(SDKError):
                inputs.selected(changed,{})
        with patch.object(inputs.component,'validate_input',side_effect=SDKError('cross-paired receipt')):
            with self.assertRaises(SDKError):inputs.selected(plan,{})


@unittest.skipUnless(os.environ.get('EMBERBSD_OS_SOURCE') and os.environ.get('EMBERBSD_PARSEDATE_SOURCE'),
                     'select both canonical component sources')
class RetainedComponentInputs(unittest.TestCase):
    def test_two_real_validators_keep_retained_sources_configs_modes_and_abi(self):
        from test_bootstrap_compat_component import FiletimeComponentTests
        from test_bootstrap_compat_parsedate import ParsedateComponentTests
        cases = {}
        for name, cls in (('filetime', FiletimeComponentTests), ('parsedate', ParsedateComponentTests)):
            case = cls(methodName='runTest')
            case.setUp()
            self.addCleanup(case.doCleanups)
            cases[name] = case
        first, second = cases['filetime'].case, cases['parsedate'].case
        # Both synthetic producers select one fixture's exact GCC and ABI.
        first.descriptor['tools']['yacc'] = dict(kind='native', path=str(first.f.bundle/'yacc'))
        first.commands['yacc'] = (str(first.f.bundle/'yacc'),)
        second.descriptor = copy.deepcopy(first.descriptor)
        second.commands = copy.deepcopy(first.commands)
        second.flags = second.descriptor['abi_flags']
        for c in (first, second):
            c.plan['commands'] = program.program_steps(c.plan, c.descriptor, c.commands, c.flags)
        receipts = {}
        exported = {}
        for name, fixture in cases.items():
            result = fixture.run_fixture()
            self.assertEqual(result['status'], 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW', result.get('error'))
            c = fixture.case
            candidate = inputs.component.candidate(result['bootstrap_plan'], c.f.document(result))
            reference = c.f.document(candidate)
            review = c.f.document(dict(status='REVIEWED_INITIAL_OS_COMPAT_COMPONENT', candidate=reference,
                findings=[], all_leases_closed=True, ordinary_provider_qualified=False))
            receipts[name] = c.f.document(dict(format='emberbsd-os-compat-component-acceptance', version=1,
                scope='gcc16-bootstrap', status='ACCEPTED_INITIAL_OS_COMPAT_COMPONENT', component=name,
                candidate=reference, independent_review=review, initial_only=True, ordinary_provider_qualified=False))
            exported[name] = candidate['exports']
            for row in candidate['exports'].values():
                Path(row['path']).chmod(0o444)
        plan = dict(stage='touch-bootstrap', program=dict(components=receipts))
        self.assertEqual(inputs.selected(plan, first.descriptor), exported)
        changed = copy.deepcopy(first.descriptor)
        changed['abi_flags']['cflags'].append('-DWRONG_ABI')
        with self.assertRaisesRegex(SDKError, 'compiler or ABI differs'):
            inputs.selected(plan, changed)
        swapped = copy.deepcopy(plan)
        swapped['program']['components']['parsedate'] = receipts['filetime']
        with self.assertRaises(SDKError):
            inputs.selected(swapped, first.descriptor)
        path = Path(exported['parsedate']['libnbcompat.a']['path'])
        path.chmod(0o644)
        with self.assertRaisesRegex(SDKError, 'read-only'):
            inputs.selected(plan, first.descriptor)
        path.chmod(0o444)
        header = Path(exported['filetime']['compat_filetime.h']['path'])
        header.chmod(0o644)
        header.write_bytes(header.read_bytes() + b'changed retained bytes\n')
        with self.assertRaises(SDKError):
            inputs.selected(plan, first.descriptor)


if __name__ == '__main__':
    unittest.main()
