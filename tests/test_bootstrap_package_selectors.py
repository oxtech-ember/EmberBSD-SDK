# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Package installation selectors. All inputs and child commands are synthetic."""

import copy
import hashlib
from pathlib import Path
import unittest
from unittest.mock import patch

import test_bootstrap_producer
import test_bootstrap_recipe
from emberbsd_sdk import bootstrap_producer as producer
from emberbsd_sdk.errors import SDKError


class PackageSelectorTests(unittest.TestCase):
    def setUp(self):
        self.case = test_bootstrap_recipe.RecipeTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def test_full_plan_binds_both_exact_selected_package_paths(self):
        case = self.case
        values = producer.metadata_assignments(case.plan, case.descriptor, case.commands,
                                              case.descriptor['abi_flags'])
        for role, name in [('pkg_add', 'ADD'), ('pkg_create', 'CREATE')]:
            self.assertEqual(values['NATIVE_PKG_' + name + '_CMD'], case.commands[role][0])
            self.assertIn(role, case.plan['metadata']['native_tools'])
            self.assertIn(role, case.plan['required_roles'])
            self.assertIn(('NATIVE_PKG_' + name + '_CMD= ' + case.commands[role][0] + '\n').encode(),
                          Path(case.plan['metadata']['makeconf']['path']).read_bytes())
        with patch('subprocess.Popen', side_effect=AssertionError('no native child')):
            self.assertEqual(case.validate(), case.plan)
            self.assertEqual(case.validate(inspect_inputs=False), case.plan)

    def test_full_plan_cannot_omit_native_package_role(self):
        case = self.case
        for role in ('pkg_add', 'pkg_create'):
            with self.subTest(role=role):
                original = copy.deepcopy(case.plan['metadata']['native_tools'])
                case.plan['metadata']['native_tools'].remove(role)
                with patch('emberbsd_sdk.bootstrap_pilot.run_logged') as run:
                    with self.assertRaisesRegex(SDKError, 'explicit native pkg_add and pkg_create'):
                        case.invoke()
                    run.assert_not_called()
                self.assertFalse(case.work.exists())
                case.plan['metadata']['native_tools'] = original

    def test_full_plan_cannot_omit_required_package_role(self):
        case = self.case
        for role in ('pkg_add', 'pkg_create'):
            with self.subTest(role=role):
                case.plan['required_roles'].remove(role)
                with self.assertRaisesRegex(SDKError, 'tool roles are incomplete'):
                    case.validate()
                case.plan['required_roles'].append(role)
                case.plan['required_roles'].sort()

    def test_full_plan_requires_native_package_payloads(self):
        case = self.case
        for role in ('pkg_add', 'pkg_create'):
            with self.subTest(role=role):
                record = case.descriptor['tools'][role]
                record['kind'] = 'script'
                with self.assertRaisesRegex(SDKError, 'explicit native pkg_add and pkg_create'):
                    case.validate()
                record['kind'] = 'native'

    def test_missing_selected_command_or_omitted_tail_is_refused(self):
        case = self.case
        for role in ('pkg_add', 'pkg_create'):
            original = case.commands[role]
            for change in ('missing', 'tail'):
                with self.subTest(role=role, change=change):
                    if change == 'missing':
                        del case.commands[role]
                    else:
                        case.commands[role] = (*original, '--required-tail')
                    with self.assertRaises(SDKError):
                        producer.metadata_makeconf(case.plan, case.descriptor, case.commands,
                                                   case.descriptor['abi_flags'])
                    case.commands[role] = original

    def test_caller_cannot_replace_selected_path_in_makeconf(self):
        case = self.case
        reference = case.plan['metadata']['makeconf']
        path = Path(reference['path'])
        original = path.read_bytes()
        for role in ('pkg_add', 'pkg_create'):
            with self.subTest(role=role):
                changed = original.replace(case.commands[role][0].encode(), b'/unselected/pkg-command')
                path.write_bytes(changed)
                reference['sha256'] = hashlib.sha256(changed).hexdigest()
                case.descriptor['make_binding']['makeconf'] = copy.deepcopy(reference)
                with self.assertRaisesRegex(SDKError, 'MAKECONF differs'):
                    case.validate()
        path.write_bytes(original)

    def test_metadata_only_retains_existing_role_roster_and_bytes(self):
        metadata = test_bootstrap_producer.MetadataTests()
        metadata.setUp()
        self.addCleanup(metadata.doCleanups)
        arguments = (metadata.plan, metadata.descriptor, metadata.commands, metadata.descriptor['abi_flags'])
        original = producer.metadata_makeconf(*arguments)
        self.assertNotIn(b'NATIVE_PKG_ADD_CMD', original)
        self.assertNotIn(b'NATIVE_PKG_CREATE_CMD', original)
        for role in ('pkg_add', 'pkg_create'):
            self.assertNotIn(role, metadata.plan['metadata']['native_tools'])
            self.assertNotIn(role, metadata.plan['required_roles'])
            metadata.commands[role] = ('/synthetic/unused/' + role,)
        self.assertEqual(producer.metadata_makeconf(*arguments), original)
        self.assertEqual(metadata.validate(), metadata.plan)


if __name__ == '__main__':
    unittest.main()
