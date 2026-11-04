# SPDX-License-Identifier: BSD-2-Clause
"""Delivery fixtures test transfer and selection, not real target execution."""

import copy
import hashlib
import io
from pathlib import Path
import sys
import tarfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from test_core import TemporaryCase
import test_llvm_builder as llvm_tests
from emberbsd_sdk import application_toolchain, jsonio, sysroot_package, sysroot_transfer
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
        def install(path, config, *, checked):
            self.assertTrue((destination / sysroot_package.RECEIPT).is_file())
            self.assertTrue((destination / 'usr/include/stdio.h').is_file())
            self.assertTrue((destination / 'usr/pkg/pkgdb.sdk-lock').is_dir())
        installer.install = install
        apply_plan(plan, self.config, installer, self.cache, Store(self.archive.read_bytes()))
        self.assertEqual(self.select()['action'], 'installed')
        self.assertFalse((destination / 'usr/pkg/pkgdb.sdk-lock').exists())

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
