# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Real-filesystem workspace contracts. No payload qualification."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from unittest.mock import patch

from test_core import TemporaryCase, SDK_SOURCE
from emberbsd_sdk import workspace as w
from emberbsd_sdk.errors import SDKError


def snapshot(root):
    root = Path(root)
    if not root.exists() and not root.is_symlink():
        return None
    def row(path):
        info = path.lstat()
        content = os.readlink(path) if path.is_symlink() else path.read_bytes() if path.is_file() else None
        return (str(path.relative_to(root)), info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, content)
    paths = [root]
    if not root.is_symlink():
        for directory, dirs, files in os.walk(root, followlinks=False):
            paths.extend(Path(directory) / name for name in dirs + files)
    return [row(p) for p in sorted(set(paths))]


class WorkspaceTests(TemporaryCase):
    def legacy(self, name='source'):
        root = self.root / name; root.mkdir()
        for child in w.REQUIRED:
            (root / child).mkdir()
        (root / 'sysroot/empty').write_bytes(b'')
        (root / 'packages/payload').write_bytes(b'payload\0\xff')
        (root / 'obj/payload-link').symlink_to('../packages/payload')
        return root

    def cli(self, kind, *args):
        return subprocess.run([sys.executable, '-B', str(SDK_SOURCE / 'scripts' / (kind + '-prebuild-sdk.py')), *map(str, args)], cwd=self.root, capture_output=True)

    def test_create_and_read_only_inspect(self):
        root = self.root / 'sdk'; result = w.create(root, target='aarch64')
        self.assertEqual(result['structural_status'], 'managed')
        self.assertEqual(result['qualification'], 'not-assessed')
        self.assertEqual(json.loads((root / w.MANIFEST_NAME).read_text()), w.target_manifest('aarch64'))
        self.assertTrue(all(not list((root / name).iterdir()) for name in w.DIRECTORIES))
        before = snapshot(root)
        self.assertEqual(w.inspect_layout(root), result)
        with self.assertRaises(SDKError): w.create(root, target='aarch64')
        self.assertEqual(snapshot(root), before)

    def test_legacy_adoption_idempotent(self):
        source = self.legacy(); root = self.root / 'sdk'; before = snapshot(source)
        result = w.create(root, adopt=source)
        self.assertEqual(result['structural_status'], 'adoptable-existing')
        self.assertIsNone(result['manifest']); self.assertIsNone(result['directories']['receipts'])
        alias = snapshot(root)
        self.assertEqual(w.create(root, adopt=source), result)
        self.assertEqual(snapshot(root), alias); self.assertEqual(snapshot(source), before)

    def test_managed_adoption_relative_alias(self):
        source = self.root / 'source'; w.create(source, target='aarch64')
        root = self.root / 'sdk'; root.symlink_to('source')
        before, alias = snapshot(source), snapshot(root)
        self.assertEqual(w.create(root, adopt=source)['structural_status'], 'managed')
        self.assertEqual(snapshot(source), before); self.assertEqual(snapshot(root), alias)

    def test_receipts_missing_or_linked(self):
        source = self.root / 'managed'; w.create(source, target='aarch64'); (source / 'receipts').rmdir()
        with self.assertRaises(SDKError): w.inspect_layout(source)
        other = self.legacy(); (other / 'receipts').symlink_to(self.root)
        with self.assertRaises(SDKError): w.inspect_layout(other)

    def test_dangling_root_and_linked_adoption_source(self):
        link = self.root / 'link'; link.symlink_to('missing')
        with self.assertRaises(SDKError): w.inspect_layout(link)
        with self.assertRaises(SDKError): w.create(self.root / 'sdk', adopt=link)
        self.legacy('missing')
        with self.assertRaises(SDKError): w.create(self.root / 'sdk', adopt=link)

    def test_alias_to_other_workspace(self):
        a, b = self.legacy('a'), self.legacy('b')
        root = self.root / 'sdk'; root.symlink_to(a); before = snapshot(root)
        with self.assertRaises(SDKError): w.create(root, adopt=b)
        self.assertEqual(snapshot(root), before)

    def test_symlink_collision_race(self):
        source = self.legacy(); root = self.root / 'sdk'; before = snapshot(source)
        original = os.symlink
        def collide(src, dst):
            Path(dst).write_text('racing file'); original(src, dst)
        with patch.object(w.os, 'symlink', collide), self.assertRaises(SDKError):
            w.create(root, adopt=source)
        self.assertEqual(root.read_text(), 'racing file'); self.assertEqual(snapshot(source), before)

    def test_physical_root_is_not_own_alias(self):
        source = self.legacy(); before = snapshot(source)
        with self.assertRaises(SDKError): w.create(source, adopt=source)
        self.assertEqual(snapshot(source), before)

    def test_mkdir_collision_race(self):
        root = self.root / 'sdk'; original = os.mkdir
        def collide(path, *args, **kwargs):
            if Path(path) == root: root.write_text('racing file')
            return original(path, *args, **kwargs)
        with patch.object(w.os, 'mkdir', collide), self.assertRaises(SDKError): w.create(root, target='aarch64')
        self.assertEqual(root.read_text(), 'racing file')

    def test_unsupported_symlink_no_fallback(self):
        source = self.legacy(); before = snapshot(source)
        with patch.object(w.os, 'symlink', side_effect=NotImplementedError('unavailable')), self.assertRaisesRegex(SDKError, 'unavailable'):
            w.create(self.root / 'sdk', adopt=source)
        self.assertEqual(snapshot(source), before); self.assertFalse((self.root / 'sdk').exists())

    def test_generic_paths_accept_spaces_unicode_and_refuse_controls(self):
        for name in ('', 'tab\tname', 'newline\nname', 'nul\0name'):
            with self.assertRaises(SDKError): w.create('' if not name else self.root / name, target='aarch64')
        parent = self.root / 'space пример'; parent.mkdir()
        link = self.root / 'parent'; link.symlink_to(parent)
        result = w.create(link / 'sdk', target='aarch64')
        self.assertEqual(result['canonical_root'], str(parent / 'sdk'))

    def test_cli_argv_json_usage_and_error(self):
        root = self.root / 'sdk;$(literal)`quote` пример'
        result = self.cli('create', root, '--target', 'aarch64'); self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['logical_root'], str(root))
        result = self.cli('inspect', root); self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['qualification'], 'not-assessed')
        result = self.cli('create', root, '--target', 'aarch64'); self.assertEqual(result.returncode, 1); self.assertIn(b'error:', result.stderr)
        for kind, args in (('inspect', ()), ('create', (root, '--unknown', root))):
            result = self.cli(kind, *args); self.assertEqual(result.returncode, 2); self.assertIn(b'usage:', result.stderr.lower())
        self.assertEqual(list(self.root.iterdir()), [root])


