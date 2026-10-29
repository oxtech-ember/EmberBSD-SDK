# SPDX-License-Identifier: BSD-2-Clause
import unittest
from unittest.mock import patch
import hashlib
import lzma
import os
import tarfile
import subprocess
from pathlib import Path
import tempfile
from types import SimpleNamespace

from test_core import SDK_SOURCE, git_environment  # noqa: F401  # puts the SDK sources on sys.path
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.target_acceptance import (CHECKS, GUEST_CHECKS, HOST_CHECKS,
    _clean_source_revision, _copy_runtime, _copy_test_bundle, _install_diagnostic_runcom,
    _select_os_sources, _require_os_makedev, _compile_tests_command, _extract_base_archive,
    _efi_boot_inputs, _qemu_efi_command, parse_guest_log, run)

class TestBundleTests(unittest.TestCase):
    def test_required_target_options_are_copied(self):
        names = ('target.txt', 'compiler-family.txt', 'runtime-files.tsv',
                 'atomic-tls', 'atomic-tls-lto', 'boundary', 'libboundary.so',
                 'check-runtime.sh', 'target-options.sh', 'run-target.sh',
                 'libc-aliases', 'base-loader-smoke', 'fork-cas',
                 'fork-cas-static', 'libm-valid', 'setjmp-valid')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / 'bundle'
            bundle.mkdir()
            for name in names:
                (bundle / name).write_text(name)
            _copy_test_bundle(bundle, root / 'guest')
            self.assertEqual((root / 'guest/tests/target-options.sh').read_text(), 'target-options.sh')
            (bundle / 'target-options.sh').unlink()
            with self.assertRaises(SDKError):
                _copy_test_bundle(bundle, root / 'missing')


class EFIBootTests(unittest.TestCase):
    def test_selected_firmware_loader_and_qemu_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefix, tree = root / 'prefix', root / 'tree'
            firmware = prefix / 'share/qemu/edk2-aarch64-code.fd'
            loader = tree / 'usr/mdec/bootaa64.efi'
            firmware.parent.mkdir(parents=True)
            loader.parent.mkdir(parents=True)
            firmware.write_bytes(b'firmware')
            loader.write_bytes(b'loader')
            selected, selected_loader = _efi_boot_inputs(prefix, tree)
            self.assertEqual((selected, selected_loader), (firmware.resolve(), loader))
            argv = _qemu_efi_command('/qemu', firmware, '/root.ffs', '/boot.img', '/serial')
            self.assertLess(argv.index('file=/root.ffs,if=none,format=raw,id=root,readonly=on'),
                            argv.index('file=/boot.img,if=none,format=raw,id=boot,readonly=on'))
            self.assertIn('virtio-rng-pci,rng=rng', argv)
            self.assertIn('rng-random,id=rng,filename=/dev/urandom', argv)
            self.assertNotIn('-kernel', argv)
            self.assertEqual(argv[argv.index('-bios') + 1], str(firmware))
            firmware.unlink()
            outside = root / 'outside'
            outside.write_bytes(b'foreign')
            firmware.symlink_to(outside)
            with self.assertRaises(SDKError):
                _efi_boot_inputs(prefix, tree)


class ExtractBaseTests(unittest.TestCase):
    def test_gnu_longlink_and_cleanup(self):
        tar = os.environ.get('EMBER_TEST_NBTAR')
        if not tar:
            self.skipTest('selected nbtar not supplied')
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            root = work / 'root'
            root.mkdir()
            archive = work / 'base.tar.xz'
            target = 'a' * 101
            with tarfile.open(archive, 'w:xz', format=tarfile.GNU_FORMAT) as output:
                entry = tarfile.TarInfo('link')
                entry.type = tarfile.SYMTYPE
                entry.linkname = target
                output.addfile(entry)
            original = archive.read_bytes()
            temporary = work / 'base.uncompressed.tar'
            temporary.write_bytes(b'existing')
            with self.assertRaises(FileExistsError):
                _extract_base_archive(archive, root, work, tar, dict(os.environ), work / 'existing.log')
            self.assertEqual(temporary.read_bytes(), b'existing')
            temporary.unlink()
            _extract_base_archive(archive, root, work, tar, dict(os.environ), work / 'extract.log')
            self.assertEqual(archive.read_bytes(), original)
            self.assertEqual(os.readlink(root / 'link'), target)
            self.assertFalse((work / 'base.uncompressed.tar').exists())
            archive.write_bytes(b'not xz')
            with self.assertRaises(SDKError):
                _extract_base_archive(archive, root, work, tar, dict(os.environ), work / 'bad.log')
            self.assertFalse((work / 'base.uncompressed.tar').exists())
            archive.write_bytes(lzma.compress(b'not tar'))
            with self.assertRaises(SDKError):
                _extract_base_archive(archive, root, work, tar, dict(os.environ), work / 'bad-tar.log')
            self.assertFalse((work / 'base.uncompressed.tar').exists())


PORTS_GUEST_MARKERS = ('target_c11', 'target_cxx20', 'libc_aliases',
                       'base_loader_smoke', 'fork_cas')


