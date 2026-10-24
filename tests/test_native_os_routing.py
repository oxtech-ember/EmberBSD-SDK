# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Check mapping publication. Synthetic members are never executed."""

import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.native_os_routing import (publish_native_os_routing,
                                            read_native_os_contract)
from emberbsd_sdk.native_toolset import NativeToolset


CONTRACT = (b'EmberBSD-native-tools 1\n'
            b'cc cc HOST_CC\ncmp cmp -\nmake make MAKE\n'
            b'readlink readlink -\nsh sh HOST_SH\n')


class NativeOSRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sdk-os-routing-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.bundle = self.root / 'bundle'
        self.bundle.mkdir()
        self.contract = self.root / 'os-contract'
        self.contract.write_bytes(CONTRACT)
        self.sha = hashlib.sha256(CONTRACT).hexdigest()
        tools = {}
        for role in ('cc', 'cmp', 'make', 'readlink', 'sh'):
            member = self.bundle / role
            member.write_bytes(b'Never execute this synthetic member.\n')
            member.chmod(0o755)
            tools[role] = str(member)
        alias = self.bundle / 'selected-cc'
        alias.symlink_to('cc')
        tools['cc'] = str(alias)
        # Only the emitter is under test. This fixture does not claim that its
        # synthetic executables passed load_native_toolset qualification.
        self.toolset = NativeToolset('a' * 64, {}, str(self.bundle), {}, {}, tools, {})

    def publish(self, **overrides):
        args = dict(contract_path=str(self.contract), contract_sha256=self.sha,
                    destination=str(self.root / 'routing'))
        args.update(overrides)
        return publish_native_os_routing(self.toolset, **args)

    def test_publish_preserves_selected_alias_and_contract(self):
        result = self.publish()
        output = self.root / 'routing'
        self.assertEqual((output / 'contract').read_bytes(), CONTRACT)
        self.assertFalse((output / 'contract').is_symlink())
        self.assertEqual(os.readlink(output / 'cc'), self.toolset.tool('cc'))
        self.assertEqual(set(p.name for p in output.iterdir()),
                         {'contract', 'cc', 'cmp', 'make', 'readlink', 'sh'})
        self.assertFalse(result['production_build_admitted'])
        self.assertEqual(result['native_toolset_sha256'], 'a' * 64)

    def test_existing_destination_is_preserved(self):
        output = self.root / 'routing'
        output.mkdir()
        (output / 'sentinel').write_bytes(b'keep')
        with self.assertRaises(OSError):
            self.publish()
        self.assertEqual((output / 'sentinel').read_bytes(), b'keep')
        self.assertEqual(list(self.root.glob('.native-os-tools-*')), [])

    def test_publication_failure_cleans_only_owned_stage(self):
        sentinel = self.root / '.native-os-tools-foreign'
        sentinel.mkdir()
        with patch('emberbsd_sdk.native_os_routing.publish_directory_noreplace',
                   side_effect=OSError('refused')):
            with self.assertRaises(OSError):
                self.publish()
        self.assertTrue(sentinel.is_dir())
        self.assertEqual(list(self.root.glob('.native-os-tools-*')), [sentinel])

    def test_seed_or_unqualified_mapping_object_is_refused(self):
        with self.assertRaisesRegex(SDKError, 'ordinary NativeToolset'):
            publish_native_os_routing(object(), contract_path=str(self.contract),
                contract_sha256=self.sha, destination=str(self.root / 'routing'))

    def test_missing_role_and_changed_member_are_refused_before_writes(self):
        del self.toolset.tools['cc']
        with self.assertRaisesRegex(SDKError, 'not selected'):
            self.publish()
        self.assertFalse((self.root / 'routing').exists())
        self.toolset.tools['cc'] = str(self.bundle / 'absent')
        with self.assertRaisesRegex(SDKError, 'no longer executable'):
            self.publish()

    def test_unsafe_or_overlapping_destination_is_refused(self):
        for destination in ('relative', str(self.root / 'unsafe root'),
                            str(self.root / 'bad:root'), str(self.bundle / 'routing')):
            with self.subTest(destination=destination), self.assertRaises(SDKError):
                self.publish(destination=destination)

    def test_contract_hash_and_symlink_are_refused(self):
        with self.assertRaisesRegex(SDKError, 'changed'):
            self.publish(contract_sha256='0' * 64)
        alias = self.root / 'contract-alias'
        alias.symlink_to(self.contract)
        with self.assertRaises(SDKError):
            self.publish(contract_path=str(alias))

    def test_malformed_contracts_are_refused(self):
        for data in (CONTRACT.replace(b'tools 1', b'tools 2'),
                     CONTRACT + b'cc cc -\n', CONTRACT + b'evil cc HOST_CC\n',
                     CONTRACT + b'../cc cc -\n', CONTRACT.replace(b'sh sh', b'sh  sh'),
                     CONTRACT.replace(b'make make MAKE\n', b'')):
            with self.subTest(data=data):
                self.contract.write_bytes(data)
                with self.assertRaises(SDKError):
                    read_native_os_contract(self.contract,
                        sha256=hashlib.sha256(data).hexdigest())


if __name__ == '__main__':
    unittest.main()
