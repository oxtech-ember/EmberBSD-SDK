# SPDX-License-Identifier: BSD-2-Clause
"""Native target installation contracts, without a real pkgdb or target host."""
from pathlib import Path
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.package_install import PackageInstaller, install_paths
from emberbsd_sdk import package_state, sdk_client, sysroot_package


class NativeTargetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = dict(arch='x86_64', triplet='x86_64--netbsd', abi='netbsd-11', runtime='libc++')
        self.cfg = dict(prefix=str(self.root / 'pkg'), pkgdb=str(self.root / 'pkgdb'),
                        target=self.target.copy(), os_version='11.0', install_mode='native')
        self.host = patch.multiple('emberbsd_sdk.package_install.platform',
                                   system=Mock(return_value='NetBSD'),
                                   machine=Mock(return_value='amd64'),
                                   release=Mock(return_value='11.0'))
        self.host.start()
        self.addCleanup(self.host.stop)
        self.cpu = patch('emberbsd_sdk.package_install.native_cpu', return_value='amd64')
        self.cpu.start()
        self.addCleanup(self.cpu.stop)

    def test_native_prefix_and_default_sysroot(self):
        self.assertEqual(install_paths(self.cfg), (Path(self.cfg['prefix']), Path(self.cfg['pkgdb'])))
        default = dict(self.cfg, prefix='/usr/pkg', pkgdb='/usr/pkg/pkgdb', sysroot=str(self.root / 'sysroot'))
        del default['install_mode']
        self.assertEqual(install_paths(default), (self.root / 'sysroot/usr/pkg', self.root / 'sysroot/usr/pkg/pkgdb'))
        default['sysroot'] = '/'
        with self.assertRaises(SDKError):
            install_paths(default)

    def test_invalid_mode_or_identity_refused(self):
        for change in (dict(install_mode='unknown'), dict(sysroot=str(self.root)),
                       dict(os_version='10.0'), dict(target={**self.target, 'arch': 'aarch64'}),
                       dict(target={**self.target, 'triplet': 'aarch64--netbsd'}),
                       dict(target={**self.target, 'abi': 'netbsd-10'})):
            with self.subTest(change=change), self.assertRaises(SDKError):
                install_paths(dict(self.cfg, **change))
        with patch('emberbsd_sdk.package_install.platform.system', return_value='Darwin'):
            with self.assertRaises(SDKError):
                install_paths(self.cfg)
        with patch('emberbsd_sdk.package_install.native_cpu', return_value='aarch64'):
            with self.assertRaises(SDKError):
                install_paths(self.cfg)
        with patch('emberbsd_sdk.package_install.platform.release', return_value='11.1'):
            self.assertEqual(install_paths(self.cfg)[0], Path(self.cfg['prefix']))

    def test_pkg_add_native_arguments_and_hook_path(self):
        trust = self.root / 'trust'; trust.write_bytes(b'trust')
        package = self.root / 'fixture.tgz'; package.write_bytes(b'fixture')
        prefix = SimpleNamespace(path='/selected/bin', tool=lambda name: '/selected/bin/' + name)
        installer = PackageInstaller(SimpleNamespace(prefix=prefix), trust)
        with patch('emberbsd_sdk.package_install.inspect_install_paths', return_value=None), \
             patch('emberbsd_sdk.package_install.subprocess.run', return_value=SimpleNamespace(returncode=0, stderr=b'')) as run:
            installer.install(package, self.cfg, checked=True)
        args = run.call_args.args[0]
        self.assertEqual(args[0], '/selected/bin/pkg_add')
        self.assertEqual(args[args.index('-p') + 1], self.cfg['prefix'])
        self.assertEqual(args[args.index('-K') + 1], self.cfg['pkgdb'])
        for forbidden in ('-P', '-I', '-m'):
            self.assertNotIn(forbidden, args)
        path = run.call_args.kwargs['env']['PATH'].split(os.pathsep)
        self.assertEqual(path[0], '/selected/bin')
        self.assertIn('/usr/bin', path)
        self.assertIn('/bin', path)
        self.assertIn('/usr/sbin', path)
        self.assertIn('/sbin', path)

    def test_live_sysroot_and_source_fallback_refused(self):
        config = {'repositories': {'target': self.cfg}, 'host_prefix': str(self.root), 'source': {}}
        with self.assertRaises(SDKError):
            sysroot_package.selection({'artifacts': []}, config, Mock(), 'amd64')
        with patch('emberbsd_sdk.sdk_client.load_host_prefix', side_effect=AssertionError('must refuse before tools')):
            with self.assertRaises(SDKError):
                sdk_client.apply_plan({'build_roots': ['graphics/fixture']}, config, Mock(), Mock(), Mock())
            with self.assertRaises(SDKError):
                sdk_client.run_source(config, ['graphics/fixture'], 'build')

    def test_live_state_reads_running_os_release(self):
        installer = Mock(); installer.inventory.return_value = []
        original = Path.exists
        def exists(path):
            if str(path) == '/etc/release':
                return True
            return original(path)
        with patch.object(Path, 'exists', exists), \
             patch.object(package_state, 'read_bytes', return_value=b'EmberBSD 11.0\n') as read:
            result = package_state.installed_state({'repositories': {'target': self.cfg}}, installer)
        self.assertEqual(read.call_args.args[0], Path('/etc/release'))
        self.assertEqual(result['repositories']['target']['os_release'], 'EmberBSD 11.0')


if __name__ == '__main__':
    unittest.main()
