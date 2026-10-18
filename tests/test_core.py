# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Host API contracts. ELF fixtures deliberately separate header and dynamic scope."""

import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
SDK_SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SDK_SOURCE / 'src'))
from emberbsd_sdk import API_VERSION
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk import elf, inventory, jsonio, paths, process, provenance


def header(machine=183, kind=3):
    data = bytearray(64)
    data[:7] = b'\x7fELF\x02\x01\x01'
    struct.pack_into('<HHI', data, 16, kind, machine, 1)
    struct.pack_into('<H', data, 52, 64)
    return bytes(data)


def archive(data, name='fixture.o/'):
    record = f'{name:<16}{0:<12}{0:<6}{0:<6}{"100644":<8}{len(data):<10}`\n'.encode()
    return b'!<arch>\n' + record + data + (b'\n' if len(data) % 2 else b'')


def dynamic_elf():
    data = bytearray(header())
    struct.pack_into('<Q', data, 32, 64)
    struct.pack_into('<HH', data, 54, 56, 3)
    strings = b'\0libfixture.so.1\0libc.so.12\0/unsafe\0$ORIGIN\0'
    dynamic = b''.join(struct.pack('<qQ', tag, value) for tag, value in ((5, 0x1000 + 400), (10, len(strings)), (14, 1), (1, 17), (15, 28), (29, 36), (0, 0)))
    data.extend(struct.pack('<IIQQQQQQ', 1, 4, 0, 0x1000, 0, 512, 512, 4096))
    data.extend(struct.pack('<IIQQQQQQ', 2, 4, 256, 0x1100, 0, len(dynamic), len(dynamic), 8))
    data.extend(struct.pack('<IIQQQQQQ', 0x6474e552, 4, 256, 0x1100, 0, len(dynamic), len(dynamic), 8))
    data.extend(b'\0' * (512 - len(data)))
    data[256:256 + len(dynamic)] = dynamic
    data[400:400 + len(strings)] = strings
    return bytes(data)


class TemporaryCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='sdk-python-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def write(self, name, data, mode=0o644):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.encode() if isinstance(data, str) else data)
        path.chmod(mode)
        return path


