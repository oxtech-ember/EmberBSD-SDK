# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Developed with AI assistance. Synthetic bytes do not qualify native tools.

import copy
from pathlib import Path
from unittest.mock import patch
import unittest

from test_bootstrap_relocation import Fixture
from test_bootstrap_relocation_printf import PrintfFixture
import test_bootstrap_relocation as fixtures
from emberbsd_sdk import bootstrap_relocation as R
from emberbsd_sdk.bootstrap_seed import _history
from emberbsd_sdk.errors import SDKError


COREUTILS = ('chgrp', 'chmod', 'chown', 'cp', 'install', 'ln', 'ls',
             'mkdir', 'mv', 'rm', 'rmdir', 'touch', 'sleep')


class BuildUtilitiesFixture(Fixture):
    def __init__(self, test):
        super().__init__(test)
        rows = [dict(role=role, package='coreutils-9.11', pkgpath='sysutils/coreutils',
            original_member='bin/g' + role, original_invocation='gnu/bin/' + role,
            original_chain=[dict(path='gnu/bin/' + role, target_base='original-prefix',
                target_relative='bin/g' + role)], final_regular='bin/g' + role,
            selected_invocation='bin/' + role, aliases=[dict(path='bin/' + role, target='g' + role)],
            requires_libintl_change=True) for role in COREUTILS]
        for role in ('pkg_add', 'pkg_create'):
            rows.append(dict(role=role, package='pkg_install-20260227nb3',
                pkgpath='pkgtools/pkg_install', original_member='sbin/' + role,
                original_invocation='sbin/' + role, original_chain=[], final_regular='sbin/' + role,
                selected_invocation='sbin/' + role, aliases=[], requires_libintl_change=False))
        rows.append(dict(role='install-info', package='pkg_install-info-4.5nb3',
            pkgpath='pkgtools/pkg_install-info', original_member='bin/pkg_install-info',
            original_invocation='bin/pkg_install-info', original_chain=[],
            final_regular='bin/pkg_install-info', selected_invocation='bin/pkg_install-info',
            aliases=[], requires_libintl_change=False))
        self.profile['executables'] = rows
        self.profile_ref = self.document(self.profile)
        guard = patch.object(R, 'BUILD_UTILITIES_PROFILE_SHA256', self.profile_ref['sha256'])
        guard.start()
        test.addCleanup(guard.stop)
        self.members = R._members(self.profile)
        self.originals = {}
        for row in self.members:
            path = self.original / row['original_member']
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self.image(row, transformed=False))
            self.originals[row['original_member']] = self.reference(path)
            for alias in row.get('original_chain', []):
                path = self.original / alias['path']
                path.parent.mkdir(parents=True, exist_ok=True)
                path.symlink_to(self.original / alias['target_relative'])
        self.plan['relocation'].update(profile=self.profile_ref,
            originals=self.document(self.originals))
        self.plan['commands'] = R.relocation_steps(self.plan, self.descriptor, self.commands)