def collision_case(kind):
    def test(self):
        source = self.legacy(); root = self.root / 'sdk'
        if kind == 'file': root.write_text('keep')
        elif kind == 'directory': root.mkdir()
        else: root.symlink_to(source if kind == 'symlink' else self.root / 'missing')
        before, payload = snapshot(root), snapshot(source)
        with self.assertRaises(SDKError): w.create(root, target='aarch64')
        if kind != 'symlink':
            with self.assertRaises(SDKError): w.create(root, adopt=source)
        self.assertEqual(snapshot(root), before); self.assertEqual(snapshot(source), payload)
    return test


def child_case(name, kind):
    def test(self):
        source = self.legacy(); child = source / name; shutil.rmtree(child)
        if kind == 'file': child.write_text('keep')
        elif kind in ('symlink', 'dangling'): child.symlink_to(self.root if kind == 'symlink' else self.root / 'missing')
        before = snapshot(source)
        with self.assertRaises(SDKError): w.inspect_layout(source)
        with self.assertRaises(SDKError): w.create(self.root / 'sdk', adopt=source)
        self.assertFalse((self.root / 'sdk').exists()); self.assertEqual(snapshot(source), before)
    return test


def malformed_case(text):
    def test(self):
        source = self.root / 'source'; w.create(source, target='aarch64')
        (source / w.MANIFEST_NAME).write_bytes(text)
        before = snapshot(source)
        with self.assertRaises(SDKError): w.inspect_layout(source)
        with self.assertRaises(SDKError): w.create(self.root / 'sdk', adopt=source)
        self.assertEqual(snapshot(source), before)
    return test


