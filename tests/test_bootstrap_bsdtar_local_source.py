# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Shared-source bsdtar contracts. Synthetic sources are never compiled."""

import copy
import json
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

import test_bootstrap_local_recipes as fixtures
from emberbsd_sdk import bootstrap_recipe as recipe
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.inventory import snapshot_tree


OWNER_FILES = ('Makefile', 'DESCR', 'PLIST')
# Follow archivers/bsdtar/Makefile and libarchive/Makefile.common. Do not use
# the production sentinel tuple to create a fixture that could omit a source.
SHARED_FILES = (
    'archivers/libarchive/Makefile.common',
    'archivers/libarchive/files/configure',
    'archivers/libarchive/files/Makefile.in',
    'archivers/libarchive/files/libarchive/archive.h',
    'archivers/libarchive/files/libarchive/archive_read.c',
    'archivers/libarchive/files/libarchive/archive_read_support_format_tar.c',
    'archivers/libarchive/files/tar/bsdtar.c',
    'archivers/libarchive/files/tar/bsdtar.h',
    'archivers/bzip2/files/Makefile',
    'archivers/bzip2/files/bzlib.c',
    'archivers/bzip2/files/bzlib.h',
)


class BsdtarLocalSourceTests(unittest.TestCase):
    def setUp(self):
        self.local = fixtures.LocalRecipeTests()
        self.local.setUp()
        self.addCleanup(self.local.doCleanups)
        self.case, self.plan, self.f, self.export = (
            self.local.case, self.local.plan, self.local.f, self.local.export)
        guard = patch.dict(fixtures.LOCAL_LAYOUTS, {'archivers/bsdtar': OWNER_FILES})
        guard.start()
        self.addCleanup(guard.stop)
        for relative in [*('archivers/bsdtar/' + name for name in OWNER_FILES),
                         *SHARED_FILES, 'archivers/libarchive/files/extra-source.c']:
            path = self.export / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('/* Synthetic source: ' + relative + ' */\n')
        self.plan['recipes']['archivers/bsdtar'] = dict(directory='archivers/bsdtar',
            patches=[], distfiles=[], dependencies=[],
            distfile_metadata=self.local.observations('archivers/bsdtar'))
        self.plan['recipes']['lang/gcc16']['dependencies'].append('archivers/bsdtar')
        self.local.refresh_export()

    def test_complete_local_contract_preserves_real_archive_mapping(self):
        with patch('subprocess.Popen', side_effect=AssertionError('no native process')):
            self.assertEqual(self.case.validate(), self.plan)
            self.assertEqual(self.case.validate(inspect_inputs=False), self.plan)
            expected = {row['relative']: row['file'] for name in ('lang/gcc16', 'devel/gmp')
                        for row in self.plan['recipes'][name]['distfiles']}
            with patch.object(recipe, '_inventory', wraps=recipe._inventory) as load:
                self.assertEqual(recipe.recipe_distfiles(self.plan), expected)
                load.assert_called_once_with(self.plan['ports_export']['inventory'])
        previous = dict(recipe._LOCAL_SOURCE_FILES)
        del previous['archivers/bsdtar']
        with patch.dict(recipe._LOCAL_SOURCE_FILES, previous, clear=True):
            with self.assertRaisesRegex(SDKError, 'recognized local-source'):
                self.case.validate()

    def test_every_owner_and_shared_sentinel_must_be_regular(self):
        sources = [*('archivers/bsdtar/' + name for name in OWNER_FILES), *SHARED_FILES]
        for relative in sources:
            path = self.export / relative
            original = path.read_bytes()
            for linked in (False, True):
                with self.subTest(source=relative, linked=linked):
                    try:
                        path.unlink()
                        if linked:
                            if relative.startswith('archivers/bsdtar/'):
                                target = path.with_name('DESCR' if path.name == 'Makefile' else 'Makefile')
                            else:
                                target = self.export / 'archivers/libarchive/files/extra-source.c'
                            path.symlink_to(os.path.relpath(target, path.parent))
                        self.local.refresh_export()
                        message = ('recipe Makefile is required'
                                   if relative == 'archivers/bsdtar/Makefile' and not linked
                                   else 'required regular')
                        with self.assertRaisesRegex(SDKError, message):
                            self.case.validate()
                    finally:
                        path.unlink(missing_ok=True)
                        path.write_bytes(original)
                        self.local.refresh_export()

    def test_shared_source_ancestors_must_be_real_directories(self):
        export = json.loads(Path(self.plan['ports_export']['inventory']['path']).read_text())
        row = self.plan['recipes']['archivers/bsdtar']
        inventory = json.loads(Path(row['inventory']['path']).read_text())
        # Isolate the layout rule as well as the full export byte-pin tests.
        parents = {relative.rsplit('/', 1)[0] for relative in SHARED_FILES}
        parents.update(('archivers', 'archivers/bzip2'))
        for name in sorted(parents):
            for replacement in (None, dict(kind='link', target='elsewhere', mode=0o777)):
                with self.subTest(parent=name, replacement=replacement):
                    bad = copy.deepcopy(export)
                    if replacement is None:
                        del bad[name]
                    else:
                        bad[name] = replacement
                    with self.assertRaisesRegex(SDKError, 'real ancestor directory'):
                        recipe._local_source_distfiles('archivers/bsdtar', row, inventory,
                                                       export_inventory=bad)
        with self.assertRaisesRegex(SDKError, 'complete source export inventory'):
            recipe._local_source_distfiles('archivers/bsdtar', row, inventory)

    def test_changed_shared_bytes_fail_against_the_original_complete_export(self):
        for relative in (*SHARED_FILES, 'archivers/libarchive/files/extra-source.c'):
            with self.subTest(source=relative):
                path = self.export / relative
                original = path.read_bytes()
                try:
                    path.write_bytes(original + b'changed after export\n')
                    with self.assertRaisesRegex(SDKError, 'prepared canonical export changed'):
                        self.case.validate()
                finally:
                    path.write_bytes(original)
        self.assertEqual(self.case.validate(), self.plan)

    def test_partial_owner_inventory_cannot_replace_the_complete_recipe(self):
        row = self.plan['recipes']['archivers/bsdtar']
        inventory = json.loads(Path(row['inventory']['path']).read_text())
        del inventory['DESCR']
        row['inventory'] = self.f.document(inventory)
        with self.assertRaisesRegex(SDKError, 'differs from the pinned export'):
            self.case.validate()

    def test_shared_inventory_cannot_be_replaced_without_its_export_proof(self):
        reference = self.plan['ports_export']['inventory']
        inventory = json.loads(Path(reference['path']).read_text())
        del inventory['archivers/libarchive/files/extra-source.c']
        changed = self.f.document(inventory)
        self.plan['ports_export']['inventory'] = changed
        self.plan['metadata']['export']['inventory'] = changed
        with self.assertRaises(SDKError):
            self.case.validate(inspect_inputs=False)

    def test_bsdtar_requires_exact_named_empty_observations(self):
        row = self.plan['recipes']['archivers/bsdtar']
        original = row['distfile_metadata']
        for changes in (dict(PKGPATH='archivers/libarchive'), dict(DISTFILES='archive.tar.xz'),
                        dict(PATCHFILES='extra.diff'), dict(DISTFILES=' '), dict(PATCHFILES=' ')):
            with self.subTest(changes=changes):
                observed_name = changes.get('PKGPATH', 'archivers/bsdtar')
                row['distfile_metadata'] = self.local.observations(observed_name,
                    **{key: value for key, value in changes.items() if key != 'PKGPATH'})
                with self.assertRaisesRegex(SDKError, 'name this recipe and no distfiles'):
                    self.case.validate()
        row.pop('distfile_metadata')
        with self.assertRaisesRegex(SDKError, 'recognized local-source'):
            self.case.validate()
        row['distfile_metadata'] = original
        self.assertEqual(self.case.validate(), self.plan)

    def test_similar_recipe_name_remains_unrecognized(self):
        name = 'archivers/bsdtar-extra'
        shutil.copytree(self.export / 'archivers/bsdtar', self.export / name)
        row = copy.deepcopy(self.plan['recipes']['archivers/bsdtar'])
        row.update(directory=name, inventory=self.f.document(snapshot_tree(str(self.export / name))),
                   distfile_metadata=self.local.observations(name))
        self.plan['recipes'][name] = row
        self.plan['recipes']['lang/gcc16']['dependencies'].append(name)
        self.local.refresh_export()
        with self.assertRaisesRegex(SDKError, 'recognized local-source'):
            self.case.validate()


if __name__ == '__main__':
    unittest.main()