class BuildUtilitiesRelocationTests(unittest.TestCase):
    check_history = fixtures.RelocationTests.check_history

    def package_history(self, f, result, package):
        rows = [row for row in f.members if row['pkgpath'] == package]
        with patch.object(f, 'members', rows):
            value = fixtures.RelocationTests.derived_history(self, f, result,
                library=rows[0]['role'] == 'libintl')
        if package in ('pkgtools/pkg_install', 'pkgtools/pkg_install-info'):
            original = value[1]['original_history']
            original['historical_compiler'] = 'clang-21.0.0'
            info = Path(original['package_metadata']['build_info']['path'])
            info.write_text(info.read_text().replace('CC_VERSION=gcc-16', 'CC_VERSION=clang-21.0.0'))
            original['package_metadata']['build_info'] = f.reference(info)
        for row in rows:
            for alias in row.get('aliases', []):
                (f.final / alias['path']).symlink_to(alias['target'])
        return value

    def test_previous_profiles_keep_their_exact_member_counts(self):
        for fixture, count in ((Fixture, 42), (PrintfFixture, 4)):
            f = fixture(self)
            original = copy.deepcopy(f.plan)
            R.validate_relocation_plan(f.plan, f.descriptor, f.commands)
            self.assertEqual(f.plan, original)
            self.assertEqual(len(f.plan['commands']), count)
            self.assertNotIn('cp', [row['role'] for row in f.members])

    def test_exact_34_operations_preserve_real_names_and_complete_postchecks(self):
        f = BuildUtilitiesFixture(self)
        self.assertEqual(len(f.originals), 17)
        self.assertEqual([row['role'] for row in f.profile['executables']],
                         [*COREUTILS, 'pkg_add', 'pkg_create', 'install-info'])
        old = str(f.original / 'lib/libintl.8.dylib')
        new = str(f.final / 'lib/libintl.8.dylib')
        for run, start in (('run-a', 0), ('run-b', 17)):
            steps = f.plan['commands'][start:start + 17]
            self.assertEqual([step['id'] for step in steps], [
                f'{run}-{index:02d}-{row["role"]}' for index, row in enumerate(f.members)])
            for step, row in zip(steps, f.members):
                argv = [f.commands['install_name_tool'][0], '--output',
                    str(f.work / run / row['final_regular']), '-delete_all_rpaths']
                if row['role'] in COREUTILS:
                    argv += ['-change', old, new]
                if row['role'] == 'libintl':
                    argv += ['-id', new]
                argv += [str(f.original / row['original_member'])]
                self.assertEqual(step['argv'], argv)
        self.assertEqual(len(f.plan['commands']), 34)
        with patch('subprocess.Popen', side_effect=AssertionError('no native process')):
            R.validate_relocation_plan(f.plan, f.descriptor, f.commands)
            result = f.result()
            R._derivation(f.document(result), lambda reference: R._reference(reference))
        self.assertEqual(set(result['relocation']['members']), set(f.originals))

    def test_member_alias_profile_and_operation_changes_refuse(self):
        for case in ('missing', 'extra', 'alias', 'profile', 'operation'):
            with self.subTest(case=case):
                f = BuildUtilitiesFixture(self)
                if case == 'missing':
                    del f.originals['bin/gcp']
                    f.plan['relocation']['originals'] = f.document(f.originals)
                elif case == 'extra':
                    f.originals['bin/gmktemp'] = f.originals['bin/gcp']
                    f.plan['relocation']['originals'] = f.document(f.originals)
                elif case == 'alias':
                    path = f.original / 'gnu/bin/cp'
                    path.unlink()
                    path.symlink_to('../../bin/gcp')
                elif case == 'profile':
                    f.plan['relocation']['profile']['sha256'] = '0' * 64
                else:
                    f.plan['commands'][13]['argv'].append('-arbitrary')
                with self.assertRaises(SDKError):
                    R.validate_relocation_plan(f.plan, f.descriptor, f.commands)

    def test_derived_history_is_bound_separately_to_each_original_package(self):
        packages = {'sysutils/coreutils': set(COREUTILS),
                    'pkgtools/pkg_install': {'pkg_add', 'pkg_create'},
                    'pkgtools/pkg_install-info': {'install-info'},
                    'devel/gettext-lib': set()}
        for package, roles in packages.items():
            with self.subTest(package=package):
                f = BuildUtilitiesFixture(self)
                name, history, descriptor = self.package_history(f, f.result(), package)
                self.assertEqual(set(descriptor['tools']), roles)
                self.assertEqual(self.check_history(history, name, descriptor, inspect_inputs=True),
                                 history['original_history']['unknowns'])
                self.assertEqual(history['original_history']['package_path'], package)
                self.assertEqual(history['original_history']['historical_compiler'],
                    'clang-21.0.0' if package.startswith('pkgtools/') else 'gcc-16')
                # Even a correct output path/hash from another package is not
                # a role supplied by this retained original package history.
                other = 'pkg_add' if package == 'sysutils/coreutils' else 'cp'
                row = next(row for row in f.members if row['role'] == other)
                descriptor['tools'][other] = dict(provider=name,
                    path=str(f.final / row['selected_invocation']),
                    sha256=f.reference(f.work / 'run-a' / row['final_regular'])['sha256'])
                with self.assertRaises(SDKError):
                    self.check_history(history, name, descriptor, inspect_inputs=False)

    def test_incomplete_result_or_prior_profile_cannot_supply_new_roles(self):
        for case in ('failed', 'omitted', 'prior-profile', 'original-hash'):
            with self.subTest(case=case):
                f = BuildUtilitiesFixture(self)
                result = f.result()
                name, history, descriptor = self.package_history(f, result, 'sysutils/coreutils')
                if case == 'failed':
                    result['commands'][0]['actual_exit'] = 1
                elif case == 'omitted':
                    result['commands'].pop()
                elif case == 'prior-profile':
                    result = Fixture(self).result()
                else:
                    history['provisioned_members'][0]['original_sha256'] = '0' * 64
                history['derivation'] = f.document(result)
                with self.assertRaises(SDKError):
                    self.check_history(history, name, descriptor, inspect_inputs=False)

    def test_new_roles_do_not_expand_raw_installed_package_admission(self):
        for package in ('sysutils/coreutils', 'pkgtools/pkg_install', 'pkgtools/pkg_install-info'):
            with self.subTest(package=package):
                f = BuildUtilitiesFixture(self)
                name, history, _ = self.package_history(f, f.result(), package)
                raw = history['original_history']
                rows = [row for row in f.members if row['pkgpath'] == package]
                descriptor = dict(bundle_root=str(f.original), tools={row['role']: dict(
                    provider=name, path=str(f.original / row['original_invocation']),
                    sha256=f.originals[row['original_member']]['sha256']) for row in rows})
                with self.assertRaises(SDKError):
                    _history({name: raw}, {name: dict(origin='installed-package')},
                             descriptor=descriptor, inspect_inputs=False)


if __name__ == '__main__':
    unittest.main()