class CoreTests(TemporaryCase):
    def test_api_and_isolated_import_are_stdlib_only_and_read_only(self):
        before = sorted(str(p) for p in SDK_SOURCE.rglob('__pycache__'))
        code = 'import sys; sys.dont_write_bytecode=True; sys.path.insert(0,sys.argv[1]); from emberbsd_sdk import API_VERSION, paths, jsonio, inventory, process, provenance, elf; print(API_VERSION)'
        result = subprocess.run([sys.executable, '-B', '-I', '-S', '-c', code, str(SDK_SOURCE / 'src')], cwd=self.root, capture_output=True)
        self.assertEqual((result.returncode, result.stdout), (0, b'1\n'), result.stderr)
        self.assertEqual(sorted(str(p) for p in SDK_SOURCE.rglob('__pycache__')), before)
        self.assertEqual(API_VERSION, 1)

    def test_paths_support_unicode_and_spaces_from_unrelated_cwd(self):
        place = self.root / 'space пример'; place.mkdir()
        self.assertEqual(paths.canonical_existing(place), str(place))
        self.assertEqual(paths.canonical_new(place / 'new' / 'name'), str(place / 'new' / 'name'))
        with self.assertRaisesRegex(SDKError, 'unsupported character'):
            paths.build_path(place)

    def test_overlap_resolves_aliases_and_inode(self):
        parent = self.root / 'parent'; parent.mkdir()
        (self.root / 'alias').symlink_to(parent)
        with self.assertRaisesRegex(SDKError, 'overlapping'):
            paths.reject_overlap(self.root / 'alias' / 'new', parent)
        file = self.write('file', 'x')
        os.link(file, self.root / 'hardlink')
        with self.assertRaisesRegex(SDKError, 'overlapping'):
            paths.reject_overlap(file, self.root / 'hardlink')
        paths.reject_overlap(parent, self.root / 'sibling')

    def test_real_directory_refuses_symlink(self):
        (self.root / 'dir').mkdir(); (self.root / 'link').symlink_to('dir')
        with self.assertRaisesRegex(SDKError, 'real directory'):
            paths.real_directory(self.root / 'link')

    def test_streaming_hash_algorithms_and_stable_identity(self):
        payload = b'abc' * 1_000_000
        file = self.write('data', payload)
        for algorithm in ('sha256', 'sha512', 'sha1'):
            self.assertEqual(inventory.hash_file(file, algorithm), hashlib.new(algorithm, payload).hexdigest())
        self.assertEqual(inventory.sha256_file(file), hashlib.sha256(payload).hexdigest())
        with self.assertRaises(SDKError):
            inventory.hash_file(file, 'md5')
        (self.root / 'link').symlink_to(file)
        with self.assertRaises(SDKError):
            inventory.sha256_file(self.root / 'link')
        original = inventory.verify_stable
        def mutate(path, before):
            file.write_bytes(b'changed')
            original(path, before)
        with patch.object(inventory, 'verify_stable', mutate), self.assertRaisesRegex(SDKError, 'changed'):
            inventory.sha256_file(file)

    def test_json_duplicate_nonfinite_utf8_depth_size(self):
        bad = (b'{"x":1,"x":2}', b'NaN', b'Infinity', b'1e9999', b'"\xff"', b'"\\ud800"', b'[' * 34 + b'0' + b']' * 34)
        for data in bad:
            with self.subTest(data=data[:30]), self.assertRaises(SDKError):
                jsonio.loads_json(data)
        with self.assertRaisesRegex(SDKError, 'size limit'):
            jsonio.loads_json(b'"long"', max_bytes=2)
        file = self.write('json', '{"a":[1,true,"пример"]}')
        self.assertEqual(jsonio.read_json(file), {'a': [1, True, 'пример']})
        with self.assertRaises(SDKError):
            jsonio.read_json(file, max_bytes=2)

    def test_json_exclusive_durable_and_symlink_refusal(self):
        file = self.root / 'json'; jsonio.write_json(file, {'x': 1})
        before = file.read_bytes()
        with self.assertRaises(FileExistsError):
            jsonio.write_json(file, {})
        self.assertEqual(file.read_bytes(), before)
        (self.root / 'alias').symlink_to(file)
        with self.assertRaises((OSError, SDKError)):
            jsonio.write_json(self.root / 'alias', {}, exclusive=False)
        self.assertEqual(file.read_bytes(), before)
        jsonio.write_json(file, {'y': 2}, exclusive=False)
        self.assertEqual(jsonio.read_json(file), {'y': 2})

    def test_inventory_modes_bytes_and_sorted_paths(self):
        file = self.write('payload/z', b'\x00\xff', 0o640)
        self.write('payload/a', b'')
        (file.parent / 'link').symlink_to('z')
        rows = inventory.snapshot_tree(file.parent)
        self.assertEqual(list(rows), ['a', 'link', 'z'])
        self.assertEqual(rows['z'], dict(kind='file', mode=0o640, size=2, sha256=hashlib.sha256(b'\0\xff').hexdigest()))
        self.assertEqual(rows['link']['target'], 'z')

    def test_inventory_refuses_special_control_and_escaped_links(self):
        root = self.root / 'payload'; root.mkdir()
        for target in ('/outside', '../outside', 'missing'):
            self.write('outside', 'x')
            link = root / 'link'; link.symlink_to(target)
            with self.assertRaises(SDKError):
                inventory.snapshot_tree(root)
            link.unlink()
        self.write('payload/bad\nname', 'x')
        with self.assertRaisesRegex(SDKError, 'control character'):
            inventory.snapshot_tree(root)
        (root / 'bad\nname').unlink()
        if hasattr(os, 'mkfifo'):
            os.mkfifo(root / 'fifo')
            with self.assertRaisesRegex(SDKError, 'special file'):
                inventory.snapshot_tree(root)

    def test_checked_argv_bytes_and_environment(self):
        result = process.capture([sys.executable, '-c', 'import os,sys;sys.stdout.buffer.write(bytes([255]));sys.stderr.write(os.environ["EXPLICIT"])'], cwd=self.root, env={'EXPLICIT': 'yes'})
        self.assertEqual((result.stdout, result.stderr), (b'\xff', b'yes'))
        with self.assertRaisesRegex(SDKError, 'command failed'):
            process.capture([sys.executable, '-c', 'raise SystemExit(7)'])
        with self.assertRaises(SDKError):
            process.capture('echo shell')
        self.assertNotIn('CC', process.sanitized_environment({'CC': 'clang'}))

    def test_elf_header_scope_and_refusals(self):
        file = self.write('elf', header(kind=1))
        self.assertEqual(elf.read_elf_header(file, 183, 1)['machine'], 183)
        self.assertIsNone(elf.parse_elf_header(b'text'))
        for data, machine, kind in ((b'\x7fELF', 183, 1), (header(62), 183, None), (header(kind=3), 183, 1)):
            with self.subTest(data=data[:8]), self.assertRaises(SDKError):
                elf.parse_elf_header(data, machine=machine, required_type=kind)
        self.assertEqual(elf.read_elf_dynamic(file)['needed'], [])

    def test_dynamic_fields_and_bounds(self):
        file = self.write('elf', dynamic_elf())
        result = elf.read_elf_dynamic(file, machine=183)
        self.assertEqual((result['soname'], result['needed'], result['rpath'], result['runpath'], result['relro']), ('libfixture.so.1', ['libc.so.12'], '/unsafe', '$ORIGIN', True))
        for offset, packing, value in ((32, '<Q', 999999), (54, '<H', 55), (256 + 8, '<Q', 0x900000), (256 + 16 + 8, '<Q', 1 << 30), (256 + 32 + 8, '<Q', 99999)):
            bad = bytearray(dynamic_elf()); struct.pack_into(packing, bad, offset, value); file.write_bytes(bad)
            with self.subTest(offset=offset), self.assertRaises(SDKError):
                elf.read_elf_dynamic(file)
        file.write_bytes(dynamic_elf()[:300])
        with self.assertRaises(SDKError):
            elf.read_elf_dynamic(file)

    def test_archive_gnu_bsd_and_malformed(self):
        file = self.write('file.a', archive(header(kind=1)))
        self.assertEqual(elf.read_archive(file, 183), {'elf_members': 1, 'machine': 183})
        file.write_bytes(archive(b'index', '/') + archive(b'long-name.o/\n', '//')[8:] + archive(header(kind=1), '/0')[8:])
        self.assertEqual(elf.read_archive(file, 183)['elf_members'], 1)
        name = b'long-object-name.o\0'; index = b'__.SYMDEF SORTED\0'
        file.write_bytes(archive(index + b'index', '#1/' + str(len(index))) + archive(name + header(kind=1), '#1/' + str(len(name)))[8:])
        self.assertEqual(elf.read_archive(file, 183)['elf_members'], 1)
        for data in (b'!<thin>\n', archive(b'text'), archive(header(62, 1)), archive(header(kind=1))[:-1], archive(header(kind=1), '/999')):
            file.write_bytes(data)
            with self.subTest(data=data[:24]), self.assertRaises(SDKError):
                elf.read_archive(file, 183)

    def test_posix_unavailable_operations_reject_early(self):
        with patch.object(process.os, 'name', 'nt'):
            with self.assertRaisesRegex(SDKError, 'POSIX'):
                process.OwnedProcessGroup(['unused'], cwd=self.root, env={}, stdout=None, stderr=None)
            with self.assertRaisesRegex(SDKError, 'POSIX'):
                process.ContextLocks([], self.root)