def valid_log(nonce='fixture-123'):
    checks = '\n'.join(f'EMBERBSD-CHECK {nonce} {name} PASS'
                       for name in PORTS_GUEST_MARKERS)
    return (f'boot noise\nEMBERBSD-ACCEPTANCE {nonce} ROOT /dev/ld0a ffs ro,local\n'
            f'{checks}\nEMBERBSD-ACCEPTANCE {nonce} COMPLETE\n').encode()


class CompileGitSelectionTests(unittest.TestCase):
    def test_absolute_git_is_passed_without_expanding_tool_path(self):
        native = SimpleNamespace(tool=lambda role: '/native/bin/python')
        options = SimpleNamespace(compiler_family='clang')
        with tempfile.TemporaryDirectory() as temporary:
            git = Path(temporary).resolve() / 'git'
            git.write_text('#!/bin/sh\nexit 0\n')
            git.chmod(0o755)
            with patch('emberbsd_sdk.target_acceptance.shutil.which', return_value=str(git)):
                argv = _compile_tests_command(native, options, Path('/ports/compile-tests.py'),
                    Path('/os'), Path('/compiler'), Path('/sysroot'), Path('/tests'),
                    Path('/sdk'), Path('/native'))
            self.assertEqual(argv[argv.index('--git') + 1], str(git))
            self.assertIn('--host-prefix', argv)

    def test_missing_git_is_reported(self):
        native = SimpleNamespace(tool=lambda role: '/native/bin/python')
        options = SimpleNamespace(compiler_family='clang')
        with patch('emberbsd_sdk.target_acceptance.shutil.which', return_value=None):
            with self.assertRaisesRegex(SDKError, 'Git'):
                _compile_tests_command(native, options, Path('/ports/compile-tests.py'),
                    Path('/os'), Path('/compiler'), Path('/sysroot'), Path('/tests'),
                    Path('/sdk'), Path('/native'))


class MakeDevLocationTests(unittest.TestCase):
    def test_generated_makedev_uses_os_installed_dev_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'dest/dev').mkdir(parents=True)
            (root / 'dest/dev/MAKEDEV').write_text('fixture')
            self.assertEqual(_require_os_makedev(root), root / 'dest/dev/MAKEDEV')
            (root / 'dest/dev/MAKEDEV').unlink()
            (root / 'dest/etc').mkdir()
            (root / 'dest/etc/MAKEDEV').write_text('wrong location')
            with self.assertRaisesRegex(SDKError, 'MAKEDEV'):
                _require_os_makedev(root)


class TargetAcceptanceProtocolTests(unittest.TestCase):
    def test_v2_requires_math_and_setjmp_markers(self):
        old = valid_log()
        with self.assertRaises(SDKError):
            parse_guest_log(old, 'fixture-123', 'ld0a', corpus='target-runtime-v2')
        extended = old.replace(b'EMBERBSD-ACCEPTANCE fixture-123 COMPLETE',
            b'EMBERBSD-CHECK fixture-123 libm_valid PASS\n'
            b'EMBERBSD-CHECK fixture-123 setjmp_valid PASS\n'
            b'EMBERBSD-ACCEPTANCE fixture-123 COMPLETE')
        result = parse_guest_log(extended, 'fixture-123', 'ld0a', corpus='target-runtime-v2')
        self.assertTrue(result['libm_valid'] and result['setjmp_valid'])
        for name in ('libm_valid', 'setjmp_valid'):
            missing = extended.replace(f'EMBERBSD-CHECK fixture-123 {name} PASS\n'.encode(), b'')
            with self.assertRaises(SDKError):
                parse_guest_log(missing, 'fixture-123', 'ld0a', corpus='target-runtime-v2')

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


