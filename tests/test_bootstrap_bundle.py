# SPDX-License-Identifier: BSD-2-Clause
import io
from pathlib import Path
import sys
import tempfile
import tarfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk.bootstrap_bundle import MANIFEST, initialize, unpack_package, relocate_text
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.jsonio import read_json, write_json


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

    def test_relocation_updates_files_and_real_package_ownership_once(self):
        name, prefix = unpack_package(self.package(), self.root)
        info = self.root / 'pkgdb/example-1/+BUILD_INFO'
        info.write_text('REQUIRES=/producer/sdk/lib/example.dylib\nCFLAGS=-I/producer/sdk/include\n')
        (self.root / MANIFEST).parent.mkdir(parents=True)
        write_json(self.root / MANIFEST, dict(schema='emberbsd.bootstrap.v1', prefix=prefix,
            packages=[name], text_files=['bin/example', 'pkgdb/example-1/+CONTENTS']))
        with patch('emberbsd_sdk.bootstrap_bundle.subprocess.run') as run:
            run.return_value.returncode = 0
            initialize(self.root)
            self.assertEqual([call.args[0][-1] for call in run.call_args_list], ['rebuild', 'rebuild-tree'])
            content = (self.root / 'pkgdb/example-1/+CONTENTS').read_text()
            self.assertIn('@cwd ' + str(self.root), content)
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

    def test_external_links_and_traversal_are_rejected(self):
        for fields in ({'link': '/outside/file'}, {'link': '../../outside'}, {'path': '../outside'}):
            with self.subTest(fields=fields), self.assertRaises(SDKError):
                unpack_package(self.package(**fields), self.root)
        self.assertFalse((self.work / 'outside').exists())

    def test_internal_absolute_links_are_relative(self):
        unpack_package(self.package(link='/producer/sdk/bin/real'), self.root)
        self.assertEqual((self.root / 'bin/example').readlink(), Path('real'))

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
