# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Resolve symbolic links before parent traversal in every public path caller."""

import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch
from test_core import TemporaryCase, SDK_SOURCE
from test_workspace import snapshot
from emberbsd_sdk import inventory, jsonio, paths, workspace
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.sysroot_builder import SysrootBuilder


class PathIdentityTests(TemporaryCase):
    def setUp(self):
        super().setUp()
        self.base, self.other = self.root / 'base', self.root / 'other'
        workspace.create(self.base, target='aarch64'); workspace.create(self.other, target='aarch64')
        (self.other / 'sub').mkdir()
        (self.base / 'alias').symlink_to(self.other / 'sub')
        self.selected = str(self.base / 'alias') + '/..'

    def test_inspect_uses_selected_directory_identity(self):
        before = snapshot(self.base), snapshot(self.other)
        result = workspace.inspect_layout(self.selected)
        self.assertTrue(os.path.samefile(self.selected, result['canonical_root']))
        self.assertEqual(result['canonical_root'], str(self.other))
        self.assertEqual(result['logical_root'], self.selected)
        self.assertEqual((snapshot(self.base), snapshot(self.other)), before)

    def test_create_new_uses_existing_parent_traversal(self):
        before = snapshot(self.base)
        result = workspace.create(self.selected + '/new-sdk', target='aarch64')
        self.assertEqual(result['canonical_root'], str(self.other / 'new-sdk'))
        self.assertFalse((self.base / 'new-sdk').exists())
        self.assertEqual(snapshot(self.base), before)

    def test_create_existing_refuses_without_touching_either_tree(self):
        before = snapshot(self.base), snapshot(self.other)
        with self.assertRaises(SDKError):
            workspace.create(self.selected, target='aarch64')
        self.assertEqual((snapshot(self.base), snapshot(self.other)), before)

    def test_adopt_destination_uses_existing_parent_traversal(self):
        source = self.root / 'source'; workspace.create(source, target='aarch64')
        before = snapshot(self.base), snapshot(source)
        result = workspace.create(self.selected + '/adopted', adopt=source)
        self.assertEqual(result['canonical_root'], str(source))
        self.assertTrue((self.other / 'adopted').is_symlink())
        self.assertFalse((self.base / 'adopted').exists())
        self.assertEqual((snapshot(self.base), snapshot(source)), before)

    def test_adopt_source_uses_selected_directory_identity(self):
        before = snapshot(self.base), snapshot(self.other)
        result = workspace.create(self.root / 'adopted', adopt=self.selected)
        self.assertEqual(result['canonical_root'], str(self.other))
        self.assertTrue(os.path.samefile(self.root / 'adopted', self.other))
        self.assertEqual((snapshot(self.base), snapshot(self.other)), before)

    def test_layout_cli_preserves_literal_path_identity(self):
        before = snapshot(self.base)
        for command, selected in (('inspect', self.selected), ('create', self.selected + '/cli-sdk')):
            row = subprocess.run([sys.executable, '-B', '-I', '-S', str(SDK_SOURCE / 'scripts' / (command + '-prebuild-sdk.py')), selected, *(['--target', 'aarch64'] if command == 'create' else [])], cwd='/', capture_output=True)
            self.assertEqual(row.returncode, 0, row.stderr)
            self.assertTrue(os.path.samefile(selected, json.loads(row.stdout)['canonical_root']))
        self.assertEqual(snapshot(self.base), before)

    def test_builder_accepts_selected_workspace_root_traversal(self):
        builder = SysrootBuilder({})
        builder.logical_sdk = self.selected
        builder.paths = {'sdk': str(self.other)}
        before = snapshot(self.base), snapshot(self.other)
        output = builder.output_path(self.selected + '/stages/new', str(self.other))
        self.assertEqual(output, str(self.other / 'stages/new'))
        (self.other / 'stages/existing').mkdir(parents=True)
        output = builder.output_path(self.selected + '/stages/existing', str(self.other))
        self.assertTrue(os.path.samefile(output, self.other / 'stages/existing'))
        self.assertEqual(snapshot(self.base), before[0])

    def test_builder_refuses_output_alias_before_parent_traversal(self):
        builder = SysrootBuilder({})
        builder.logical_sdk = str(self.other)
        builder.paths = {'sdk': str(self.other)}
        (self.base / 'sub').mkdir()
        (self.other / 'redirect').symlink_to(self.base / 'sub')
        before = snapshot(self.base), snapshot(self.other)
        with self.assertRaisesRegex(SDKError, 'not a real directory'):
            builder.output_path(str(self.other / 'redirect') + '/../new', str(self.other))
        self.assertEqual((snapshot(self.base), snapshot(self.other)), before)

    def test_builder_refuses_internal_alias_before_parent_traversal(self):
        builder = SysrootBuilder({})
        builder.logical_sdk = str(self.other)
        builder.paths = {'sdk': str(self.other)}
        (self.other / 'redirect').symlink_to(self.other / 'sub')
        before = snapshot(self.base), snapshot(self.other)
        with self.assertRaisesRegex(SDKError, 'not a real directory'):
            builder.output_path(str(self.other / 'redirect') + '/../new', str(self.other))
        self.assertEqual((snapshot(self.base), snapshot(self.other)), before)

    def test_inventory_refuses_alias_root_before_normalization(self):
        with self.assertRaisesRegex(SDKError, 'real ancestors'):
            inventory.snapshot_tree(self.selected)

    def test_atomic_json_staging_uses_actual_parent_directory(self):
        before = snapshot(self.base)
        original = os.replace
        def replace(source, target):
            self.assertTrue(os.path.samefile(Path(source).parent, self.other))
            original(source, target)
        with patch.object(jsonio.os, 'replace', replace):
            jsonio.write_json(self.selected + '/output.json', {'value': 1}, exclusive=False)
        self.assertEqual(jsonio.read_json(self.other / 'output.json'), {'value': 1})
        self.assertEqual(snapshot(self.base), before)

    def test_new_path_refuses_traversal_through_missing_ancestor(self):
        with self.assertRaises(SDKError):
            paths.canonical_new(str(self.base / 'missing') + '/../new')
