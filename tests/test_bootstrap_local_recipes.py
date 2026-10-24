# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Local recipe observations and complete synthetic source inventories."""

import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_bootstrap_recipe
from emberbsd_sdk import bootstrap_producer as producer, bootstrap_recipe as recipe
from emberbsd_sdk.bootstrap_seed import _plan
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.inventory import snapshot_tree


# These fixture layouts follow the four common Ports producers. No configure,
# make, compiler or native program runs in these controls.
LOCAL_LAYOUTS = {
    'pkgtools/cwrappers': ('Makefile', 'files/bin/Makefile', 'files/bin/cc-wrapper.c'),
    'pkgtools/digest': ('Makefile', 'files/configure', 'files/Makefile.in', 'files/digest.c'),
    'pkgtools/libnbcompat': ('Makefile', 'files/configure', 'files/Makefile.in', 'files/asprintf.c'),
    'pkgtools/mktools': ('Makefile', 'files/Makefile', 'files/mk-buildlink-symlinks.c', 'files/mkpath.c'),
}


class LocalRecipeTests(unittest.TestCase):
    def setUp(self):
        self.case = test_bootstrap_recipe.RecipeTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.plan, self.f, self.export = self.case.plan, self.case.f, self.case.fixture.export
        for name, files in LOCAL_LAYOUTS.items():
            directory = self.export / name
            for relative in (*files, 'files/extra-source.c'):
                path = directory / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('/* Synthetic pinned source; never compiled. */\n')
            self.plan['recipes'][name] = dict(directory=name, patches=[], distfiles=[],
                distfile_metadata=self.observations(name),
                dependencies=['pkgtools/libnbcompat'] if name == 'pkgtools/cwrappers' else [])
        self.plan['recipes']['lang/gcc16']['dependencies'] += [
            'pkgtools/cwrappers', 'pkgtools/digest', 'pkgtools/mktools']
        self.refresh_export()

    def observations(self, name, **changes):
        values = {key: '' for key in producer.VARIABLES}
        values.update(PKGPATH=name, **changes)
        return self.f.raw(''.join(key + '=' + values[key] + '\n'
                                  for key in producer.VARIABLES).encode('utf-8'))

    def refresh_export(self):
        for name in LOCAL_LAYOUTS:
            self.plan['recipes'][name]['inventory'] = self.f.document(
                snapshot_tree(str(self.export / name)))
        inventory = self.f.document(snapshot_tree(str(self.export)))
        proof = copy.deepcopy(self.case.fixture.proof)
        proof['inventory'] = inventory
        self.plan['metadata']['export'].update(inventory=inventory, source_proof=self.f.document(proof))
        self.plan['ports_export'] = dict(source=self.case.descriptor['sources']['ports'],
                                         **self.plan['metadata']['export'])
        self.plan['metadata']['recipes'] = sorted(self.plan['recipes'])
        self.case.refresh()

    def test_four_local_recipes_need_no_invented_archive(self):
        with patch('subprocess.Popen', side_effect=AssertionError('no native process')):
            self.assertEqual(self.case.validate(), self.plan)
            self.assertEqual(self.case.validate(inspect_inputs=False), self.plan)
        expected = {row['relative']: row['file'] for name in ('lang/gcc16', 'devel/gmp')
                    for row in self.plan['recipes'][name]['distfiles']}
        self.assertEqual(recipe.recipe_distfiles(self.plan), expected)
        self.case.work.mkdir()
        recipe.prepare_recipe_work(self.plan)
        actual = sorted(str(path.relative_to(self.case.work / 'distfiles'))
                        for path in (self.case.work / 'distfiles').rglob('*') if path.is_file())
        self.assertEqual(actual, sorted(expected))

    def test_nonlocal_and_legacy_empty_distfiles_are_refused(self):
        original = copy.deepcopy(self.plan['recipes']['devel/gmp']['distfiles'])
        for metadata in (False, True):
            with self.subTest(nonlocal_metadata=metadata):
                row = self.plan['recipes']['devel/gmp']
                row['distfiles'] = []
                if metadata:
                    row['distfile_metadata'] = self.observations('devel/gmp')
                with self.assertRaisesRegex(SDKError, 'recognized local-source'):
                    self.case.validate()
        row['distfiles'] = original
        row.pop('distfile_metadata', None)
        descriptor = copy.deepcopy(self.case.descriptor)
        descriptor.pop('make_binding')
        with self.assertRaisesRegex(SDKError, 'recipe distfiles are required'):
            _plan(self.f.document(self.plan), descriptor, self.case.commands,
                  descriptor['abi_flags'])

    def test_named_observations_must_match_recipe_and_both_empty_fields(self):
        row = self.plan['recipes']['pkgtools/digest']
        for changes in (dict(PKGPATH='pkgtools/mktools'), dict(DISTFILES='archive.tar.xz'),
                        dict(PATCHFILES='fix.diff'), dict(DISTFILES=' ')):
            with self.subTest(changes=changes):
                values = {key: '' for key in producer.VARIABLES}
                values['PKGPATH'] = 'pkgtools/digest'
                values.update(changes)
                row['distfile_metadata'] = self.f.raw(''.join(
                    key + '=' + values[key] + '\n' for key in producer.VARIABLES).encode('utf-8'))
                with self.assertRaisesRegex(SDKError, 'name this recipe and no distfiles'):
                    self.case.validate()

    def test_missing_changed_positional_or_diagnostic_log_is_refused(self):
        row = self.plan['recipes']['pkgtools/digest']
        original = row.pop('distfile_metadata')
        with self.assertRaisesRegex(SDKError, 'recognized local-source'):
            self.case.validate()
        row['distfile_metadata'] = dict(original, sha256='0' * 64)
        with self.assertRaises(SDKError):
            self.case.validate()
        for raw in (b'pkgtools/digest\n\n',
                    Path(original['path']).read_bytes() + b'bmake: warning: command failed\n'):
            row['distfile_metadata'] = self.f.raw(raw)
            with self.assertRaises(SDKError):
                self.case.validate()

    def test_only_empty_local_rows_accept_the_additional_reference(self):
        row = self.plan['recipes']['lang/gcc16']
        row['distfile_metadata'] = self.observations('lang/gcc16')
        with self.assertRaisesRegex(SDKError, 'only belong to an empty local-source'):
            self.case.validate()
        with self.assertRaisesRegex(SDKError, 'only belong to an empty local-source'):
            recipe.recipe_distfiles(self.plan)

    def test_complete_subtree_cannot_omit_a_non_sentinel_source(self):
        row = self.plan['recipes']['pkgtools/digest']
        inventory = json.loads(Path(row['inventory']['path']).read_text())
        del inventory['files/extra-source.c']
        row['inventory'] = self.f.document(inventory)
        with self.assertRaisesRegex(SDKError, 'differs from the pinned export'):
            self.case.validate()

    def test_local_layout_requires_regular_configure_makefile_and_c_sources(self):
        for name, relative in (('pkgtools/digest', 'files/configure'),
                               ('pkgtools/libnbcompat', 'files/Makefile.in'),
                               ('pkgtools/cwrappers', 'files/bin/cc-wrapper.c'),
                               ('pkgtools/mktools', 'files/mkpath.c')):
            with self.subTest(name=name, relative=relative):
                path = self.export / name / relative
                original = path.read_bytes()
                path.unlink()
                self.refresh_export()
                with self.assertRaisesRegex(SDKError, 'required regular source file'):
                    self.case.validate()
                path.write_bytes(original)
        path = self.export / 'pkgtools/digest/files/configure'
        path.unlink()
        path.symlink_to('digest.c')
        self.refresh_export()
        with self.assertRaisesRegex(SDKError, 'required regular source file'):
            self.case.validate()

    def test_observations_cannot_overlap_fresh_build_work(self):
        row = self.plan['recipes']['pkgtools/digest']
        self.case.work.mkdir()
        path = self.case.work / 'input-metadata.log'
        path.write_bytes(Path(row['distfile_metadata']['path']).read_bytes())
        row['distfile_metadata'] = dict(row['distfile_metadata'], path=str(path))
        with self.assertRaisesRegex(SDKError, 'historical bound work overlaps a recorded input'):
            self.case.validate(inspect_inputs=False)


if __name__ == '__main__':
    unittest.main()
