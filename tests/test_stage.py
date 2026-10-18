# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Real staging trees exercise the destructive package ownership boundary."""

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.inventory import snapshot_tree
from emberbsd_sdk.stage import partition_stage


class StageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='sdk-stage-')
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name).resolve()
        self.root = self.parent / 'stage space Б'
        self.root.mkdir()
        for name, content in [('lib/runtime.so.1', b'runtime'),
                              ('bin/generator', b'generator'),
                              ('include/api.h', b'header')]:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        (self.root / 'lib/runtime.so').symlink_to('runtime.so.1')
        (self.root / 'share/empty/deep').mkdir(parents=True)
        self.parts = {'runtime': ['lib/runtime.so.1'],
                      'devel': ['lib/runtime.so', 'bin/generator', 'include/api.h']}
        self.full = [p for paths in self.parts.values() for p in paths]
        self.deps = {'devel': ['runtime']}

    def select(self, **kwargs):
        args = dict(stage=self.root, full=self.full, partitions=self.parts,
                    selected='runtime', dependencies=self.deps)
        args.update(kwargs)
        return partition_stage(**args)

    def refusal(self, **kwargs):
        before = snapshot_tree(self.root)
        with self.assertRaises((SDKError, OSError)):
            self.select(**kwargs)
        self.assertEqual(snapshot_tree(self.root), before)

    def test_runtime_removes_generators_and_keeps_explicit_empty_ancestors(self):
        result = self.select(keep_empty=['share/empty/deep'])
        self.assertEqual(result['kept_files'], ['lib/runtime.so.1'])
        self.assertEqual((self.root / 'lib/runtime.so.1').read_bytes(), b'runtime')
        self.assertFalse((self.root / 'bin').exists())
        self.assertTrue((self.root / 'share/empty/deep').is_dir())

    def test_sdk_link_resolves_when_normal_runtime_dependency_is_installed(self):
        self.select(selected='devel')
        self.assertEqual(os.readlink(self.root / 'lib/runtime.so'), 'runtime.so.1')
        self.assertFalse((self.root / 'lib/runtime.so.1').exists())
        (self.root / 'lib/runtime.so.1').write_bytes(b'runtime')
        self.assertEqual((self.root / 'lib/runtime.so').read_bytes(), b'runtime')

    def test_invalid_ownership_and_unknown_dependency_leave_stage_unchanged(self):
        for options in [dict(full=self.full + ['absent']),
                        dict(full=self.full[:-1]),
                        dict(full=self.full + [self.full[0]]),
                        dict(partitions={'runtime': self.full, 'devel': self.full}),
                        dict(partitions={'runtime': self.full[:-1]}),
                        dict(dependencies={'devel': ['missing']}),
                        dict(dependencies={'missing': ['runtime']}),
                        dict(full='abc'), dict(full=b'abc'),
                        dict(partitions=['runtime', 'devel']),
                        dict(partitions={'runtime': 'abc', 'devel': []}),
                        dict(dependencies=['runtime']),
                        dict(dependencies={'devel': 'runtime'}),
                        dict(keep_empty='share/empty/deep'),
                        dict(dependencies={}), dict(selected='unknown'),
                        dict(keep_empty=['not-present'])]:
            with self.subTest(options=options):
                self.refusal(**options)

    def test_unsafe_manifest_names_leave_stage_unchanged(self):
        for name in ['/absolute', '../escape', 'a/../b', './a', 'a//b', 'a/', '', 'bad\nname']:
            with self.subTest(name=name):
                self.refusal(full=self.full + [name])

    def test_parent_alias_cannot_redirect_pruning(self):
        alias = self.parent / 'alias'
        alias.symlink_to(self.root)
        self.refusal(stage=alias)

    def test_unlisted_payload_and_missing_listed_payload_leave_stage_unchanged(self):
        extra = self.root / 'unexpected'
        extra.write_bytes(b'do not delete')
        self.refusal()
        extra.unlink()
        (self.root / 'include/api.h').unlink()
        self.refusal()

    def test_runtime_cannot_link_through_sdk_back_to_runtime(self):
        (self.root / 'lib/runtime-alias').symlink_to('runtime.so')
        self.full.append('lib/runtime-alias')
        self.parts['runtime'].append('lib/runtime-alias')
        self.refusal()

    def test_transitive_dependency_permits_all_intermediate_link_owners(self):
        (self.root / 'bin/another').symlink_to('../lib/runtime.so')
        self.full.append('bin/another')
        self.parts['tools'] = ['bin/another']
        self.select(selected='tools', dependencies={'tools': ['devel'], 'devel': ['runtime']})
        self.assertEqual(os.readlink(self.root / 'bin/another'), '../lib/runtime.so')

    def test_sdk_versioned_directory_link_retains_metadata_and_links_through_it(self):
        version = self.root / 'lib/metadata/78.3'
        version.mkdir(parents=True)
        (version / 'Makefile.inc').write_bytes(b'build metadata')
        (version.parent / 'current').symlink_to('78.3')
        (version.parent / 'Makefile.inc').symlink_to('current/Makefile.inc')
        names = ['lib/metadata/78.3/Makefile.inc', 'lib/metadata/current',
                 'lib/metadata/Makefile.inc']
        self.full.extend(names)
        self.parts['devel'].extend(names)
        self.select(selected='devel')
        self.assertEqual((version.parent / 'Makefile.inc').read_bytes(), b'build metadata')

    def test_directory_link_cannot_expose_undeclared_role(self):
        (self.root / 'directory-link').symlink_to('include')
        self.full.append('directory-link')
        self.parts['runtime'].append('directory-link')
        self.refusal()

    def test_empty_directory_link_refuses_without_mutation(self):
        (self.root / 'directory-link').symlink_to('share/empty')
        self.full.append('directory-link')
        self.parts['runtime'].append('directory-link')
        self.refusal()

    def test_external_and_cyclic_links_refuse_without_deleting_payload(self):
        link = self.root / 'lib/runtime.so'
        for target in ['/etc/passwd', '../../outside', 'runtime.so']:
            with self.subTest(target=target):
                link.unlink()
                link.symlink_to(target)
                # Invalid links cannot be inventoried; verify the real files
                # and link directly after refusal instead of hiding that error.
                with self.assertRaises((SDKError, OSError)):
                    self.select()
                self.assertEqual(os.readlink(link), target)
                self.assertEqual((self.root / 'bin/generator').read_bytes(), b'generator')
                self.assertEqual((self.root / 'lib/runtime.so.1').read_bytes(), b'runtime')

    def test_real_byte_drift_between_snapshots_prevents_any_pruning(self):
        calls = 0
        def observe(root):
            nonlocal calls
            calls += 1
            if calls == 2:
                (self.root / 'include/api.h').write_bytes(b'changed')
            return snapshot_tree(root)
        with patch('emberbsd_sdk.stage.snapshot_tree', side_effect=observe):
            with self.assertRaisesRegex(SDKError, 'changed'):
                self.select()
        self.assertEqual((self.root / 'bin/generator').read_bytes(), b'generator')
        self.assertEqual((self.root / 'include/api.h').read_bytes(), b'changed')
        self.assertTrue((self.root / 'lib/runtime.so').is_symlink())


if __name__ == '__main__':
    unittest.main()
