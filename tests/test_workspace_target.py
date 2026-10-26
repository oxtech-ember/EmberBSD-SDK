# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD; AI-assisted target layout regression controls.
"""Declared workspace targets do not qualify payload or ABI."""

import json
import subprocess
import sys
from unittest.mock import patch
from test_core import TemporaryCase, SDK_SOURCE
from test_workspace import snapshot
from emberbsd_sdk import workspace
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.sysroot_builder import SysrootBuilder


class TargetWorkspaceTests(TemporaryCase):
    def legacy(self, version=None):
        root = self.root / ('legacy' + str(version))
        root.mkdir()
        for name in workspace.DIRECTORIES:
            (root / name).mkdir()
        (root / 'sysroot/payload').write_bytes(b'keep\0')
        if version == 1:
            value = dict(format='emberbsd-prebuild-sdk-layout', version=1,
                         scope='workspace-layout', directories=list(workspace.DIRECTORIES))
            (root / workspace.MANIFEST_NAME).write_text(json.dumps(value))
        return root

    def cli(self, name, *args):
        return subprocess.run([sys.executable, '-B', '-I', '-S',
            str(SDK_SOURCE / 'scripts' / (name + '-prebuild-sdk.py')), *map(str, args)],
            cwd=self.root, capture_output=True)

    def test_new_create_requires_target_without_writes(self):
        root = self.root / 'new'
        with self.assertRaisesRegex(SDKError, 'target'):
            workspace.create(root)
        self.assertFalse(root.exists())
        result = self.cli('create', root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'target', result.stderr)
        self.assertFalse(root.exists())

    def test_both_target_clis_and_misleading_names(self):
        for target, triplet in (('aarch64', 'aarch64--netbsd'), ('amd64', 'x86_64--netbsd')):
            root = self.root / ('not-' + target)
            created = self.cli('create', root, '--target', target)
            self.assertEqual(created.returncode, 0, created.stderr)
            record = json.loads(created.stdout)
            self.assertEqual((record['target'], record['triplet'], record['target_status']),
                             (target, triplet, 'declared'))
            self.assertEqual(record['qualification'], 'not-assessed')
            manifest = json.loads((root / workspace.MANIFEST_NAME).read_text())
            self.assertEqual((manifest['version'], manifest['target'], manifest['triplet']),
                             (2, target, triplet))
            before = snapshot(root)
            inspected = self.cli('inspect', root)
            self.assertEqual(inspected.returncode, 0, inspected.stderr)
            self.assertEqual(json.loads(inspected.stdout), record)
            self.assertEqual(snapshot(root), before)

    def test_known_adoption_checks_target_before_alias(self):
        source = self.root / 'source'
        workspace.create(source, target='aarch64')
        before = snapshot(source)
        alias = self.root / 'prebuild-sdk-amd64'
        with self.assertRaisesRegex(SDKError, 'target mismatch'):
            workspace.create(alias, adopt=source, target='amd64')
        self.assertFalse(alias.exists())
        result = workspace.create(alias, adopt=source, target='aarch64')
        alias_before = snapshot(alias)
        self.assertEqual(result['target'], 'aarch64')
        self.assertEqual(workspace.create(alias, adopt=source, target='aarch64'), result)
        self.assertEqual(snapshot(alias), alias_before)
        self.assertEqual(snapshot(source), before)

    def test_legacy_inspection_and_alias_do_not_bind_target(self):
        for version in (None, 1):
            source = self.legacy(version)
            before = snapshot(source)
            record = workspace.inspect_layout(source)
            self.assertIsNone(record['target'])
            self.assertIsNone(record['triplet'])
            self.assertEqual(record['target_status'], 'unknown')
            alias = self.root / ('alias' + str(version))
            with self.assertRaisesRegex(SDKError, 'unknown'):
                workspace.create(alias, adopt=source, target='aarch64')
            self.assertFalse(alias.exists())
            result = self.cli('create', alias, '--adopt', source)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIsNone(json.loads(result.stdout)['target'])
            self.assertEqual(snapshot(source), before)

    def test_invalid_target_values_do_not_create(self):
        for target in ('', 'x86_64', 'arm64', 'AARCH64', True, [], {}):
            root = self.root / 'new'
            with self.assertRaisesRegex(SDKError, 'target'):
                workspace.create(root, target=target)
            self.assertFalse(root.exists())

    def test_v2_manifest_rejects_malformed_target_and_triplet(self):
        root = self.root / 'source'
        workspace.create(root, target='amd64')
        path = root / workspace.MANIFEST_NAME
        value = json.loads(path.read_text())
        for changes in ({'target': 'x86_64'}, {'target': []}, {'triplet': 'aarch64--netbsd'},
                        {'triplet': None}, {'version': True}, {'qualified': True}):
            path.write_text(json.dumps(value | changes))
            before = snapshot(root)
            with self.assertRaises(SDKError):
                workspace.inspect_layout(root)
            self.assertEqual(snapshot(root), before)

    def test_builder_refuses_unknown_before_host_queries_or_writes(self):
        for version in (None, 1):
            root = self.legacy(version)
            before = snapshot(root)
            for family in ('gcc', 'clang'):
                builder = SysrootBuilder({'compiler-family': family, 'gcc-archive': '/unused/gcc.tgz',
                                         'sdk': str(root), 'target': 'aarch64', 'jobs': '1'})
                with patch.object(builder, 'find_command', side_effect=AssertionError('host query')):
                    with self.assertRaisesRegex(SDKError, 'unknown'):
                        builder.preflight()
            self.assertEqual(snapshot(root), before)

    def test_clang_builder_requires_common_source_after_target_before_queries(self):
        root = self.root / 'clang'
        workspace.create(root, target='aarch64')
        before = snapshot(root)
        builder = SysrootBuilder({'sdk': str(root), 'target': 'aarch64', 'jobs': '1'})
        with patch.object(builder, 'find_command', side_effect=AssertionError('host query')):
            with self.assertRaisesRegex(SDKError, '--llvm-source'):
                builder.preflight()
        self.assertEqual(snapshot(root), before)

    def test_builder_refuses_mismatch_through_alias_before_queries(self):
        root = self.root / 'canonical'
        workspace.create(root, target='aarch64')
        alias = self.root / 'prebuild-sdk-amd64'
        alias.symlink_to(root)
        before = snapshot(root), snapshot(alias)
        for family in ('gcc', 'clang'):
            builder = SysrootBuilder({'compiler-family': family, 'gcc-archive': '/unused/gcc.tgz',
                                     'sdk': str(alias), 'target': 'amd64', 'jobs': '1'})
            with patch.object(builder, 'find_command', side_effect=AssertionError('host query')):
                with self.assertRaisesRegex(SDKError, 'target mismatch'):
                    builder.preflight()
        self.assertEqual((snapshot(root), snapshot(alias)), before)
