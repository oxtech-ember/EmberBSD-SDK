# SPDX-License-Identifier: BSD-2-Clause
"""Check local file preservation during a target base update."""

import hashlib
import os
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from test_core import TemporaryCase, dynamic_elf
from emberbsd_sdk import inventory, sysroot_update
from emberbsd_sdk.errors import SDKError


class SysrootUpdateTests(TemporaryCase):
    def setUp(self):
        super().setUp()
        self.old, self.new = self.root / 'installed', self.root / 'new'
        (self.old / 'etc').mkdir(parents=True)
        (self.old / 'etc/config').write_text('vendor=original\n')
        (self.old / 'etc/remove').touch()
        shutil.copytree(self.old, self.new)
        self.before = self.manifest(self.old)

    def manifest(self, root):
        return {'inventory': inventory.snapshot_installed_tree(str(root))}

    def preserve(self):
        sysroot_update.preserve_local_files(self.old, self.new, self.before,
                                            self.manifest(self.new), set())

    def test_preserves_configuration_deletions_and_installed_links(self):
        (self.old / 'etc/config').write_text('user=custom\n')
        (self.old / 'etc/config').chmod(0o640)
        (self.old / 'etc/remove').unlink()
        (self.new / 'etc/new-default').write_text('added upstream\n')
        os.link(self.old / 'etc/config', self.old / 'etc/copy')
        (self.old / 'etc/alias').symlink_to('/etc/config')
        self.preserve()
        self.assertEqual((self.new / 'etc/config').read_text(), 'user=custom\n')
        self.assertEqual((self.new / 'etc/config').stat().st_mode & 0o777, 0o640)
        self.assertTrue((self.new / 'etc/config').samefile(self.new / 'etc/copy'))
        self.assertTrue(sysroot_update._target_file(self.new, '/etc/alias'))
        self.assertFalse((self.new / 'etc/remove').exists())
        self.assertEqual((self.new / 'etc/new-default').read_text(), 'added upstream\n')

    def test_conflicting_configuration_survives_rejection(self):
        old, new = self.old / 'etc/config', self.new / 'etc/config'
        old.write_text('user\n')
        new.write_text('new default\n')
        with self.assertRaisesRegex(SDKError, 'etc/config'):
            self.preserve()
        self.assertEqual(old.read_text(), 'user\n')
        self.assertEqual(new.read_text(), 'new default\n')

    def test_absolute_required_library_resolves_in_the_target(self):
        (self.new / 'etc/lib').symlink_to('/etc/config')
        self.assertTrue(sysroot_update._target_file(self.new, '/etc/lib'))
        (self.new / 'etc/lib').unlink()
        (self.new / 'etc/lib').symlink_to('/bin/sh')
        self.assertFalse(sysroot_update._target_file(self.new, '/etc/lib'))
        (self.new / 'etc/loop').symlink_to('loop')
        self.assertFalse(sysroot_update._target_file(self.new, '/etc/loop'))

    def library(self, name, *, soname=b'libfixture.so.1'):
        """Write lib/NAME: an ELF shared library fixture with SONAME."""
        image = bytearray(dynamic_elf())
        offset = image.index(b'libfixture.so.1')
        image[offset:offset + len(soname)] = soname
        (self.new / 'lib').mkdir(exist_ok=True)
        (self.new / 'lib' / name).write_bytes(bytes(image))

    def test_newer_minor_of_the_same_library_satisfies_requires(self):
        self.library('libfixture.so.1.4')
        (self.new / 'lib/libfixture.so.1').symlink_to('libfixture.so.1.4')
        self.assertTrue(sysroot_update._required_target_file(self.new, '/lib/libfixture.so.1.3'))
        self.assertTrue(sysroot_update._required_target_file(self.new, '/lib/libfixture.so.1.4'))
        # A newer minor never satisfies a later one, and other files stay exact.
        self.assertFalse(sysroot_update._required_target_file(self.new, '/lib/libfixture.so.1.5'))
        self.assertFalse(sysroot_update._required_target_file(self.new, '/lib/libfixture.so.2.1'))
        self.assertFalse(sysroot_update._required_target_file(self.new, '/etc/missing'))

    def test_minor_substitution_needs_the_soname_link_and_soname(self):
        self.library('libfixture.so.1.4')
        # The dynamic linker loads the SONAME: without its link the library is not found.
        self.assertFalse(sysroot_update._required_target_file(self.new, '/lib/libfixture.so.1.3'))
        (self.new / 'lib/libfixture.so.1').symlink_to('libfixture.so.1.4')
        self.library('libfixture.so.1.4', soname=b'libfixturX.so.1')
        self.assertFalse(sysroot_update._required_target_file(self.new, '/lib/libfixture.so.1.3'))

    def test_verify_packages_accepts_a_newer_libc_minor_only(self):
        self.library('libfixture.so.1.225')
        (self.new / 'lib/libfixture.so.1').symlink_to('libfixture.so.1.225')
        package = self.new / 'usr/pkg/pkgdb/app-1'
        package.mkdir(parents=True)
        config = dict(sysroot=str(self.new), target={'arch': 'aarch64'}, prefix='/usr/pkg',
                      pkgdb='/usr/pkg/pkgdb')

        class Installer:
            def inventory(self, _config):
                return [{'pkgname': 'app-1'}]

        (package / '+BUILD_INFO').write_text('REQUIRES=/lib/libfixture.so.1.224\n')
        self.assertEqual(sysroot_update.verify_packages(Installer(), config), [{'pkgname': 'app-1'}])
        (package / '+BUILD_INFO').write_text('REQUIRES=/lib/libfixture.so.1.226\n')
        with self.assertRaisesRegex(SDKError, 'app-1: new sysroot lacks required file /lib/libfixture.so.1.226'):
            sysroot_update.verify_packages(Installer(), config)

    def test_pkg_add_can_replace_original_payload_and_must_keep_local_files(self):
        prefix = self.old / 'usr/pkg'
        db = prefix / 'pkgdb'
        package = db / 'lib-1'
        package.mkdir(parents=True)
        library = prefix / 'libfoo'
        library.write_text('original')
        checksum = hashlib.md5(library.read_bytes(), usedforsecurity=False).hexdigest()
        (package / '+CONTENTS').write_text(f'@cwd /usr/pkg\nlibfoo\n@comment MD5:{checksum}\n')
        config = dict(sysroot=str(self.old), target={'arch': 'aarch64'}, prefix='/usr/pkg', pkgdb='/usr/pkg/pkgdb')
        marker = prefix / 'local.conf'
        marker.write_text('keep')
        def check(changed):
            captured = self.manifest(self.old)['inventory']
            final = dict(captured)
            final.pop(changed)
            sysroot_update.check_local_package_changes(self.old, captured, final, self.before,
                {'target': config}, {'target': [{'pkgname': 'lib-1'}]}, {'lib-1'}, {db})
        check('usr/pkg/libfoo')
        with self.assertRaisesRegex(SDKError, 'local.conf'):
            check('usr/pkg/local.conf')
        library.write_text('local patch')
        with self.assertRaisesRegex(SDKError, 'libfoo'):
            check('usr/pkg/libfoo')
