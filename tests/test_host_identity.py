# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""CPU detection and its package/prefix consumers, without executing tools."""

from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk import host_identity, host_prefix, package_install, sdk_client
from emberbsd_sdk.errors import CommandError, SDKError


class HostIdentityTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('emberbsd_sdk.host_identity.platform.system', return_value='NetBSD'))
        self.enterContext(patch('emberbsd_sdk.host_identity.platform.machine', return_value='evbarm'))
        self.enterContext(patch('emberbsd_sdk.package_install.platform.release', return_value='11.0'))
        self.service = self.enterContext(patch('emberbsd_sdk.host_identity._service_path', return_value='/os/uname'))
        self.run = self.enterContext(patch('emberbsd_sdk.host_identity.process.capture',
                                          return_value=SimpleNamespace(stdout=b'aarch64\n')))

    def test_netbsd_cpu_is_processor_architecture_not_machine_port(self):
        self.assertEqual(host_identity.native_cpu(), 'aarch64')
        self.service.assert_called_once_with('uname')
        self.run.assert_called_once_with(['/os/uname', '-p'], env={'LC_ALL': 'C'})
        self.run.return_value.stdout = b'earmv7hf\n'
        self.assertEqual(host_identity.native_cpu(), 'earmv7hf')

    def test_unavailable_or_invalid_processor_has_no_machine_fallback(self):
        for output in (b'', b'unknown\n', b'aarch64\namd64\n', b'\xff', b'cpu with spaces'):
            with self.subTest(output=output):
                self.run.return_value.stdout = output
                with self.assertRaises(SDKError):
                    host_identity.native_cpu()
        self.run.side_effect = CommandError(1, ['/os/uname', '-p'], stderr=b'unavailable')
        with self.assertRaises(SDKError):
            host_identity.native_cpu()
        self.service.side_effect = SDKError('missing OS service')
        with self.assertRaises(SDKError):
            host_identity.native_cpu()

    def test_other_hosts_keep_existing_identity_without_a_command(self):
        for system, cpu in (('Darwin', 'arm64'), ('Linux', 'x86_64')):
            with self.subTest(system=system), \
                 patch('emberbsd_sdk.host_identity.platform.system', return_value=system), \
                 patch('emberbsd_sdk.host_identity.platform.machine', return_value=cpu):
                self.assertEqual(host_identity.native_cpu(), cpu)
        self.service.assert_not_called()
        self.run.assert_not_called()

    def test_native_package_and_repository_accept_cpu_but_reject_other_abi(self):
        target = dict(arch='aarch64', triplet='aarch64--netbsd', abi='netbsd-11', runtime='libc++')
        destination = dict(prefix='/usr/pkg', pkgdb='/usr/pkg/pkgdb', target=target,
                           install_mode='native', os_version='11.0')
        self.assertEqual(package_install.install_paths(destination),
                         (Path('/usr/pkg'), Path('/usr/pkg/pkgdb')))
        config = dict(schema='emberbsd.sdk-client.v1', host_prefix='/usr', cache_dir='/cache',
                      state_dir='/state', repositories={'target': destination})
        self.assertIs(sdk_client.validate_config(config), config)
        native = dict(prefix='/usr/pkg', pkgdb='/usr/pkg/pkgdb',
                      host=dict(os='NetBSD', arch='aarch64', abi='netbsd-11', runtime='libc++'))
        host_config = dict(config, repositories={'host': native})
        self.assertIs(sdk_client.validate_config(host_config), host_config)
        for output in (b'earmv7hf\n', b'amd64\n'):
            with self.subTest(output=output):
                self.run.return_value.stdout = output
                with self.assertRaises(SDKError):
                    sdk_client.validate_config(config)
                with self.assertRaises(SDKError):
                    sdk_client.validate_config(host_config)

    def test_default_prefix_requires_matching_actual_elf_cpu(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root / 'sbin').mkdir()
            tool = root / 'sbin/pkg_info'
            header = bytearray(64); header[:7] = b'\x7fELF\x02\x01\x01'
            struct.pack_into('<HH', header, 16, 2, 183)
            tool.write_bytes(header); tool.chmod(0o755)
            selected = host_prefix.load_host_prefix(root, required_roles=('pkg_info',))
            self.assertEqual(selected.tool('pkg_info'), str(tool.resolve()))
            struct.pack_into('<H', header, 18, 62)
            tool.write_bytes(header)
            with self.assertRaisesRegex(SDKError, 'native executable'):
                host_prefix.load_host_prefix(root, required_roles=('pkg_info',))


if __name__ == '__main__':
    unittest.main()
