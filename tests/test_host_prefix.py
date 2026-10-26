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
        self.root = Path(root) / 'host-pkg-llvm'
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
        kwargs.setdefault('compiler_family', 'gcc')
        with patch('emberbsd_sdk.host_prefix.process.capture', side_effect=self.capture):
            return load_host_prefix(self.root, required_roles=roles, host=self.host, **kwargs)

    def env_script(self, interpreter='python3.15'):
        env = self.write('env', header(62, 2))
        return self.write('meson', f'#!{env} {interpreter}\n'.encode())

    def compiler_directory(self):
        directory = self.root / 'gcc16/bin'
        directory.mkdir(parents=True)
        for name in ('gcc', 'g++', 'cpp'):
            (self.root / 'bin' / name).rename(directory / name)
        return directory


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

    def test_pkgsrc_installed_names_need_no_compatibility_links(self):
        names = {'patch': 'bin/nbpatch', 'tar': 'bin/nbtar',
                 'gzcat': 'bin/zcat', 'readelf': 'bin/llvm-readelf',
                 'install_name_tool': 'bin/llvm-install-name-tool',
                 'libtool_darwin': 'bin/llvm-libtool-darwin',
                 'lipo': 'bin/llvm-lipo', 'llvm_readtapi': 'bin/llvm-readtapi',
                 **{name: 'sbin/' + name for name in
                    ('pkg_add', 'pkg_admin', 'pkg_create', 'pkg_delete', 'pkg_info')}}
        for relative in names.values():
            path = self.fixture.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(header(62, 2)); path.chmod(0o755)
        selected = self.fixture.load(roles=tuple(names))
        self.assertEqual(dict(selected.tools),
                         {role: str(self.fixture.root / name) for role, name in names.items()})
        self.assertIn(str(self.fixture.root / 'sbin'), selected.path.split(os.pathsep))
        self.assertEqual(self.fixture.queries, [])

    def test_named_command_has_priority_and_tar_accepts_bsdtar(self):
        for name in ('patch', 'nbpatch', 'nbtar', 'bsdtar'):
            self.fixture.write(name, header(62, 2))
        self.assertEqual(self.fixture.load(roles=('patch',)).tool('patch'),
                         str(self.fixture.root / 'bin/patch'))
        self.assertEqual(self.fixture.load(roles=('tar',)).tool('tar'),
                         str(self.fixture.root / 'bin/nbtar'))
        (self.fixture.root / 'bin/nbtar').unlink()
        self.assertEqual(self.fixture.load(roles=('tar',)).tool('tar'),
                         str(self.fixture.root / 'bin/bsdtar'))

    def test_pkgsrc_alias_cannot_escape_or_fall_back(self):
        external = self.write('outside-patch', header(62, 2), 0o755)
        (self.fixture.root / 'bin/nbpatch').symlink_to(external)
        gnu = self.fixture.root / 'gnu/bin'; gnu.mkdir(parents=True)
        fallback = gnu / 'patch'; fallback.write_bytes(header(62, 2)); fallback.chmod(0o755)
        with self.assertRaisesRegex(SDKError, 'escapes host prefix'):
            self.fixture.load(roles=('patch',))
        self.assertEqual(self.fixture.queries, [])

    def test_existing_gnu_command_spellings_remain_supported(self):
        roles = ('patch', 'tar', 'gzcat', 'readelf', 'install_name_tool',
                 'libtool_darwin', 'lipo', 'llvm_readtapi', 'pkg_add',
                 'pkg_admin', 'pkg_create', 'pkg_delete', 'pkg_info')
        gnu = self.fixture.root / 'gnu/bin'; gnu.mkdir(parents=True)
        for role in roles:
            path = gnu / role
            path.write_bytes(header(62, 2)); path.chmod(0o755)
        selected = self.fixture.load(roles=roles)
        self.assertEqual(dict(selected.tools), {role: str(gnu / role) for role in roles})
        for role in ('libtool_darwin', 'llvm_readtapi', 'pkg_add'):
            path = self.fixture.write(role, header(62, 2))
            self.assertEqual(self.fixture.load(roles=(role,)).tool(role), str(path))

    def test_sbin_search_directory_cannot_leave_and_reenter_prefix(self):
        external = self.root / 'outside-sbin'; external.mkdir()
        tool = self.fixture.write('real-pkg-add', header(62, 2))
        (external / 'pkg_add').symlink_to(tool)
        (self.fixture.root / 'sbin').symlink_to(external)
        with self.assertRaisesRegex(SDKError, 'PATH directory escapes'):
            self.fixture.load(roles=('pkg_add',))
        self.assertEqual(self.fixture.queries, [])

    def test_search_directory_cannot_be_an_external_link(self):
        (self.fixture.root / 'gnu').mkdir()
        external = self.root / 'outside-tools'; external.mkdir()
        (self.fixture.root / 'gnu/bin').symlink_to(external)
        with self.assertRaisesRegex(SDKError, 'PATH directory escapes'):
            self.fixture.load()

    def test_package_query_and_otool_aliases_are_native_prefix_tools(self):
        for role, preferred, alternate in (
                ('pkg_info', 'sbin/pkg_info', 'bin/pkg_info'),
                ('otool', 'bin/otool', 'bin/llvm-otool')):
            with self.subTest(role=role):
                first = self.write('host-pkg-llvm/' + preferred, header(62, 2), 0o755)
                second = self.write('host-pkg-llvm/' + alternate, header(62, 2), 0o755)
                selected = self.fixture.load(roles=(role,))
                self.assertEqual(selected.tool(role), str(first))
                self.assertIn(str(first.parent), selected.path.split(os.pathsep))
                first.unlink()
                self.assertEqual(self.fixture.load(roles=(role,)).tool(role), str(second))
        self.assertEqual(self.fixture.queries, [])

    def test_package_query_and_otool_aliases_cannot_escape_prefix(self):
        external = self.write('outside-tool', header(62, 2), 0o755)
        for role, name in (('pkg_info', 'sbin/pkg_info'), ('pkg_info', 'bin/pkg_info'),
                           ('otool', 'bin/otool'), ('otool', 'bin/llvm-otool')):
            with self.subTest(path=name):
                path = self.fixture.root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.symlink_to(external)
                with self.assertRaisesRegex(SDKError, 'escapes host prefix'):
                    self.fixture.load(roles=(role,))
                path.unlink()
        self.assertEqual(self.fixture.queries, [])

    def test_archive_and_patch_aliases_are_native_prefix_tools(self):
        for role, names in (('tar', ('tar', 'nbtar', 'bsdtar')),
                            ('patch', ('patch', 'nbpatch'))):
            with self.subTest(role=role):
                paths = [self.fixture.write(name, header(62, 2)) for name in names]
                for path in paths:
                    self.assertEqual(self.fixture.load(roles=(role,)).tool(role), str(path))
                    path.unlink()
        self.assertEqual(self.fixture.queries, [])

    def test_archive_and_patch_invalid_alias_cannot_select_a_later_name(self):
        external = self.write('outside-tool', header(62, 2), 0o755)
        for role, names in (('tar', ('tar', 'nbtar', 'bsdtar')),
                            ('patch', ('patch', 'nbpatch'))):
            paths = [self.fixture.write(name, header(62, 2)) for name in names]
            for path in paths:
                with self.subTest(path=path.name):
                    path.unlink(); path.symlink_to(external)
                    with self.assertRaisesRegex(SDKError, 'escapes host prefix'):
                        self.fixture.load(roles=(role,))
                    path.unlink()
                    self.fixture.write(path.name, header(183, 2))
                    with self.assertRaisesRegex(SDKError, 'expected a native executable'):
                        self.fixture.load(roles=(role,))
                    path.unlink()
        self.assertEqual(self.fixture.queries, [])

    def test_package_query_and_otool_aliases_reject_wrong_native_cpu(self):
        for role, name in (('pkg_info', 'sbin/pkg_info'), ('pkg_info', 'bin/pkg_info'),
                           ('otool', 'bin/otool'), ('otool', 'bin/llvm-otool')):
            with self.subTest(path=name):
                path = self.write('host-pkg-llvm/' + name, header(183, 2), 0o755)
                with self.assertRaisesRegex(SDKError, 'expected a native executable'):
                    self.fixture.load(roles=(role,))
                path.unlink()
        self.assertEqual(self.fixture.queries, [])

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

    def test_env_interpreter_rejects_earlier_wrong_cpu(self):
        self.fixture.env_script()
        directory = self.fixture.compiler_directory()
        shadow = directory / 'python3.15'
        shadow.write_bytes(header(183, 2)); shadow.chmod(0o755)
        with self.assertRaisesRegex(SDKError, 'expected a native executable'):
            self.fixture.load(roles=('cc', 'meson'))
        self.assertEqual(self.fixture.queries, [])

    def test_env_interpreter_rejects_earlier_external_link(self):
        self.fixture.env_script()
        directory = self.fixture.compiler_directory()
        external = self.write('outside-python', header(62, 2), 0o755)
        (directory / 'python3.15').symlink_to(external)
        with self.assertRaisesRegex(SDKError, 'escapes host prefix'):
            self.fixture.load(roles=('cc', 'meson'))
        self.assertEqual(self.fixture.queries, [])

    def test_env_interpreter_uses_first_executable_in_selected_path(self):
        self.fixture.env_script()
        directory = self.fixture.compiler_directory()
        (self.fixture.root / 'bin/python3.15').rename(directory / 'python3.15')
        self.fixture.write('python3.15', header(183, 2))
        selected = self.fixture.load(roles=('cc', 'meson'))
        self.assertEqual(selected.path.split(os.pathsep)[0], str(directory))
        self.assertTrue(all(env['PATH'] == selected.path for _, env in self.fixture.queries))

    def test_env_interpreter_searches_gnu_bin_without_compiler(self):
        self.fixture.env_script('-S python3.15 -I')
        directory = self.fixture.root / 'gnu/bin'
        directory.mkdir(parents=True)
        (self.fixture.root / 'bin/python3.15').rename(directory / 'python3.15')
        selected = self.fixture.load(roles=('meson',))
        self.assertEqual(selected.tool('meson'), str(self.fixture.root / 'bin/meson'))
        self.assertEqual(self.fixture.queries, [])

    def test_env_interpreter_skips_non_executable_path_entry(self):
        self.fixture.env_script()
        directory = self.fixture.compiler_directory()
        (directory / 'python3.15').write_bytes(header(183, 2))
        self.fixture.load(roles=('cc', 'meson'))

    def test_env_interpreter_checks_final_driver_tool_path(self):
        self.fixture.env_script('helper-python')
        self.write('host-pkg-llvm/sbin/helper-python', header(62, 2), 0o755)
        self.write('host-pkg-llvm/sbin/pkg_info', header(62, 2), 0o755)
        (self.fixture.root / 'bin/as').unlink()
        assembler = self.write('host-pkg-llvm/libexec/as', header(62, 2), 0o755)
        self.write('host-pkg-llvm/libexec/helper-python', header(183, 2), 0o755)
        original = self.fixture.capture
        def capture(argv, *, env):
            if argv[-1] == '-print-prog-name=as':
                return SimpleNamespace(stdout=str(assembler).encode())
            return original(argv, env=env)
        self.fixture.capture = capture
        with self.assertRaisesRegex(SDKError, 'expected a native executable'):
            self.fixture.load(roles=('cc', 'meson', 'pkg_info'))

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
