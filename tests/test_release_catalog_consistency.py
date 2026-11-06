# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Causal integration coverage for release seal/verify/sparse catalog checks."""
from copy import deepcopy
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import test_binary_catalog as fixtures
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.jsonio import json_bytes
from emberbsd_sdk.package_relocation import INDEPENDENT
from emberbsd_sdk.release_catalog import parse_release
from emberbsd_sdk.release_cli import seal_release, verify_release
from emberbsd_sdk.release_delivery import publish_release

@unittest.skipUnless(os.environ.get('EMBERBSD_TEST_HOST_PREFIX'), 'select native Ports pkg_tools')
class CatalogReleaseGuard(unittest.TestCase):
    setUpClass = classmethod(fixtures.BinaryReleaseTests.setUpClass.__func__)
    setUp = fixtures.BinaryReleaseTests.setUp
    package = fixtures.BinaryReleaseTests.package
    compose = fixtures.BinaryReleaseTests.compose
    def signed_fixture(self):
        package = self.package('devel/generator', opsys='Darwin', marker=INDEPENDENT+'\n')
        self.spec['repositories'].append(dict(id='host', host=dict(os='Darwin', arch='arm64',
            abi='macos-27.0', runtime='libSystem'), target=None, graph=str(self.graph_path),
            sets=['kernel'], packages={'host:devel/generator':package}))
        self.compose()
        directory = self.work / 'release'
        release = parse_release(self.crypto.verify_cms((directory/'release.p7m').read_bytes(), self.ca.certificate))
        return directory, release

    @staticmethod
    def bad_release(release):
        bad = deepcopy(release)
        for artifact in bad['artifacts']:
            if artifact['kind'] == 'package-index':
                artifact['host']['abi'] = 'macos-27'
        return bad

    def test_causal_good_seal_and_verify(self):
        directory, release = self.signed_fixture()
        self.assertEqual(verify_release(self.crypto, (directory/'release.p7m').read_bytes(), directory,
                                        self.ca.certificate), release)
        output = directory / 'resealed.p7m'
        seal_release(self.crypto, json_bytes(release), directory, self.leaf, self.ca.certificate, output)
        self.assertTrue(output.exists())

    def test_causal_mismatch_seal(self):
        directory, release = self.signed_fixture()
        bad = self.bad_release(release)
        output = directory / 'bad-seal.p7m'
        with self.assertRaises(SDKError):
            seal_release(self.crypto, json_bytes(bad), directory, self.leaf, self.ca.certificate, output)
        self.assertFalse(output.exists())
    def test_causal_mismatch_verify(self):
        directory, release = self.signed_fixture()
        envelope = self.crypto.sign_cms(json_bytes(self.bad_release(release)), self.leaf, self.ca.certificate)
        with self.assertRaises(SDKError):
            verify_release(self.crypto, envelope, directory, self.ca.certificate)

    def test_causal_signed_bad_sparse_publish_before_any_store_mutation(self):
        directory, release = self.signed_fixture()
        bad = self.bad_release(release)
        envelope = self.crypto.sign_cms(json_bytes(bad), self.leaf, self.ca.certificate)
        bad_path = directory / 'bad-sparse.p7m'
        bad_path.write_bytes(envelope)
        store, public = MagicMock(), MagicMock()
        store.put.side_effect = AssertionError("store mutated before catalog validation")
        with self.assertRaises(SDKError):
            publish_release(self.crypto, store, public, bad_path, self.leaf, self.ca.certificate,
                            expected_current='a' * 64, reuse_current=True)
        store.put.assert_not_called()
        store.delete.assert_not_called()

if __name__ == "__main__":
    unittest.main()
