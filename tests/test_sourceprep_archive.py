# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Source component extraction without links or case collisions escaping a stage."""

import io
import os
import subprocess
import tarfile
import unittest

from test_core import TemporaryCase
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.sourceprep import extract_archive


class SourceArchiveTests(TemporaryCase):
    def setUp(self):
        super().setUp()
        self.archive = self.root / 'source.tar'
        self.output = self.root / 'output'; self.output.mkdir()

    def pack(self, rows):
        with tarfile.open(self.archive, 'w') as packed:
            for name, data, link in rows:
                member = tarfile.TarInfo('source/' + name)
                if link is not None:
                    member.type = tarfile.SYMTYPE; member.linkname = link
                    packed.addfile(member)
                else:
                    member.size = len(data)
                    packed.addfile(member, io.BytesIO(data))

    def extract(self, **options):
        extract_archive(self.archive, self.output, 'source', 'fixture', **options)

    def timestamp_fixture(self):
        with tarfile.open(self.archive, 'w') as packed:
            # Archive order deliberately differs from build dependency order.
            for name, data, mtime in (
                ('generated', b'generated\n', 1643858263),
                ('input', b'input\n', 1643857092),
                ('Makefile', b'all: generated\ngenerated: input\n\t@false\n', 1643858264),
            ):
                member = tarfile.TarInfo('source/' + name)
                member.size = len(data)
                member.mtime = mtime
                packed.addfile(member, io.BytesIO(data))
        self.extract()
        return self.output / 'source'

    def test_archive_mtimes_preserve_generated_file_dependency_order(self):
        root = self.timestamp_fixture()
        self.assertEqual((root / 'input').stat().st_mtime_ns, 1643857092 * 10**9)
        self.assertEqual((root / 'generated').stat().st_mtime_ns, 1643858263 * 10**9)

    @unittest.skipUnless(os.environ.get('SOURCEPREP_TEST_GMAKE'), 'select GNU make')
    def test_make_does_not_rebuild_newer_archived_output(self):
        root = self.timestamp_fixture()
        gmake = os.environ['SOURCEPREP_TEST_GMAKE']
        result = subprocess.run([gmake, '-q', '-f', 'Makefile', 'all'], cwd=root,
                                capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        os.utime(root / 'generated', (1643857091, 1643857091))
        stale = subprocess.run([gmake, '-q', '-f', 'Makefile', 'all'], cwd=root,
                               capture_output=True, check=False)
        self.assertEqual(stale.returncode, 1, stale.stderr.decode(errors='replace'))

    def test_flat_layout_retains_archive_checks(self):
        def flat(rows):
            with tarfile.open(self.archive, "w") as packed:
                for name, kind in rows:
                    member = tarfile.TarInfo(name)
                    if kind == "link":
                        member.type = tarfile.SYMTYPE
                        member.linkname = "elsewhere"
                        packed.addfile(member)
                    else:
                        member.size = 1
                        packed.addfile(member, io.BytesIO(b"x"))
        flat([("C/file.c", "file"), ("CPP/file.cpp", "file")])
        self.extract(layout="flat")
        self.assertEqual((self.output / "source/C/file.c").read_bytes(), b"x")
        self.assertEqual((self.output / "source/CPP/file.cpp").read_bytes(), b"x")
        for rows in ([ ("../escape", "file") ], [("C/alias", "link")],
                     [("C/A", "file"), ("C/a", "file")]):
            with self.subTest(rows=rows):
                flat(rows)
                with self.assertRaises(SDKError):
                    self.extract(layout="flat")

    def test_flat_layout_default_limits_metadata_and_mtime(self):
        with tarfile.open(self.archive, "w") as packed:
            for name, data, mode, mtime in (("C/a.c", b"a", 0o755, 1643857092),
                                            ("CPP/CVS/Root", b"x", 0o644, 1643857092)):
                member = tarfile.TarInfo(name)
                member.mode, member.mtime, member.size = mode, mtime, len(data)
                packed.addfile(member, io.BytesIO(data))
        with self.assertRaises(SDKError):
            self.extract()
        with self.assertRaises(SDKError):
            self.extract(layout="unknown")
        with self.assertRaises(SDKError):
            self.extract(layout="flat", max_members=1)
        (self.output / "source/C/a.c").unlink()
        self.extract(layout="flat", skip_directories=("CVS",))
        source = self.output / "source/C/a.c"
        self.assertEqual(source.stat().st_mode & 0o777, 0o755)
        self.assertEqual(source.stat().st_mtime_ns, 1643857092 * 10**9)
        self.assertFalse((self.output / "source/CPP/CVS").exists())

    def test_explicit_components_and_bounds(self):
        self.pack([('llvm/cmake/config.cmake', b'yes', None),
                   ('llvm/lib/unselected.cpp', b'unselected', None),
                   ('libcxx/CMakeLists.txt', b'yes', None)])
        self.extract(components=('llvm/cmake', 'libcxx'), max_members=3, max_bytes=6)
        self.assertTrue((self.output / 'source/llvm/cmake/config.cmake').is_file())
        self.assertTrue((self.output / 'source/libcxx/CMakeLists.txt').is_file())
        self.assertFalse((self.output / 'source/llvm/lib').exists())

    def test_member_bound_counts_unselected_archive_entries(self):
        self.pack([('llvm/unselected', b'x', None), ('libcxx/selected', b'x', None)])
        with self.assertRaisesRegex(SDKError, 'too many members'):
            self.extract(components=('libcxx',), max_members=1)

    def test_selected_byte_bound_is_enforced(self):
        self.pack([('libcxx/selected', b'xx', None)])
        with self.assertRaisesRegex(SDKError, 'size limit'):
            self.extract(components=('libcxx',), max_bytes=1)

    def test_default_retains_link_refusal(self):
        self.pack([('file', b'x', None), ('alias', b'', 'file')])
        with self.assertRaisesRegex(SDKError, 'link or special'):
            self.extract()

    def test_component_selection_retains_link_refusal(self):
        self.pack([('libcxx/alias', b'', 'real'), ('libcxx/real', b'x', None)])
        with self.assertRaisesRegex(SDKError, 'link or special'):
            self.extract(components=('libcxx',))

    def test_links_cannot_escape_or_select_unextracted_components(self):
        for target in ('../../outside', '/etc/passwd', '../llvm/real'):
            self.pack([('libcxx/alias', b'', target)])
            with self.subTest(target=target), self.assertRaises(SDKError):
                self.extract(components=('libcxx',))

    def test_case_collisions_in_selected_components_are_refused(self):
        self.pack([('libcxx/Config', b'x', None), ('libcxx/config', b'y', None)])
        with self.assertRaisesRegex(SDKError, 'duplicate'):
            self.extract(components=('libcxx',))

    def test_preexisting_symlink_parent_is_not_followed(self):
        outside = self.root / 'outside'; outside.mkdir()
        (self.output / 'source').symlink_to(outside)
        self.pack([('libcxx/file', b'x', None)])
        with self.assertRaises(SDKError):
            self.extract(components=('libcxx',))
        self.assertEqual(list(outside.iterdir()), [])

    def test_case_collisions_in_implicit_parent_directories_are_refused(self):
        self.pack([('libcxx/Config/a', b'x', None), ('libcxx/config/b', b'y', None)])
        with self.assertRaisesRegex(SDKError, 'duplicate'):
            self.extract(components=('libcxx',))

    def cvs_fixture(self):
        with tarfile.open(self.archive, 'w') as packed:
            for name, data in (('Makefile', b'all:\n'), ('CVS/Root', b':ext:cvs\n'),
                               ('lib/Config', b'x'), ('lib/CVS/Entries', b'D\n')):
                member = tarfile.TarInfo('source/' + name)
                member.size = len(data)
                packed.addfile(member, io.BytesIO(data))
            # Release source sets record CVS metadata as hard links.
            link = tarfile.TarInfo('source/lib/CVS/Root')
            link.type = tarfile.LNKTYPE; link.linkname = 'source/CVS/Root'
            packed.addfile(link)

    def test_skipped_metadata_directories_are_omitted_before_link_checks(self):
        self.cvs_fixture()
        self.extract(skip_directories=('CVS',), max_bytes=6)
        root = self.output / 'source'
        self.assertEqual(sorted(str(p.relative_to(root)) for p in root.rglob('*')),
                         ['Makefile', 'lib', 'lib/Config'])

    def test_metadata_links_are_refused_without_skip(self):
        self.cvs_fixture()
        with self.assertRaisesRegex(SDKError, 'link or special'):
            self.extract()

    def test_skipped_directories_still_count_toward_member_bound(self):
        self.cvs_fixture()
        with self.assertRaisesRegex(SDKError, 'too many members'):
            self.extract(skip_directories=('CVS',), max_members=4)

    def test_invalid_skipped_directories_are_refused(self):
        self.cvs_fixture()
        for value in ('CVS', ('a/CVS',), ('..',), ('',)):
            with self.subTest(value=value), self.assertRaisesRegex(SDKError, 'skipped directories'):
                self.extract(skip_directories=value)

    def test_missing_component_and_invalid_limits_are_refused(self):
        self.pack([('libcxx/file', b'x', None)])
        for options in ({'components': ('libunwind',)}, {'max_members': True},
                        {'max_bytes': 0}, {'components': ('../outside',)}):
            with self.subTest(options=options), self.assertRaises(SDKError):
                self.extract(**options)
