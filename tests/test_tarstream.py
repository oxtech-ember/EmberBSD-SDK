# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Raw tar/PAX controls use the production bounded reader."""

import io
import tarfile
import unittest
from test_core import SDK_SOURCE
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.tarstream import CHUNK, TAR_STREAM_API, tar_records, pax_records


def entry(name='file', data=b'payload', kind=b'0', link='', size=None):
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = link
    info.size = len(data) if size is None else size
    return info.tobuf(format=tarfile.USTAR_FORMAT) + data + bytes((-len(data)) % 512)


def pax(key, value):
    record = key + b'=' + value + b'\n'
    size = len(record) + 2
    while True:
        candidate = str(size).encode() + b' ' + record
        if len(candidate) == size:
            return candidate
        size = len(candidate)


class ShortReads:
    def __init__(self, data):
        self.stream = io.BytesIO(data)
        self.maximum = 0

    def read(self, size):
        self.maximum = max(self.maximum, size)
        if size > CHUNK:
            raise AssertionError('unbounded source read')
        return self.stream.read(min(17, size))


class TarStreamTests(unittest.TestCase):
    def test_version_and_short_reads_skip_data_and_keep_raw_fields(self):
        source = ShortReads(entry('parent/file', b'x' * 1234) +
                            entry('directory///', b'', b'5') +
                            entry('link', b'', b'2', '../raw') + bytes(1024))
        records = tar_records(source)
        first = next(records)
        self.assertEqual(TAR_STREAM_API, 1)
        self.assertEqual((first.name, first.typeflag, first.size),
                         (b'parent/file', b'0', 1234))
        self.assertEqual(first.read(2), b'xx')
        directory = next(records)
        self.assertEqual(directory.name, b'directory///')
        with self.assertRaisesRegex(SDKError, 'no longer active'):
            first.read(1)
        link = next(records)
        self.assertEqual((link.linkname, link.typeflag), (b'../raw', b'2'))
        self.assertEqual(link.read(1), b'')
        self.assertEqual(list(records), [])
        self.assertLessEqual(source.maximum, CHUNK)

    def test_null_regular_type_is_not_rewritten_to_directory(self):
        header = bytearray(entry('raw/', b'', b'\0'))
        record = next(tar_records(io.BytesIO(header)))
        self.assertEqual((record.name, record.typeflag), (b'raw/', b'\0'))

    def test_stdlib_stream_hides_pax_size_but_raw_reader_preserves_it(self):
        data = entry('pax', pax(b'size', b'4'), b'x')
        data += entry('raw', b'data', size=0) + bytes(1024)
        with tarfile.open(fileobj=io.BytesIO(data), mode='r|') as archive:
            self.assertEqual(next(iter(archive)).size, 4)
        records = tar_records(io.BytesIO(data))
        extended = next(records)
        self.assertEqual(extended.typeflag, b'x')
        attributes = pax_records(extended.read(CHUNK))
        record = next(records)
        self.assertEqual((record.name, record.size, attributes),
                         (b'raw', 0, {b'size': b'4'}))
        # The caller can reject this mismatch before interpreting payload.
        records.close()

    def test_raw_global_local_types_names_and_links_are_not_applied(self):
        data = entry('global', pax(b'path', b'renamed'), b'g')
        data += entry('local', pax(b'path', b'') + pax(b'linkpath', b'long\nlink'), b'x')
        data += entry('raw', b'', b'2', 'original') + bytes(1024)
        records = tar_records(io.BytesIO(data))
        one = next(records)
        self.assertEqual(pax_records(one.read(CHUNK)), {b'path': b'renamed'})
        two = next(records)
        self.assertEqual(pax_records(two.read(CHUNK)),
                         {b'path': b'', b'linkpath': b'long\nlink'})
        three = next(records)
        self.assertEqual((three.name, three.linkname), (b'raw', b'original'))
        self.assertEqual(list(records), [])

    def test_sparse_keys_and_special_types_remain_visible_to_policy(self):
        for key in (b'GNU.sparse.size', b'GNU.sparse.map', b'SCHILY.realsize'):
            self.assertEqual(pax_records(pax(key, b'1')), {key: b'1'})
        for kind in (b'1', b'2', b'3', b'4', b'5', b'6', b'S', b'L', b'K', b'Z'):
            with self.subTest(kind=kind):
                self.assertEqual(next(tar_records(io.BytesIO(entry(kind=kind)))).typeflag, kind)

    def test_duplicate_pax_last_value_and_bytes_are_preserved(self):
        data = pax(b'path', b'one') + pax(b'path', b'\xff\nvalue')
        self.assertEqual(pax_records(data), {b'path': b'\xff\nvalue'})
        self.assertEqual(pax_records(b''), {})

    def test_bad_pax_records_and_limits(self):
        bad = (b'x x=y\n', b'0 x=y\n', b'07 x=y\n', b'7 x=y', b'7 x=yX',
               b'7 xyz\n', b'5 x=\nX', b'5 x=\0', b'5 x=\n\0')
        for data in bad:
            with self.subTest(data=data), self.assertRaises(SDKError):
                pax_records(data)
        with self.assertRaisesRegex(SDKError, 'oversized'):
            pax_records(pax(b'x', b'y'), limit=1)
        for limit in (-1, True, 1.5):
            with self.subTest(limit=limit), self.assertRaises(SDKError):
                pax_records(b'', limit=limit)

    def test_bad_header_checksum_and_payload_truncation(self):
        bad = bytearray(entry()); bad[0] ^= 1
        for data in (b'x', bytes(bad), entry()[:513], entry()[:519]):
            with self.subTest(length=len(data)), self.assertRaises(SDKError):
                list(tar_records(io.BytesIO(data)))

    def test_negative_base256_size_and_oversized_read_refuse(self):
        info = tarfile.TarInfo('negative'); info.size = -1
        with self.assertRaisesRegex(SDKError, 'negative'):
            list(tar_records(io.BytesIO(info.tobuf(format=tarfile.GNU_FORMAT))))
        records = tar_records(io.BytesIO(entry()))
        record = next(records)
        for size in (-1, CHUNK + 1, True, None):
            with self.subTest(size=size), self.assertRaises(SDKError):
                record.read(size)
        self.assertEqual(record.read(0), b'')
        records.close()
        with self.assertRaises(SDKError):
            record.read(1)

    def test_nonbinary_or_overlong_stream_read_is_rejected(self):
        class InvalidStream:
            def __init__(self, value):
                self.value = value

            def read(self, size):
                return self.value

        for value in (None, '', 'bytes', bytes(513)):
            with self.subTest(value=repr(value)), self.assertRaises(SDKError):
                list(tar_records(InvalidStream(value)))

    def test_header_prefix_and_non_utf8_name_are_preserved(self):
        name = 'prefix/' + 'x' * 98
        record = next(tar_records(io.BytesIO(entry(name))))
        self.assertEqual(record.name, name.encode())
        info = tarfile.TarInfo('file\udcff')
        header = info.tobuf(format=tarfile.USTAR_FORMAT, encoding='ascii', errors='surrogateescape')
        record = next(tar_records(io.BytesIO(header)))
        self.assertEqual(record.name, b'file\xff')


if __name__ == '__main__':
    unittest.main()
