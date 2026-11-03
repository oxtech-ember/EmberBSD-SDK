# SPDX-License-Identifier: BSD-2-Clause
import os
from pathlib import Path
import platform
import subprocess
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.package_install import PackageInstaller, inspect_install_paths, PORTABLE_PREFIX


@unittest.skipUnless(os.environ.get('EMBERBSD_TEST_HOST_PREFIX'), 'select native Ports pkg_tools')
class InstallTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from emberbsd_sdk.host_prefix import load_host_prefix
        from emberbsd_sdk.release_signing import ReleaseCrypto
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.root = Path(cls.tmp.name)
        cls.crypto = ReleaseCrypto(load_host_prefix(os.environ['EMBERBSD_TEST_HOST_PREFIX'],
            required_roles=('openssl', 'pkg_admin', 'pkg_info', 'pkg_create', 'pkg_add')))
        cls.ca = cls.crypto.create_root(cls.root / 'root')
        cls.signer = cls.crypto.issue_signer(cls.ca, cls.root / 'signer')

    def setUp(self):
        self.work = self.root / self._testMethodName
        self.work.mkdir()
        self.destination = self.work / 'prefix'
        self.cfg = dict(prefix=str(self.destination), pkgdb=str(self.destination / 'pkgdb'),
                        host=dict(os=platform.system(), arch=platform.machine(), abi='test', runtime='native'))

    def package(self, *, target=False, link=None, commands='', arch='aarch64', requires_self=False):
        stage = self.work / 'stage'
        stage.mkdir()
        if link:
            (stage / 'payload').symlink_to(link)
        else:
            (stage / 'payload').write_text('installed data')
        prefix = '/usr/pkg' if target else str(self.destination)
        (stage / 'PLIST').write_text('@name fixture-1.0\npayload\n' + commands)
        (stage / 'INFO').write_text('PKGPATH=devel/fixture\nLICENSE=2-clause-bsd\n' +
            (f'REQUIRES={prefix}/payload\n' if requires_self else '') +
            (f'OPSYS=NetBSD\nMACHINE_ARCH={arch}\nOS_VERSION=11.0\n' if target else
             f'OPSYS={platform.system()}\nMACHINE_ARCH={"aarch64" if platform.machine() == "arm64" else platform.machine()}\nOS_VERSION={platform.release()}\n'))
        unsigned, signed = self.work / 'unsigned.tgz', self.work / 'fixture-1.0.tgz'
        self.crypto.run_package('pkg_create', ['-c', '-fixture', '-d', '-fixture',
            '-f', stage / 'PLIST', '-B', stage / 'INFO', '-p', stage, '-I', prefix, unsigned])
        self.crypto.sign_package(unsigned, signed, self.signer, self.ca.certificate)
        return signed

    def test_native_package_is_owned_by_pkgdb_and_repeat_detected(self):
        installer = PackageInstaller(self.crypto, self.ca.certificate)
        package = self.package()
        installer.install(package, self.cfg)
        self.assertEqual((self.destination / 'payload').read_text(), 'installed data')
        self.assertIn('fixture-1.0', installer.installed(self.cfg))

    def test_target_scripts_do_not_run_on_host(self):
        marker = self.work / 'executed'
        package = self.package(target=True, commands=f'@exec touch {marker}\n')
        self.cfg = dict(prefix='/usr/pkg', pkgdb='/usr/pkg/pkgdb', sysroot=str(self.work / 'sysroot'),
                        target=dict(arch='aarch64', triplet='aarch64--netbsd', abi='11.0', runtime='test'),
                        os_version='11.0')
        installer = PackageInstaller(self.crypto, self.ca.certificate)
        installer.install(package, self.cfg)
        self.assertFalse(marker.exists())
        self.assertEqual((self.work / 'sysroot/usr/pkg/payload').read_text(), 'installed data')
        self.assertIn('fixture-1.0', installer.installed(self.cfg))

    def test_escaping_link_refused_before_install(self):
        package = self.package(link='../../outside')
        with self.assertRaisesRegex(SDKError, 'escape'):
            PackageInstaller(self.crypto, self.ca.certificate).install(package, self.cfg)
        self.assertFalse(self.destination.exists())

    def test_target_amd64_uses_pkgsrc_machine_arch(self):
        package = self.package(target=True, arch='x86_64')
        self.cfg = dict(prefix='/usr/pkg', pkgdb='/usr/pkg/pkgdb', sysroot=str(self.work / 'sysroot'),
                        target=dict(arch='amd64', triplet='x86_64--netbsd', abi='11.0', runtime='test'),
                        os_version='11.0')
        installer = PackageInstaller(self.crypto, self.ca.certificate)
        installer.install(package, self.cfg)
        self.assertIn('fixture-1.0', installer.installed(self.cfg))

    def test_existing_escaping_directory_link_refused(self):
        package = self.package()
        self.destination.mkdir()
        (self.destination / 'payload').symlink_to(self.work / 'outside')
        with self.assertRaisesRegex(SDKError, 'escape'):
            inspect_install_paths(package, self.cfg)

    def test_unqualified_native_prefix_relocation_is_refused(self):
        package = self.package()
        self.cfg['prefix'] = str(self.work / 'different-prefix')
        with self.assertRaisesRegex(SDKError, 'prefix'):
            inspect_install_paths(package, self.cfg)

    def test_signed_prefix_independent_package_installs_into_selected_prefix(self):
        package = self.package(commands=PORTABLE_PREFIX + '\n')
        self.cfg['prefix'] = str(self.work / 'different-prefix')
        self.cfg['pkgdb'] = self.cfg['prefix'] + '/pkgdb'
        PackageInstaller(self.crypto, self.ca.certificate).install(package, self.cfg)
        self.assertEqual((Path(self.cfg['prefix']) / 'payload').read_text(), 'installed data')

    def test_prefix_independent_commands_are_refused(self):
        package = self.package(commands=PORTABLE_PREFIX + '\n@exec echo unsafe\n')
        with self.assertRaisesRegex(SDKError, 'installation commands'):
            inspect_install_paths(package, self.cfg)

    @unittest.skipUnless(platform.system() == 'Darwin', 'Darwin packaging')
    def test_portable_producer_omits_empty_requires_before_real_pkg_add(self):
        package = self.package(requires_self=True)
        portable, signed = self.work / 'portable.tgz', self.work / 'portable-signed.tgz'
        subprocess.run([sys.executable, '-B', str(Path(__file__).resolve().parents[1] / 'scripts/package-portable.py'),
            str(package), '--host-prefix', os.environ['EMBERBSD_TEST_HOST_PREFIX'],
            '--dependency-prefix', str(self.work / 'unused'), '--output', str(portable),
            '--bootstrap-install-name-tool', '/usr/bin/false', '--codesign', '/usr/bin/false'],
            check=True, capture_output=True)
        self.crypto.sign_package(portable, signed, self.signer, self.ca.certificate)
        self.cfg['prefix'] = str(self.work / 'relocated')
        self.cfg['pkgdb'] = self.cfg['prefix'] + '/pkgdb'
        PackageInstaller(self.crypto, self.ca.certificate).install(signed, self.cfg)
        self.assertEqual((Path(self.cfg['prefix']) / 'payload').read_text(), 'installed data')


if __name__ == '__main__':
    unittest.main()
