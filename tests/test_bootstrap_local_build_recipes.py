# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Developed with AI assistance. Synthetic source trees are never compiled.

import copy
import json
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

import test_bootstrap_local_recipes as fixtures
from emberbsd_sdk import bootstrap_producer as producer, bootstrap_recipe as recipe
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.inventory import snapshot_tree


BUILD_LAYOUTS = {
    'lang/nawk': ('Makefile', 'files/Makefile', 'files/awk.h', 'files/awkgram.tab.c',
                   'files/awkgram.tab.h', 'files/main.c', 'files/proctab.c', 'files/LICENSE'),
    'devel/bmake': ('Makefile', 'files/configure', 'files/make-bootstrap.sh.in',
                    'files/main.c', 'files/job.c'),
    'pkgtools/bootstrap-mk-files': ('Makefile', 'files/sys.mk', 'files/bsd.prog.mk',
                                   'files/mods/bsd.own.mk.in'),
    'pkgtools/pkg_install': ('Makefile', 'files/configure', 'files/Makefile.in',
                            'files/lib/version.h', 'files/add/main.c', 'files/create/main.c',
                            'files/admin/main.c', 'files/info/main.c'),
    'archivers/bzip2': ('Makefile', 'files/Makefile', 'files/bzip2.c', 'files/bzlib.c'),
    'archivers/libarchive': ('Makefile', 'Makefile.common', 'files/configure', 'files/Makefile.in',
                           'files/libarchive/archive_read.c', 'files/libarchive/archive.h'),
    'archivers/pax': ('Makefile', 'files/configure', 'files/Makefile.in', 'files/pax.c'),
    'devel/nbpatch': ('Makefile', 'files/Makefile', 'files/patch.c', 'files/pch.c'),
    'net/fetch': ('Makefile', 'files/configure', 'files/Makefile.in', 'files/fetch.c'),
    'net/libfetch': ('Makefile', 'files/Makefile', 'files/common.c', 'files/fetch.c', 'files/fetch.h'),
    'shells/mksh': ('Makefile', 'files/Build.sh', 'files/main.c', 'files/sh.h'),
    'sysutils/bsdinstall': ('Makefile', 'DESCR', 'PLIST', 'files/Makefile',
                          'files/bsdinstall.c', 'files/setmode.c', 'files/bsdinstall.1'),
}


