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
from test_darwin_runtime import macho

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

    def consumer_files(self, name, files):
        """Install CONSUMER with FILES (relative path to bytes, or None for a missing file)."""
        directory = self.database / name
        directory.mkdir()
        (directory / '+CONTENTS').write_text('@cwd ' + str(self.root) + '\n@name ' + name + '\n'
            '@pkgdep lib>=1\n' + ''.join(path + '\n' for path in files) +
            '@ignore\n+COMMENT\n@ignore\n+BUILD_INFO\n')
        (directory / '+BUILD_INFO').write_text('')
        for path, data in files.items():
            if data is not None:
                (self.root / path).parent.mkdir(parents=True, exist_ok=True)
                (self.root / path).write_bytes(data)

    def removal(self, removed, files):
        self.installed('lib-1', providers=(removed,), dependents=('consumer-1',))
        self.consumer_files('consumer-1', files)
        self.replacement_archive('lib-2')
        with patch('emberbsd_sdk.package_updates._matches', return_value=True):
            self.check([self.package('lib-2')], {'lib-1', 'consumer-1'})

    def test_provider_removal_is_allowed_when_no_consumer_image_loads_it(self):
        # dynamic_elf() has the SONAME libfixture.so.1 and loads libc.so.12.
        self.removal('lib/libremoved.so.1', {'bin/tool': dynamic_elf(), 'share/tool.txt': b'text'})

    def test_consumer_image_loading_the_removed_provider_is_refused(self):
        with self.assertRaisesRegex(SDKError, 'removes library providers'):
            self.removal('lib/libc.so.12', {'bin/tool': dynamic_elf()})

    def test_consumer_without_an_image_or_with_a_missing_file_is_refused(self):
        for files in ({'bin/script': b'#!/bin/sh\n'}, {'bin/tool': dynamic_elf(), 'bin/gone': None}):
            with self.subTest(files=sorted(files)):
                self.setUp()
                with self.assertRaisesRegex(SDKError, 'removes library providers'):
                    self.removal('lib/libremoved.so.1', files)

    def test_macho_consumer_of_the_retained_name_allows_versioned_removal(self):
        # lz4 1.10 -> 1.11: the old dylib had its versioned file as install ID,
        # and the consumer loads the retained name.
        self.cfg['host'] = {'os': 'Darwin', 'arch': 'arm64', 'abi': 'native', 'runtime': 'native'}
        for load, refused in (('@loader_path/../lib/libexample.1.dylib', False),
                              ('@rpath/libexample.1.0.dylib', True)):
            with self.subTest(load=load):
                self.setUp()
                self.cfg['host'] = {'os': 'Darwin', 'arch': 'arm64', 'abi': 'native', 'runtime': 'native'}
                self.installed('lib-1', providers=('lib/libexample.1.0.dylib', 'lib/libexample.1.dylib'),
                               dependents=('consumer-1',))
                self.consumer_files('consumer-1', {'bin/tool': macho(0x100000000, loads=((0xc, load),))})
                self.replacement_archive('lib-2', providers=('lib/libexample.1.1.dylib', 'lib/libexample.1.dylib'))
                with patch('emberbsd_sdk.package_updates._matches', return_value=True):
                    if refused:
                        with self.assertRaisesRegex(SDKError, 'removes library providers'):
                            self.check([self.package('lib-2')], {'lib-1', 'consumer-1'})
                    else:
                        self.check([self.package('lib-2')], {'lib-1', 'consumer-1'})

    def coordinated_batch(self, *, successor_depends=('lib-2{,nb*}',), successor_first=False,
                          successor=True):
        """lib-1 -> lib-2 while consumer-1 pins lib-1 exactly; consumer-2 may follow."""
        self.installed('lib-1', providers=('lib/libexample.so.1',), dependents=('consumer-1',))
        self.installed('consumer-1', depends=('lib-1{,nb*}',))
        lib = self.make_archive(self.root / 'lib-2.tgz', 'lib-2', providers=('lib/libexample.so.1',))
        consumer = self.make_archive(self.root / 'consumer-2.tgz', 'consumer-2', depends=successor_depends)
        provider = dict(pkgname='lib-2', replaces=['lib-1'], artifact={'id': 'lib'}, action='download')
        packages = [provider]
        if successor:
            follower = dict(pkgname='consumer-2', replaces=['consumer-1'], artifact={'id': 'consumer'},
                            action='download')
            packages = [follower, provider] if successor_first else [provider, follower]
        return packages, provider, {'lib': lib, 'consumer': consumer}

    @staticmethod
    def exact(_crypto, pattern, name):
        if pattern.endswith('{,nb*}'):
            return name == pattern[:-6]
        base, version = pattern.split('>=')
        return name.rsplit('-', 1)[0] == base and name.rsplit('-', 1)[1] >= version

    def test_exact_dependency_with_its_dependent_following_is_coordinated(self):
        packages, provider, archives = self.coordinated_batch()
        with patch('emberbsd_sdk.package_updates._matches', side_effect=self.exact):
            check_updates(self.crypto, self.cfg, packages, archives, {'lib-1', 'consumer-1'})
        self.assertEqual(provider['coordinated'], ['consumer-2'])
        self.assertNotIn('coordinated', packages[1])

    def test_rerun_after_an_interrupted_coordinated_update_replaces_the_dependent(self):
        # lib-2 is installed with -D; consumer-1 still pins lib-1 exactly.
        self.installed('lib-2', providers=('lib/libexample.so.1',), dependents=('consumer-1',))
        self.installed('consumer-1', depends=('lib-1{,nb*}',))
        consumer = self.make_archive(self.root / 'consumer-2.tgz', 'consumer-2', depends=('lib-2{,nb*}',))
        packages = [dict(pkgname='lib-2', replaces=[], artifact={'id': 'lib'}, action='installed'),
                    dict(pkgname='consumer-2', replaces=['consumer-1'], artifact={'id': 'consumer'},
                         action='download')]
        with patch('emberbsd_sdk.package_updates._matches', side_effect=self.exact):
            check_updates(self.crypto, self.cfg, packages, {'consumer': consumer}, {'lib-2', 'consumer-1'})
        self.assertNotIn('coordinated', packages[1])

    def test_exact_dependency_without_a_following_replacement_is_refused(self):
        cases = (dict(successor=False), dict(successor_first=True),
                 dict(successor_depends=('lib-1{,nb*}',)),
                 # A requirement that matches neither version is not satisfied by lib-2.
                 dict(successor_depends=('lib>=3',)))
        for case in cases:
            with self.subTest(case=case):
                self.setUp()
                packages, provider, archives = self.coordinated_batch(**case)
                installed = {'lib-1', 'consumer-1'}
                with patch('emberbsd_sdk.package_updates._matches', side_effect=self.exact):
                    with self.assertRaisesRegex(SDKError, 'consumer-1: dependency lib-1{,nb\\*} does not accept lib-2'):
                        check_updates(self.crypto, self.cfg, packages, archives, installed)
                self.assertNotIn('coordinated', provider)

    def test_coordinated_update_keeps_the_provider_guard(self):
        # The old dependent must stay usable until its replacement: a removed
        # provider without proof is still refused.
        packages, provider, archives = self.coordinated_batch()
        self.make_archive(self.root / 'lib-2.tgz', 'lib-2', providers=('lib/libexample.so.2',))
        with patch('emberbsd_sdk.package_updates._matches', side_effect=self.exact):
            with self.assertRaisesRegex(SDKError, 'removes library providers'):
                check_updates(self.crypto, self.cfg, packages, archives, {'lib-1', 'consumer-1'})

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

    def installed_consumer_reuse(self, missing):
        """An installed consumer whose image loads libc.so.12; MISSING is the catalog-only provider."""
        packages, archives = self.compatible_packages(candidate_providers=(missing,),
                                                      installed_providers=())
        directory = self.database / 'consumer-1'
        directory.mkdir()
        (directory / '+CONTENTS').write_text('@cwd ' + str(self.root) + '\n@name consumer-1\n'
                                             '@pkgdep libxml2>=2.14.6nb2\nbin/tool\n')
        (directory / '+BUILD_INFO').write_text('')
        (self.root / 'bin').mkdir(exist_ok=True)
        (self.root / 'bin/tool').write_bytes(dynamic_elf())  # loads libc.so.12
        packages[1]['action'] = 'installed'
        with patch('emberbsd_sdk.package_updates._matches', side_effect=self.pmatch):
            return packages, reuse_compatible_dependencies(self.crypto, self.cfg, packages, archives,
                {'libxml2-2.15.4nb5', 'consumer-1'}, roots=('cad/consumer@native',))

    def test_installed_consumer_without_the_missing_provider_allows_reuse(self):
        packages, reused = self.installed_consumer_reuse('lib/libxml2.so.2')
        self.assertEqual([item['pkgname'] for item in reused], ['libxml2-2.15.4nb5'])
        self.assertEqual(packages[0]['action'], 'installed')

    def test_installed_consumer_loading_the_missing_provider_prevents_reuse(self):
        packages, reused = self.installed_consumer_reuse('lib/libc.so.12')
        self.assertEqual(reused, [])
        self.assertEqual(packages[0]['pkgname'], 'libxml2-2.15.4nb4')

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

    def macho_provider(self, version, *, install_id=None, cpu=0x100000c,
                       subtype=0, filetype=6, platform_id=1, minimum=0x10000,
                       current=0x10000, compatibility=0x10000, no_platform=False):
        stable = 'libexample.1.dylib'
        identity = install_id or str(self.root / 'lib' / stable)
        data = bytearray(macho(0x180000000, install_name=identity))
        struct.pack_into('<III', data, 4, cpu, subtype, filetype)
        offset = 32
        while offset < 32 + struct.unpack_from('<I', data, 20)[0]:
            kind, size = struct.unpack_from('<II', data, offset)
            if kind == 0x32:
                if no_platform:
                    struct.pack_into('<I', data, offset, 0x7f000001)
                else:
                    struct.pack_into('<II', data, offset + 8, platform_id, minimum)
            if kind == 0xd:
                struct.pack_into('<II', data, offset + 16, current, compatibility)
            offset += size
        return bytes(data)

    def macho_case(self, *, old_options=None, new_options=None, alias=None, implementation=None):
        stable = 'lib/libexample.1.dylib'
        old = 'lib/libexample.1.0.dylib'
        new = implementation or 'lib/libexample.1.1.dylib'
        self.cfg['host'] = {'os': 'Darwin', 'arch': 'arm64', 'abi': 'native', 'runtime': 'native'}
        self.installed('lib-1', providers=(stable, old), dependents=('consumer-1',))
        self.installed('consumer-1', depends=('lib>=1',))
        (self.root/'lib').mkdir(exist_ok=True)
        (self.root/old).write_bytes(self.macho_provider('0', **(old_options or {})))
        (self.root/stable).symlink_to(Path(old).name)
        info = ''.join('PROVIDES='+str(self.root)+'/'+x+'\n' for x in (stable, new))
        with tarfile.open(self.archive, 'w:gz') as archive:
            for filename, data in (('+CONTENTS', ('@cwd '+str(self.root)+'\n@name lib-2\n').encode()),
                                   ('+BUILD_INFO', info.encode()),
                                   (new, self.macho_provider('1', **(new_options or {})))):
                entry = tarfile.TarInfo(filename); entry.size = len(data); archive.addfile(entry, io.BytesIO(data))
            link = tarfile.TarInfo(stable); link.type = tarfile.SYMTYPE
            link.linkname = alias if alias is not None else Path(new).name
            archive.addfile(link)
        with patch('emberbsd_sdk.package_updates._matches', return_value=True):
            self.check([self.package('lib-2')], {'lib-1', 'consumer-1'})

    def test_macho_retained_install_id_accepts_versioned_implementation(self):
        self.macho_case(new_options={'current': 0x10001})

    def test_macho_missing_platform_on_both_sides_is_refused(self):
        with self.assertRaises(SDKError):
            self.macho_case(old_options={'no_platform': True}, new_options={'no_platform': True})

    def test_macho_rpath_install_id_accepts_stable_alias(self):
        self.macho_case(old_options={'install_id': '@rpath/libexample.1.dylib'},
                        new_options={'install_id': '@rpath/libexample.1.dylib', 'current': 0x10001})

    def test_macho_rpath_alias_allows_implementation_in_subdirectory(self):
        self.macho_case(old_options={'install_id': '@rpath/libexample.1.dylib'},
                        new_options={'install_id': '@rpath/libexample.1.dylib'},
                        implementation='lib/private/libexample.1.1.dylib',
                        alias='private/libexample.1.1.dylib')

    def test_elf_alias_allows_implementation_in_subdirectory(self):
        self.installed_abi('lib-1', '2')
        self.installed('consumer-1', depends=('lib>=1',))
        self.abi_archive(self.archive, 'lib-2', '3',
                         provider='lib/private/libexample.so.1.3',
                         alias='private/libexample.so.1.3')
        with patch('emberbsd_sdk.package_updates._matches', return_value=True):
            self.check([self.package('lib-2')], {'lib-1', 'consumer-1'})

    def test_macho_incompatible_or_unowned_provider_refused(self):
        cases = ({'new_options': {'install_id': '/foreign/libexample.1.dylib'}},
                 {'new_options': {'install_id': '@rpath/other.dylib'}},
                 {'new_options': {'cpu': 0x1000007}},
                 {'new_options': {'subtype': 2}},
                 {'new_options': {'filetype': 2}},
                 {'new_options': {'platform_id': 2}},
                 {'new_options': {'minimum': 0x20000}},
                 {'new_options': {'compatibility': 0x20000}},
                 {'old_options': {'current': 0x20000}},
                 {'alias': '../../outside'}, {'alias': 'missing.dylib'})
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                self.root = Path(temp); self.database = self.root/'pkgdb'; self.database.mkdir()
                self.cfg = {'prefix': str(self.root), 'pkgdb': str(self.database)}
                self.archive = self.root/'replacement.tgz'
                with self.assertRaises(SDKError):
                    self.macho_case(**case)

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
