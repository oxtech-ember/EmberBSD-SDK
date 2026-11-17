# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Finite metadata fixtures only. These bytes are not a qualified toolset."""

import hashlib
from pathlib import Path
import platform
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.native_toolset import _gcc, _native_image, _native_triplet


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class DarwinSharedHeaderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'library.dylib'
        self.host = dict(os='Darwin', cpu='arm64')
        image = struct.pack('<8I', 0xfeedfacf, 0x100000c, 0, 6, 1, 24, 0, 0)
        self.data = bytearray(struct.pack('>7I', 0xcafebabe, 1, 0x100000c, 0,
                                         4096, len(image), 12).ljust(4096, b'\0') + image)

    def test_one_matching_slice_is_only_a_shared_library(self):
        self.path.write_bytes(self.data)
        _native_image(str(self.path), self.host, shared_library=True)
        with self.assertRaisesRegex(SDKError, 'fat executable slice identity mismatch'):
            _native_image(str(self.path), self.host)
        with self.assertRaisesRegex(SDKError, 'unsupported native Mach-O fat image'):
            _native_image(str(self.path), self.host, object_file=True)

    def test_multiple_wrong_arch_or_unbounded_fat_members_are_refused(self):
        for offset, value in ((4, 2), (8, 0x1000007), (12, 2), (16, 16),
                              (16, 4097), (20, 0xffffffff), (24, 32)):
            with self.subTest(offset=offset, value=value):
                data = bytearray(self.data)
                struct.pack_into('>I', data, offset, value)
                self.path.write_bytes(data)
                with self.assertRaisesRegex(SDKError, 'native Mach-O fat slice'):
                    _native_image(str(self.path), self.host, shared_library=True)

    def test_fat_member_identity_cannot_disagree_with_its_table(self):
        for offset, value in ((0, 0xfeedface), (4, 0x1000007), (8, 2), (12, 2)):
            with self.subTest(offset=offset, value=value):
                data = bytearray(self.data)
                struct.pack_into('<I', data, 4096 + offset, value)
                self.path.write_bytes(data)
                with self.assertRaisesRegex(SDKError, 'fat slice identity mismatch'):
                    _native_image(str(self.path), self.host, shared_library=True)


class DarwinExecutableHeaderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'tool'
        self.host = dict(os='Darwin', cpu='arm64')

    def image(self, *, wide=False):
        # Include an unknown arm64 subtype as current macOS universal tools do.
        members = ((0x1000007, 3), (0x100000c, 0x80000002),
                   (0x100000c, 0x8000000c))
        data = bytearray(0x3020)
        struct.pack_into('>II', data, 0, 0xcafebabf if wide else 0xcafebabe, len(members))
        for index, (cpu, subtype) in enumerate(members):
            offset = (index + 1) * 0x1000
            entry = (cpu, subtype, offset, 32, 12)
            if wide:
                struct.pack_into('>IIQQII', data, 8 + index * 32, *entry, 0)
            else:
                struct.pack_into('>5I', data, 8 + index * 20, *entry)
            struct.pack_into('<8I', data, offset, 0xfeedfacf, cpu, subtype, 2, 0, 0, 0, 0)
        return data

    def check(self, data, host=None):
        self.path.write_bytes(data)
        _native_image(str(self.path), host or self.host)

    def test_selects_native_arm64e_and_x86_64_from_both_fat_tables(self):
        for wide in (False, True):
            for cpu in ('arm64', 'aarch64', 'x86_64', 'amd64'):
                with self.subTest(wide=wide, cpu=cpu):
                    self.check(self.image(wide=wide), dict(os='Darwin', cpu=cpu))

    def test_generic_arm64_slice_is_supported(self):
        data = self.image()
        struct.pack_into('>I', data, 32, 0)
        struct.pack_into('<I', data, 0x2008, 0)
        self.check(data)

    def test_zero_excessive_and_truncated_tables_are_refused(self):
        for count in (0, 129, 0xffffffff):
            with self.subTest(count=count):
                data = self.image()
                struct.pack_into('>I', data, 4, count)
                with self.assertRaisesRegex(SDKError, 'fat table'):
                    self.check(data)
        with self.assertRaisesRegex(SDKError, 'fat table'):
            self.check(self.image()[:40])

    def test_bounds_and_alignment_of_every_slice_are_checked(self):
        # Mutate the unselected last member, after a valid native slice.
        for field, value in ((56, 16), (56, 0x3001), (56, 0xffffffff),
                             (60, 0), (60, 33), (60, 0xffffffff), (64, 32)):
            with self.subTest(field=field, value=value):
                data = self.image()
                struct.pack_into('>I', data, field, value)
                with self.assertRaisesRegex(SDKError, 'fat slice bounds'):
                    self.check(data)

    def test_overlap_and_duplicate_architectures_are_refused(self):
        data = self.image()
        struct.pack_into('>I', data, 56, 0x2000)
        with self.assertRaisesRegex(SDKError, 'overlapping'):
            self.check(data)
        data = self.image()
        struct.pack_into('>I', data, 52, 0x80000002)
        with self.assertRaisesRegex(SDKError, 'duplicate'):
            self.check(data)

    def test_missing_native_cpu_and_unsupported_subtypes_are_refused(self):
        data = self.image()
        struct.pack_into('>I', data, 4, 1)  # The table now contains x86_64 only.
        with self.assertRaisesRegex(SDKError, 'no supported native'):
            self.check(data)
        with self.assertRaisesRegex(SDKError, 'no supported native'):
            self.check(self.image(), dict(os='Darwin', cpu='riscv64'))
        for subtype in (1, 2, 0x81000002, 0x40000002):
            with self.subTest(subtype=subtype):
                data = self.image()
                struct.pack_into('>I', data, 32, subtype)
                struct.pack_into('<I', data, 0x2008, subtype)
                with self.assertRaisesRegex(SDKError, 'no supported native'):
                    self.check(data)

    def test_native_slice_header_and_type_must_match(self):
        for field, value in ((0, 0xfeedface), (4, 0x1000007), (8, 0), (12, 6), (12, 1)):
            with self.subTest(field=field, value=value):
                data = self.image()
                struct.pack_into('<I', data, 0x2000 + field, value)
                with self.assertRaisesRegex(SDKError, 'executable slice identity mismatch'):
                    self.check(data)

    def test_fat64_reserved_field_and_full_width_bounds_are_checked(self):
        data = self.image(wide=True)
        struct.pack_into('>I', data, 100, 1)
        with self.assertRaisesRegex(SDKError, 'fat slice bounds'):
            self.check(data)
        data = self.image(wide=True)
        struct.pack_into('>Q', data, 80, 1 << 40)
        with self.assertRaisesRegex(SDKError, 'fat slice bounds'):
            self.check(data)

    def test_swapped_fat_headers_are_not_a_native_disk_format(self):
        data = self.image()
        data[:4] = b'\xbe\xba\xfe\xca'
        with self.assertRaisesRegex(SDKError, 'unsupported native Mach-O fat image'):
            self.check(data)

    @unittest.skipUnless(platform.system() == 'Darwin', 'requires native macOS tools')
    def test_actual_macos_uname_and_cat_are_native_executables(self):
        host = dict(os=platform.system(), cpu=platform.machine())
        for path in ('/usr/bin/uname', '/bin/cat'):
            with self.subTest(path=path):
                _native_image(path, host)


class NativeTripletTests(unittest.TestCase):
    LINUX = dict(os='Linux', cpu='x86_64')

    def test_supported_native_tuple_forms(self):
        for host, triplets in (
                (self.LINUX, ('x86_64-pc-linux-gnu', 'x86_64-linux-gnu', 'x86_64--linux-musl', 'x86_64-linux')),
                (dict(os='Darwin', cpu='arm64'),
                 ('aarch64-apple-darwin25.0.0', 'arm64-apple-darwin25', 'aarch64-darwin')),
                (dict(os='NetBSD', cpu='amd64'),
                 ('x86_64--netbsd', 'x86_64-unknown-netbsd10.1', 'amd64-netbsd'))):
            for triplet in triplets:
                with self.subTest(triplet=triplet):
                    _gcc(dict(family='gcc', version='16.2.0', triplet=triplet), host)

    def test_cross_or_conflicting_os_triplet_is_refused(self):
        for triplet in ('aarch64-linux-gnu', 'x86_64--netbsd', 'x86_64-linux-netbsd',
                        'x86_64-pc-linux-extra-netbsd', 'x86_64-netbsd-linux-gnu',
                        'x86_64-linux-linux-gnu', 'x86_64-pc-linux-gnu-netbsd'):
            with self.subTest(triplet=triplet), \
                    self.assertRaisesRegex(SDKError, 'triplet does not describe the actual native host'):
                _native_triplet(triplet, self.LINUX)

    def test_gcc_identity_requires_gcc16(self):
        for version in ('15.2.0', '16', '17.1.0'):
            with self.subTest(version=version), self.assertRaisesRegex(SDKError, 'require GCC16'):
                _gcc(dict(family='gcc', version=version, triplet='x86_64-linux-gnu'), self.LINUX)


if __name__ == '__main__':
    unittest.main()