def manifest_type_case(kind):
    def test(self):
        source = self.legacy(); manifest = source / w.MANIFEST_NAME
        if kind == 'directory': manifest.mkdir()
        else:
            if kind == 'symlink': (self.root / 'metadata').write_text(json.dumps(w.MANIFEST))
            manifest.symlink_to(self.root / 'metadata')
        before = snapshot(source)
        with self.assertRaises(SDKError): w.inspect_layout(source)
        self.assertEqual(snapshot(source), before)
    return test


def mkdir_failure_case(failure):
    def test(self):
        root = self.root / 'sdk'; original = os.mkdir; count = 0
        def fail(path, *args, **kwargs):
            nonlocal count
            if str(path).startswith(str(root) + '/'):
                count += 1
                if count == failure:
                    (root / 'foreign').write_text('do not remove'); raise OSError('injected fault')
            return original(path, *args, **kwargs)
        with patch.object(w.os, 'mkdir', fail), self.assertRaises(SDKError): w.create(root, target='aarch64')
        self.assertEqual((root / 'foreign').read_text(), 'do not remove')
        self.assertTrue((root / w.INCOMPLETE_NAME).is_file())
        with self.assertRaises(SDKError): w.inspect_layout(root)
        with self.assertRaises(SDKError): w.create(self.root / 'alias', adopt=root)
    return test


def late_failure_case(phase):
    def test(self):
        root = self.root / 'sdk'
        if phase == 'manifest':
            context = patch.object(w, 'write_json', side_effect=OSError('injected fault'))
        else:
            original = os.unlink
            def fail(path, *args, **kwargs):
                if str(path) == str(root / w.INCOMPLETE_NAME): raise OSError('injected fault')
                return original(path, *args, **kwargs)
            context = patch.object(w.os, 'unlink', fail)
        with context, self.assertRaises(SDKError): w.create(root, target='aarch64')
        self.assertTrue((root / w.INCOMPLETE_NAME).is_file())
        with self.assertRaises(SDKError): w.inspect_layout(root)
    return test


for _kind in ('file', 'directory', 'symlink', 'dangling'):
    setattr(WorkspaceTests, 'test_collision_' + _kind, collision_case(_kind))
for _name in w.REQUIRED:
    for _kind in ('missing', 'file', 'symlink', 'dangling'):
        setattr(WorkspaceTests, f'test_child_{_name}_{_kind}', child_case(_name, _kind))
_malformed = dict(empty=b'', broken=b'{', null=b'null', array=b'[]', oversized=b' ' * (w.MAX_MANIFEST + 1), deep=b'[' * 10 + b'0' + b']' * 10,
                  duplicate=json.dumps(w.MANIFEST).replace('"version": 1', '"version": 0, "version": 1').encode(), invalid_utf8=b'"\xff"')
for _name, _change in (('version', {'version': 2}), ('float', {'version': 1.0}), ('bool', {'version': True}), ('unknown', {'qualified': True}), ('directory', {'directories': ['other']})):
    _malformed[_name] = json.dumps(w.MANIFEST | _change).encode()
for _name, _text in _malformed.items():
    setattr(WorkspaceTests, 'test_manifest_' + _name, malformed_case(_text))
for _kind in ('directory', 'symlink', 'dangling'):
    setattr(WorkspaceTests, 'test_manifest_type_' + _kind, manifest_type_case(_kind))
for _failure in (1, 2, 3, 4):
    setattr(WorkspaceTests, 'test_mkdir_failure_' + str(_failure), mkdir_failure_case(_failure))
for _phase in ('manifest', 'marker'):
    setattr(WorkspaceTests, 'test_late_failure_' + _phase, late_failure_case(_phase))
