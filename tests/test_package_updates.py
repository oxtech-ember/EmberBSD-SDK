# SPDX-License-Identifier: BSD-2-Clause
from pathlib import Path
import platform
import io
import struct
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from test_core import dynamic_elf

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.package_updates import check_updates, reuse_compatible_dependencies


class PackageUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.database = self.root / 'pkgdb'
        self.database.mkdir()
        self.archive = self.root / 'replacement.tgz'
        self.crypto = object()
        self.cfg = {'prefix': str(self.root), 'pkgdb': str(self.database)}

    def installed(self, name, *, depends=(), providers=(), dependents=()):
        directory = self.database / name
        directory.mkdir()
        (directory / '+CONTENTS').write_text('@cwd /old/prefix\n@name ' + name + '\n' +
            ''.join('@pkgdep ' + value + '\n' for value in depends))
        (directory / '+BUILD_INFO').write_text(''.join('PROVIDES=/old/prefix/' + value + '\n'
            for value in providers))
        if dependents:
            (directory / '+REQUIRED_BY').write_text(''.join(value + '\n' for value in dependents))

    def replacement_archive(self, name, *, depends=(), providers=(), prefix='/producer/prefix'):
        contents = '@cwd ' + prefix + '\n@name ' + name + '\n' + ''.join(
            '@pkgdep ' + value + '\n' for value in depends)
        info = ''.join('PROVIDES=' + prefix + '/' + value + '\n' for value in providers)
        with tarfile.open(self.archive, 'w:gz') as archive:
            for filename, data in (('+CONTENTS', contents), ('+BUILD_INFO', info)):
                path = self.root / filename.lstrip('+')
                path.write_text(data)
                archive.add(path, arcname=filename)
        return self.archive

    def package(self, name, *, replaces=('lib-1',)):
        return dict(pkgname=name, replaces=list(replaces), artifact={'id': 'replacement'},
                    action='download' if replaces else 'installed')

    def check(self, packages, installed):
        return check_updates(self.crypto, self.cfg, packages,
            {'replacement': self.archive}, installed)

    def test_prefix_relocation_normalizes_provider_paths(self):
        self.installed('lib-1', providers=('lib/libexample.so.1',), dependents=('consumer-1',))
        self.installed('consumer-1', depends=('lib>=1',))
        self.replacement_archive('lib-2', providers=('lib/libexample.so.1',), prefix='/new/prefix')
        with patch('emberbsd_sdk.package_updates._matches', return_value=True):
            self.check([self.package('lib-2')], {'lib-1', 'consumer-1'})

    def test_provider_removal_with_installed_consumer_is_refused(self):
        self.installed('lib-1', providers=('lib/libexample.so.1',), dependents=('consumer-1',))
        self.installed('consumer-1', depends=('lib>=1',))
        self.replacement_archive('lib-2')
        with patch('emberbsd_sdk.package_updates._matches', return_value=True):
            with self.assertRaisesRegex(SDKError, 'removes library providers'):
                self.check([self.package('lib-2')], {'lib-1', 'consumer-1'})

    def test_dependency_constraint_rejects_incompatible_replacement(self):
        self.installed('lib-1', dependents=('consumer-1',))
        self.installed('consumer-1', depends=('lib<2',))
        self.replacement_archive('lib-2')
        with patch('emberbsd_sdk.package_updates._matches', side_effect=lambda _c, pattern, name:
                   pattern == 'lib>=1' or (name == 'lib-1' and pattern == 'lib<2')):
            with self.assertRaisesRegex(SDKError, 'does not accept lib-2'):
                self.check([self.package('lib-2')], {'lib-1', 'consumer-1'})

    def test_stale_installed_state_is_refused(self):
        self.installed('lib-3')
        self.replacement_archive('lib-2')
        with self.assertRaisesRegex(SDKError, 'installed state changed'):
            self.check([self.package('lib-2')], {'lib-3'})

    def test_update_without_dependents_allows_provider_removal(self):
        self.installed('lib-1', providers=('lib/libexample.so.1',))
        self.replacement_archive('lib-2')
        with patch('emberbsd_sdk.package_updates._matches', return_value=True):
            self.check([self.package('lib-2')], {'lib-1'})

    def test_repeated_noop_with_current_package(self):
        self.installed('lib-2')
        package = self.package('lib-2', replaces=())
        self.check([package], {'lib-2'})
        self.check([package], {'lib-2'})

    def test_signed_catalog_cannot_downgrade_newer_installed_package(self):
        self.installed('lib-3')
        self.replacement_archive('lib-2')
        with patch('emberbsd_sdk.package_updates._matches', side_effect=lambda _c, pattern, name:
                   pattern == 'lib>=3' and name == 'lib-3'):
            with self.assertRaisesRegex(SDKError, 'downgrade'):
                self.check([self.package('lib-2', replaces=('lib-3',))], {'lib-3'})

    def make_archive(self, path, name, *, depends=(), providers=(), prefix='/producer/prefix'):
        contents = '@cwd ' + prefix + '\n@name ' + name + '\n' + ''.join(
            '@pkgdep ' + value + '\n' for value in depends)
        info = ''.join('PROVIDES=' + prefix + '/' + value + '\n' for value in providers)
        with tarfile.open(path, 'w:gz') as archive:
            for filename, data in (('+CONTENTS', contents), ('+BUILD_INFO', info)):
                member = self.root / (path.stem + filename)
                member.write_text(data)
                archive.add(member, arcname=filename)
        return path

    def compatible_packages(self, repository='native', role='native', *, candidate_providers=(),
                            installed_providers=()):
        suffix = '@native' if role == 'native' else ''
        provider = self.root / 'libxml2-2.15.4nb4.tgz'
        consumer = self.root / 'consumer-1.tgz'
        self.make_archive(provider, 'libxml2-2.15.4nb4', providers=candidate_providers)
        self.make_archive(consumer, 'consumer-1', depends=('libxml2>=2.14.6nb2',))
        packages = [
            dict(root=f'textproc/libxml2{suffix}', repository=repository, pkgname='libxml2-2.15.4nb4',
                 action='download', replaces=['libxml2-2.15.4nb5'], artifact={
                     'id': 'xml', 'depends': []}),
            dict(root=f'cad/consumer{suffix}', repository=repository, pkgname='consumer-1',
                 action='download', replaces=[], artifact={'id': 'consumer', 'depends': ['xml']}),
        ]
        self.installed('libxml2-2.15.4nb5', providers=installed_providers)
        return packages, {'xml': provider, 'consumer': consumer}

    def pmatch(self, _crypto, pattern, name):
        if pattern.startswith('libxml2>='):
            return name in ('libxml2-2.15.4nb4', 'libxml2-2.15.4nb5')
        if pattern.startswith('libxml2<'):
            return name == 'libxml2-2.15.4nb4'
        if pattern == 'libxml2-2.15.4nb4':
            return name == 'libxml2-2.15.4nb4'
        return False

    def test_compatible_newer_installed_dependency_is_reused(self):
        packages, archives = self.compatible_packages()
        with patch('emberbsd_sdk.package_updates._matches', side_effect=self.pmatch):
            reused = reuse_compatible_dependencies(self.crypto, self.cfg, packages, archives,
                {'libxml2-2.15.4nb5'}, roots=('cad/consumer@native',))
        self.assertEqual([item['pkgname'] for item in reused], ['libxml2-2.15.4nb5'])
        self.assertIsNone(packages[0]['artifact'])
        self.assertEqual(packages[0]['action'], 'installed')
        self.assertEqual(packages[0]['replaces'], [])

    def test_one_incompatible_selected_consumer_prevents_reuse(self):
        packages, archives = self.compatible_packages()
        second = self.root / 'second-1.tgz'
        self.make_archive(second, 'second-1', depends=('libxml2<2.15.4nb5',))
        packages.append(dict(root='cad/second@native', repository='native', pkgname='second-1',
            action='download', replaces=[], artifact={'id': 'second', 'depends': ['xml']}))
        archives['second'] = second
        with patch('emberbsd_sdk.package_updates._matches', side_effect=self.pmatch):
            reused = reuse_compatible_dependencies(self.crypto, self.cfg, packages, archives,
                {'libxml2-2.15.4nb5'}, roots=('cad/consumer@native', 'cad/second@native'))
        self.assertEqual(reused, [])
        self.assertEqual(packages[0]['pkgname'], 'libxml2-2.15.4nb4')

    def test_explicit_dependency_root_is_not_reused(self):
        packages, archives = self.compatible_packages()
        with patch('emberbsd_sdk.package_updates._matches', side_effect=self.pmatch):
            reused = reuse_compatible_dependencies(self.crypto, self.cfg, packages, archives,
                {'libxml2-2.15.4nb5'}, roots=('textproc/libxml2@native',))
        self.assertEqual(reused, [])
        self.assertEqual(packages[0]['action'], 'download')

    def test_removed_provider_prevents_reuse_even_when_pkgdep_accepts_version(self):
        packages, archives = self.compatible_packages(candidate_providers=('lib/libxml2.so.2',),
                                                       installed_providers=('lib/libxml2.so.1',))
        with patch('emberbsd_sdk.package_updates._matches', side_effect=self.pmatch):
            reused = reuse_compatible_dependencies(self.crypto, self.cfg, packages, archives,
                {'libxml2-2.15.4nb5'}, roots=('cad/consumer@native',))
        self.assertEqual(reused, [])
        self.assertEqual(packages[0]['pkgname'], 'libxml2-2.15.4nb4')

    def elf_image(self, *, soname='libexample.so.1', machine=183, no_soname=False, osabi=0, abiversion=0, flags=0):
        image = bytearray(dynamic_elf())
        old = b'libfixture.so.1'
        replacement = soname.encode()
        self.assertEqual(len(old), len(replacement))
        offset = image.index(old)
        image[offset:offset + len(old)] = replacement
        struct.pack_into('<H', image, 18, machine)
        image[7] = osabi; image[8] = abiversion
        struct.pack_into('<I', image, 48, flags)
        if no_soname:
            struct.pack_into('<q', image, 256 + 32, 0)
        return bytes(image)

    def abi_archive(self, path, name, version, *, soname='libexample.so.1',
                    machine=183, alias=None, payload=True, cycle=False, provider=None, malformed=False, no_soname=False, osabi=0, abiversion=0, flags=0, prefix=None):
        stable = 'lib/libexample.so.1'
        implementation = provider or stable + '.' + version
        prefix = prefix or str(self.root)
        contents = '@cwd ' + prefix + '\n@name ' + name + '\n'
        info = ''.join('PROVIDES=' + prefix + '/' + x + '\n'
                       for x in (stable, implementation))
        with tarfile.open(path, 'w:gz') as archive:
            def regular(filename, data):
                entry = tarfile.TarInfo(filename); entry.size = len(data); entry.mode = 0o644
                archive.addfile(entry, io.BytesIO(data))
            regular('+CONTENTS', contents.encode())
            regular('+BUILD_INFO', info.encode())
            if payload:
                regular(implementation, b'\x7fELFbroken' if malformed else self.elf_image(soname=soname, machine=machine, no_soname=no_soname, osabi=osabi, abiversion=abiversion, flags=flags))
            link = tarfile.TarInfo(stable); link.type = tarfile.SYMTYPE
            link.linkname = alias if alias is not None else Path(implementation).name
            archive.addfile(link)
            if cycle:
                link = tarfile.TarInfo('lib/loop'); link.type = tarfile.SYMTYPE
                link.linkname = Path(stable).name; archive.addfile(link)
        return path

    def installed_abi(self, name, version, *, old_alias=None):
        stable = 'lib/libexample.so.1'; implementation = stable + '.' + version
        self.installed(name, providers=(stable, implementation), dependents=('consumer-1',))
        lib = self.root / 'lib'; lib.mkdir(exist_ok=True)
        (self.root / implementation).write_bytes(self.elf_image())
        (self.root / stable).symlink_to(old_alias or Path(implementation).name)

    def test_versioned_elf_provider_replacement_accepts_stable_soname(self):
        self.installed_abi('lib-1', '2')
        self.installed('consumer-1', depends=('lib>=1',))
        self.abi_archive(self.archive, 'lib-2', '3')
        with patch('emberbsd_sdk.package_updates._matches', return_value=True):
            self.check([self.package('lib-2')], {'lib-1', 'consumer-1'})

    def test_old_provider_set_rule_rejects_compatible_update(self):
        self.installed_abi('lib-1', '2')
        self.installed('consumer-1', depends=('lib>=1',))
        self.abi_archive(self.archive, 'lib-2', '3')
        # Previous rule rejected whenever any old PROVIDES filename disappeared.
        def old_rule(_config, _archive, old, new, **_kwargs):
            return old <= new
        with patch('emberbsd_sdk.package_updates._matches', return_value=True), \
             patch('emberbsd_sdk.package_updates.providers_preserved', side_effect=old_rule):
            with self.assertRaisesRegex(SDKError, 'removes library providers'):
                self.check([self.package('lib-2')], {'lib-1', 'consumer-1'})

    def test_target_sysroot_uses_physical_prefix_not_host_prefix(self):
        sysroot = self.root / 'target'
        self.root = sysroot / 'usr/pkg'; self.root.mkdir(parents=True)
        self.database = self.root / 'pkgdb'; self.database.mkdir()
        self.cfg = {'prefix': '/usr/pkg', 'pkgdb': '/usr/pkg/pkgdb',
                    'target': {'arch': 'x86_64'}, 'sysroot': str(sysroot)}
        self.archive = sysroot / 'replacement.tgz'
        self.installed_abi('lib-1', '2')
        self.installed('consumer-1', depends=('lib>=1',))
        self.abi_archive(self.archive, 'lib-2', '3', prefix='/usr/pkg')
        with patch('emberbsd_sdk.package_updates._matches', return_value=True):
            self.check([self.package('lib-2')], {'lib-1', 'consumer-1'})

    def test_versioned_elf_provider_rejects_unsafe_or_incompatible_cases(self):
        cases = {
            'wrong_soname': {'soname': 'libdiffers.so.1'},
            'wrong_machine': {'machine': 62},
            'no_soname': {'no_soname': True},
            'wrong_osabi': {'osabi': 2},
            'wrong_abiversion': {'abiversion': 1},
            'wrong_flags': {'flags': 1},
            'plugin_name': {'provider': 'lib/plugin.so'},
            'escape': {'alias': '../../outside'},
            'cycle': {'alias': 'loop', 'cycle': True},
            'missing_payload': {'payload': False},
            'malformed_elf': {'malformed': True},
            'old_alias_elsewhere': {'old_alias': 'other.so'},
        }
        for label, options in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                # Each fixture has independent installed prefix and package DB.
                self.root = Path(tmp); self.database = self.root / 'pkgdb'; self.database.mkdir()
                self.cfg = {'prefix': str(self.root), 'pkgdb': str(self.database)}
                self.archive = self.root / 'replacement.tgz'
                args = dict(options)
                old_alias = args.pop('old_alias', None)
                self.installed_abi('lib-1', '2', old_alias=old_alias)
                if old_alias:
                    (self.root / 'lib' / old_alias).write_bytes(self.elf_image())
                self.installed('consumer-1', depends=('lib>=1',))
                self.abi_archive(self.archive, 'lib-2', '3', **args)
                with patch('emberbsd_sdk.package_updates._matches', return_value=True):
                    with self.assertRaises(SDKError):
                        self.check([self.package('lib-2')], {'lib-1', 'consumer-1'})

    def test_reuse_newer_installed_with_compatible_versioned_provider(self):
        stable = 'lib/libexample.so.1'
        packages, archives = self.compatible_packages(
            candidate_providers=(stable, stable + '.2'),
            installed_providers=(stable, stable + '.3'))
        lib = self.root / 'lib'; lib.mkdir(exist_ok=True)
        (lib / 'libexample.so.1.3').write_bytes(self.elf_image())
        (lib / 'libexample.so.1').symlink_to('libexample.so.1.3')
        self.abi_archive(archives['xml'], 'libxml2-2.15.4nb4', '2')
        with patch('emberbsd_sdk.package_updates._matches', side_effect=self.pmatch):
            reused = reuse_compatible_dependencies(self.crypto, self.cfg, packages, archives,
                {'libxml2-2.15.4nb5'}, roots=('cad/consumer@native',))
        self.assertEqual([item['pkgname'] for item in reused], ['libxml2-2.15.4nb5'])

    def test_target_pkgdb_does_not_reuse_native_installed_package(self):
        target_db = self.root / 'target-pkgdb'
        target_db.mkdir()
        packages, archives = self.compatible_packages('target', 'target')
        # A host package with the same name is not consulted when selecting in a
        # separate repository database.
        with patch('emberbsd_sdk.package_updates._matches', side_effect=self.pmatch):
            reused = reuse_compatible_dependencies(self.crypto, {'prefix': str(self.root), 'pkgdb': str(target_db)}, packages, archives,
                set(), roots=('cad/consumer',))
        self.assertEqual(reused, [])


if __name__ == '__main__':
    unittest.main()
