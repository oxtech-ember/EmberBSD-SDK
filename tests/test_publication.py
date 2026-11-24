# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted). Native filesystem and isolated ABI controls.
"""Exercise no-replace publication without any package or producer policy."""

import ctypes
import errno
import os
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_core import TemporaryCase
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk import publication


class NativeFunction:
    def __init__(self, error=0):
        self.error = error
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        ctypes.set_errno(self.error)
        return -1 if self.error else 0


class KernelFlags:
    def __init__(self, flags=1, result=0):
        self.flags = flags
        self.result = result

    def __call__(self, name, buffer, size, new, length):
        assert name == b'vfs.generic.renameat2_flags' and new is None and length == 0
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_int))[0] = self.flags
        return self.result


class PublicationTests(TemporaryCase):
    def source(self, name='source'):
        source = self.root / name
        source.mkdir()
        (source / 'payload').write_bytes(b'original\x00bytes')
        (source / 'alias').symlink_to('payload')
        return source

    def assert_source(self, source):
        self.assertEqual((source / 'payload').read_bytes(), b'original\x00bytes')
        self.assertEqual(os.readlink(source / 'alias'), 'payload')

    def test_native_exchange_swaps_existing_directory_entries(self):
        first = self.source('first')
        second = self.source('second')
        (first / 'payload').write_bytes(b'first')
        (second / 'payload').write_bytes(b'second')
        first_inode, second_inode = first.stat().st_ino, second.stat().st_ino
        publication.require_directory_exchange()
        self.assertIsNone(publication.exchange_directories(first, second))
        self.assertEqual(first.stat().st_ino, second_inode)
        self.assertEqual(second.stat().st_ino, first_inode)
        self.assertEqual((first / 'payload').read_bytes(), b'second')
        self.assertEqual((second / 'payload').read_bytes(), b'first')
        self.assertEqual(os.readlink(first / 'alias'), 'payload')
        self.assertEqual(os.readlink(second / 'alias'), 'payload')

    def test_exchange_rejects_same_nested_and_non_directory_entries(self):
        source = self.source()
        nested = source / 'nested'
        nested.mkdir()
        file = self.write('file', 'keep')
        for first, second in ((source, source), (source, nested), (file, self.source('other')),
                              (source, self.root / 'missing')):
            with self.subTest(first=first, second=second), self.assertRaises((SDKError, OSError)):
                publication.exchange_directories(first, second)
        self.assert_source(source)
        self.assertEqual(file.read_text(), 'keep')

    def test_emberbsd_exchange_capability_is_checked_before_native_call(self):
        for query in (KernelFlags(flags=1), KernelFlags(result=-1)):
            function = NativeFunction()
            library = SimpleNamespace(renameat2=function, sysctlbyname=query)
            with patch.object(publication.sys, 'platform', 'netbsd11'), \
                    patch.object(publication.ctypes, 'CDLL', return_value=library):
                with self.assertRaisesRegex(SDKError, 'exchange.*running EmberBSD kernel'):
                    publication.exchange_directories('absent', 'target')
            self.assertEqual(function.calls, [])

    def test_native_success_preserves_inode_bytes_modes_and_links(self):
        source = self.source('source space пример')
        source.chmod(0o750)
        before = source.stat()
        destination = self.root / 'output space другой'
        publication.require_directory_publication()
        self.assertEqual(publication.DIRECTORY_PUBLICATION_API, 1)
        self.assertIsNone(publication.publish_directory_noreplace(source, destination))
        after = destination.stat()
        self.assertEqual((before.st_dev, before.st_ino, before.st_mode),
                         (after.st_dev, after.st_ino, after.st_mode))
        self.assertFalse(source.exists())
        self.assert_source(destination)

    def test_native_existing_entries_are_not_replaced(self):
        for kind in ('file', 'empty', 'nonempty', 'link', 'dangling'):
            with self.subTest(kind=kind):
                source = self.source('source-' + kind)
                destination = self.root / ('output-' + kind)
                if kind == 'file':
                    destination.write_bytes(b'keep-file')
                elif kind in ('empty', 'nonempty'):
                    destination.mkdir()
                    if kind == 'nonempty':
                        (destination / 'sentinel').write_bytes(b'keep-directory')
                else:
                    destination.symlink_to(source if kind == 'link' else self.root / 'absent')
                before = destination.lstat()
                with self.assertRaises(OSError):
                    publication.publish_directory_noreplace(source, destination)
                after = destination.lstat()
                self.assertEqual((before.st_dev, before.st_ino, before.st_mode),
                                 (after.st_dev, after.st_ino, after.st_mode))
                self.assert_source(source)
                if kind == 'file':
                    self.assertEqual(destination.read_bytes(), b'keep-file')
                elif kind in ('empty', 'nonempty'):
                    self.assertEqual(sorted(p.name for p in destination.iterdir()),
                                     ['sentinel'] if kind == 'nonempty' else [])
                else:
                    self.assertEqual(os.readlink(destination),
                                     str(source if kind == 'link' else self.root / 'absent'))

    def test_late_destination_is_rejected_by_real_native_operation(self):
        for empty in (True, False):
            with self.subTest(empty=empty):
                source = self.source('source-' + str(empty))
                destination = self.root / ('race-' + str(empty))
                function, relative = publication._native_rename()
                created = None

                def race(*args):
                    nonlocal created
                    subprocess.run([sys.executable, '-c',
                                    'import os, sys\nos.mkdir(sys.argv[1])',
                                    str(destination)], check=True)
                    if not empty:
                        (destination / 'sentinel').write_bytes(b'keep')
                    created = destination.lstat()
                    return function(*args)
                with patch.object(publication, '_native_rename', return_value=(race, relative)):
                    with self.assertRaises(OSError) as caught:
                        publication.publish_directory_noreplace(source, destination)
                self.assertEqual(caught.exception.errno, errno.EEXIST)
                after = destination.lstat()
                self.assertEqual((after.st_dev, after.st_ino), (created.st_dev, created.st_ino))
                self.assert_source(source)
                self.assertEqual([p.name for p in destination.iterdir()],
                                 [] if empty else ['sentinel'])
                if not empty:
                    self.assertEqual((destination / 'sentinel').read_bytes(), b'keep')

    def test_source_link_file_and_missing_are_rejected(self):
        source = self.source()
        link = self.root / 'source-link'; link.symlink_to(source)
        file = self.write('source-file', 'keep')
        for value in (link, file, self.root / 'missing'):
            with self.subTest(value=value), self.assertRaises(SDKError):
                publication.publish_directory_noreplace(value, self.root / 'output')
        self.assert_source(source)
        self.assertTrue(link.is_symlink())
        self.assertEqual(file.read_text(), 'keep')
        self.assertFalse((self.root / 'output').exists())

    def test_same_source_and_nested_destination_are_rejected(self):
        source = self.source()
        for destination in (source, source / 'nested'):
            with self.subTest(destination=destination), self.assertRaises(SDKError):
                publication.publish_directory_noreplace(source, destination)
        self.assert_source(source)
        self.assertFalse((source / 'nested').exists())

    def test_parent_alias_resolves_without_following_destination_leaf(self):
        source = self.source()
        alias = self.root / 'parent-alias'; alias.symlink_to(self.root)
        destination = alias / 'output'
        publication.publish_directory_noreplace(source, destination)
        self.assert_source(self.root / 'output')
        self.assertTrue(alias.is_symlink())

    def test_bad_paths_and_missing_parent_do_not_publish(self):
        source = self.source()
        for destination in ('', 'bad\x00path', self.root / 'missing' / 'out', str(self.root) + '/'):
            with self.subTest(destination=destination), self.assertRaises(SDKError):
                publication.publish_directory_noreplace(source, destination)
        self.assert_source(source)

    def test_native_adapter_abi_matches_literal_platform_contracts(self):
        source = self.source()
        destination = self.root / 'output'
        for platform, symbol, arguments, types in (
            ('darwin', 'renamex_np', (os.fsencode(source), os.fsencode(destination), 4),
             (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)),
            ('linux', 'renameat2', (-100, os.fsencode(source), -100, os.fsencode(destination), 1),
             (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)),
            ('netbsd11', 'renameat2', (-100, os.fsencode(source), -100, os.fsencode(destination), 1),
             (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint))):
            with self.subTest(platform=platform):
                function = NativeFunction()
                library = SimpleNamespace(sysctlbyname=KernelFlags(), **{symbol: function})
                with patch.object(publication.sys, 'platform', platform), \
                        patch.object(publication.ctypes, 'CDLL', return_value=library) as loader:
                    publication.require_directory_publication()
                    self.assertEqual(function.calls, [])
                    publication.publish_directory_noreplace(source, destination)
                self.assertEqual(function.calls, [arguments])
                self.assertEqual(tuple(function.argtypes), types)
                self.assertIs(function.restype, ctypes.c_int)
                loader.assert_called_with(None, use_errno=True)
        for platform, symbol, arguments, types, flags in (
            ('darwin', 'renamex_np', (os.fsencode(source), os.fsencode(destination), 2),
             (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint), 0),
            ('linux', 'renameat2', (-100, os.fsencode(source), -100, os.fsencode(destination), 2),
             (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint), 0),
            ('netbsd11', 'renameat2', (-100, os.fsencode(source), -100, os.fsencode(destination), 2),
             (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint), 3)):
            with self.subTest(exchange_platform=platform):
                exchange_source = self.source('exchange-source-' + platform)
                exchange_destination = self.source('exchange-destination-' + platform)
                if platform == 'darwin':
                    arguments = (os.fsencode(exchange_source), os.fsencode(exchange_destination), 2)
                else:
                    arguments = (-100, os.fsencode(exchange_source), -100,
                                 os.fsencode(exchange_destination), 2)
                function = NativeFunction()
                library = SimpleNamespace(sysctlbyname=KernelFlags(flags=flags), **{symbol: function})
                with patch.object(publication.sys, 'platform', platform), \
                        patch.object(publication.ctypes, 'CDLL', return_value=library):
                    publication.require_directory_exchange()
                    publication.exchange_directories(exchange_source, exchange_destination)
                self.assertEqual(function.calls, [arguments])
                self.assertEqual(tuple(function.argtypes), types)
        self.assert_source(source)

    def test_emberbsd_new_libc_with_older_kernel_fails_before_syscall(self):
        for query in (KernelFlags(result=-1), KernelFlags(flags=0)):
            function = NativeFunction()
            library = SimpleNamespace(renameat2=function, sysctlbyname=query)
            with patch.object(publication.sys, 'platform', 'netbsd11'), \
                    patch.object(publication.ctypes, 'CDLL', return_value=library):
                with self.assertRaisesRegex(SDKError, 'running EmberBSD kernel'):
                    publication.publish_directory_noreplace('absent', 'target')
            self.assertEqual(function.calls, [])

    def test_missing_symbol_and_library_fail_before_mutation(self):
        for platform in ('darwin', 'linux', 'netbsd11'):
            with self.subTest(platform=platform), patch.object(publication.sys, 'platform', platform):
                with patch.object(publication.ctypes, 'CDLL', return_value=SimpleNamespace()):
                    with self.assertRaisesRegex(SDKError, 'unavailable'):
                        publication.require_directory_publication()
                    source = self.source('missing-symbol-' + platform)
                    destination = self.root / 'output'
                    with self.assertRaisesRegex(SDKError, 'unavailable'):
                        publication.publish_directory_noreplace(source, destination)
                    self.assert_source(source)
                    self.assertFalse(os.path.lexists(destination))
                with patch.object(publication.ctypes, 'CDLL', side_effect=OSError('missing loader')):
                    with self.assertRaisesRegex(SDKError, 'unavailable'):
                        publication.require_directory_publication()

    def test_unsupported_host_refuses_before_native_loader_or_path_access(self):
        with patch.object(publication.sys, 'platform', 'win32'), \
                patch.object(publication.ctypes, 'CDLL') as loader:
            with self.assertRaisesRegex(SDKError, 'Darwin, Linux, or EmberBSD'):
                publication.publish_directory_noreplace('absent', 'output')
            loader.assert_not_called()

    def test_native_error_keeps_errno_and_never_uses_rename_fallback(self):
        source = self.source()
        destination = self.root / 'output'
        for error in (errno.EXDEV, errno.EIO, errno.ENOSYS, errno.ENOTSUP):
            function = NativeFunction(error)
            with self.subTest(error=error), \
                    patch.object(publication, '_native_rename', return_value=(function, False)), \
                    patch.object(os, 'rename', side_effect=AssertionError('overwrite fallback')), \
                    patch.object(os, 'replace', side_effect=AssertionError('overwrite fallback')):
                with self.assertRaises(OSError) as caught:
                    publication.publish_directory_noreplace(source, destination)
                self.assertEqual(caught.exception.errno, error)
                self.assertEqual(caught.exception.filename, str(destination))
            self.assertEqual(len(function.calls), 1)
            self.assert_source(source)
            self.assertFalse(destination.exists())


if __name__ == '__main__':
    unittest.main()