class LocalBuildRecipeTests(unittest.TestCase):
    def setUp(self):
        self.local = fixtures.LocalRecipeTests()
        self.local.setUp()
        self.addCleanup(self.local.doCleanups)
        self.case, self.plan, self.f, self.export = (
            self.local.case, self.local.plan, self.local.f, self.local.export)
        guard = patch.dict(fixtures.LOCAL_LAYOUTS, BUILD_LAYOUTS)
        guard.start()
        self.addCleanup(guard.stop)
        for name, files in BUILD_LAYOUTS.items():
            for relative in (*files, 'files/extra-source.c'):
                path = self.export / name / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('/* Synthetic pinned source; never compiled. */\n')
            self.plan['recipes'][name] = dict(directory=name, patches=[], distfiles=[],
                distfile_metadata=self.local.observations(name), dependencies=[])
        self.plan['recipes']['lang/gcc16']['dependencies'] += list(BUILD_LAYOUTS)
        for name in ('patch-expr.c', 'patch-lalloc.c'):
            path = self.export / 'shells/mksh/patches' / name
            path.parent.mkdir(exist_ok=True)
            path.write_text('Synthetic common patch, not applied by these controls.\n')
        self.plan['recipes']['shells/mksh']['patches'] = [
            'patches/patch-expr.c', 'patches/patch-lalloc.c']
        self.local.refresh_export()

    def test_sixteen_fixed_local_roots_preserve_real_archive_mapping(self):
        with patch('subprocess.Popen', side_effect=AssertionError('no native process')):
            self.assertEqual(self.case.validate(), self.plan)
            self.assertEqual(self.case.validate(inspect_inputs=False), self.plan)
            expected = {row['relative']: row['file'] for name in ('lang/gcc16', 'devel/gmp')
                        for row in self.plan['recipes'][name]['distfiles']}
            self.assertEqual(recipe.recipe_distfiles(self.plan), expected)
            self.case.work.mkdir()
            recipe.prepare_recipe_work(self.plan)
        copied = sorted(str(p.relative_to(self.case.work / 'distfiles'))
                        for p in (self.case.work / 'distfiles').rglob('*') if p.is_file())
        self.assertEqual(copied, sorted(expected))
        self.assertEqual(len(fixtures.LOCAL_LAYOUTS), 16)

    def test_nawk_generated_tables_cannot_be_omitted(self):
        for relative in ('files/awkgram.tab.c', 'files/awkgram.tab.h', 'files/proctab.c'):
            path = self.export / 'lang/nawk' / relative
            original = path.read_bytes()
            with self.subTest(source=relative):
                path.unlink()
                self.local.refresh_export()
                with self.assertRaisesRegex(SDKError, 'required regular source file'):
                    self.case.validate()
                path.write_bytes(original)
                self.local.refresh_export()

    def test_bsd_profile_preserves_legacy_policy_recognition(self):
        self.assertTrue(producer._recognized_profile(producer.BSDINSTALL_PROFILE_SHA256))
        self.assertTrue(producer._parallel_profile(producer.BSDINSTALL_PROFILE_SHA256))
        self.assertTrue(producer._native_provider_profile(producer.BSDINSTALL_PROFILE_SHA256))
        for digest in (producer.NATIVE_PROVIDER_PROFILE_SHA256,
                       producer.NO_DEFAULT_RPATH_PROFILE_SHA256,
                       producer.UNPRIVILEGED_PROFILE_SHA256):
            self.assertTrue(producer._native_provider_profile(digest))
        self.assertTrue(producer._recognized_profile(producer.FOUNDATION_PROFILE_SHA256))
        self.assertTrue(producer._parallel_profile(producer.FOUNDATION_PROFILE_SHA256))
        self.assertTrue(producer._native_provider_profile(producer.FOUNDATION_PROFILE_SHA256))
        self.assertFalse(producer._recognized_profile('0' * 64))

    def test_foundation_local_roots_need_their_explicit_layout_entries(self):
        for name in ('devel/bmake', 'pkgtools/bootstrap-mk-files', 'pkgtools/pkg_install', 'lang/nawk'):
            previous = dict(recipe._LOCAL_SOURCE_FILES)
            del previous[name]
            with self.subTest(recipe=name):
                with patch.dict(recipe._LOCAL_SOURCE_FILES, previous, clear=True):
                    with self.assertRaisesRegex(SDKError, 'recognized local-source'):
                        self.case.validate()

    def test_bsd_output_provider_is_reachable_without_a_dependency_cycle(self):
        plan = {'recipes': {
            'lang/gcc16': {'dependencies': ['sysutils/bsdinstall']},
            'sysutils/bsdinstall': {'dependencies': ['lang/llvm', 'shells/mksh', 'pkgtools/libnbcompat']},
            'lang/llvm': {'dependencies': ['pkgtools/libnbcompat']},
            'shells/mksh': {'dependencies': ['pkgtools/libnbcompat']},
            'pkgtools/libnbcompat': {'dependencies': []}}}
        order = recipe.recipe_order(plan)
        self.assertLess(order.index('lang/llvm'), order.index('sysutils/bsdinstall'))
        self.assertLess(order.index('shells/mksh'), order.index('sysutils/bsdinstall'))
        self.assertLess(order.index('sysutils/bsdinstall'), order.index('lang/gcc16'))
        plan['recipes']['pkgtools/libnbcompat']['dependencies'].append('sysutils/bsdinstall')
        with self.assertRaisesRegex(SDKError, 'dependency graph'):
            recipe.recipe_order(plan)

    def test_each_additional_root_requires_bound_exact_empty_observations(self):
        for name in BUILD_LAYOUTS:
            row = self.plan['recipes'][name]
            original = row['distfile_metadata']
            for case in ('missing', 'empty-log', 'wrong-recipe', 'distfile', 'patchfile', 'hash'):
                with self.subTest(recipe=name, case=case):
                    if case == 'missing':
                        row.pop('distfile_metadata')
                    elif case == 'empty-log':
                        row['distfile_metadata'] = self.f.raw(b'')
                    elif case == 'hash':
                        row['distfile_metadata'] = dict(original, sha256='0' * 64)
                    else:
                        changes = {'wrong-recipe': dict(PKGPATH='devel/gmp'),
                                   'distfile': dict(DISTFILES='archive.tar.xz'),
                                   'patchfile': dict(PATCHFILES='extra.diff')}[case]
                        values = {key: '' for key in producer.VARIABLES}
                        values.update(PKGPATH=name)
                        values.update(changes)
                        row['distfile_metadata'] = self.f.raw(''.join(
                            key + '=' + values[key] + '\n' for key in producer.VARIABLES).encode())
                    with self.assertRaises(SDKError):
                        self.case.validate()
                    row['distfile_metadata'] = original

    def test_additional_roots_keep_legacy_55_variable_observations(self):
        for name in BUILD_LAYOUTS:
            values = {key: '' for key in producer.LEGACY_VARIABLES}
            values['PKGPATH'] = name
            reference = self.f.raw(''.join(key + '=' + values[key] + '\n'
                for key in producer.LEGACY_VARIABLES).encode())
            self.plan['recipes'][name]['distfile_metadata'] = reference
            self.assertNotIn('BOOTSTRAP_DEPENDS', producer.metadata_values(reference))
        self.assertEqual(self.case.validate(), self.plan)

    def test_missing_or_symlinked_required_source_is_not_a_regular_local_layout(self):
        for name, relative in ((name, files[1]) for name, files in BUILD_LAYOUTS.items()):
            path = self.export / name / relative
            original = path.read_bytes()
            for symlink in (False, True):
                with self.subTest(recipe=name, symlink=symlink):
                    path.unlink()
                    if symlink:
                        target = self.export / name / BUILD_LAYOUTS[name][-1]
                        path.symlink_to(os.path.relpath(target, path.parent))
                    self.local.refresh_export()
                    with self.assertRaisesRegex(SDKError, 'required regular source file'):
                        self.case.validate()
                    if symlink:
                        path.unlink()
                    path.write_bytes(original)
                    self.local.refresh_export()

    def test_complete_new_subtree_cannot_omit_unlisted_source_bytes(self):
        for name in BUILD_LAYOUTS:
            with self.subTest(recipe=name):
                row = self.plan['recipes'][name]
                original = row['inventory']
                inventory = json.loads(Path(original['path']).read_text())
                del inventory['files/extra-source.c']
                row['inventory'] = self.f.document(inventory)
                with self.assertRaisesRegex(SDKError, 'differs from the pinned export'):
                    self.case.validate()
                row['inventory'] = original

    def test_mksh_requires_both_carried_common_patches(self):
        row = self.plan['recipes']['shells/mksh']
        original = list(row['patches'])
        for missing in original:
            with self.subTest(missing=missing):
                row['patches'] = [name for name in original if name != missing]
                with self.assertRaisesRegex(SDKError, 'omits common recipe patches'):
                    self.case.validate()
        row['patches'] = original
        self.assertEqual(self.case.validate(), self.plan)

    def test_similar_name_with_the_same_layout_remains_unrecognized(self):
        name = 'devel/nbpatch-extra'
        shutil.copytree(self.export / 'devel/nbpatch', self.export / name)
        row = copy.deepcopy(self.plan['recipes']['devel/nbpatch'])
        row.update(directory=name, inventory=self.f.document(snapshot_tree(str(self.export / name))),
                   distfile_metadata=self.local.observations(name))
        self.plan['recipes'][name] = row
        self.plan['recipes']['lang/gcc16']['dependencies'].append(name)
        self.local.refresh_export()
        with self.assertRaisesRegex(SDKError, 'recognized local-source'):
            self.case.validate()


if __name__ == '__main__':
    unittest.main()
