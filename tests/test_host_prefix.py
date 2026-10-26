# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Prefix containment and native command contracts. No compiler runs."""

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_core import TemporaryCase, header
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.native_toolset import load_host_prefix, validate_selection


class PrefixFixture:
    def __init__(self, root):
        self.root = Path(root) / 'host-pkg-g16'
        self.host = {'os': 'Linux', 'cpu': 'x86_64'}
        self.queries = []
        for name in ('gcc', 'g++', 'cpp', 'as', 'ld', 'ar', 'sh', 'python3.15',
                     'bmake', 'cmp', 'readlink', 'cc1', 'cc1plus', 'collect2', 'lto1', 'lto-wrapper'):
            self.write(name, header(62, 2))

    def write(self, name, data):
        path = self.root / 'bin' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o755)
        return path

    def capture(self, argv, *, env):
        self.queries.append((argv, env))
        option = argv[-1]
        if option == '-dumpfullversion':
            output = '16.2.0'
        elif option == '-dumpmachine':
            output = 'x86_64-pc-linux-gnu'
        elif option.startswith('-print-prog-name='):
            output = str(self.root / 'bin' / option.split('=', 1)[1])
        else:
            raise AssertionError(argv)
        return SimpleNamespace(stdout=(output + '\n').encode())

    def load(self, roles=('cc', 'sh', 'python', 'make'), **kwargs):
        with patch('emberbsd_sdk.host_prefix.process.capture', side_effect=self.capture):
            return load_host_prefix(self.root, required_roles=roles, host=self.host, **kwargs)


