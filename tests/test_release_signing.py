#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Real OpenSSL/pkg_tools interoperability, selected explicitly from Ports."""

import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.host_prefix import load_host_prefix
from emberbsd_sdk.release_signing import ReleaseCrypto
from emberbsd_sdk import release_cli
from emberbsd_sdk.jsonio import json_bytes


class ReleasePathTests(unittest.TestCase):
    def test_parent_alias_added_during_sealing_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix='release-path-') as temporary:
            root = Path(temporary)
            (root / 'payloads').mkdir()
            (root / 'payloads/data').write_bytes(b'payload')
            draft = {'schema': 'emberbsd.release.v1', 'release_id': 'fixture', 'created_at': 1,
                'sources': {name: 'a' * 40 for name in ('os', 'ports', 'sdk')},
                'artifacts': [{'id': 'bootstrap', 'path': 'payloads/data', 'kind': 'bootstrap',
                    'host': {'os': 'Darwin', 'arch': 'arm64', 'abi': 'macos-15.0', 'runtime': 'libSystem'},
                    'target': None, 'unpacked_size': 7, 'depends': [], 'licenses': ['bsd-2-clause']}]}

            class MutatingSigner:
                def sign_cms(self, data, identity, trust):
                    (root / 'payloads').rename(root / 'moved')
                    (root / 'payloads').symlink_to(root / 'moved', target_is_directory=True)
                    return b'fixture signature'

            output = root / 'release.p7m'
            with self.assertRaises(SDKError):
                release_cli.seal_release(MutatingSigner(), json_bytes(draft), root, None, None, output)
            self.assertFalse(output.exists())


@unittest.skipUnless(os.environ.get('EMBERBSD_TEST_HOST_PREFIX'),
                     'set EMBERBSD_TEST_HOST_PREFIX to run real signing contracts')
class ReleaseSigningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='release-signing-')
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.root = Path(cls.tmp.name)
        cls.crypto = ReleaseCrypto(load_host_prefix(os.environ['EMBERBSD_TEST_HOST_PREFIX'],
            required_roles=('openssl', 'pkg_admin', 'pkg_create')))
        cls.ca = cls.crypto.create_root(cls.root / 'root')
        cls.other = cls.crypto.create_root(cls.root / 'other')
        cls.leaf = cls.crypto.issue_signer(cls.ca, cls.root / 'leaf')
        cls.payload = b'{"exact": "bytes", "utf8": "\xd0\x94"}\n'

    def test_cms_round_trip_and_native_verifier(self):
        signed = self.crypto.sign_cms(self.payload, self.leaf, self.ca.certificate)
        self.assertEqual(self.crypto.verify_cms(signed, self.ca.certificate), self.payload)
        # OpenSSL is also the independent native protocol consumer, not a JSON re-encoder.
        verified = self.crypto.run(['cms', '-verify', '-binary', '-inform', 'DER',
            '-CAfile', str(self.ca.certificate), '-no-CApath', '-no-CAstore',
            '-purpose', 'smimesign'], data=signed)
        self.assertEqual(verified, self.payload)

    def test_cms_rejects_changed_unsigned_and_untrusted(self):
        signed = self.crypto.sign_cms(self.payload, self.leaf, self.ca.certificate)
        changed = bytearray(signed)
        changed[-1] ^= 1
        for data, root in ((bytes(changed), self.ca.certificate),
                           (self.payload, self.ca.certificate),
                           (signed, self.other.certificate)):
            with self.subTest(root=root, data_size=len(data)), self.assertRaises(SDKError):
                self.crypto.verify_cms(data, root)

    def test_certificates_are_time_checked(self):
        signed = self.crypto.sign_cms(self.payload, self.leaf, self.ca.certificate)
        for instant in (int(time.time()) - 86400, int(time.time()) + 400 * 86400):
            with self.subTest(instant=instant), self.assertRaisesRegex(SDKError, 'clock|certificate'):
                self.crypto.verify_cms(signed, self.ca.certificate, now=instant)

    def test_leaf_rotation_preserves_root(self):
        rotated = self.crypto.issue_signer(self.ca, self.root / 'rotated')
        signed = self.crypto.sign_cms(self.payload, rotated, self.ca.certificate)
        self.assertEqual(self.crypto.verify_cms(signed, self.ca.certificate), self.payload)

    def test_root_is_not_a_release_signer(self):
        with self.assertRaises(SDKError):
            self.crypto.sign_cms(self.payload, self.ca, self.ca.certificate)

    def test_complete_release_channel_and_offline_recovery(self):
        repo = self.root / 'repository'
        payloads = repo / 'fixture-1'
        payloads.mkdir(parents=True)
        (payloads / 'bootstrap.tar').write_bytes(b'release bootstrap fixture')
        draft = {'schema': 'emberbsd.release.v1', 'release_id': 'fixture-1',
            'created_at': int(time.time()), 'sources': {name: 'a' * 40 for name in ('os', 'ports', 'sdk')},
            'artifacts': [{'id': 'bootstrap', 'path': 'bootstrap.tar', 'kind': 'bootstrap',
                'host': {'os': 'Darwin', 'arch': 'arm64', 'abi': 'macos-15.0',
                         'runtime': 'libSystem-libcxx'}, 'target': None,
                'unpacked_size': len(b'release bootstrap fixture'),
                'depends': [], 'licenses': ['bsd-2-clause']}]}
        release_path = payloads / 'release.p7m'
        release_cli.seal_release(self.crypto, json_bytes(draft), payloads, self.leaf,
                                 self.ca.certificate, release_path)
        now = int(time.time())
        channel = {'schema': 'emberbsd.channel.v1', 'channel': 'development', 'generation': 2,
            'issued_at': now, 'expires_at': now + 3600,
            'release': {'id': 'fixture-1', 'path': 'fixture-1/release.p7m'}}
        pointer = repo / 'development.p7m'
        release_cli.seal_channel(self.crypto, json_bytes(channel), repo, self.leaf,
                                 self.ca.certificate, pointer)
        state = repo / 'accepted.json'
        verified = release_cli.verify_channel(self.crypto, pointer.read_bytes(), repo,
                                              self.ca.certificate, 'development', state)
        self.assertEqual(verified['release_id'], 'fixture-1')
        before = state.read_bytes()
        # A newer signed pointer with a bad release envelope must not advance the floor.
        channel['generation'] = 3
        newer = repo / 'development-new.p7m'
        release_cli.seal_channel(self.crypto, json_bytes(channel), repo, self.leaf,
                                 self.ca.certificate, newer)
        original_envelope = release_path.read_bytes()
        release_path.write_bytes(original_envelope + b'corruption')
        with self.assertRaises(SDKError):
            release_cli.verify_channel(self.crypto, newer.read_bytes(), repo,
                                       self.ca.certificate, 'development', state)
        self.assertEqual(state.read_bytes(), before)
        release_path.write_bytes(original_envelope)
        # Catalog verification needs only metadata, so clients can fetch a subset.
        (payloads / 'bootstrap.tar').unlink()
        release_cli.verify_channel(self.crypto, pointer.read_bytes(), repo,
                                   self.ca.certificate, 'development', state)
        (payloads / 'bootstrap.tar').write_bytes(b'corrupted bootstrap fixture')
        with self.assertRaises(SDKError):
            release_cli.verify_release(self.crypto, original_envelope, payloads, self.ca.certificate)
        self.assertEqual(state.read_bytes(), before)
        (payloads / 'bootstrap.tar').write_bytes(b'release bootstrap fixture')
        # Offline restore validates the signed release directly and cannot reset channel state.
        restored = release_cli.verify_release(self.crypto, release_path.read_bytes(), payloads,
                                             self.ca.certificate)
        self.assertEqual(restored, verified)
        self.assertEqual(state.read_bytes(), before)

    def test_private_material_permissions_and_no_overwrite(self):
        for identity in (self.ca, self.leaf):
            self.assertEqual(identity.key.stat().st_mode & 0o777, 0o600)
            self.assertEqual(identity.key.parent.stat().st_mode & 0o777, 0o700)
        before = self.ca.key.read_bytes()
        with self.assertRaises((OSError, SDKError)):
            self.crypto.create_root(self.ca.key.parent)
        self.assertEqual(before, self.ca.key.read_bytes())

    def _package(self, name):
        work = self.root / name
        work.mkdir()
        prefix = work / 'prefix'
        prefix.mkdir()
        (prefix / 'hello').write_text('EmberBSD signature fixture\n')
        plist = work / 'PLIST'
        plist.write_text('@name ember-signature-test-1.0\nhello\n')
        unsigned = work / 'unsigned.tgz'
        self.crypto.run_package('pkg_create', ['-c', '-fixture comment', '-d', '-fixture description',
            '-f', str(plist), '-p', str(prefix), str(unsigned)])
        return work, unsigned

    def test_pkg_tools_signed_package(self):
        work, unsigned = self._package('package')
        signed = work / 'signed.tgz'
        self.crypto.sign_package(unsigned, signed, self.leaf, self.ca.certificate)
        self.crypto.verify_package(signed, self.ca.certificate)
        for package, root in ((unsigned, self.ca.certificate), (signed, self.other.certificate),
                              (work / 'missing.tgz', self.ca.certificate)):
            with self.subTest(package=package, root=root), self.assertRaises(SDKError):
                self.crypto.verify_package(package, root)
        changed = work / 'changed.tgz'
        data = bytearray(signed.read_bytes())
        # pkg_tools wraps the unchanged inner package in ar. Locate our known
        # fixture bytes rather than modifying unsigned container padding.
        original = unsigned.read_bytes()
        offset = data.find(original)
        self.assertGreaterEqual(offset, 0)
        self.assertEqual(data.find(original, offset + 1), -1)
        data[offset + len(original) // 2] ^= 1
        changed.write_bytes(data)
        with self.assertRaises(SDKError):
            self.crypto.verify_package(changed, self.ca.certificate)

    def test_pkg_tools_rejects_expired_and_future_leaf(self):
        work, unsigned = self._package('package-dates')
        database = work / 'index'
        database.write_bytes(b'')
        serial = work / 'serial'
        serial.write_text('01\n')
        config = work / 'ca.cnf'
        config.write_text(f'[ca]\ndefault_ca=issuer\n[issuer]\ndatabase={database}\n'
            f'serial={serial}\nnew_certs_dir={work}\ndefault_md=sha256\ndefault_days=365\n'
            'policy=policy\nx509_extensions=leaf\nunique_subject=no\n[policy]\ncommonName=supplied\n'
            '[leaf]\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n'
            'extendedKeyUsage=codeSigning,emailProtection\nsubjectKeyIdentifier=hash\n'
            'authorityKeyIdentifier=keyid,issuer\n')
        request = work / 'request.pem'
        self.crypto.run(['req', '-new', '-key', self.leaf.key, '-out', request,
                         '-subj', '/CN=EmberBSD Invalid Time Fixture'])
        for name, start, end in (('expired', '20000101000000Z', '20010101000000Z'),
                                 ('future', '20990101000000Z', '21000101000000Z')):
            certificate = work / (name + '.pem')
            self.crypto.run(['ca', '-batch', '-notext', '-config', config,
                '-cert', self.ca.certificate, '-keyfile', self.ca.key, '-in', request,
                '-out', certificate, '-startdate', start, '-enddate', end])
            signed = work / (name + '.tgz')
            self.crypto.run_package('pkg_admin', ['x509-sign-package', unsigned, signed,
                                                 self.leaf.key, certificate])
            with self.subTest(name=name), self.assertRaises(SDKError):
                self.crypto.verify_package(signed, self.ca.certificate)


if __name__ == '__main__':
    unittest.main()
