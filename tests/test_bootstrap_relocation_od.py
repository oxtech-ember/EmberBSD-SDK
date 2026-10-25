# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Developed with AI assistance. Synthetic bytes do not qualify native tools.

import copy
from unittest.mock import patch
import unittest

from test_bootstrap_relocation import Fixture
import test_bootstrap_relocation as fixtures
from emberbsd_sdk import bootstrap_relocation as R
from emberbsd_sdk.bootstrap_seed import _history
from emberbsd_sdk.errors import SDKError


class OdFixture(Fixture):
    def __init__(self, test):
        super().__init__(test)
        self.profile['executables'] = [dict(role='od', package='coreutils-9.11',
            pkgpath='sysutils/coreutils', original_member='bin/god',
            original_invocation='gnu/bin/od', original_chain=[dict(
                path='gnu/bin/od', target_base='original-prefix',
                target_relative='bin/god')], final_regular='bin/god',
            selected_invocation='bin/od', aliases=[dict(path='bin/od', target='god')],
            requires_libintl_change=True)]
        self.profile_ref = self.document(self.profile)
        guard = patch.object(R, 'OD_PROFILE_SHA256', self.profile_ref['sha256'])
        guard.start()
        test.addCleanup(guard.stop)
        self.members = R._members(self.profile)
        self.originals = {}
        for row in self.members:
            path = self.original / row['original_member']
            path.write_bytes(self.image(row, transformed=False))
            self.originals[row['original_member']] = self.reference(path)
        alias = self.original / 'gnu/bin/od'
        alias.parent.mkdir(parents=True)
        alias.symlink_to(self.original / 'bin/god')
        self.plan['relocation'].update(profile=self.profile_ref,
            originals=self.document(self.originals))
        self.plan['commands'] = R.relocation_steps(self.plan, self.descriptor, self.commands)


class OdRelocationTests(unittest.TestCase):
    check_history = fixtures.RelocationTests.check_history
    derived_history = fixtures.RelocationTests.derived_history

    def test_legacy_profile_keeps_its_42_commands(self):
        f = Fixture(self)
        before = copy.deepcopy(f.plan)
        R.validate_relocation_plan(f.plan, f.descriptor, f.commands)
        self.assertEqual(f.plan, before)
        self.assertEqual(len(f.plan['commands']), 42)
        self.assertEqual(len(f.originals), 21)
        self.assertNotIn('od', [row['role'] for row in f.members])

    def test_od_subset_has_four_exact_operations_and_complete_postchecks(self):
        f = OdFixture(self)
        old = str(f.original / 'lib/libintl.8.dylib')
        new = str(f.final / 'lib/libintl.8.dylib')
        self.assertEqual(set(f.originals), {'bin/god', 'lib/libintl.8.dylib'})
        self.assertEqual([step['id'] for step in f.plan['commands']], [
            'run-a-00-od', 'run-a-01-libintl', 'run-b-00-od', 'run-b-01-libintl'])
        for run, start in (('run-a', 0), ('run-b', 2)):
            od, library = f.plan['commands'][start:start + 2]
            self.assertEqual(od['argv'], [f.commands['install_name_tool'][0], '--output',
                str(f.work / run / 'bin/god'), '-delete_all_rpaths', '-change', old, new,
                str(f.original / 'bin/god')])
            self.assertEqual(library['argv'], [f.commands['install_name_tool'][0], '--output',
                str(f.work / run / 'lib/libintl.8.dylib'), '-delete_all_rpaths', '-id', new,
                str(f.original / 'lib/libintl.8.dylib')])
        with patch('subprocess.Popen', side_effect=AssertionError('no native process')):
            R.validate_relocation_plan(f.plan, f.descriptor, f.commands)
            result = f.result()
            R._derivation(f.document(result), lambda reference: R._reference(reference))
        self.assertEqual(set(result['relocation']['members']), set(f.originals))

    def test_od_original_scope_alias_and_operations_are_fixed(self):
        for case in ('missing', 'extra', 'alias', 'operation', 'profile'):
            with self.subTest(case=case):
                f = OdFixture(self)
                if case == 'missing':
                    del f.originals['bin/god']
                    f.plan['relocation']['originals'] = f.document(f.originals)
                elif case == 'extra':
                    f.originals['bin/gcat'] = f.originals['bin/god']
                    f.plan['relocation']['originals'] = f.document(f.originals)
                elif case == 'alias':
                    alias = f.original / 'gnu/bin/od'
                    alias.unlink()
                    alias.symlink_to('../../bin/god')
                elif case == 'operation':
                    f.plan['commands'][0]['argv'].append('-arbitrary')
                else:
                    f.plan['relocation']['profile']['sha256'] = '0' * 64
                with self.assertRaises(SDKError):
                    R.validate_relocation_plan(f.plan, f.descriptor, f.commands)

    def test_od_derived_history_preserves_raw_hash_and_completed_result(self):
        f = OdFixture(self)
        result = f.result()
        name, history, descriptor = self.derived_history(f, result)
        (f.final / 'bin/od').symlink_to('god')
        self.assertEqual(self.check_history(history, name, descriptor, inspect_inputs=True),
                         history['original_history']['unknowns'])
        self.assertEqual(set(descriptor['tools']), {'od'})
        self.assertEqual(history['provisioned_members'][0]['original_sha256'],
                         f.originals['bin/god']['sha256'])
        for case in ('failed-command', 'omitted-command', 'original-hash', 'role'):
            with self.subTest(case=case):
                changed = copy.deepcopy(history)
                if case in ('failed-command', 'omitted-command'):
                    evidence = copy.deepcopy(result)
                    if case == 'failed-command':
                        evidence['commands'][0]['actual_exit'] = 1
                    else:
                        evidence['commands'].pop()
                    changed['derivation'] = f.document(evidence)
                    selected = descriptor
                elif case == 'original-hash':
                    changed['provisioned_members'][0]['original_sha256'] = '0' * 64
                    selected = descriptor
                else:
                    selected = copy.deepcopy(descriptor)
                    selected['tools']['cat'] = selected['tools'].pop('od')
                with self.assertRaises(SDKError):
                    self.check_history(changed, name, selected, inspect_inputs=False)

    def test_prior_full_utility_result_cannot_admit_od(self):
        old = Fixture(self)
        previous = old.result()
        f = OdFixture(self)
        name, history, descriptor = self.derived_history(f, f.result())
        history['derivation'] = f.document(previous)
        with self.assertRaises(SDKError):
            self.check_history(history, name, descriptor, inspect_inputs=False)



if __name__ == '__main__':
    unittest.main()