class SourceRoleTests(unittest.TestCase):
    def test_run_reaches_profile_validation_after_output_path_checks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = {}
            for name in ('os_source', 'os_test_source', 'ports_source', 'os_build',
                         'sysroot', 'target_prefix', 'host_prefix'):
                path = root / name
                path.mkdir()
                inputs[name] = str(path)
            sdk_source = Path(__file__).resolve().parents[1]
            options = SimpleNamespace(
                sdk_source=str(sdk_source), output=str(root / 'acceptance-output'),
                compiler_family='clang', **inputs)
            with self.assertRaisesRegex(SDKError, 'expected a regular file'):
                run(options)
            self.assertFalse(Path(options.output).exists())


    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def make_source(self, name, include_tests=True):
        source = self.root / name
        source.mkdir()
        subprocess.run(['git', '-C', str(source), 'init', '-q'], check=True, env=git_environment())
        subprocess.run(['git', '-C', str(source), 'config', 'user.name', 'Fixture'], check=True, env=git_environment())
        subprocess.run(['git', '-C', str(source), 'config', 'user.email', 'fixture@example.invalid'], check=True, env=git_environment())
        (source / 'README').write_text(name + '\n')
        if include_tests:
            for relative in (
                    'ember/tests/target-acceptance/libc-aliases.c',
                    'ember/tests/target-acceptance/base-loader-smoke.c',
                    'ember/tests/target-acceptance/fork-cas.c',
                    'ember/tests/target-acceptance/libm-valid.c',
                    'ember/tests/target-acceptance/setjmp-valid.c',
                    'ember/image/target-acceptance-rc',
                    'ember/image/target-acceptance-root.sh'):
                path = source / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('fixture\n')
        subprocess.run(['git', '-C', str(source), 'add', '.'], check=True, env=git_environment())
        subprocess.run(['git', '-C', str(source), 'commit', '-qm', 'fixture'], check=True, env=git_environment())
        revision = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True, env=git_environment()).strip()
        return source, revision

    def test_new_test_snapshot_does_not_change_core_build_identity(self):
        core, core_revision = self.make_source('core')
        tests, tests_revision = self.make_source('tests')
        build_revision, test_revision = _select_os_sources(core_revision, core, tests)
        self.assertEqual(build_revision, core_revision)
        self.assertEqual(test_revision, tests_revision)
        self.assertNotEqual(build_revision, test_revision)

    def test_rejects_core_source_that_does_not_match_build(self):
        core, _ = self.make_source('core')
        tests, tests_revision = self.make_source('tests')
        with self.assertRaisesRegex(SDKError, 'differs from the selected OS build'):
            _select_os_sources('0' * 40, core, tests)
        self.assertEqual(len(tests_revision), 40)

    def test_rejects_missing_helper_before_acceptance_side_effects(self):
        core, core_revision = self.make_source('core')
        tests, _ = self.make_source('tests', include_tests=False)
        output = self.root / 'acceptance-output'
        with self.assertRaisesRegex(SDKError, 'missing required input'):
            _select_os_sources(core_revision, core, tests)
        self.assertFalse(output.exists())

    def test_rejects_modified_source_snapshot(self):
        core, core_revision = self.make_source('core')
        tests, _ = self.make_source('tests')
        (tests / 'README').write_text('modified\n')
        with self.assertRaisesRegex(SDKError, 'not a complete clean Git root'):
            _select_os_sources(core_revision, core, tests)

    def test_ports_source_accepts_only_the_existing_pkgsrc_gitlink(self):
        ports, revision = self.make_source('ports-with-gitlink')
        (ports / 'upstream/pkgsrc').mkdir(parents=True)
        subprocess.run(['git', '-C', str(ports), 'update-index', '--add', '--cacheinfo',
                        f'160000,{revision},upstream/pkgsrc'], check=True, env=git_environment())
        subprocess.run(['git', '-C', str(ports), 'commit', '-qm', 'Ports pin'],
                       check=True, env=git_environment())
        expected = subprocess.check_output(['git', '-C', str(ports), 'rev-parse', 'HEAD'],
                                           env=git_environment(), text=True).strip()
        self.assertEqual(_clean_source_revision(ports, 'Ports producer', kind='ports'), expected)
        with self.assertRaises(SDKError):
            _clean_source_revision(ports, 'SDK controller')
        (ports / 'unexpected').mkdir()
        subprocess.run(['git', '-C', str(ports), 'update-index', '--add', '--cacheinfo',
                        f'160000,{revision},unexpected'], check=True, env=git_environment())
        subprocess.run(['git', '-C', str(ports), 'commit', '-qm', 'Unexpected pin'],
                       check=True, env=git_environment())
        with self.assertRaises(SDKError):
            _clean_source_revision(ports, 'Ports producer', kind='ports')

    def test_rejects_dirty_sdk_or_ports_producer(self):
        for role in ('SDK controller', 'Ports producer'):
            with self.subTest(role=role):
                source, _ = self.make_source(role.replace(' ', '-'))
                (source / 'README').write_text('uncommitted producer change\n')
                with self.assertRaisesRegex(SDKError, 'not a complete clean Git root'):
                    _clean_source_revision(source, role)


class DiagnosticRuncomTests(unittest.TestCase):
    def test_installs_selected_diagnostic_rc_as_boot_entrypoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source'
            (source / 'ember/image').mkdir(parents=True)
            runcom = source / 'ember/image/target-acceptance-rc'
            runcom.write_text('#!/bin/sh\n/sbin/halt -p -q\n')
            guest = root / 'guest'
            (guest / 'etc').mkdir(parents=True)
            _install_diagnostic_runcom(source, guest)
            installed = guest / 'etc/rc'
            self.assertEqual(installed.read_bytes(), runcom.read_bytes())
            self.assertTrue(installed.stat().st_mode & 0o111)
            self.assertFalse((guest / 'etc/rc.local').exists())

    def test_refuses_base_runcom_collision(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source'
            (source / 'ember/image').mkdir(parents=True)
            (source / 'ember/image/target-acceptance-rc').write_text('#!/bin/sh\n')
            guest = root / 'guest'
            (guest / 'etc').mkdir(parents=True)
            (guest / 'etc/rc').symlink_to('/tmp/external-rc')
            with self.assertRaisesRegex(SDKError, 'base set unexpectedly contains /etc/rc'):
                _install_diagnostic_runcom(source, guest)


if __name__ == '__main__':
    unittest.main()
