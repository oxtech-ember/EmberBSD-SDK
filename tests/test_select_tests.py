# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Local affected-test selection; CI still runs the whole suite."""

import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
SDK_SOURCE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('select_tests', SDK_SOURCE / 'scripts/select-tests.py')
select_tests = importlib.util.module_from_spec(spec)
spec.loader.exec_module(select_tests)

FILES = {
    'src/emberbsd_sdk/__init__.py': '',
    'src/emberbsd_sdk/core.py': '',
    'src/emberbsd_sdk/alpha.py': 'from . import core\n',
    'src/emberbsd_sdk/beta.py': 'from .alpha import thing\n',
    'src/emberbsd_sdk/gamma.py': 'from emberbsd_sdk import core\n',
    'src/emberbsd_sdk/delta.py': 'import emberbsd_sdk.core\n',
    'scripts/tool.py': 'from emberbsd_sdk.delta import main\n',
    'tests/helper.py': 'from emberbsd_sdk import core\n',
    'tests/test_alpha.py': 'import helper\nfrom emberbsd_sdk import alpha\n',
    'tests/test_beta.py': 'import helper\nfrom emberbsd_sdk.beta import thing\n',
    'tests/test_gamma.py': 'import helper\nfrom emberbsd_sdk import gamma\n',
    'tests/test_tool.py': 'import helper\nTOOL = "scripts/tool.py"\nDOC = "docs/named.md"\n',
    'tests/test_module.py': 'import helper\nCOMMAND = ["-m", "emberbsd_sdk.gamma"]\n',
    'docs/named.md': '', 'docs/other.md': '', 'Makefile': '',
}


class SelectTestsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        for name, text in FILES.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def tearDown(self):
        self.temp.cleanup()

    def select(self, *changed):
        return select_tests.select(self.root, set(changed))[0]

    def test_changed_module_selects_direct_and_transitive_importers(self):
        self.assertEqual(self.select('src/emberbsd_sdk/alpha.py'), ['test_alpha', 'test_beta'])
        self.assertEqual(self.select('src/emberbsd_sdk/beta.py'), ['test_beta'])
        self.assertEqual(self.select('tests/test_gamma.py'), ['test_gamma'])

    def test_programs_and_files_are_selected_by_name(self):
        # A script reaches its imports; a test that runs it names its path.
        self.assertEqual(self.select('src/emberbsd_sdk/delta.py'), ['test_tool'])
        self.assertEqual(self.select('scripts/tool.py'), ['test_tool'])
        self.assertEqual(self.select('src/emberbsd_sdk/gamma.py'), ['test_gamma', 'test_module'])
        self.assertEqual(self.select('docs/named.md'), ['test_tool'])
        self.assertEqual(self.select('docs/other.md'), [])

    def test_shared_core_and_unmapped_changes_select_the_whole_suite(self):
        self.assertIsNone(self.select('src/emberbsd_sdk/core.py'))
        self.assertIsNone(self.select('src/emberbsd_sdk/__init__.py'))
        self.assertIsNone(self.select('tests/helper.py'))
        self.assertIsNone(self.select('Makefile'))
        self.assertIsNone(self.select('src/emberbsd_sdk/removed.py'))
        self.assertIsNone(self.select('src/emberbsd_sdk/alpha.py', '.github/workflows/contracts.yml'))

    def test_changed_paths_include_commits_index_worktree_and_new_files(self):
        def git(*args):
            subprocess.run(['git', '-C', str(self.root), *args], check=True, capture_output=True)
        git('init', '-q', '-b', 'main')
        git('-c', 'user.name=T', '-c', 'user.email=t@example.invalid', 'add', '.')
        git('-c', 'user.name=T', '-c', 'user.email=t@example.invalid', 'commit', '-q', '-m', 'base')
        git('branch', 'base')
        (self.root / 'docs/other.md').write_text('committed\n')
        git('-c', 'user.name=T', '-c', 'user.email=t@example.invalid', 'commit', '-q', '-am', 'change')
        (self.root / 'src/emberbsd_sdk/alpha.py').write_text('staged\n')
        git('add', 'src/emberbsd_sdk/alpha.py')
        (self.root / 'scripts/tool.py').write_text('unstaged\n')
        (self.root / 'tests/test_new.py').write_text('new\n')
        self.assertEqual(select_tests.changed_paths(self.root, 'base'), {
            'docs/other.md', 'src/emberbsd_sdk/alpha.py', 'scripts/tool.py', 'tests/test_new.py'})

    def test_this_checkout_maps_its_own_selector_test(self):
        modules, _ = select_tests.select(SDK_SOURCE, {'scripts/select-tests.py'})
        self.assertEqual(modules, ['test_select_tests'])


if __name__ == '__main__':
    unittest.main()