class AdditionalCoreTests(TemporaryCase):
    def test_relro_requires_positive_contained_memory_range(self):
        file = self.write('elf', dynamic_elf())
        self.assertTrue(elf.read_elf_dynamic(file)['relro'])
        for offset, value in ((64 + 112 + 40, 0), (64 + 112 + 16, 0x900000), (64 + 112 + 40, 1000)):
            data = bytearray(dynamic_elf()); struct.pack_into('<Q', data, offset, value); file.write_bytes(data)
            with self.subTest(offset=offset, value=value), self.assertRaises(SDKError):
                elf.read_elf_dynamic(file)
        data = bytearray(dynamic_elf()); struct.pack_into('<I', data, 64 + 56, 0x6474e552); file.write_bytes(data)
        with self.assertRaises(SDKError): elf.read_elf_dynamic(file)

    def test_atomic_replacement_preserves_old_hardlinked_receipt(self):
        receipt = self.write('receipt.json', '{"accepted":true}\n')
        output = self.root / 'output.json'; os.link(receipt, output)
        original = receipt.read_bytes(); original_inode = receipt.stat().st_ino
        jsonio.write_json(output, {'new': 1}, exclusive=False)
        self.assertEqual(receipt.read_bytes(), original)
        self.assertEqual(receipt.stat().st_ino, original_inode)
        self.assertNotEqual(output.stat().st_ino, original_inode)
        self.assertEqual(jsonio.read_json(output), {'new': 1})

    def test_capture_explicit_bytes_input(self):
        result = process.capture([sys.executable, '-c', 'import sys;sys.stdout.buffer.write(sys.stdin.buffer.read())'], input=b'\x00\xff')
        self.assertEqual(result.stdout, b'\x00\xff')
        with self.assertRaises(SDKError): process.capture(['unused'], input='text')

class CanonicalTraversalTests(TemporaryCase):
    def test_symlink_parent_traversal_uses_filesystem_semantics(self):
        (self.root / 'real/sub').mkdir(parents=True)
        target = self.write('real/payload', 'preserve')
        (self.root / 'alias').symlink_to('real/sub')
        traversed = str(self.root / 'alias') + '/../payload'
        self.assertEqual(paths.canonical_existing(traversed), str(target))
        self.assertEqual(paths.canonical_new(traversed), str(target))
        with self.assertRaisesRegex(SDKError, 'overlapping'):
            paths.reject_overlap(traversed, target)
        self.assertEqual(paths.canonical_new(str(self.root / 'alias') + '/../new/path'), str(self.root / 'real/new/path'))


class CaseIdentityTests(TemporaryCase):
    def test_case_alias_existing_ancestor_blocks_new_child_overlap(self):
        source = self.root / 'Source'; source.mkdir()
        alias = self.root / 'source'
        if not alias.exists() or not os.path.samefile(source, alias):
            self.skipTest('filesystem does not expose case aliases')
        # Distinct directories must remain usable on the same filesystem.
        other = self.root / 'Distinct'; other.mkdir()
        paths.reject_overlap(source, other / 'new')
        with self.assertRaisesRegex(SDKError, 'overlapping'):
            paths.reject_overlap(source, alias / 'new-output')
        self.assertTrue(paths.inside(alias / 'new-output', source))
        self.assertFalse(paths.inside(alias, source))


if __name__ == '__main__':
    unittest.main()
