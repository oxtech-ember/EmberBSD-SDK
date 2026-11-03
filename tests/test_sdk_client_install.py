# SPDX-License-Identifier: BSD-2-Clause
from contextlib import redirect_stdout, redirect_stderr
import gzip
import io
import os
from pathlib import Path
import shutil
import time
import unittest

import test_package_install as install_fixture
import test_release_s3 as http_fixture
from emberbsd_sdk.jsonio import json_bytes
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
            result = main(['--config', str(self.config), command, '--set', 'fixture'])
        output.flush()
        return result, output.buffer.getvalue().decode() + errors.getvalue()

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


for name in install_fixture.InstallTests.__dict__:
    if name.startswith('test_') and name not in ClientInstallTests.__dict__:
        setattr(ClientInstallTests, name, None)

if __name__ == '__main__':
    unittest.main()
