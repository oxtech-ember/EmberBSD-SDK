# SPDX-License-Identifier: BSD-2-Clause
import unittest
import hashlib
from pathlib import Path
import tempfile

from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.target_acceptance import (CHECKS, GUEST_CHECKS, HOST_CHECKS,
    _copy_runtime, parse_guest_log)

PORTS_GUEST_MARKERS = ('target_c11', 'target_cxx20', 'libc_aliases',
                       'base_loader_smoke', 'fork_cas')


def valid_log(nonce='fixture-123'):
    checks = '\n'.join(f'EMBERBSD-CHECK {nonce} {name} PASS'
                       for name in PORTS_GUEST_MARKERS)
    return (f'boot noise\nEMBERBSD-ACCEPTANCE {nonce} ROOT /dev/ld0a ffs ro,local\n'
            f'{checks}\nEMBERBSD-ACCEPTANCE {nonce} COMPLETE\n').encode()


class TargetAcceptanceProtocolTests(unittest.TestCase):
    def test_accepts_complete_nonce_bound_readonly_guest_run(self):
        self.assertEqual(set(GUEST_CHECKS), set(PORTS_GUEST_MARKERS))
        result = parse_guest_log(valid_log(), 'fixture-123', 'ld0a')
        self.assertEqual(set(result), set(GUEST_CHECKS))
        self.assertTrue(all(result.values()))
        compile_receipt = {name: True for name in HOST_CHECKS}
        self.assertEqual(set({**compile_receipt, **result}), set(CHECKS))

    def test_rejects_wrong_nonce_and_missing_checks(self):
        with self.assertRaisesRegex(SDKError, 'another run'):
            parse_guest_log(valid_log('other-run'), 'fixture-123', 'ld0a')
        lines = valid_log().decode().splitlines()
        lines.remove(f'EMBERBSD-CHECK fixture-123 {GUEST_CHECKS[0]} PASS')
        with self.assertRaisesRegex(SDKError, 'every required'):
            parse_guest_log(('\n'.join(lines) + '\n').encode(), 'fixture-123', 'ld0a')

    def test_rejects_failed_duplicate_and_unknown_checks(self):
        cases = (
            valid_log().replace(b' PASS', b' FAIL', 1),
            valid_log() + f'EMBERBSD-CHECK fixture-123 {GUEST_CHECKS[0]} PASS\n'.encode(),
            valid_log() + b'EMBERBSD-CHECK fixture-123 guessed PASS\n',
            valid_log() + b'EMBERBSD-CHECK fixture-123 cross_c11 PASS\n',
            valid_log().replace(b'EMBERBSD-ACCEPTANCE fixture-123 COMPLETE',
                                 b'EMBERBSD-ACCEPTANCE other-run FAIL failure\n'
                                 b'EMBERBSD-ACCEPTANCE fixture-123 COMPLETE'),
        )
        for data in cases:
            with self.subTest(data=data[-80:]), self.assertRaises(SDKError):
                parse_guest_log(data, 'fixture-123', 'ld0a')


class RuntimePlacementTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.sysroot = self.root / 'sysroot'
        self.bundle = self.root / 'bundle'
        self.guest = self.root / 'guest'
        self.bundle.mkdir()
        self.guest.mkdir()

    def write_manifest(self, family, roles):
        rows = []
        for role, guest_path, content in roles:
            source = self.sysroot / guest_path.lstrip('/')
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(content)
            rows.append(f'{role}\t{guest_path}\t{hashlib.sha256(content).hexdigest()}\n')
        (self.bundle / 'runtime-files.tsv').write_text(''.join(rows))
        return roles

    def test_clang_runtime_must_match_existing_base_files(self):
        roles = self.write_manifest('clang', [
            ('libcxx', '/usr/pkg/llvm/lib/libc++.so.1.0', b'cxx'),
            ('libcxxabi', '/usr/pkg/llvm/lib/libc++abi.so.1.0', b'abi'),
            ('libunwind', '/usr/pkg/llvm/lib/libunwind.so.1.0', b'unwind')])
        for _, path, content in roles:
            target = self.guest / path.lstrip('/')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        _copy_runtime(self.sysroot, self.bundle, self.guest, 'clang')
        with self.assertRaisesRegex(SDKError, 'base set Clang runtime'):
            (self.guest / 'usr/pkg/llvm/lib/libc++.so.1.0').write_bytes(b'wrong')
            _copy_runtime(self.sysroot, self.bundle, self.guest, 'clang')

    def test_gcc_runtime_is_copied_from_the_verified_sysroot(self):
        roles = self.write_manifest('gcc', [
            ('libstdc++', '/usr/pkg/gcc16/lib/libstdc++.so.7', b'stdcxx'),
            ('libgcc_s', '/usr/pkg/gcc16/lib/libgcc_s.so.1', b'gcc_s')])
        _copy_runtime(self.sysroot, self.bundle, self.guest, 'gcc')
        for _, path, content in roles:
            self.assertEqual((self.guest / path.lstrip('/')).read_bytes(), content)

    def test_rejects_rw_or_wrong_root(self):
        rw = valid_log().replace(b' ffs ro,local', b' ffs rw,local')
        wrong_fs = valid_log().replace(b' ffs ro,local', b' nfs ro,local')
        for data in (rw, wrong_fs):
            with self.subTest(data=data[:120]), self.assertRaisesRegex(SDKError, 'read-only FFS'):
                parse_guest_log(data, 'fixture-123', 'ld0a')

    def test_rejects_a_different_root_unit(self):
        data = valid_log().replace(b'/dev/ld0a', b'/dev/ld1a')
        with self.assertRaisesRegex(SDKError, 'selected read-only FFS'):
            parse_guest_log(data, 'fixture-123', 'ld0a')


if __name__ == '__main__':
    unittest.main()
