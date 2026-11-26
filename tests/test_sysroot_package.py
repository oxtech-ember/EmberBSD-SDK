# SPDX-License-Identifier: BSD-2-Clause
"""Delivery fixtures test transfer and selection, not real target execution."""

import copy
import fcntl
import hashlib
import io
import os
from pathlib import Path
import sys
import tarfile
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from test_core import TemporaryCase
import test_llvm_builder as llvm_tests
from emberbsd_sdk import application_toolchain, jsonio, prefix_lock, sysroot_package, sysroot_transfer, sysroot_update
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.package_cache import PackageCache
from emberbsd_sdk.sdk_client import include_sysroot
from test_package_cache import Store


class SysrootPackageTests(TemporaryCase):
    def setUp(self):
        super().setUp()
        builder, _ = llvm_tests.LLVMBuilderTests.builder(self)
        builder.run()
        self.source = Path(builder.paths['destination'])
        self.bundle = self.root / 'bundle'
        transfer = sysroot_transfer.export_sysroot(self.source, self.bundle, target='aarch64')
        self.archive = self.root / 'sysroot.tar'
        result = sysroot_package.package(self.bundle, self.archive, target='aarch64',
                                         manifest_sha256=transfer['manifest_sha256'])
        self.target = dict(arch='aarch64', triplet='aarch64--netbsd', abi='test', runtime='llvm')
        self.artifact = dict(id='sysroot', kind='sysroot', host=None, target=self.target, depends=[],
            path='sysroot.tar', sha256=hashlib.sha256(self.archive.read_bytes()).hexdigest(),
            size=self.archive.stat().st_size, unpacked_size=result['unpacked_size'])
        self.release = dict(artifacts=[self.artifact])
        self.config = dict(host_prefix=str(self.root / 'installed'), repositories={})
        self.cache = PackageCache(self.root / 'cache')

    def select(self):
        return sysroot_package.selection(self.release, self.config, self.cache, 'aarch64')

    def test_real_transfer_roundtrip_and_zero_download_repeat(self):
        selected = self.select()
        self.assertEqual(selected['action'], 'download')
        sysroot_package.check(self.archive, selected)
        sysroot_package.install(self.archive, selected)
        runtime = application_toolchain.load_clang_runtime(selected['destination'], target='aarch64')
        self.assertEqual(runtime['llvm_version'], '23.1.4')
        self.assertEqual(self.select()['action'], 'installed')
        plan = include_sysroot(dict(download_size=0, unpacked_size=0), self.release, self.config,
                               self.cache, 'aarch64')
        self.assertEqual((plan['download_size'], plan['unpacked_size']), (0, 0))
        # Both target files and the original producer remain available.
        self.assertEqual((Path(selected['destination']) / 'usr/include/stdio.h').read_bytes(),
                         (self.source / 'usr/include/stdio.h').read_bytes())

    def test_existing_unknown_or_superseded_sysroot_is_preserved(self):
        selected = self.select()
        destination = Path(selected['destination'])
        destination.mkdir(parents=True)
        marker = destination / 'personal.h'
        marker.write_text('keep')
        with self.assertRaisesRegex(SDKError, 'another sysroot'):
            self.select()
        self.assertEqual(marker.read_text(), 'keep')
        jsonio.write_json(destination / sysroot_package.RECEIPT,
                         dict(sha256='0'*64, target=self.target))
        with self.assertRaisesRegex(SDKError, 'another sysroot'):
            self.select()

    def replacement(self):
        destination = self.root / 'target'
        cfg = dict(target=self.target, prefix='/usr/pkg', pkgdb='/usr/pkg/pkgdb',
                   sysroot=str(destination))
        self.config['repositories'] = {'target': cfg}
        self.release['release_id'] = 'fixture'
        sysroot_package.install(self.archive, self.select())
        # A distinct valid tar envelope around the same signed-transfer payload.
        payload = self.archive.read_bytes() + bytes(512)
        self.artifact.update(size=len(payload), sha256=hashlib.sha256(payload).hexdigest())
        self.cache.path(self.artifact).write_bytes(payload)
        installer = Mock()
        installer.inventory.return_value = []
        plan = sysroot_update.plan_update(self.release, [], self.config, installer, self.cache,
                                          'aarch64', ())
        return destination, plan, installer

    def test_sysroot_exchange_keeps_files_and_serializes_waiting_writers(self):
        from emberbsd_sdk.sdk_client import apply_plan
        destination, plan, installer = self.replacement()
        writer = destination / 'usr/pkg/pkgdb.writer'
        writer.touch()
        with writer.open('rb') as waiting:
            marker = destination / 'personal.h'
            marker.write_text('local')
            apply_plan(plan, self.config, installer, self.cache, None)
            self.assertEqual(self.select()['action'], 'installed')
            self.assertEqual(marker.read_text(), 'local')
            self.assertEqual(os.fstat(waiting.fileno()).st_ino, writer.stat().st_ino)
            self.assertTrue((destination / 'usr/pkg/pkgdb.readers').is_dir())

    def test_failed_verification_or_exchange_leaves_the_installed_prefix(self):
        from emberbsd_sdk.sdk_client import apply_plan
        destination, plan, installer = self.replacement()
        before = (destination / sysroot_package.RECEIPT).read_bytes()
        writer = destination / 'usr/pkg/pkgdb.writer'
        writer.touch()
        identity = writer.stat().st_ino
        marker = destination / 'personal.h'
        marker.write_text('local')
        for target in ('sysroot_update.verify_packages', 'publication.exchange_directories'):
            with self.subTest(target=target), patch('emberbsd_sdk.' + target, side_effect=SDKError('fixture')):
                with self.assertRaisesRegex(SDKError, 'fixture'):
                    apply_plan(plan, self.config, installer, self.cache, None)
                self.assertEqual((destination / sysroot_package.RECEIPT).read_bytes(), before)
                self.assertEqual(marker.read_text(), 'local')
                self.assertEqual(writer.stat().st_ino, identity)
                self.assertFalse(list(destination.parent.glob('.sysroot-install-*')))

    def test_source_state_inside_target_is_refused_before_taking_its_lock(self):
        from emberbsd_sdk.sdk_client import apply_plan
        destination, plan, installer = self.replacement()
        state = destination / 'ports-state'
        self.config['source'] = {'state': str(state)}
        with self.assertRaisesRegex(SDKError, 'Ports state must be outside'):
            apply_plan(plan, self.config, installer, self.cache, None)
        self.assertFalse(state.exists())
        self.assertEqual(jsonio.read_json(destination / sysroot_package.RECEIPT),
                         plan['sysroot']['previous'])

    def test_update_refuses_a_reading_build_even_in_the_same_process_tree(self):
        from emberbsd_sdk.sdk_client import apply_plan
        destination, plan, installer = self.replacement()
        with prefix_lock.reading([destination / 'usr/pkg/pkgdb']):
            with self.assertRaisesRegex(SDKError, 'another build uses the prefix'):
                apply_plan(plan, self.config, installer, self.cache, None)
        self.assertEqual(jsonio.read_json(destination / sysroot_package.RECEIPT),
                         plan['sysroot']['previous'])

    def test_target_packages_use_configured_sysroot_after_base_publication(self):
        from emberbsd_sdk.sdk_client import apply_plan
        from test_sdk_client import Installer
        destination = self.root / 'configured-sysroot'
        self.config['repositories']['target'] = dict(target=self.target, prefix='/usr/pkg',
            pkgdb='/usr/pkg/pkgdb', sysroot=str(destination))
        selected = self.select()
        self.assertEqual(selected['destination'], str(destination))
        installer = Installer()
        item = dict(self.artifact, id='fixture-package', kind='target-package', path='fixture.tgz')
        plan = dict(build_roots=[], sysroot=selected, download_size=item['size'],
            unpacked_size=item['unpacked_size'] * 2,
            packages=[dict(artifact=item, repository='target', action='download')])
        def install(path, config, *, checked, configure=True):
            self.assertTrue((destination / sysroot_package.RECEIPT).is_file())
            self.assertTrue((destination / 'usr/include/stdio.h').is_file())
            # The installer holds the writer lock of the new package database.
            with open(destination / 'usr/pkg/pkgdb.writer', 'rb') as lock:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        installer.install = install
        apply_plan(plan, self.config, installer, self.cache, Store(self.archive.read_bytes()))
        self.assertEqual(self.select()['action'], 'installed')
        with open(destination / 'usr/pkg/pkgdb.writer', 'rb') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_foreign_compiler_alias_precedes_base_and_package_installation(self):
        from emberbsd_sdk.sdk_client import install_selection
        from test_sdk_client import Installer
        self.config['state_dir'] = str(self.root / 'state')
        commands = Path(self.config['host_prefix']) / 'bin'
        commands.mkdir(parents=True)
        (commands / 'clang').touch()
        alias = commands / (self.target['triplet'] + '-cc')
        alias.symlink_to(self.root / 'other-sdk' / 'cc')
        item = dict(self.artifact, id='fixture-package', kind='target-package', path='fixture.tgz')
        for existing in (False, True):
            with self.subTest(existing=existing):
                destination = self.root / ('installed-base' if existing else 'new-base')
                self.config['repositories']['target'] = dict(target=self.target, prefix='/usr/pkg',
                    pkgdb='/usr/pkg/pkgdb', sysroot=str(destination))
                if existing:
                    sysroot_package.install(self.archive, self.select())
                installer = Installer()
                store = Store(self.archive.read_bytes())
                plan = dict(build_roots=[], sysroot=self.select(), download_size=item['size'],
                    unpacked_size=item['unpacked_size'] * 2,
                    packages=[dict(artifact=item, repository='target', action='download')])
                with self.assertRaisesRegex(SDKError, 'compiler command already belongs'):
                    install_selection(plan, self.config, installer, self.cache, store)
                self.assertEqual(store.calls, [])
                self.assertEqual(installer.actions, [])
                self.assertEqual(destination.exists(), existing)
                self.assertEqual(alias.readlink(), self.root / 'other-sdk' / 'cc')

    def test_signed_package_runtime_mismatch_precedes_new_and_existing_base_mutation(self):
        from emberbsd_sdk.package_install import PackageInstaller
        from emberbsd_sdk.sdk_client import apply_plan

        destination = self.root / 'target'
        cfg = dict(target=self.target, prefix='/usr/pkg', pkgdb='/usr/pkg/pkgdb', sysroot=str(destination))
        self.config['repositories']['target'] = cfg
        selected = self.select()
        record = application_toolchain.load_clang_runtime(self.source, target='aarch64')
        shared = record['artifacts']['libunwind']['shared']
        archive = self.root / 'runtime-package.tgz'

        def plan(contents):
            members = {'+CONTENTS': b'@name runtime-1\n@cwd /usr/pkg\n', shared.removeprefix('usr/pkg/'): contents}
            with tarfile.open(archive, 'w:gz') as tar:
                for name, data in members.items():
                    member = tarfile.TarInfo(name)
                    member.size = len(data)
                    tar.addfile(member, io.BytesIO(data))
            item = dict(self.artifact, kind='target-package', id='runtime', path='runtime.tgz',
                        size=archive.stat().st_size, sha256=hashlib.sha256(archive.read_bytes()).hexdigest())
            self.cache.path(item).write_bytes(archive.read_bytes())
            return dict(roots=['lang/runtime'], build_roots=[], packages=[dict(pkgname='runtime-1',
                root='lang/runtime', repository='target', action='cache', artifact=item, replaces=[])],
                download_size=0, unpacked_size=0)

        self.cache.path(self.artifact).write_bytes(self.archive.read_bytes())
        installer = PackageInstaller(Mock(), self.root / 'trust')
        mismatched = plan(b'different bytes')
        with patch.object(installer, 'installed', return_value=set()), patch.object(installer, 'install') as install, \
             patch('emberbsd_sdk.package_automatic.finish'):
            with self.assertRaisesRegex(SDKError, 'runtime-1.*' + shared):
                apply_plan(dict(mismatched, sysroot=selected), self.config, installer, self.cache, None)
            self.assertFalse(destination.exists())
            sysroot_package.install(self.archive, selected)
            before = (destination / shared).read_bytes()
            with self.assertRaisesRegex(SDKError, 'runtime-1.*' + shared):
                apply_plan(mismatched, self.config, installer, self.cache, None)
            install.assert_not_called()
            self.assertEqual((destination / shared).read_bytes(), before)
            apply_plan(plan(before), self.config, installer, self.cache, None)
            install.assert_called_once()
            self.assertEqual(installer.crypto.verify_package.call_count, 3)

    def test_ambiguous_or_incompatible_target_is_not_selected(self):
        self.release['artifacts'].append(copy.deepcopy(self.artifact))
        with self.assertRaisesRegex(SDKError, 'found 2'):
            self.select()
        self.release['artifacts'].pop()
        self.config['repositories']['target'] = {'target': dict(self.target, abi='different')}
        with self.assertRaisesRegex(SDKError, 'found 0'):
            self.select()

    def test_cache_and_signed_size_are_used_by_plan(self):
        plan = include_sysroot(dict(download_size=7, unpacked_size=8), self.release, self.config,
                               self.cache, 'aarch64')
        self.assertEqual(plan['download_size'], 7 + self.artifact['size'])
        self.assertEqual(plan['unpacked_size'], 8 + self.artifact['unpacked_size'])
        self.cache.path(self.artifact).write_bytes(self.archive.read_bytes())
        self.assertEqual(self.select()['action'], 'cache')
        self.artifact['unpacked_size'] += 1
        with self.assertRaisesRegex(SDKError, 'size or LLVM runtime'):
            sysroot_package.check(self.archive, self.select())

    def test_unsafe_or_extra_outer_member_is_rejected(self):
        for kind in ('escape', 'link', 'extra'):
            with self.subTest(kind=kind):
                bad = self.root / (kind + '.tar')
                with tarfile.open(bad, 'w') as archive:
                    for index, name in enumerate(sysroot_package.MEMBERS):
                        content = (self.bundle / name).read_bytes()
                        member = tarfile.TarInfo('../escape' if kind == 'escape' and index == 0 else name)
                        member.size = len(content)
                        if kind == 'link' and index == 0:
                            member.type, member.linkname, member.size = tarfile.SYMTYPE, '/etc/passwd', 0
                        archive.addfile(member, io.BytesIO(content))
                    if kind == 'extra':
                        archive.addfile(tarfile.TarInfo('extra'))
                with self.assertRaisesRegex(SDKError, 'sysroot package member'):
                    sysroot_package.install(bad, self.select())
                self.assertFalse(Path(self.select()['destination']).exists())

    def test_failed_final_publication_leaves_no_installed_tree(self):
        selected = self.select()
        publish = sysroot_package.publication.publish_directory_noreplace
        def fail_final(source, destination):
            if destination == selected['destination']:
                raise SDKError('publication fixture')
            return publish(source, destination)
        with patch.object(sysroot_package.publication, 'publish_directory_noreplace', side_effect=fail_final):
            with self.assertRaisesRegex(SDKError, 'publication fixture'):
                sysroot_package.install(self.archive, selected)
        self.assertFalse(Path(selected['destination']).exists())
        self.assertEqual(list(Path(selected['destination']).parent.iterdir()), [])
