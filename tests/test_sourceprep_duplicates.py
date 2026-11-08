# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Repeated source members must describe the same extracted file."""

import io
import stat
import tarfile
import time
import warnings
import zipfile

from test_core import TemporaryCase
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.sourceprep import extract_archive


class DuplicateSourceTests(TemporaryCase):
    def fixture(self, kind, changes=None, *, first_changes=None):
        first = dict(name='source/file', data=b'source data', mode=0o755,
                     mtime=1784880175.7770188, type='file')
        second = first | (changes or {})
        first.update(first_changes or {})
        archive = self.root / ('source.' + kind)
        rows = (first, second)
        if kind == 'tar':
            with tarfile.open(archive, 'w') as packed:
                for row in rows:
                    member = tarfile.TarInfo(row['name'])
                    member.mode, member.mtime = row['mode'], row['mtime']
                    if row['type'] == 'link':
                        member.type, member.linkname = tarfile.SYMTYPE, 'file'
                        packed.addfile(member)
                    else:
                        member.size = len(row['data'])
                        packed.addfile(member, io.BytesIO(row['data']))
        else:
            with zipfile.ZipFile(archive, 'w') as packed, warnings.catch_warnings():
                warnings.simplefilter('ignore', UserWarning)
                for row in rows:
                    member = zipfile.ZipInfo(row['name'], time.gmtime(row['mtime'])[:6])
                    member.create_system = 3
                    file_type = stat.S_IFLNK if row['type'] == 'link' else stat.S_IFREG
                    member.external_attr = (file_type | row['mode']) << 16
                    packed.writestr(member, b'file' if row['type'] == 'link' else row['data'])
        output = self.root / ('output-' + str(len(list(self.root.glob('output-*')))))
        output.mkdir()
        return archive, output

    def test_identical_regular_members_keep_data_mode_and_timestamp(self):
        for kind in ('tar', 'zip'):
            with self.subTest(kind=kind):
                archive, output = self.fixture(kind)
                extract_archive(archive, output, 'source', 'fixture')
                path = output / 'source/file'
                self.assertEqual(path.read_bytes(), b'source data')
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o755)
                expected = 1784880175.7770188 if kind == 'tar' else 1784880174
                self.assertAlmostEqual(path.stat().st_mtime, expected, places=6)

    def test_changed_payload_metadata_and_path_are_refused(self):
        changes = ({'data': b'changed dat'}, {'data': b'short'}, {'mode': 0o644},
                   {'mtime': 1784880178}, {'name': 'source/File'}, {'name': 'source/file/'})
        for kind in ('tar', 'zip'):
            for changed in changes:
                with self.subTest(kind=kind, changed=changed):
                    archive, output = self.fixture(kind, changed)
                    with self.assertRaisesRegex(SDKError, 'duplicate'):
                        extract_archive(archive, output, 'source', 'fixture')
                    self.assertEqual((output / 'source/file').read_bytes(), b'source data')

    def test_links_cannot_replace_or_become_regular_members(self):
        for kind in ('tar', 'zip'):
            for first_link in (False, True):
                with self.subTest(kind=kind, first_link=first_link):
                    archive, output = self.fixture(kind,
                        None if first_link else {'type': 'link'},
                        first_changes={'type': 'link'} if first_link else None)
                    with self.assertRaisesRegex(SDKError, 'duplicate'):
                        extract_archive(archive, output, 'source', 'fixture', internal_symlinks=True)

    def test_repeated_members_count_toward_both_limits(self):
        for kind in ('tar', 'zip'):
            for options, error in (({'max_members': 1}, 'too many members'),
                                   ({'max_bytes': 11}, 'size limit')):
                with self.subTest(kind=kind, options=options):
                    archive, output = self.fixture(kind)
                    with self.assertRaisesRegex(SDKError, error):
                        extract_archive(archive, output, 'source', 'fixture', **options)
