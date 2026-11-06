# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Publisher-only package updates reuse current binaries and prepared signatures."""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import test_release_s3 as http_fixture
from test_binary_catalog import BinaryReleaseFixture, graph
from emberbsd_sdk.binary_release import load_catalogs
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.jsonio import json_bytes
from emberbsd_sdk.package_publish import update_packages
from emberbsd_sdk.release_catalog import parse_channel
import emberbsd_sdk.release_delivery as release_delivery
from emberbsd_sdk.release_delivery import publish_release


class PackagePublishTests(BinaryReleaseFixture):
    def setUp(self):
        http_fixture.HTTPTests.setUp(self)
        BinaryReleaseFixture.setUp(self)

    def publish_baseline(self):
        repo = self.repository()
        release_dir = self.work / 'baseline'
        self.compose(output=release_dir)
        pointer = publish_release(self.crypto, self.store, self.public,
            release_dir / 'release.p7m', self.leaf, self.ca.certificate)
        self.assertNotIn('generation', pointer)
        return repo

    def prepare_addition(self, *, root='net/new', release_id='package-update', created_at=2):
        repo = self.publish_baseline()
        value = graph()
        value['nodes'].append(dict(id='target:' + root, version='1.0',
            input_sha256='c' * 64,
            dependencies=[dict(id='target:devel/lib', kind='runtime')]))
        self.graph_path.write_bytes(json_bytes(value))
        archive = self.package(root, depends=('lib>=1',))
        repo['sets'] = []
        repo['roots'] = [root]
        repo['packages'] = {'target:' + root: archive}
        self.spec.update(release_id=release_id, created_at=created_at)
        return repo, Path(archive), self.work / 'publisher-work'

    def update(self, workdir, **kwargs):
        return update_packages(self.crypto, self.store, self.public, self.spec,
            self.ports, workdir, self.leaf, self.ca.certificate, **kwargs)

    def test_addition_fetch_compose_publish_preserves_old_packages_without_get(self):
        _, _, workdir = self.prepare_addition()
        original_read = self.public.read
        reads = []

        def track_read(key, *args, **kwargs):
            reads.append(key)
            return original_read(key, *args, **kwargs)

        with patch.object(self.public, 'read', side_effect=track_read):
            result = self.update(workdir)

        self.assertNotIn('generation', result['channel'])
        self.assertEqual(result['statistics']['added'], 1)
        self.assertEqual(result['statistics']['replaced'], 0)
        self.assertEqual(result['statistics']['uploaded_packages'], 1)
        self.assertEqual(result['statistics']['built'], 0)
        self.assertFalse(any(key.endswith('.tgz') for key in reads))

        release, catalogs = load_catalogs(self.crypto, workdir / 'release/release.p7m',
            workdir / 'release', self.ca.certificate, ())
        self.assertEqual({item['root'] for item in catalogs[0]['packages']},
            {'net/app', 'net/other', 'devel/lib', 'devel/lib-devel', 'net/new'})
        self.assertEqual(release['release_id'], 'package-update')

    def test_bad_successful_build_checksum_refuses_before_channel_switch(self):
        _, archive, workdir = self.prepare_addition(release_id='bad-checksum')
        pointer = self.server.objects['/bucket/current.p7m']
        current_puts = []
        original_put = self.store.put

        def count_current_put(key, *args, **kwargs):
            if key == 'current.p7m':
                current_puts.append(key)
            return original_put(key, *args, **kwargs)

        with patch.object(self.store, 'put', side_effect=count_current_put):
            with self.assertRaisesRegex(SDKError, 'archive differs from successful Ports result'):
                self.update(workdir, checksums={str(archive): '0' * 64})

        self.assertEqual(current_puts, [])
        self.assertEqual(self.server.objects['/bucket/current.p7m'], pointer)

    def test_graph_downgrade_from_current_version_is_rejected(self):
        value = graph()
        next(node for node in value['nodes'] if node['id'] == 'target:net/app')['version'] = '2.0'
        self.graph_path.write_bytes(json_bytes(value))
        repo = self.repository()
        version_one_archive = Path(repo['packages']['target:net/app'])
        repo['packages']['target:net/app'] = self.package('net/app', version='2.0')
        baseline = self.work / 'version-two-baseline'
        self.compose(output=baseline)
        initial = publish_release(self.crypto, self.store, self.public, baseline / 'release.p7m',
                                  self.leaf, self.ca.certificate)
        self.assertNotIn('generation', initial)
        pointer = self.server.objects['/bucket/current.p7m']

        value = graph()
        self.graph_path.write_bytes(json_bytes(value))
        repo['sets'] = []
        repo['roots'] = ['net/app']
        repo['packages'] = {'target:net/app': str(version_one_archive)}
        self.spec.update(release_id='attempt-downgrade', created_at=3)
        current_puts = []
        original_put = self.store.put

        def count_current_put(key, *args, **kwargs):
            if key == 'current.p7m':
                current_puts.append(key)
            return original_put(key, *args, **kwargs)

        with patch.object(self.store, 'put', side_effect=count_current_put):
            with self.assertRaisesRegex(SDKError, 'package would downgrade current: net/app'):
                self.update(self.work / 'downgrade-work')

        self.assertEqual(current_puts, [])
        self.assertEqual(self.server.objects['/bucket/current.p7m'], pointer)

    def _run_partial_retry(self, failure):
        _, _, workdir = self.prepare_addition(release_id='partial-' + failure)
        original_verify = self.public.verify
        failed = False

        def fail_current_once(key, *args, **kwargs):
            nonlocal failed
            if key == 'current.p7m' and not failed:
                failed = True
                raise SDKError('injected current verification failure')
            return original_verify(key, *args, **kwargs)

        with patch.object(self.crypto, 'sign_package', wraps=self.crypto.sign_package) as package_sign, \
                patch.object(self.crypto, 'sign_cms', wraps=self.crypto.sign_cms) as cms_sign:
            if failure == 'verify':
                injected = patch.object(self.public, 'verify', side_effect=fail_current_once)
            else:
                injected = patch.object(release_delivery, '_clean_current',
                    side_effect=SDKError('injected cleanup failure'))
            with injected:
                with self.assertRaisesRegex(SDKError, 'injected .* failure'):
                    self.update(workdir)
            calls = (package_sign.call_count, cms_sign.call_count)
            prepared = (workdir / 'release/release.p7m').read_bytes()
            self.assertTrue((workdir / 'snapshot/current.p7m').is_file())

            result = self.update(workdir)

            self.assertEqual((package_sign.call_count, cms_sign.call_count), calls)
            self.assertEqual((workdir / 'release/release.p7m').read_bytes(), prepared)
        self.assertNotIn('generation', result['channel'])

    def test_partial_current_verify_failure_retries_prepared_signature(self):
        self._run_partial_retry('verify')

    def test_partial_cleanup_failure_retries_prepared_signature(self):
        self._run_partial_retry('cleanup')

    def test_new_release_id_reuses_workdir_and_removes_only_operation_outputs(self):
        repo, first_archive, workdir = self.prepare_addition(release_id='first-update')
        first_archive_bytes = first_archive.read_bytes()
        first = self.update(workdir)
        self.assertNotIn('generation', first['channel'])
        old_snapshot_marker = workdir / 'snapshot/obsolete-marker'
        old_release_marker = workdir / 'release/obsolete-marker'
        old_snapshot_marker.write_bytes(b'owned snapshot output')
        old_release_marker.write_bytes(b'owned release output')

        root = 'net/second'
        value = json.loads(self.graph_path.read_text())
        value['nodes'].append(dict(id='target:' + root, version='1.0',
            input_sha256='d' * 64,
            dependencies=[dict(id='target:devel/lib', kind='runtime')]))
        self.graph_path.write_bytes(json_bytes(value))
        second_archive = Path(self.package(root, depends=('lib>=1',)))
        repo['roots'] = [root]
        repo['packages'] = {'target:' + root: str(second_archive)}
        self.spec.update(release_id='second-update', created_at=3)

        second = self.update(workdir)

        self.assertNotIn('generation', second['channel'])
        self.assertFalse(old_snapshot_marker.exists())
        self.assertFalse(old_release_marker.exists())
        self.assertEqual(first_archive.read_bytes(), first_archive_bytes)
        self.assertTrue(second_archive.is_file())
        self.assertTrue((workdir / 'snapshot/release.p7m').is_file())
        self.assertTrue((workdir / 'release/release.p7m').is_file())

    def test_same_root_version_with_renamed_pkgbase_is_replaced(self):
        repo = self.publish_baseline()
        baseline = self.work / 'baseline'
        _, catalogs = load_catalogs(self.crypto, baseline / 'release.p7m', baseline,
                                   self.ca.certificate, ())
        old_package = next(item for item in catalogs[0]['packages'] if item['root'] == 'net/app')

        renamed_archive = Path(self.package('net/app', pkgbase='renamed-app'))
        repo['sets'] = []
        repo['roots'] = ['net/app']
        repo['packages'] = {'target:net/app': str(renamed_archive)}
        self.spec.update(release_id='renamed-package', created_at=2)
        result = self.update(self.work / 'rename-work')

        self.assertNotIn('generation', result['channel'])
        self.assertEqual(result['statistics']['added'], 0)
        self.assertEqual(result['statistics']['replaced'], 1)
        self.assertEqual(result['statistics']['uploaded_packages'], 1)
        output = self.work / 'rename-work/release'
        _, updated_catalogs = load_catalogs(self.crypto, output / 'release.p7m', output,
                                            self.ca.certificate, ())
        new_package = next(item for item in updated_catalogs[0]['packages']
                           if item['root'] == 'net/app')
        self.assertEqual(new_package['pkgname'], 'renamed-app-1.0')
        self.assertEqual(new_package['artifact'], old_package['artifact'])
        self.assertTrue((output / 'arm64/renamed-app-1.0.tgz').is_file())
        self.assertNotIn('/bucket/current/target/aarch64/app-1.0.tgz', self.server.objects)
        self.assertIn('/bucket/current/target/aarch64/renamed-app-1.0.tgz', self.server.objects)

    def test_already_current_packages_are_noop_without_signing_or_pointer_put(self):
        _, _, workdir = self.prepare_addition(release_id='already-current')
        first = self.update(workdir)
        self.assertNotIn('generation', first['channel'])
        pointer = self.server.objects['/bucket/current.p7m']
        signed_input = workdir / 'release/arm64/new-1.0.tgz'

        # A fresh work area receives the exact already-current signed package.
        repo = self.spec['repositories'][0]
        repo['packages'] = {'target:net/new': str(signed_input)}
        noop_workdir = self.work / 'no-op-work'
        current_puts = []
        original_put = self.store.put
        def count_current_put(key, *args, **kwargs):
            if key == 'current.p7m':
                current_puts.append(key)
            return original_put(key, *args, **kwargs)

        with patch.object(self.crypto, 'sign_package', wraps=self.crypto.sign_package) as package_sign, \
                patch.object(self.crypto, 'sign_cms', wraps=self.crypto.sign_cms) as cms_sign, \
                patch.object(self.store, 'put', side_effect=count_current_put):
            result = self.update(noop_workdir)

        self.assertNotIn('generation', result['channel'])
        current = parse_channel(self.crypto.verify_cms(pointer, self.ca.certificate), 'current')
        self.assertEqual(result['channel'], current)
        self.assertEqual(result['statistics']['added'], 0)
        self.assertEqual(result['statistics']['replaced'], 0)
        self.assertEqual(result['statistics']['uploaded_packages'], 0)
        self.assertEqual(result['statistics']['upload_bytes'], 0)
        self.assertEqual(result['statistics']['built'], 0)
        self.assertEqual(package_sign.call_count, 0)
        self.assertEqual(cms_sign.call_count, 0)
        self.assertEqual(current_puts, [])
        self.assertEqual(self.server.objects['/bucket/current.p7m'], pointer)
