# SPDX-License-Identifier: BSD-2-Clause
from contextlib import contextmanager, redirect_stdout, redirect_stderr
import gzip
import io
import os
from pathlib import Path
import shutil
import time
import unittest
from unittest.mock import patch

import test_package_install as install_fixture
import test_release_s3 as http_fixture
from emberbsd_sdk.jsonio import json_bytes, read_json
from emberbsd_sdk.release_cli import seal_release
from emberbsd_sdk.release_delivery import publish_release
from emberbsd_sdk.sdk_client import main


@unittest.skipUnless(os.environ.get('EMBERBSD_TEST_HOST_PREFIX'), 'select native Ports pkg_tools')
class ClientInstallTests(install_fixture.InstallTests):
    def setUp(self):
        super().setUp()
        http_fixture.HTTPTests.setUp(self)
        self.repository = self.work / 'release'
        (self.repository / 'native').mkdir(parents=True)
        shutil.copyfile(self.package(), self.repository / 'native/fixture-1.0.tgz')
        (self.repository / 'native/pkg_summary.gz').write_bytes(gzip.compress(b'PKGNAME=fixture-1.0\n\n'))
        (self.repository / 'native/catalog.json').write_bytes(json_bytes(dict(
            schema='emberbsd.packages.v1', repository='native', summary='summary',
            sets={'fixture': ['devel/fixture@native']},
            packages=[dict(root='devel/fixture@native', pkgname='fixture-1.0', artifact='fixture')])))
        artifacts = [dict(id=name, path='native/' + filename, kind=kind, host=self.cfg['host'],
                          target=None, unpacked_size=1024, depends=[], licenses=['2-clause-bsd'])
                     for name, filename, kind in [('fixture', 'fixture-1.0.tgz', 'host-package'),
                        ('catalog', 'catalog.json', 'package-index'), ('summary', 'pkg_summary.gz', 'package-index')]]
        draft = dict(schema='emberbsd.release.v1', release_id='fixture', created_at=int(time.time()),
                     sources={name: 'a' * 40 for name in ('os', 'ports', 'sdk')}, artifacts=artifacts)
        self.draft = draft
        envelope = self.repository / 'release.p7m'
        seal_release(self.crypto, json_bytes(draft), self.repository, self.signer, self.ca.certificate, envelope)
        publish_release(self.crypto, self.store, self.public, envelope, self.signer, self.ca.certificate)
        self.config = self.work / 'client.json'
        self.config.write_bytes(json_bytes(dict(schema='emberbsd.sdk-client.v1',
            host_prefix=os.environ['EMBERBSD_TEST_HOST_PREFIX'], cache_dir=str(self.work / 'cache'),
            state_dir=str(self.work / 'state'), trust_root=str(self.ca.certificate),
            url=self.url + '/bucket', repositories={'native': self.cfg})))

    def run_client(self, command='install'):
        output = io.TextIOWrapper(io.BytesIO(), encoding='utf-8')
        errors = io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            selection = ['--set', 'fixture'] if command == 'install' else []
            result = main(['--config', str(self.config), command, *selection])
        output.flush()
        return result, output.buffer.getvalue().decode() + errors.getvalue()

    def test_list_reads_signed_private_snapshot_without_publication(self):
        with patch('emberbsd_sdk.release_delivery.publish_directory_noreplace',
                   side_effect=RuntimeError('public publication unavailable')) as publish:
            status, output = self.run_client('list')
        self.assertEqual(status, 0, output)
        self.assertIn('fixture', output)
        publish.assert_not_called()
        self.assertEqual(list((self.work / 'state').glob('.release-metadata-*')), [])

    def test_online_install_and_repeat_without_download(self):
        status, output = self.run_client()
        self.assertEqual(status, 0, output)
        self.assertEqual((self.destination / 'payload').read_text(), 'installed data')
        for key in list(self.server.objects):
            if key.endswith('.tgz'):
                del self.server.objects[key]
        status, output = self.run_client()
        self.assertEqual(status, 0, output)
        self.assertIn('download 0 bytes', output)

    def test_changed_archive_fails_without_install(self):
        for key in list(self.server.objects):
            if key.endswith('.tgz'):
                self.server.objects[key] = b'corrupt package'
        status, output = self.run_client()
        self.assertEqual(status, 1, output)
        self.assertFalse(self.destination.exists())

    def publish_package(self, name, payload):
        shutil.rmtree(self.work / 'stage')
        replacement = self.package(package_name=name, payload=payload)
        shutil.copyfile(replacement, self.repository / f'native/{name}.tgz')
        catalog_path = self.repository / 'native/catalog.json'
        catalog = read_json(catalog_path)
        catalog['packages'][0]['pkgname'] = name
        catalog_path.write_bytes(json_bytes(catalog))
        (self.repository / 'native/pkg_summary.gz').write_bytes(gzip.compress(f'PKGNAME={name}\n\n'.encode()))
        self.draft['release_id'] = name
        self.draft['artifacts'][0]['path'] = f'native/{name}.tgz'
        envelope = self.repository / f'{name}.p7m'
        seal_release(self.crypto, json_bytes(self.draft), self.repository,
                     self.signer, self.ca.certificate, envelope)
        publish_release(self.crypto, self.store, self.public, envelope, self.signer, self.ca.certificate)

    @contextmanager
    def archive_responses(self, statuses, *, replacement=None):
        """Switch the served snapshot when the first archive request arrives."""
        requests = []
        serve = http_fixture.ObjectHandler.get

        def get(handler, head=False):
            if not head and handler.path.endswith('.tgz'):
                requests.append(handler.path)
                if len(requests) == 1 and replacement is not None:
                    handler.server.objects = replacement
                if len(requests) <= len(statuses):
                    handler.send_error(statuses[len(requests) - 1])
                    return
            return serve(handler, head)

        with patch.object(http_fixture.ObjectHandler, 'get', get):
            yield requests

    def prepare_rotation(self, name):
        previous = self.server.objects.copy()
        self.publish_package(name, 'replacement payload')
        replacement = self.server.objects
        self.server.objects = previous
        return replacement

    def test_install_replans_after_forbidden_displaced_archive(self):
        replacement = self.prepare_rotation('fixture-2.0')
        with self.archive_responses([403], replacement=replacement) as requests:
            status, output = self.run_client()
        self.assertEqual(status, 0, output)
        self.assertEqual(len(requests), 2)
        self.assertNotEqual(requests[0], requests[1])
        self.assertTrue((self.destination / 'pkgdb/fixture-2.0').is_dir())
        self.assertEqual((self.destination / 'payload').read_text(), 'replacement payload')

    def test_update_replans_after_missing_displaced_archive(self):
        status, output = self.run_client()
        self.assertEqual(status, 0, output)
        self.publish_package('fixture-2.0', 'intermediate payload')
        replacement = self.prepare_rotation('fixture-3.0')
        with self.archive_responses([404], replacement=replacement) as requests:
            status, output = self.run_client('update')
        self.assertEqual(status, 0, output)
        self.assertEqual(len(requests), 2)
        self.assertNotEqual(requests[0], requests[1])
        self.assertTrue((self.destination / 'pkgdb/fixture-3.0').is_dir())
        self.assertFalse((self.destination / 'pkgdb/fixture-2.0').exists())

    def test_unchanged_release_preserves_forbidden_error(self):
        earlier = self.server.objects.copy()
        publish_release(self.crypto, self.store, self.public, self.repository / 'release.p7m',
                        self.signer, self.ca.certificate, refresh=True, ttl=8 * 86400)
        renewed = self.server.objects
        self.assertNotEqual(earlier['/bucket/current.p7m'], renewed['/bucket/current.p7m'])
        self.server.objects = earlier
        with self.archive_responses([403], replacement=renewed) as requests:
            status, output = self.run_client()
        self.assertEqual(status, 1, output)
        self.assertIn('HTTP 403', output)
        self.assertEqual(len(requests), 1)
        self.assertFalse(self.destination.exists())

    def test_second_archive_failure_stops_the_install(self):
        replacement = self.prepare_rotation('fixture-2.0')
        with self.archive_responses([403, 403], replacement=replacement) as requests:
            status, output = self.run_client()
        self.assertEqual(status, 1, output)
        self.assertIn('HTTP 403', output)
        self.assertEqual(len(requests), 2)
        self.assertFalse(self.destination.exists())

    def test_replan_rejects_invalid_current_signature(self):
        replacement = self.prepare_rotation('fixture-2.0')
        replacement['/bucket/current.p7m'] = b'invalid CMS'
        with self.archive_responses([403], replacement=replacement) as requests:
            status, output = self.run_client()
        self.assertEqual(status, 1, output)
        self.assertIn('CMS signature/certificate verification failed', output)
        self.assertEqual(len(requests), 1)
        self.assertFalse(self.destination.exists())

    def test_online_update_uses_pkgdb_and_reuses_updated_package(self):
        status, output = self.run_client()
        self.assertEqual(status, 0, output)
        self.publish_package('fixture-2.0', 'updated data')
        # Saved selections do not determine which packages are updated.
        (self.work / 'state/selection.json').unlink()
        status, output = self.run_client('update')
        self.assertEqual(status, 0, output)
        self.assertIn('replace candidates 1', output)
        self.assertEqual((self.destination / 'payload').read_text(), 'updated data')
        self.assertTrue((self.destination / 'pkgdb/fixture-2.0').is_dir())
        self.assertFalse((self.destination / 'pkgdb/fixture-1.0').exists())
        for key in list(self.server.objects):
            if key.endswith('.tgz'):
                del self.server.objects[key]
        status, output = self.run_client('update')
        self.assertEqual(status, 0, output)
        self.assertIn('download 0 bytes', output)
        self.server.objects.clear()
        status, output = self.run_client('installed')
        self.assertEqual(status, 0, output)
        self.assertIn('fixture-2.0', output)


for name in install_fixture.InstallTests.__dict__:
    if name.startswith('test_') and name not in ClientInstallTests.__dict__:
        setattr(ClientInstallTests, name, None)

if __name__ == '__main__':
    unittest.main()
