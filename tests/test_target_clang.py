# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Target compiler routing, independent of a completed application runtime."""

from types import SimpleNamespace
from unittest.mock import patch

from test_core import TemporaryCase, header
from test_host_clang import ClangFixture
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.sysroot_builder import SysrootBuilder
from emberbsd_sdk.targets import target_definition


class TargetClangTests(TemporaryCase):
    def setUp(self):
        super().setUp()
        self.fixture = ClangFixture(self.root)
        for name in ('objcopy', 'objdump', 'readelf', 'size', 'strings'):
            self.fixture.write('llvm-' + name, header(62, 2))
        self.builder = SysrootBuilder({})
        self.builder.paths = {'compiler': str(self.fixture.root)}
        self.builder.target = target_definition('aarch64')
        self.builder.native = SimpleNamespace(host=self.fixture.host)
        self.builder.environment = {'PATH': str(self.fixture.root / 'bin')}

    def capture(self, argv, *, env):
        triplet = self.builder.target['triplet']
        if '-###' in argv:
            self.assertIn('--target=' + triplet, argv)
            self.assertIn('--ld-path=' + str(self.fixture.root / 'bin/ld.lld'), argv)
        if argv[-1] == '-dumpmachine':
            self.assertIn('--target=' + triplet, argv)
            return SimpleNamespace(stdout=(triplet.replace('--', '-unknown-', 1) + '\n').encode(), stderr=b'')
        return self.fixture.capture(argv, env=env)

    def compiler(self):
        with patch('emberbsd_sdk.host_prefix.process.capture', side_effect=self.capture):
            return self.builder.compiler()

    def test_default_selects_clang_and_explicit_target(self):
        record = self.compiler()
        self.assertEqual(record['family'], 'clang')
        self.assertEqual(record['triplet'], 'aarch64--netbsd')
        self.assertEqual(record['reported_triplet'], 'aarch64-unknown-netbsd')
        self.assertEqual(record['version'], '23.1.4')
        self.assertEqual(record['binaries']['cc']['selected_path'], str(self.fixture.root / 'bin/clang'))
        self.assertEqual(record['binaries']['ar']['selected_path'], str(self.fixture.root / 'bin/llvm-ar'))
        self.assertEqual(record['commands']['as'], [str(self.fixture.root / 'bin/clang'),
            '--target=aarch64--netbsd', '-c', '-x', 'assembler'])

    def test_missing_llvm_tool_does_not_fall_back_to_gnu(self):
        (self.fixture.root / 'bin/llvm-ar').unlink()
        with self.assertRaisesRegex(SDKError, 'llvm-ar'):
            self.compiler()

    def test_linker_cannot_escape_the_selected_prefix(self):
        self.fixture.linker = self.write('outside-linker', header(62, 2), 0o755)
        with self.assertRaisesRegex(SDKError, 'escapes host prefix'):
            self.compiler()

    def test_default_runtime_cannot_reuse_gcc_stage(self):
        with self.assertRaisesRegex(SDKError, 'LLVM target runtime'):
            SysrootBuilder({}).preflight()

    def test_unknown_family_is_rejected_before_selecting_inputs(self):
        with self.assertRaisesRegex(SDKError, 'compiler family'):
            SysrootBuilder({'compiler-family': 'automatic'}).preflight()

    def test_explicit_gcc_requires_its_runtime_archive(self):
        with self.assertRaisesRegex(SDKError, '--gcc-archive'):
            SysrootBuilder({'compiler-family': 'gcc'}).preflight()

    def test_wrong_target_is_not_a_native_fallback(self):
        capture = self.capture
        for reported in ('x86_64-unknown-netbsd', 'aarch64-unknown-freebsd', 'aarch64-apple-darwin'):
            def wrong_target(argv, *, env):
                if argv[-1] == '-dumpmachine':
                    return SimpleNamespace(stdout=(reported + '\n').encode(), stderr=b'')
                return capture(argv, env=env)
            with self.subTest(reported=reported), patch('emberbsd_sdk.host_prefix.process.capture', side_effect=wrong_target):
                with self.assertRaisesRegex(SDKError, 'triplet differs'):
                    self.builder.compiler()

    def test_amd64_uses_the_normalized_unknown_vendor(self):
        self.builder.target = target_definition('amd64')
        record = self.compiler()
        self.assertEqual(record['triplet'], 'x86_64--netbsd')
        self.assertEqual(record['reported_triplet'], 'x86_64-unknown-netbsd')

    def test_linker_trace_must_include_the_compiler_frontend(self):
        capture = self.capture
        def no_frontend(argv, *, env):
            if '-###' in argv:
                return SimpleNamespace(stdout=b'', stderr=f'"{self.fixture.linker}" "-o" "/dev/null"\n'.encode())
            return capture(argv, env=env)
        with patch('emberbsd_sdk.host_prefix.process.capture', side_effect=no_frontend):
            with self.assertRaisesRegex(SDKError, 'compiler and linker routes'):
                self.builder.compiler()