class HostPrefixTests(TemporaryCase):
    def setUp(self):
        super().setUp()
        self.fixture = PrefixFixture(self.root)

    def test_prefix_needs_no_descriptor_hash_or_receipt(self):
        selected = self.fixture.load()
        self.assertEqual(selected.compiler['version'], '16.2.0')
        self.assertEqual(selected.command('make'), (str(self.fixture.root / 'bin/bmake'),))
        self.assertEqual(selected.path, os.pathsep.join(str(self.fixture.root / name) for name in ('bin', 'gnu/bin')))
        self.assertNotIn('producer_receipt', selected.as_dict())
        self.assertNotIn('descriptor_sha256', selected.as_dict())
        validate_selection(selected.as_dict(), required_roles=('sh', 'python'))
        for argv, env in self.fixture.queries:
            self.assertTrue(argv[0].startswith(str(self.fixture.root) + '/'))
            self.assertEqual(env['PATH'], selected.path)

    def test_missing_tool_is_not_found_in_ambient_path(self):
        with self.assertRaisesRegex(SDKError, 'missing native tool.*ninja'):
            self.fixture.load(roles=('ninja',))
        self.assertEqual(self.fixture.queries, [])

    def test_source_tools_do_not_require_a_compiler(self):
        self.fixture.write('git', header(62, 2))
        self.fixture.write('patch', header(62, 2))
        (self.fixture.root / 'bin/gcc').unlink()
        selected = self.fixture.load(roles=('git', 'patch'))
        self.assertEqual(set(selected.tools), {'git', 'patch'})
        self.assertEqual(selected.compiler, {})
        self.assertEqual(selected.path, os.pathsep.join(str(self.fixture.root / name) for name in ('bin', 'gnu/bin')))
        self.assertEqual(self.fixture.queries, [])
        validate_selection(selected.as_dict(), required_roles=('git', 'patch'))

    def test_compiler_wrapper_uses_a_prefix_interpreter(self):
        self.fixture.write('gcc', ('#!' + str(self.fixture.root / 'bin/sh') + '\n').encode())
        self.fixture.load()
        self.fixture.write('gcc', b'#!/bin/sh\n')
        with self.assertRaisesRegex(SDKError, 'escapes host prefix'):
            self.fixture.load()

    def test_ports_command_aliases_and_gnu_directory(self):
        (self.fixture.root / 'bin/ar').rename(self.fixture.root / 'bin/llvm-ar')
        self.fixture.write('bsdinstall', header(62, 2))
        self.fixture.write('nawk', header(62, 2))
        gnu = self.fixture.root / 'gnu/bin'; gnu.mkdir(parents=True)
        cat = gnu / 'cat'; cat.write_bytes(header(62, 2)); cat.chmod(0o755)
        selected = self.fixture.load(roles=('ar', 'install', 'cat', 'awk'))
        self.assertEqual(selected.tool('ar'), str(self.fixture.root / 'bin/llvm-ar'))
        self.assertEqual(selected.tool('install'), str(self.fixture.root / 'bin/bsdinstall'))
        self.assertEqual(selected.tool('awk'), str(self.fixture.root / 'bin/nawk'))
        self.assertEqual(selected.tool('cat'), str(cat))
        self.assertIn(str(gnu), selected.path.split(os.pathsep))

    def test_search_directory_cannot_be_an_external_link(self):
        (self.fixture.root / 'gnu').mkdir()
        external = self.root / 'outside-tools'; external.mkdir()
        (self.fixture.root / 'gnu/bin').symlink_to(external)
        with self.assertRaisesRegex(SDKError, 'PATH directory escapes'):
            self.fixture.load()

    def test_offline_validation_does_not_inspect_live_symlinks(self):
        selected = self.fixture.load().as_dict()
        path = self.fixture.root / 'bin/gcc'
        path.unlink(); path.symlink_to('/outside/compiler')
        with patch('emberbsd_sdk.host_prefix.canonical_existing', side_effect=AssertionError('offline filesystem read')):
            validate_selection(selected, required_roles=('cc', 'sh'))

    def test_driver_supplies_missing_as_and_ld_names(self):
        (self.fixture.root / 'bin/as').rename(self.fixture.root / 'bin/llvm-mc')
        (self.fixture.root / 'bin/ld').rename(self.fixture.root / 'bin/ld64.lld')
        original = self.fixture.capture
        def capture(argv, *, env):
            component = {'-print-prog-name=as': 'llvm-mc', '-print-prog-name=ld': 'ld64.lld'}.get(argv[-1])
            if component:
                return SimpleNamespace(stdout=str(self.fixture.root / 'bin' / component).encode())
            return original(argv, env=env)
        self.fixture.capture = capture
        selected = self.fixture.load()
        self.assertEqual(selected.tool('as'), str(self.fixture.root / 'bin/llvm-mc'))
        self.assertEqual(selected.tool('ld'), str(self.fixture.root / 'bin/ld64.lld'))

    def test_external_symlink_is_rejected_before_queries(self):
        external = self.write('outside', header(62, 2), 0o755)
        selected = self.fixture.root / 'bin/gcc'
        selected.unlink(); selected.symlink_to(external)
        with self.assertRaisesRegex(SDKError, 'escapes host prefix'):
            self.fixture.load()
        self.assertEqual(self.fixture.queries, [])

    def test_wrong_native_cpu_is_rejected_before_queries(self):
        self.fixture.write('gcc', header(183, 2))
        with self.assertRaisesRegex(SDKError, 'expected a native executable'):
            self.fixture.load()
        self.assertEqual(self.fixture.queries, [])

    def test_script_interpreter_must_remain_inside_prefix(self):
        self.fixture.write('meson', b'#!/usr/bin/python3\npass\n')
        with self.assertRaises(SDKError):
            self.fixture.load(roles=('meson',))
        self.fixture.write('meson', ('#!' + str(self.fixture.root / 'bin/python3.15') + '\npass\n').encode())
        self.fixture.load(roles=('meson',))

    def test_driver_component_cannot_escape_prefix(self):
        external = self.write('outside', header(62, 2), 0o755)
        original = self.fixture.capture
        def capture(argv, *, env):
            if argv[-1] == '-print-prog-name=ld':
                return SimpleNamespace(stdout=(str(external) + '\n').encode())
            return original(argv, env=env)
        self.fixture.capture = capture
        with self.assertRaisesRegex(SDKError, 'escapes host prefix'):
            self.fixture.load()

    def test_driver_bare_binutils_resolve_only_in_prefix(self):
        original = self.fixture.capture
        def capture(argv, *, env):
            if argv[-1] in ('-print-prog-name=as', '-print-prog-name=ld'):
                return SimpleNamespace(stdout=argv[-1].split('=', 1)[1].encode())
            return original(argv, env=env)
        self.fixture.capture = capture
        self.fixture.load()

    def test_target_compiler_cannot_replace_native_gcc(self):
        original = self.fixture.capture
        def capture(argv, *, env):
            if argv[-1] == '-dumpmachine':
                return SimpleNamespace(stdout=b'aarch64--netbsd\n')
            return original(argv, env=env)
        self.fixture.capture = capture
        with self.assertRaisesRegex(SDKError, 'actual native host'):
            self.fixture.load()
