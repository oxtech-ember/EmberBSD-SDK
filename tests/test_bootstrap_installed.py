# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Installed-package provenance fixtures. No package or native tool is run."""

import copy
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_bootstrap_seed import SeedFixture
from emberbsd_sdk.bootstrap_seed import _history
from emberbsd_sdk.errors import SDKError


class InstalledHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.seed = SeedFixture(self.base)
        self.name = 'installed-bmake'
        self.seed.descriptor['providers'][self.name] = dict(origin='installed-package')
        self.seed.descriptor['tools']['make']['provider'] = self.name
        self.member = self.seed.descriptor['tools']['make']
        f = self.seed.native
        self.history = dict(kind='installed-package', package_name='bmake-20240909',
                            package_path='devel/bmake', installation_prefix='/unavailable/old-installation',
                            package_metadata=dict(build_info=f.raw(b'PKGPATH=devel/bmake\nCC_VERSION=clang-21.0.0\n'),
                                                  contents=f.raw(b'@name bmake-20240909\nbin/bmake\n')),
                            installed_member_inventory=f.document({'bin/bmake': dict(kind='file',
                                sha256=self.member['sha256'], mode=0o755, size=64)}),
                            provisioned_members=[dict(installed_relative='bin/bmake',
                                                       selected_path=self.member['path'],
                                                       sha256=self.member['sha256'])],
                            source_correlation=f.raw(b'Synthetic limited source correlation; not a binary build proof.\n'),
                            source_archives=[], patches=[], package_archive=None, source_commit=None,
                            historical_build_log=None, historical_compiler='clang-21.0.0',
                            historical_toolchain_closure=None, patches_complete=False,
                            unknowns=sorted(['package-archive', 'source-commit', 'historical-build-log',
                                             'historical-toolchain-closure', 'historical-patches', 'source-archives']))
        self.seed.descriptor['history'][self.name] = self.history
        for name, value in (('system', 'Linux'), ('machine', 'x86_64')):
            mock = patch('emberbsd_sdk.native_toolset.platform.' + name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)
        self.seed.seal()

    def test_installed_payload_uses_original_metadata_without_old_tree_or_process(self):
        self.assertFalse(Path(self.history['installation_prefix']).exists())
        with patch('subprocess.Popen', side_effect=AssertionError('no native command')):
            selected = self.seed.load()
        self.assertEqual(selected.command('make'), self.seed.commands['make'])
        self.assertEqual(self.seed.acceptance['accepted_history_unknowns'][self.name], self.history['unknowns'])
        self.assertEqual(self.history['historical_compiler'], 'clang-21.0.0')

    def test_unknowns_require_explicit_independent_acceptance(self):
        self.seed.acceptance['accepted_history_unknowns'][self.name] = []
        self.seed.acceptance_ref = self.seed.native.document(self.seed.acceptance)
        with self.assertRaisesRegex(SDKError, 'explicitly accept its historical unknowns'):
            self.seed.load()

    def test_omitted_unknown_or_original_metadata_is_refused(self):
        self.history['unknowns'].remove('package-archive')
        with self.assertRaisesRegex(SDKError, 'name every unknown'):
            self.seed.seal()
        self.history['unknowns'].append('package-archive')
        self.history['unknowns'].sort()
        self.history['package_metadata'].pop('contents')
        with self.assertRaisesRegex(SDKError, 'original installed package metadata'):
            self.seed.seal()

    def test_metadata_name_and_recipe_path_are_bound_to_original_bytes(self):
        self.history['package_name'] = 'bmake-forged'
        with self.assertRaisesRegex(SDKError, 'metadata identity mismatch'):
            self.seed.seal()

    def test_reported_historical_compiler_cannot_replace_original_metadata(self):
        self.history['historical_compiler'] = 'gcc-16.2.0'
        with self.assertRaisesRegex(SDKError, 'historical compiler differs'):
            self.seed.seal()
        self.history['historical_compiler'] = None
        self.history['unknowns'].append('historical-compiler')
        self.history['unknowns'].sort()
        with self.assertRaisesRegex(SDKError, 'omits reported compiler metadata'):
            self.seed.seal()

    def test_member_mapping_cannot_change_source_digest_or_omit_selected_member(self):
        original = copy.deepcopy(self.history['provisioned_members'])
        self.history['provisioned_members'][0]['sha256'] = '0' * 64
        with self.assertRaisesRegex(SDKError, 'preserved SHA256 roster'):
            self.seed.seal()
        self.history['provisioned_members'] = original
        self.history['provisioned_members'][0]['selected_path'] = str(self.base / 'outside')
        with self.assertRaisesRegex(SDKError, 'escapes or repeats'):
            self.seed.seal()
        self.history['provisioned_members'] = original
        self.history['provisioned_members'][0]['selected_path'] = self.seed.descriptor['tools']['ninja']['path']
        with self.assertRaisesRegex(SDKError, 'omits a selected provider member'):
            self.seed.seal()

    def test_original_package_metadata_cannot_be_a_fake_archive_or_log(self):
        for field, label in (('package_archive', 'package-archive'),
                             ('historical_build_log', 'historical-build-log'),
                             ('historical_toolchain_closure', 'historical-toolchain-closure')):
            with self.subTest(field=field):
                self.history[field] = self.history['package_metadata']['build_info']
                self.history['unknowns'].remove(label)
                with self.assertRaisesRegex(SDKError, 'cannot be relabeled'):
                    self.seed.seal()
                self.history[field] = None
                self.history['unknowns'].append(label)
                self.history['unknowns'].sort()

    def test_installed_make_package_cannot_provide_compiler_roles(self):
        self.seed.descriptor['tools']['cc']['provider'] = self.name
        with self.assertRaisesRegex(SDKError, 'cannot provide these bootstrap roles'):
            self.seed.seal()

    def test_source_owned_utility_packages_only_provide_their_named_roles(self):
        for package, path, roles in (
                ('nawk', 'lang/nawk', ['awk']), ('grep', 'textproc/grep', ['grep']),
                ('gsed', 'textproc/gsed', ['sed']), ('nbsed', 'textproc/nbsed', ['sed']),
                ('coreutils', 'sysutils/coreutils',
                    ['basename', 'cat', 'cut', 'date', 'dirname', 'env', 'expr', 'head', 'id',
                     'od', 'pwd', 'sort', 'tail', 'tr', 'uname', 'uniq', 'wc']),
                ('pkg_install', 'pkgtools/pkg_install', ['pkg_info', 'pkg_admin'])):
            with self.subTest(package=package):
                descriptor = copy.deepcopy(self.seed.descriptor)
                history = descriptor['history'][self.name]
                descriptor['tools']['make']['provider'] = 'ports'
                for role in roles:
                    descriptor['tools'][role] = copy.deepcopy(self.member)
                history.update(package_name=package + '-fixture1', package_path=path,
                    package_metadata=dict(build_info=self.seed.native.raw(
                        ('PKGPATH=' + path + '\nCC_VERSION=clang-21.0.0\n').encode()),
                        contents=self.seed.native.raw(('@name ' + package + '-fixture1\n').encode())))
                unknowns = _history(descriptor['history'], descriptor['providers'], descriptor=descriptor)
                self.assertEqual(unknowns[self.name], self.history['unknowns'])
                # These providers cannot turn their bytes into a compiler or another utility.
                for forbidden in ('cc', 'cpp', 'cxx', 'as', 'ld', 'python', 'make'):
                    descriptor['tools'][forbidden] = copy.deepcopy(self.member)
                    with self.assertRaisesRegex(SDKError, 'cannot provide these bootstrap roles'):
                        _history(descriptor['history'], descriptor['providers'], descriptor=descriptor)
                    descriptor['tools'].pop(forbidden)

    def _nbsed_descriptor(self):
        descriptor = copy.deepcopy(self.seed.descriptor)
        descriptor['tools']['make']['provider'] = 'ports'
        descriptor['tools']['sed'] = copy.deepcopy(self.member)
        history = descriptor['history'][self.name]
        history.update(package_name='nbsed-fixture1', package_path='textproc/nbsed',
            package_metadata=dict(build_info=self.seed.native.raw(
                b'PKGPATH=textproc/nbsed\nCC_VERSION=clang-21.0.0\n'),
                contents=self.seed.native.raw(b'@name nbsed-fixture1\nbin/nbsed\n')),
            installed_member_inventory=self.seed.native.document({'bin/nbsed': dict(
                kind='file', sha256=self.member['sha256'])}))
        history['provisioned_members'][0]['installed_relative'] = 'bin/nbsed'
        return descriptor, history

    def test_nbsed_preserves_original_identity_and_unknowns_without_execution(self):
        descriptor, history = self._nbsed_descriptor()
        with patch('subprocess.Popen', side_effect=AssertionError('no native command')):
            unknowns = _history(descriptor['history'], descriptor['providers'], descriptor=descriptor)
        self.assertEqual(unknowns[self.name], self.history['unknowns'])
        self.assertEqual(history['package_path'], 'textproc/nbsed')
        self.assertEqual(history['provisioned_members'][0]['installed_relative'], 'bin/nbsed')

    def test_nbsed_cannot_be_relabelled_as_gsed(self):
        for changes, message in (
                ({'package_path': 'textproc/gsed'}, 'package name does not match'),
                ({'package_name': 'gsed-fixture1'}, 'package name does not match'),
                ({'package_path': 'textproc/gsed', 'package_name': 'gsed-fixture1'},
                 'metadata identity mismatch')):
            with self.subTest(changes=changes):
                descriptor, history = self._nbsed_descriptor()
                history.update(changes)
                with self.assertRaisesRegex(SDKError, message):
                    _history(descriptor['history'], descriptor['providers'], descriptor=descriptor)

    def test_nbsed_keeps_member_path_digest_and_unknown_checks(self):
        for change, message in (
                (lambda h: h['provisioned_members'][0].update(sha256='0' * 64),
                 'preserved SHA256 roster'),
                (lambda h: h['provisioned_members'][0].update(
                    selected_path=str(self.base / 'outside')), 'escapes or repeats'),
                (lambda h: h['provisioned_members'][0].update(installed_relative='bin/gsed'),
                 'preserved SHA256 roster'),
                (lambda h: h['unknowns'].remove('historical-toolchain-closure'),
                 'name every unknown')):
            with self.subTest(message=message):
                descriptor, history = self._nbsed_descriptor()
                change(history)
                with self.assertRaisesRegex(SDKError, message):
                    _history(descriptor['history'], descriptor['providers'], descriptor=descriptor)

    def test_unmapped_installed_utility_package_is_refused(self):
        self.history['package_path'] = 'lang/gawk'
        with self.assertRaisesRegex(SDKError, 'named bootstrap input packages'):
            self.seed.seal()

    def test_changed_provisioned_bytes_refuse_and_retired_bytes_are_not_required_for_history(self):
        path = Path(self.member['path'])
        path.write_bytes(b'changed provisioned bytes')
        with self.assertRaisesRegex(SDKError, 'member changed'):
            self.seed.seal()
        path.unlink()
        unknowns = _history(self.seed.descriptor['history'], self.seed.descriptor['providers'],
                            descriptor=self.seed.descriptor, inspect_inputs=False)
        self.assertEqual(unknowns[self.name], self.history['unknowns'])

    def test_unselected_mk_data_has_a_mapping_without_claiming_native_build(self):
        self.seed.descriptor['tools']['make']['provider'] = 'ports'
        self.history.update(package_name='bootstrap-mk-files-20250601',
                            package_path='pkgtools/bootstrap-mk-files',
                            package_metadata=dict(
                                build_info=self.seed.native.raw(b'PKGPATH=pkgtools/bootstrap-mk-files\nCC_VERSION=clang-21.0.0\n'),
                                contents=self.seed.native.raw(b'@name bootstrap-mk-files-20250601\nshare/mk/sys.mk\n')))
        target = self.seed.native.bundle / 'sys-upstream.mk'
        target.write_bytes(b'preserved source-owned synthetic mk text\n')
        sha = hashlib.sha256(target.read_bytes()).hexdigest()
        self.history['installed_member_inventory'] = self.seed.native.document(
            {'share/mk/sys.mk': dict(kind='file', sha256=sha)})
        self.history['provisioned_members'] = [dict(installed_relative='share/mk/sys.mk',
                                                   selected_path=str(target), sha256=sha)]
        self.seed.seal()
        self.seed.load()
