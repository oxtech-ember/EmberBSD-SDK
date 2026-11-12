# SPDX-License-Identifier: BSD-2-Clause
import io
import importlib.util
import marshal
import os
from pathlib import Path
import py_compile
import struct
import sys
import subprocess
from types import SimpleNamespace
import tempfile
import tarfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk.bootstrap_bundle import (MANIFEST, bundle_paths, initialize, make_relative, unpack_package,
                                           relocate_text, text_references, write_archive)
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.jsonio import read_json, write_json


def fat_archive(payload=b'!<arch>\n', *, cpu=0x100000c, subtype=0, offset=32):
    table = struct.pack('>5I', cpu, subtype, offset, len(payload), 3)
    header = struct.pack('>II', 0xcafebabe, 1) + table
    return header.ljust(offset, b'\0') + payload


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='ember-bootstrap-')
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name).resolve()
        self.root = self.work / 'sdk'
        self.root.mkdir()

    def package(self, *, path='bin/example', link=None, install=False):
        archive = self.work / 'example.tgz'
        contents = ('@cwd /producer/sdk\n@name example-1\n' + path + '\n' +
                    ('@comment Symlink:' + link if link else '@comment MD5:old') + '\n')
        with tarfile.open(archive, 'w:gz') as stream:
            entries = [('+CONTENTS', contents.encode()), ('+COMMENT', b'example'),
                       (path, b'#!/producer/sdk/bin/sh\n')]
            if install:
                entries.append(('+INSTALL', b'#!/bin/sh\n'))
            for name, data in entries:
                member = tarfile.TarInfo(name)
                member.mode = 0o755
                if name == path and link:
                    member.type = tarfile.SYMTYPE
                    member.linkname = link
                    stream.addfile(member)
                else:
                    member.size = len(data)
                    stream.addfile(member, io.BytesIO(data))
        return archive

    def test_native_single_slice_fat_static_archive_is_not_rewritten_or_signed(self):
        archive = self.root / 'libclang_rt.a'
        archive.write_bytes(fat_archive())
        with patch('emberbsd_sdk.bootstrap_bundle.subprocess.run') as run:
            changed = make_relative(self.root, '/producer/sdk', {'os': 'Darwin', 'cpu': 'arm64'},
                                    '/usr/bin/install_name_tool', '/usr/bin/codesign')
        self.assertEqual(changed, [])
        run.assert_not_called()

    def test_fat_static_archive_wrong_architecture_and_bad_bounds_are_refused(self):
        archive = self.root / 'libclang_rt.a'
        malformed = bytearray(fat_archive())
        struct.pack_into('>I', malformed, 16, 4096)
        for contents, message in ((fat_archive(cpu=0x1000007, subtype=3), 'does not match'),
                                  (bytes(malformed), 'slice bounds')):
            with self.subTest(message=message):
                archive.write_bytes(contents)
                with self.assertRaisesRegex(SDKError, message):
                    make_relative(self.root, '/producer/sdk', {'os': 'Darwin', 'cpu': 'arm64'},
                                  '/usr/bin/install_name_tool', '/usr/bin/codesign')

    def test_make_relative_preserves_owned_stable_install_id(self):
        lib = self.root/'lib'; lib.mkdir()
        real = lib/'libexample.1.2.dylib'
        real.write_bytes(b'\xcf\xfa\xed\xfe' + b'\0'*28)
        (lib/'libexample.1.dylib').symlink_to(real.name)
        before = dict(loads=[], install_name='/producer/sdk/lib/libexample.1.dylib', rpaths=[])
        after = dict(loads=[], install_name='@rpath/libexample.1.dylib', rpaths=[])
        with patch('emberbsd_sdk.bootstrap_bundle.read_macho_runtime', side_effect=[before, after]), \
             patch('emberbsd_sdk.bootstrap_bundle.subprocess.run') as run:
            make_relative(self.root, '/producer/sdk', {'os':'Darwin', 'cpu':'arm64'},
                          '/usr/bin/install_name_tool', '/usr/bin/codesign')
        self.assertIn('@rpath/libexample.1.dylib', run.call_args_list[0].args[0])

    def test_make_relative_refuses_foreign_or_dangling_install_id(self):
        lib = self.root/'lib'; lib.mkdir()
        real = lib/'libexample.1.2.dylib'
        real.write_bytes(b'\xcf\xfa\xed\xfe' + b'\0'*28)
        for identity in ('/foreign/libexample.1.dylib', '/producer/sdk/lib/missing.dylib',
                         '@rpath/../other.dylib'):
            with self.subTest(identity=identity), \
                 patch('emberbsd_sdk.bootstrap_bundle.read_macho_runtime', return_value=dict(
                     loads=[], install_name=identity, rpaths=[])), \
                 patch('emberbsd_sdk.bootstrap_bundle.subprocess.run') as run:
                with self.assertRaises(SDKError):
                    make_relative(self.root, '/producer/sdk', {'os':'Darwin', 'cpu':'arm64'},
                                  '/usr/bin/install_name_tool', '/usr/bin/codesign')
                run.assert_not_called()

    def test_fat_macho_image_still_requires_thin_native_runtime(self):
        image = self.root / 'libexample.dylib'
        image.write_bytes(fat_archive(b'\xcf\xfa\xed\xfe' * 2))
        with self.assertRaisesRegex(SDKError, 'thin native Mach-O'):
            make_relative(self.root, '/producer/sdk', {'os': 'Darwin', 'cpu': 'arm64'},
                          '/usr/bin/install_name_tool', '/usr/bin/codesign')

    def test_relocation_updates_files_and_real_package_ownership_once(self):
        name, prefix = unpack_package(self.package(), self.root)
        info = self.root / 'pkgdb/example-1/+BUILD_INFO'
        info.write_text('LOCALBASE=/producer/sdk\nREQUIRES=/producer/sdk/lib/example.dylib\nCFLAGS=-I/producer/sdk/include\n')
        (self.root / MANIFEST).parent.mkdir(parents=True)
        write_json(self.root / MANIFEST, dict(schema='emberbsd.bootstrap.v1', prefix=prefix,
            packages=[name], text_files=['bin/example', 'pkgdb/example-1/+CONTENTS']))
        with patch('emberbsd_sdk.bootstrap_bundle.subprocess.run') as run:
            run.return_value.returncode = 0
            initialize(self.root)
            self.assertEqual([call.args[0][-1] for call in run.call_args_list], ['rebuild', 'rebuild-tree'])
            content = (self.root / 'pkgdb/example-1/+CONTENTS').read_text()
            self.assertIn('@cwd ' + str(self.root), content)
            self.assertIn('LOCALBASE=' + str(self.root) + '\n', info.read_text())
            self.assertIn('REQUIRES=' + str(self.root) + '/lib/example.dylib', info.read_text())
            self.assertIn('CFLAGS=-I/producer/sdk/include', info.read_text())
            self.assertNotIn('MD5:old', content)
            self.assertEqual((self.root / 'bin/example').read_text(), '#!' + str(self.root) + '/bin/sh\n')
            stamp = (self.root / 'bin/example').stat().st_mtime_ns
            initialize(self.root)
            self.assertEqual(run.call_count, 2)
            self.assertEqual((self.root / 'bin/example').stat().st_mtime_ns, stamp)

    def test_interrupted_initialization_can_resume(self):
        name, prefix = unpack_package(self.package(), self.root)
        (self.root / MANIFEST).parent.mkdir(parents=True)
        write_json(self.root / MANIFEST, dict(schema='emberbsd.bootstrap.v1', prefix=prefix,
            packages=[name], text_files=['bin/example', 'pkgdb/example-1/+CONTENTS']))
        with patch('emberbsd_sdk.bootstrap_bundle.subprocess.run') as run:
            run.return_value.returncode = 1
            run.return_value.stderr = b'failure'
            with self.assertRaises(SDKError):
                initialize(self.root)
            self.assertEqual(read_json(self.root / MANIFEST)['prefix'], prefix)
            run.return_value.returncode = 0
            initialize(self.root)
            self.assertEqual(read_json(self.root / MANIFEST)['prefix'], str(self.root))

    def test_deferred_portable_hooks_complete_after_success_and_retry(self):
        from emberbsd_sdk import package_relocation as relocation
        from emberbsd_sdk.package_install import PackageInstaller

        name, prefix = unpack_package(self.package(install=True), self.root)
        database = self.root / 'pkgdb'
        package = database / name
        contents = package / '+CONTENTS'
        contents.write_text(contents.read_text() + relocation.MARKER + '\n' +
                            relocation.SOURCE + prefix + '\n' + relocation.HOOKS + '\n')
        hook = package / '+INSTALL'
        hook.write_text('#!/bin/sh\nprintf "%s\\n" "$2" >> "$PKG_PREFIX/hook-log"\n'
                        'test -f "$PKG_PREFIX/allow-hooks"\n')
        hook.chmod(0o755)
        (self.root / MANIFEST).parent.mkdir(parents=True)
        write_json(self.root / MANIFEST, dict(schema='emberbsd.bootstrap.v1', prefix=prefix,
            initialized=False, packages=[name], text_files=['bin/example', 'pkgdb/example-1/+CONTENTS']))
        actual_run = subprocess.run

        def run(command, **kwargs):
            if command[0].endswith('/sbin/pkg_admin'):
                return SimpleNamespace(returncode=0, stderr=b'')
            if command[0].endswith('/pkg_info'):
                return SimpleNamespace(returncode=0, stdout=(name + '\n').encode(), stderr=b'')
            return actual_run(command, **kwargs)

        with patch('emberbsd_sdk.bootstrap_bundle.subprocess.run', side_effect=run):
            with self.assertRaisesRegex(SDKError, 'package configuration failed'):
                initialize(self.root)
            self.assertFalse(read_json(self.root / MANIFEST)['initialized'])
            self.assertIn(relocation.HOOKS, contents.read_text())
            self.assertEqual(list(relocation.pending(self.root, database)), [name])
            (self.root / 'allow-hooks').touch()
            initialize(self.root)
            self.assertTrue(read_json(self.root / MANIFEST)['initialized'])
            self.assertNotIn(relocation.HOOKS, contents.read_text())
            self.assertIn(relocation.SOURCE + str(self.root), contents.read_text())
            self.assertEqual(list(relocation.pending(self.root, database)), [])
            self.assertFalse((package / relocation.READY).exists())
            self.assertEqual((self.root / 'hook-log').read_text().splitlines(),
                             ['PRE-INSTALL', 'PRE-INSTALL', 'POST-INSTALL'])
            crypto = SimpleNamespace(prefix=SimpleNamespace(tool=lambda _: '/fake/pkg_info', path='/fake'))
            installer = PackageInstaller(crypto, self.root / 'trust')
            self.assertEqual(installer.installed({'prefix': str(self.root), 'pkgdb': str(database)}), {name})
            initialize(self.root)
            self.assertEqual((self.root / 'hook-log').read_text().splitlines(),
                             ['PRE-INSTALL', 'PRE-INSTALL', 'POST-INSTALL'])

    def test_bootstrap_rejects_unrelocated_deferred_declaration(self):
        from emberbsd_sdk import package_relocation as relocation

        name, prefix = unpack_package(self.package(), self.root)
        contents = self.root / 'pkgdb' / name / '+CONTENTS'
        contents.write_text(contents.read_text() + relocation.MARKER + '\n' +
                            relocation.SOURCE + prefix + '\n' + relocation.HOOKS + '\n')
        (self.root / MANIFEST).parent.mkdir(parents=True)
        write_json(self.root / MANIFEST, dict(schema='emberbsd.bootstrap.v1', prefix=prefix,
            initialized=False, packages=[name], text_files=['bin/example']))
        with self.assertRaisesRegex(SDKError, 'relocation prefix differs'):
            initialize(self.root)
        self.assertIn(relocation.HOOKS, contents.read_text())
        self.assertFalse(read_json(self.root / MANIFEST)['initialized'])

    def test_external_links_and_traversal_are_rejected(self):
        for fields in ({'link': '/outside/file'}, {'link': '../../outside'}, {'path': '../outside'}):
            with self.subTest(fields=fields), self.assertRaises(SDKError):
                unpack_package(self.package(**fields), self.root)
        self.assertFalse((self.work / 'outside').exists())

    def test_internal_absolute_links_are_relative(self):
        unpack_package(self.package(link='/producer/sdk/bin/real'), self.root)
        self.assertEqual((self.root / 'bin/example').readlink(), Path('real'))

    def test_unpack_preserves_package_file_mtime(self):
        archive = self.work / 'mtime.tgz'
        with tarfile.open(archive, 'w:gz') as tar:
            for name, data, timestamp in [('+CONTENTS', b'@cwd /producer/sdk\n@name p-1\nmod.py\n', 1),
                                          ('mod.py', b'x = 1\n', 1234567890)]:
                item = tarfile.TarInfo(name)
                item.size, item.mtime = len(data), timestamp
                tar.addfile(item, io.BytesIO(data))
        unpack_package(archive, self.root)
        self.assertEqual(int((self.root / 'mod.py').stat().st_mtime), 1234567890)

    def test_initialize_refreshes_invalid_bytecode_and_preserves_valid(self):
        directory = self.root / 'lib'
        directory.mkdir()
        db = self.root / 'pkgdb/python-1'
        db.mkdir(parents=True)
        sources = {}
        caches = {}
        for name, mode, optimizations in (
            ('stale', py_compile.PycInvalidationMode.TIMESTAMP, (0, 1)),
            ('checked', py_compile.PycInvalidationMode.CHECKED_HASH, (0,)),
            ('unchecked', py_compile.PycInvalidationMode.UNCHECKED_HASH, (1,)),
            ('valid', py_compile.PycInvalidationMode.TIMESTAMP, (0,)),
        ):
            source = directory / (name + '.py')
            source.write_text('value = "old"\n')
            sources[name] = source
            for opt in optimizations:
                cache = Path(importlib.util.cache_from_source(str(source), optimization=str(opt) if opt else ''))
                py_compile.compile(str(source), cfile=str(cache), doraise=True,
                                   optimize=opt, invalidation_mode=mode)
                caches[(name, opt)] = cache
            if name != 'valid':
                source.write_text('value = "new"\n')
                if name == 'stale':
                    os.utime(source, (source.stat().st_mtime + 5, source.stat().st_mtime + 5))
        valid_before = caches[('valid', 0)].stat().st_mtime_ns
        unrelated = directory / '__pycache__/outside.cpython-315.pyc'
        ignored = directory / '__pycache__/ignored.cpython-315.pyc'
        unrelated.write_bytes(b'not a cache')
        ignored.write_bytes(b'ignored cache')
        owned = [p.relative_to(self.root).as_posix() for p in [*sources.values(), *caches.values()]]
        (db / '+CONTENTS').write_text('@cwd /producer/sdk\n@name python-1\n' + '\n'.join(owned) +
                                      '\n@ignore\nlib/__pycache__/ignored.cpython-315.pyc\n')
        (self.root / MANIFEST).parent.mkdir(parents=True)
        write_json(self.root / MANIFEST, dict(schema='emberbsd.bootstrap.v1', prefix='/producer/sdk',
            packages=['python-1'], text_files=[]))
        with patch('emberbsd_sdk.bootstrap_bundle.subprocess.run') as run:
            run.return_value.returncode = 0
            initialize(self.root)
            after = {key: (path.read_bytes(), path.stat().st_mtime_ns) for key, path in caches.items()}
            initialize(self.root)
        self.assertEqual(valid_before, caches[('valid', 0)].stat().st_mtime_ns)
        self.assertEqual(unrelated.read_bytes(), b'not a cache')
        self.assertEqual(ignored.read_bytes(), b'ignored cache')
        for (name, opt), path in caches.items():
            data = path.read_bytes()
            self.assertEqual((data, path.stat().st_mtime_ns), after[(name, opt)])
            self.assertIn('old' if name == 'valid' else 'new', marshal.loads(data[16:]).co_consts)
            flags = struct.unpack('<I', data[4:8])[0]
            self.assertEqual(flags, {'checked': 3, 'unchecked': 1}.get(name, 0))
            if flags:
                self.assertEqual(data[8:16], importlib.util.source_hash(sources[name].read_bytes()))
            else:
                self.assertEqual(data[8:16], struct.pack('<II', int(sources[name].stat().st_mtime),
                                                         sources[name].stat().st_size))

    def test_same_size_same_second_relocation_refreshes_cache(self):
        from emberbsd_sdk.bootstrap_bundle import _replace_file
        old = '/' + 'a' * (len(str(self.root)) - 1)
        source = self.root / 'module.py'
        source.write_text('value = "' + old + '/lib"\n')
        original_mtime = int(source.stat().st_mtime)
        os.utime(source, (original_mtime, original_mtime))
        cache = Path(importlib.util.cache_from_source(str(source)))
        py_compile.compile(str(source), cfile=str(cache), doraise=True)
        before_header = cache.read_bytes()[:16]
        db = self.root / 'pkgdb/python-1'
        db.mkdir(parents=True)
        (db / '+CONTENTS').write_text('@cwd ' + old + '\n@name python-1\nmodule.py\n' +
            cache.relative_to(self.root).as_posix() + '\n')
        (self.root / MANIFEST).parent.mkdir(parents=True)
        write_json(self.root / MANIFEST, dict(schema='emberbsd.bootstrap.v1', prefix=old,
            packages=['python-1'], text_files=['module.py']))
        def same_second(path, content):
            _replace_file(path, content)
            if path == source:
                os.utime(path, (original_mtime, original_mtime))
        with patch('emberbsd_sdk.bootstrap_bundle._replace_file', side_effect=same_second), \
             patch('emberbsd_sdk.bootstrap_bundle.subprocess.run') as run:
            run.return_value.returncode = 0
            initialize(self.root)
        data = cache.read_bytes()
        self.assertEqual(data[:16], before_header)
        self.assertIn(str(self.root) + '/lib', marshal.loads(data[16:]).co_consts)

    def test_archive_case_aliases_and_relocated_text(self):
        data = self.root / 'share'
        data.mkdir()
        (data / 'Name').write_text('/producer/sdk/lib\n')
        if not (data / 'name').exists():
            (data / 'name').write_text('/producer/sdk/lib\n')
        (data / 'Link').symlink_to('Name')
        if not (data / 'link').is_symlink():
            (data / 'link').symlink_to('Name')
        os.link(data / 'Name', data / 'hard')
        db = self.root / 'pkgdb/example-1'
        db.mkdir(parents=True)
        (db / '+CONTENTS').write_text('@cwd /producer/sdk\n@name example-1\n'
            'share/Name\nshare/name\nshare/Link\nshare/link\nshare/hard\n'
            '@ignore\nshare/not-present\n')
        refs = text_references(self.root, '/producer/sdk', ['example-1'])
        self.assertIn('share/Name', refs)
        self.assertIn('share/name', refs)
        self.assertNotIn('share/Link', refs)
        self.assertNotIn('share/not-present', [p.relative_to(self.root).as_posix()
                                                for p in bundle_paths(self.root, ['example-1'])])
        (self.root / MANIFEST).parent.mkdir(parents=True)
        write_json(self.root / MANIFEST, dict(schema='emberbsd.bootstrap.v1',
            prefix='/producer/sdk', packages=['example-1'], text_files=refs))
        output = self.work / 'bundle.tar.gz'
        write_archive(self.root, output, ['example-1'])
        with tarfile.open(output) as archive:
            members = {m.name: m for m in archive.getmembers()}
            for name in ('Name', 'name', 'hard'):
                member = members['ember-sdk/share/' + name]
                self.assertTrue(member.isfile(), name)
                self.assertEqual(archive.extractfile(member).read(), b'/producer/sdk/lib\n')
            for name in ('Link', 'link'):
                member = members['ember-sdk/share/' + name]
                self.assertTrue(member.issym(), name)
                self.assertEqual(member.linkname, 'Name')
            # Separate files are independently extracted and relocated on a
            # case-sensitive consumer. Case-insensitive hosts still verify the
            # exact archive entries above.
            probe = self.work / 'case-probe'
            probe.write_text('probe')
            if not (self.work / 'CASE-PROBE').exists():
                destination = self.work / 'extract'
                destination.mkdir()
                archive.extractall(destination, filter='data')
                installed = destination / 'ember-sdk'
                self.assertNotEqual((installed / 'share/Name').stat().st_ino,
                                    (installed / 'share/hard').stat().st_ino)
                with patch('emberbsd_sdk.bootstrap_bundle.subprocess.run') as run:
                    run.return_value.returncode = 0
                    initialize(installed)
                for name in ('Name', 'name', 'hard'):
                    self.assertEqual((installed / 'share' / name).read_text(),
                                     str(installed) + '/lib\n')

    def test_missing_owned_member_fails_but_ignored_member_does_not(self):
        db = self.root / 'pkgdb/example-1'
        db.mkdir(parents=True)
        contents = db / '+CONTENTS'
        contents.write_text('@cwd /producer/sdk\n@name example-1\n@ignore\nmissing\n')
        bundle_paths(self.root, ['example-1'])
        contents.write_text(contents.read_text() + 'other-missing\n')
        with self.assertRaisesRegex(SDKError, 'other-missing'):
            bundle_paths(self.root, ['example-1'])

    def test_overlapping_packages_cannot_replace_files(self):
        archive = self.package()
        unpack_package(archive, self.root)
        with self.assertRaisesRegex(SDKError, 'overlap'):
            unpack_package(archive, self.root)

    def test_nested_relocation_is_idempotent_and_respects_path_boundaries(self):
        original = b'/old/bin/python /old-other/bin/python /old\n'
        changed = relocate_text(original, b'/old', b'/new')
        self.assertEqual(changed, b'/new/bin/python /old-other/bin/python /new\n')
        self.assertEqual(relocate_text(changed, b'/old', b'/new'), changed)
        with self.assertRaises(SDKError):
            relocate_text(b'/old/lib/pkgconfig', b'/old', b'/old/lib')

    def test_package_install_scripts_keep_their_mode_and_run(self):
        archive = self.package(install=True)
        unpack_package(archive, self.root)
        database = self.root / 'pkgdb/example-1'
        script = database / '+INSTALL'
        self.assertEqual(script.stat().st_mode & 0o777, 0o755)
        (self.root / MANIFEST).parent.mkdir(parents=True)
        write_json(self.root / MANIFEST, dict(schema='emberbsd.bootstrap.v1', prefix='/producer/sdk',
            packages=['example-1'], text_files=['bin/example', 'pkgdb/example-1/+CONTENTS']))
        with patch('emberbsd_sdk.bootstrap_bundle.subprocess.run') as run:
            run.return_value.returncode = 0
            initialize(self.root)
            self.assertEqual([call.args[0][-1] for call in run.call_args_list],
                             ['PRE-INSTALL', 'POST-INSTALL', 'rebuild', 'rebuild-tree'])

    def test_same_prefix_fresh_bundle_is_registered_but_installed_move_is_refused(self):
        unpack_package(self.package(), self.root)
        (self.root / MANIFEST).parent.mkdir(parents=True)
        write_json(self.root / MANIFEST, dict(schema='emberbsd.bootstrap.v1', prefix=str(self.root),
            initialized=False, packages=['example-1'], text_files=[]))
        with patch('emberbsd_sdk.bootstrap_bundle.subprocess.run') as run:
            run.return_value.returncode = 0
            initialize(self.root)
            self.assertEqual(run.call_count, 2)
        moved = self.work / 'moved'
        self.root.rename(moved)
        with self.assertRaisesRegex(SDKError, 'cannot be moved'):
            initialize(moved)


if __name__ == '__main__':
    unittest.main()
