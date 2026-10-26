# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Live timestamp work-root admission. No compiler or native child runs."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from emberbsd_sdk.bootstrap_seed import _plan
from emberbsd_sdk.errors import SDKError


class AtimeAdmissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.manifest = {'runtime_cases': [{'filesystem': {'expect': {
            'file': {'kind': 'file', 'atime_ns': 0, 'mtime_ns': 1}}}}]}
        self.plan = dict(format='emberbsd-native-gcc16-producer-plan',
                         host=dict(os='Darwin', cpu='arm64'),
                         work_root=str(self.root/'work'), program={})

    def document(self, name, value):
        path = self.root/name
        raw = json.dumps(value).encode()
        path.write_bytes(raw)
        return dict(path=str(path), sha256=hashlib.sha256(raw).hexdigest())

    def check(self, *, live=True):
        self.plan['program']['manifest'] = self.document('manifest.json', self.manifest)
        reference = self.document('plan.json', self.plan)
        original = copy.deepcopy(self.plan)
        # Isolate the live dispatch policy after successful producer validation.
        # Canonical source and command reconstruction have their own tests.
        with patch('emberbsd_sdk.bootstrap_producer.validate_producer_plan',
                   side_effect=lambda plan, *args: plan) as validate:
            result = _plan(reference, {}, {}, {}, inspect_inputs=live)
        validate.assert_called_once()
        self.assertEqual(result, original)
        return result

    def protected(self):
        parent = self.root/'checks.noindex'
        parent.mkdir()
        self.plan['work_root'] = str(parent/'work')
        return parent

    def test_plain_atime_plan_refused_before_work_creation(self):
        with self.assertRaisesRegex(SDKError, 'atime_ns.*[.]noindex'):
            self.check()
        self.assertFalse(Path(self.plan['work_root']).exists())

    def test_real_noindex_ancestor_allows_fresh_and_postdrain_paths(self):
        parent = self.protected()
        nested = parent/'nested'
        nested.mkdir()
        self.plan['work_root'] = str(nested/'work')
        self.check()
        self.assertFalse(Path(self.plan['work_root']).exists())
        Path(self.plan['work_root']).mkdir()
        self.check()

    def test_suffix_and_work_leaf_are_not_protected_ancestors(self):
        for name in ('checks.noindex-other', 'checksnoindex'):
            parent = self.root/name
            parent.mkdir()
            self.plan['work_root'] = str(parent/'work')
            with self.assertRaises(SDKError):
                self.check()
        self.plan['work_root'] = str(self.root/'work.noindex')
        with self.assertRaises(SDKError):
            self.check()

    def test_symlink_named_noindex_does_not_protect_plain_target(self):
        parent = self.root/'alias.noindex'
        parent.symlink_to(self.root, target_is_directory=True)
        self.plan['work_root'] = str(parent/'work')
        with self.assertRaises(SDKError):
            self.check()

    def test_traversal_out_of_protected_directory_refused(self):
        parent = self.protected()
        self.plan['work_root'] = str(parent/'..'/'work')
        with self.assertRaises(SDKError):
            self.check()

    def test_missing_parent_and_symlink_work_root_refused(self):
        parent = self.protected()
        self.plan['work_root'] = str(parent/'missing'/'work')
        with self.assertRaises(SDKError):
            self.check()
        leaf = parent/'work'
        leaf.symlink_to(self.root, target_is_directory=True)
        self.plan['work_root'] = str(leaf)
        with self.assertRaises(SDKError):
            self.check()

    def test_retained_atime_plan_does_not_inspect_retired_work_root(self):
        self.plan['work_root'] = str(self.root/'absent'/'retired')
        with patch('emberbsd_sdk.bootstrap_seed.canonical_existing',
                   side_effect=AssertionError('retained path inspected')):
            self.check(live=False)

    def test_untimed_mtime_only_and_fixture_only_contracts_unchanged(self):
        for manifest in ({'runtime_cases': []}, {'runtime_cases': [{}]},
            {'runtime_cases': [{'filesystem': {'expect': {'file': {'mtime_ns': 1}}}}]},
            {'runtime_cases': [{'filesystem': {'fixtures': {'file': {'atime_ns': 0}}, 'expect': {}}}]}):
            self.manifest = manifest
            self.check()

    def test_non_darwin_contract_unchanged(self):
        self.plan['host']['os'] = 'NetBSD'
        self.check()


if __name__ == '__main__':
    unittest.main()
