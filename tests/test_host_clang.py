# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Default LLVM selection and explicit GCC compatibility contracts."""

import copy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_core import TemporaryCase, header
from test_host_prefix import PrefixFixture
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.native_toolset import load_host_prefix, validate_selection


class ClangFixture(PrefixFixture):
    def __init__(self, root):
        super().__init__(root)
        for name in ('clang', 'clang++', 'clang-cpp', 'ld.lld', 'llvm-ar',
                     'llvm-ranlib', 'llvm-nm', 'llvm-strip'):
            self.write(name, header(62, 2))
        for name in ('clang++', 'clang-cpp'):
            alias = self.root / 'bin' / name
            alias.unlink()
            alias.symlink_to('clang')
        self.write('clang-as', ('#!' + str(self.root / 'bin/sh') + '\n').encode())
        self.resource = self.root / 'lib/clang/23'
        self.resource.mkdir(parents=True)
        self.linker = self.root / 'bin/ld.lld'
        self.version = '23.1.4'

    def load(self, roles=('cc', 'sh', 'python', 'make'), **kwargs):
        kwargs.setdefault('compiler_family', 'clang')
        return super().load(roles, **kwargs)

    def capture(self, argv, *, env):
        self.queries.append((argv, env))
        if '-###' in argv:
            return SimpleNamespace(stdout=b'', stderr=(
                f'"{self.root}/bin/clang" "-cc1" "-triple" "x86_64-pc-linux-gnu"\n'
                f'"{self.linker}" "-o" "/dev/null"\n').encode())
        option = argv[-1]
        if option == '--version':
            output = 'clang version ' + self.version
        elif option == '-dumpmachine':
            output = 'x86_64-pc-linux-gnu'
        elif option == '-print-resource-dir':
            output = str(self.resource)
        else:
            raise AssertionError(argv)
        return SimpleNamespace(stdout=(output + '\n').encode(), stderr=b'')


class HostClangTests(TemporaryCase):
    def setUp(self):
        super().setUp()
        self.fixture = ClangFixture(self.root)

    def test_default_selects_llvm_even_when_gcc_is_installed(self):
        selected = self.fixture.load()
        self.assertEqual(dict(selected.compiler), dict(family='clang', version='23.1.4',
                                                       triplet='x86_64-pc-linux-gnu'))
        self.assertEqual(selected.tool('cc'), str(self.fixture.root / 'bin/clang'))
        self.assertEqual(selected.tool('as'), str(self.fixture.root / 'bin/clang-as'))
        self.assertEqual(selected.command('as'), (selected.tool('as'),))
        self.assertEqual(selected.tool('ld'), str(self.fixture.linker))
        self.assertEqual(selected.driver_components['resource-dir'], str(self.fixture.resource))
        validate_selection(selected.as_dict(), required_roles=('cc', 'sh'))
        self.assertTrue(all(env['PATH'] == selected.path for _, env in self.fixture.queries))

    def test_gcc_requires_explicit_selection(self):
        fixture = PrefixFixture(self.root / 'gcc-only')
        with patch('emberbsd_sdk.host_prefix.process.capture', side_effect=fixture.capture):
            with self.assertRaisesRegex(SDKError, 'missing native tool'):
                load_host_prefix(fixture.root, required_roles=('cc',), host=fixture.host)
            selected = load_host_prefix(fixture.root, required_roles=('cc',),
                                        host=fixture.host, compiler_family='gcc')
        self.assertEqual(selected.compiler['family'], 'gcc')

    def test_llvm_archive_roles_precede_generic_names(self):
        selected = self.fixture.load(roles=('ar', 'ranlib', 'nm', 'strip'))
        for role in ('ar', 'ranlib', 'nm', 'strip'):
            self.assertEqual(selected.tool(role), str(self.fixture.root / ('bin/llvm-' + role)))

    def test_missing_clang_as_does_not_return_an_incomplete_group(self):
        (self.fixture.root / 'bin/clang-as').unlink()
        with self.assertRaisesRegex(SDKError, 'missing native tool.*as'):
            self.fixture.load()
        self.assertEqual(self.fixture.queries, [])

    def test_linker_only_selection_uses_llvm_without_compiler_queries(self):
        selected = self.fixture.load(roles=('ld',))
        self.assertEqual(selected.tool('ld'), str(self.fixture.linker))
        self.assertEqual(self.fixture.queries, [])

    def test_default_linker_cannot_escape_prefix(self):
        self.fixture.linker = self.write('outside-ld', header(62, 2), 0o755)
        with self.assertRaisesRegex(SDKError, 'escapes host prefix'):
            self.fixture.load()

    def test_resource_directory_cannot_escape_prefix(self):
        self.fixture.resource = self.root / 'outside-resource'
        self.fixture.resource.mkdir()
        with self.assertRaisesRegex(SDKError, 'resource directory escapes'):
            self.fixture.load()

    def test_target_triplet_cannot_replace_native_compiler(self):
        original = self.fixture.capture
        def capture(argv, *, env):
            if argv[-1] == '-dumpmachine':
                return SimpleNamespace(stdout=b'aarch64--netbsd\n', stderr=b'')
            return original(argv, env=env)
        self.fixture.capture = capture
        with self.assertRaisesRegex(SDKError, 'actual native host'):
            self.fixture.load()

    def test_c_and_cxx_versions_must_match(self):
        original = self.fixture.capture
        def capture(argv, *, env):
            if argv[0].endswith('/clang++') and argv[-1] == '--version':
                return SimpleNamespace(stdout=b'clang version 22.1.0\n', stderr=b'')
            return original(argv, env=env)
        self.fixture.capture = capture
        with self.assertRaisesRegex(SDKError, 'compilers differ'):
            self.fixture.load()

    def test_unknown_family_never_selects_another_compiler(self):
        with self.assertRaisesRegex(SDKError, 'compiler family'):
            self.fixture.load(compiler_family='automatic')
        self.assertEqual(self.fixture.queries, [])

    def test_saved_family_cannot_be_changed_to_an_unchecked_compiler(self):
        selected = copy.deepcopy(self.fixture.load().as_dict())
        selected['compiler']['family'] = 'automatic'
        with self.assertRaisesRegex(SDKError, 'compiler family'):
            validate_selection(selected, required_roles=('cc',))
