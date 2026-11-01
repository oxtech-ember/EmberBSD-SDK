# SPDX-License-Identifier: BSD-2-Clause
"""Select OpenSSH's configure tools only when its common source is used."""
import unittest
from unittest import mock

from test_core import SDK_SOURCE  # noqa: F401
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.sysroot_builder import SysrootBuilder

V1_CONTRACT = b'EmberBSD-native-tools 1\ncmp cmp -\nmake make MAKE\nreadlink readlink -\nsh sh HOST_SH\n'


class OpenSSHNativeToolsTests(unittest.TestCase):
    def contract(self, data):
        return mock.patch('emberbsd_sdk.sysroot_builder.native_os_routing.read_native_os_contract',
                          return_value=(data, None, {'make'}))

    def test_roles_are_scoped_to_selected_openssh(self):
        builder = SysrootBuilder.__new__(SysrootBuilder)
        builder.options = {'host-prefix': '/native', 'os': '/os'}
        with mock.patch('emberbsd_sdk.sysroot_builder.inventory.sha256_file', return_value='a' * 64), \
             mock.patch('emberbsd_sdk.sysroot_builder.native_os_routing.read_native_os_contract',
                        return_value=(b'EmberBSD-native-tools 2\n', None, {'make'})), \
             mock.patch('emberbsd_sdk.sysroot_builder.native_toolset.load_host_prefix') as load:
            builder.load_native_toolset()
            self.assertTrue({'autoconf', 'autom4te', 'gm4'}.isdisjoint(load.call_args.kwargs['required_roles']))
            builder.options['openssh-source'] = '/prepared/openssh'
            builder.load_native_toolset()
            self.assertTrue({'autoconf', 'autom4te', 'gm4', 'perl', 'gmake', 'python'} <=
                            load.call_args.kwargs['required_roles'])

    def test_contract_without_host_abi_support_is_refused_before_selection(self):
        builder = SysrootBuilder.__new__(SysrootBuilder)
        builder.options = {'host-prefix': '/native', 'os': '/os', 'openssh-source': '/prepared/openssh'}
        for data in (V1_CONTRACT, None):
            with self.subTest(data=data), \
                 mock.patch('emberbsd_sdk.sysroot_builder.inventory.sha256_file', return_value='a' * 64), \
                 self.contract(data), \
                 mock.patch('emberbsd_sdk.sysroot_builder.native_toolset.load_host_prefix') as load, \
                 self.assertRaisesRegex(SDKError, 'lacks separate host ABI'):
                builder.load_native_toolset()
            load.assert_not_called()


if __name__ == '__main__':
    unittest.main()
