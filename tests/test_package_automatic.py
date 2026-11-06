# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
import tempfile
import unittest
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.dont_write_bytecode = True
SDK_SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SDK_SOURCE / 'src'))

from emberbsd_sdk import package_automatic as automatic
from emberbsd_sdk.errors import SDKError


class AutomaticTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / 'pkgdb'
        self.database.mkdir()
        self.config = {'prefix': str(self.root), 'pkgdb': str(self.database)}
        self.crypto = SimpleNamespace(prefix=SimpleNamespace(
            tool=lambda name: '/tool/' + name, path='/tool'))

    def old(self, name, info=None):
        directory = self.database / name
        directory.mkdir()
        if info is not None:
            (directory / '+INSTALLED_INFO').write_text(info)

    def package(self, name, root, replaces=()):
        return {'pkgname': name, 'root': root, 'replaces': list(replaces)}

    def test_new_roots_and_dependencies(self):
        packages = [self.package('app-1', 'www/app'),
                    self.package('lib-1', 'devel/lib')]
        self.assertEqual(automatic.plan(self.config, packages, ['www/app']),
                         {'app-1': None, 'lib-1': 'yes'})

    def test_replacement_preserves_yes_no_and_absence(self):
        self.old('a-1', 'automatic=YES\n')
        self.old('b-1', 'automatic=no\n')
        self.old('c-1')
        packages = [self.package('a-2', 'x/a', ['a-1']),
                    self.package('b-2', 'x/b', ['b-1']),
                    self.package('c-2', 'x/c', ['c-1'])]
        self.assertEqual(automatic.plan(self.config, packages, ['x/a']),
                         {'a-2': 'yes', 'b-2': 'no', 'c-2': None})

    def test_invalid_or_duplicate_metadata_fails_before_intent(self):
        self.old('a-1', 'automatic=yes\nautomatic=no\n')
        with self.assertRaises(SDKError):
            automatic.plan(self.config, [self.package('a-2', 'x/a', ['a-1'])], [])
        (self.database / 'a-1' / '+INSTALLED_INFO').write_text('automatic=maybe\n')
        with self.assertRaises(SDKError):
            automatic.plan(self.config, [self.package('a-2', 'x/a', ['a-1'])], [])
        with self.assertRaises(SDKError):
            automatic.prepare(self.config, '../escape', 'yes')

    def test_intent_is_idempotent_and_refuses_conflict(self):
        automatic.prepare(self.config, 'a-2', 'yes')
        automatic.prepare(self.config, 'a-2', 'yes')
        with self.assertRaises(SDKError):
            automatic.prepare(self.config, 'a-2', None)

    def test_failure_retains_intent_then_recovery(self):
        automatic.prepare(self.config, 'a-2', 'no')
        failed = SimpleNamespace(returncode=1, stderr=b'failure')
        success = SimpleNamespace(returncode=0, stderr=b'')
        with patch('emberbsd_sdk.package_automatic.subprocess.run',
                   side_effect=[failed, success]) as run:
            with self.assertRaises(SDKError):
                automatic.finish(self.crypto, self.config, 'a-2')
            automatic.recover(self.crypto, self.config, [])
            automatic.recover(self.crypto, self.config, ['a-2'])
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args.args[0],
                         ['/tool/pkg_admin', '-K', str(self.database),
                          'set', 'automatic=no', 'a-2'])

    def test_plan_rejects_later_conflict_before_mutation(self):
        automatic.prepare(self.config, 'later-1', 'no')
        packages = [self.package('first-1', 'x/first'),
                    self.package('later-1', 'x/later')]
        with self.assertRaisesRegex(SDKError, 'conflicting'):
            automatic.plan(self.config, packages, ['x/first'])

    def test_recovery_ignores_only_regular_unpublished_staging(self):
        automatic.prepare(self.config, 'a-2', 'yes')
        directory = self.root / 'pkgdb.sdk-automatic'
        staging = directory / ('.a-2.' + 'a' * 32)
        staging.write_text('incomplete')
        with patch('emberbsd_sdk.package_automatic.subprocess.run',
                   return_value=SimpleNamespace(returncode=0, stderr=b'')):
            automatic.recover(self.crypto, self.config, ['a-2'])
        self.assertTrue(staging.exists())
        staging.unlink()
        staging.symlink_to(self.root / 'outside')
        with self.assertRaises(SDKError):
            automatic.recover(self.crypto, self.config, [])

    def test_absent_state_unsets(self):
        automatic.prepare(self.config, 'a-2', None)
        with patch('emberbsd_sdk.package_automatic.subprocess.run',
                   return_value=SimpleNamespace(returncode=0, stderr=b'')) as run:
            automatic.finish(self.crypto, self.config, 'a-2')
        self.assertEqual(run.call_args.args[0][-3:], ['unset', 'automatic', 'a-2'])


if __name__ == '__main__':
    unittest.main()
