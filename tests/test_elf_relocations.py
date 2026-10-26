# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Hand-authored sectionless ELF bytes. No compiler or extractor is used."""
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk import elf
from emberbsd_sdk.errors import SDKError


BASE = 0x400000
DYNAMIC = 0x200


def relocation(kind, *, rela=False, symbol=0x12345678):
    # ELF64 r_info: symbol in the upper word, numeric type in the lower word.
    values = (BASE + 0xF00, (symbol << 32) | kind)
    return struct.pack('<QQq', *values, -9) if rela else struct.pack('<QQ', *values)


def image(tags=(), payloads=(), *, machine=62, kind=3, extra_load=None, terminate=True):
    """Write literal ELF64 fields; these values do not come from the reader."""
    data = bytearray(0x1000)
    ident = b'\x7fELF\x02\x01\x01' + b'\0' * 9
    count = 2 + (extra_load is not None)
    # Section offset/count are zero. A section-based scan cannot find these tables.
    struct.pack_into('<16sHHIQQQIHHHHHH', data, 0, ident, kind, machine, 1,
                     0, 64, 0, 0, 64, 56, count, 0, 0, 0)
    dynamic = b''.join(struct.pack('<qQ', tag, value) for tag, value in tags)
    if terminate:
        dynamic += struct.pack('<qQ', 0, 0)
    struct.pack_into('<IIQQQQQQ', data, 64, 1, 4, 0, BASE, 0, len(data), len(data), 0x1000)
    struct.pack_into('<IIQQQQQQ', data, 120, 2, 4, DYNAMIC, BASE + DYNAMIC,
                     0, len(dynamic), len(dynamic), 8)
    if extra_load is not None:
        offset, address, size = extra_load
        struct.pack_into('<IIQQQQQQ', data, 176, 1, 4, offset, address, 0, size, size, 8)
    data[DYNAMIC:DYNAMIC + len(dynamic)] = dynamic
    for offset, payload in payloads:
        data[offset:offset + len(payload)] = payload
    return data


class RelocationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='sdk-elf-relocations-')
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name).resolve() / 'fixture.so'

    def read(self, data, **kwargs):
        self.path.write_bytes(data)
        return elf.read_elf_relocations(self.path, **kwargs)

    def test_sectionless_rel_rela_and_plt_counts_and_exact_ranges(self):
        tags = [(17, BASE + 0x600), (18, 16), (19, 16),
                (7, BASE + 0x680), (8, 48), (9, 24),
                (23, BASE + 0x700), (2, 24), (20, 7)]
        data = image(tags, [(0x600, relocation(6)),
                           (0x680, relocation(8, rela=True) + relocation(36, rela=True)),
                           (0x700, relocation(7, rela=True))])
        self.assertEqual(bytes(data[40:48]), b'\0' * 8)
        result = self.read(data, machine=62)
        self.assertEqual(elf.ELF_RELOCATIONS_API, 1)
        self.assertEqual((result['class'], result['machine'], result['type']), (64, 62, 3))
        self.assertEqual(result['count'], 4)
        self.assertEqual(result['types'], [6, 7, 8, 36])
        self.assertEqual(result['type_counts'], [dict(type=x, count=1) for x in (6, 7, 8, 36)])
        for table, name, encoding, offset, size, count in zip(
            result['tables'], ('REL', 'RELA', 'JMPREL'), ('REL', 'RELA', 'RELA'),
            (0x600, 0x680, 0x700), (16, 48, 24), (1, 2, 1)):
            self.assertEqual(table['kind'], name)
            self.assertEqual(table['encoding'], encoding)
            self.assertEqual(table['address'], BASE + offset)
            self.assertEqual(table['file_offset'], offset)
            self.assertEqual(table['size'], size)
            self.assertEqual(table['count'], count)
            self.assertEqual(table['entry_size'], 16 if encoding == 'REL' else 24)
        self.assertEqual(result['tlsdesc'], dict(plt=None, got=None))

    def test_plt_only_does_not_require_optional_entry_size_tags(self):
        for tag, rela, entry_size in ((17, False, 16), (7, True, 24)):
            tags = [(23, BASE + 0x600), (2, entry_size * 2), (20, tag)]
            for extra in ([], [(19 if tag == 17 else 9, entry_size)]):
                with self.subTest(tag=tag, extra=extra):
                    result = self.read(image(tags + extra, [(0x600, relocation(36, rela=rela) * 2)]))
                    self.assertEqual(result['count'], 2)
                    self.assertEqual(result['types'], [36])
                    self.assertEqual(result['type_counts'], [dict(type=36, count=2)])
                    self.assertEqual(result['tables'][0]['kind'], 'JMPREL')

    def test_target_types_are_metadata_not_sdk_support_policy(self):
        for machine, types in ((62, [0, 34, 35, 36, 0xFFFFFFFF]), (183, [1027, 1031])):
            payload = b''.join(relocation(t, rela=True) for t in types)
            tags = [(7, BASE + 0x600), (8, len(payload)), (9, 24)]
            with self.subTest(machine=machine):
                result = self.read(image(tags, [(0x600, payload)], machine=machine), machine=machine)
                self.assertEqual(result['types'], types)

    def test_tlsdesc_dynamic_tags_report_raw_values_including_zero(self):
        result = self.read(image([(0x6FFFFEF6, BASE + 0x100), (0x6FFFFEF7, 0)]))
        self.assertEqual(result['tlsdesc'], dict(plt=BASE + 0x100, got=0))
        self.assertEqual(result['count'], 0)

    def test_dynamic_result_compatibility_and_packed_scope_are_separate(self):
        self.path.write_bytes(image([(36, BASE + 0x600)]))
        self.assertEqual(elf.read_elf_dynamic(self.path),
                         {'class': 64, 'endian': 'little', 'machine': 62, 'type': 3,
                          'soname': None, 'needed': [], 'rpath': None, 'runpath': None, 'relro': False})
        with self.assertRaisesRegex(SDKError, 'unsupported packed'):
            elf.read_elf_relocations(self.path)

    def test_adjacent_ranges_and_empty_advertised_table(self):
        tags = [(17, BASE + 0x600), (18, 32), (19, 16),
                (7, BASE + 0x620), (8, 24), (9, 24)]
        result = self.read(image(tags, [(0x600, relocation(6) * 2), (0x620, relocation(8, rela=True))]))
        self.assertEqual(result['count'], 3)
        result = self.read(image([(7, BASE + 0x600), (8, 0), (9, 24)]))
        self.assertEqual((result['count'], result['tables'][0]['file_offset']), (0, 0x600))

    def test_missing_paired_main_or_plt_tags_is_not_an_empty_scan(self):
        for tags in ([(17, BASE + 0x600), (18, 16), (19, 16)],
                     [(7, BASE + 0x600), (8, 24), (9, 24)],
                     [(23, BASE + 0x600), (2, 24), (20, 7)]):
            for index in range(3):
                with self.subTest(tags=tags, missing=index), self.assertRaises(SDKError):
                    self.read(image(tags[:index] + tags[index + 1:]))

    def test_singleton_duplicates_fail_even_if_values_are_identical(self):
        for tag in (2, 7, 8, 9, 17, 18, 19, 20, 23,
                    0x6FFFFFF9, 0x6FFFFFFA, 0x6FFFFEF6, 0x6FFFFEF7):
            with self.subTest(tag=tag), self.assertRaisesRegex(SDKError, 'duplicate.*singleton'):
                self.read(image([(tag, 0), (tag, 0)]))

    def test_wrong_entries_sizes_or_partial_entries_fail(self):
        for tags in ([(7, BASE + 0x600), (8, 24), (9, 16)],
                     [(17, BASE + 0x600), (18, 16), (19, 24)],
                     [(7, BASE + 0x600), (8, 23), (9, 24)],
                     [(17, BASE + 0x600), (18, 17), (19, 16)],
                     [(23, BASE + 0x600), (2, 16), (20, 7)],
                     [(23, BASE + 0x600), (2, 24), (20, 36)],
                     [(9, 24)], [(19, 16)]):
            with self.subTest(tags=tags), self.assertRaises(SDKError):
                self.read(image(tags))

    def test_all_known_packed_tags_fail_even_for_zero_sized_payloads(self):
        # gABI RELR and Android APS2 / legacy RELR descriptors.
        for tag in (35, 36, 37, 0x6000000F, 0x60000010, 0x60000011, 0x60000012,
                    0x6FFFE000, 0x6FFFE001, 0x6FFFE003, 0x6FFFE005):
            with self.subTest(tag=tag), self.assertRaisesRegex(SDKError, 'unsupported packed'):
                self.read(image([(tag, 0)]))

    def test_nonempty_table_overlap_and_metadata_aliases_fail(self):
        for pointer in (BASE + 0x600, BASE + 0x610, BASE + DYNAMIC, BASE + 64, BASE):
            tags = [(7, BASE + 0x600), (8, 48), (9, 24),
                    (23, pointer), (2, 24), (20, 7)]
            with self.subTest(pointer=pointer), self.assertRaisesRegex(SDKError, 'overlapping'):
                self.read(image(tags))
        tags = [(7, BASE + 0x600), (8, 24), (9, 24),
                (23, BASE + 0x10000), (2, 24), (20, 7)]
        with self.assertRaisesRegex(SDKError, 'overlapping'):
            self.read(image(tags, extra_load=(0x600, BASE + 0x10000, 0x100)))

    def test_ambiguous_or_non_file_backed_mapping_and_overflow_fail(self):
        tags = [(7, BASE + 0x600), (8, 24), (9, 24)]
        with self.assertRaisesRegex(SDKError, 'unique file-backed'):
            self.read(image(tags, extra_load=(0x600, BASE + 0x600, 0x100)))
        for pointer in (BASE + 0x2000, (1 << 64) - 8):
            with self.subTest(pointer=pointer), self.assertRaises(SDKError):
                self.read(image([(7, pointer), (8, 24), (9, 24)]))
        data = image([(7, BASE + 0x900), (8, 24), (9, 24)])
        struct.pack_into('<Q', data, 64 + 32, 0x800)  # PT_LOAD file bytes end before BSS.
        with self.assertRaisesRegex(SDKError, 'file-backed'):
            self.read(data)

    def test_truncated_unterminated_and_malformed_dynamic_layout_fail(self):
        for data in (image()[:120], image()[:-1], image([(9, 24)], terminate=False)):
            with self.subTest(size=len(data)), self.assertRaises(SDKError):
                self.read(data)
        for offset, value in ((120 + 8, 0x208), (120 + 32, 15), (120 + 16, BASE + 0x208)):
            data = image()
            struct.pack_into('<Q', data, offset, value)
            with self.subTest(offset=offset), self.assertRaises(SDKError):
                self.read(data)

    def test_entry_limit_and_relative_prefix_counts_are_bounded(self):
        tags = [(7, BASE + 0x600), (8, 48), (9, 24)]
        with patch.object(elf, 'MAX_RELOCATION_ENTRIES', 1), self.assertRaisesRegex(SDKError, 'exceeds limit'):
            self.read(image(tags))
        result = self.read(image(tags + [(0x6FFFFFF9, 2)]))
        self.assertEqual(result['count'], 2)
        for extra in ([(0x6FFFFFF9, 3)], [(0x6FFFFFFA, 1)]):
            with self.subTest(extra=extra), self.assertRaisesRegex(SDKError, 'relative relocation count'):
                self.read(image(tags + extra))

    def test_causal_type_and_plt_size_changes_affect_the_scan(self):
        tags = [(23, BASE + 0x600), (2, 48), (20, 7)]
        data = image(tags, [(0x600, relocation(8, rela=True) + relocation(36, rela=True))])
        self.assertEqual(self.read(data)['types'], [8, 36])
        struct.pack_into('<Q', data, 0x600 + 24 + 8, (7 << 32) | 8)
        self.assertEqual(self.read(data)['types'], [8])
        struct.pack_into('<Q', data, DYNAMIC + 16 + 8, 49)
        with self.assertRaises(SDKError):
            self.read(data)

    def test_machine_class_type_and_file_identity_guards_remain_live(self):
        with self.assertRaises(SDKError):
            self.read(image(machine=183), machine=62)
        with self.assertRaises(SDKError):
            self.read(image(kind=1))
        for offset, value in ((4, 1), (5, 2), (6, 0)):
            data = image(); data[offset] = value
            with self.subTest(offset=offset), self.assertRaises(SDKError):
                self.read(data)
        self.path.write_bytes(image())
        link = self.path.with_name('link.so'); link.symlink_to(self.path)
        with self.assertRaises(SDKError):
            elf.read_elf_relocations(link)
        with patch.object(elf, 'verify_stable', side_effect=SDKError('changed fixture')), \
             self.assertRaisesRegex(SDKError, 'changed fixture'):
            elf.read_elf_relocations(self.path)


if __name__ == '__main__':
    unittest.main()
