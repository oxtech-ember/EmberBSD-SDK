# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Target compiler routing, independent of a completed application runtime."""

import hashlib
import os
from types import SimpleNamespace
from unittest.mock import patch

from test_core import TemporaryCase, header
from test_host_clang import ClangFixture
from emberbsd_sdk import inventory, target_toolchain
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.sysroot_builder import SysrootBuilder
from emberbsd_sdk.targets import target_definition
from emberbsd_sdk.workspace import create
from emberbsd_sdk.target_toolchain import validate_clang_identity


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
        validate_clang_identity(record, 'aarch64--netbsd')
        self.assertEqual(record['family'], 'clang')
        self.assertEqual(record['triplet'], 'aarch64--netbsd')
        self.assertEqual(record['reported_triplet'], 'aarch64-unknown-netbsd')
        self.assertEqual(record['version'], '23.1.4')
        self.assertEqual(record['binaries']['cc']['selected_path'], str(self.fixture.root / 'bin/clang'))
        self.assertEqual(record['binaries']['ar']['selected_path'], str(self.fixture.root / 'bin/llvm-ar'))
        self.assertEqual(record['commands']['as'], [str(self.fixture.root / 'bin/clang'),
            '--target=aarch64--netbsd', '-c', '-x', 'assembler'])

    def test_shared_executable_is_hashed_once_per_identity(self):
        with patch('emberbsd_sdk.target_toolchain.inventory.sha256_file', wraps=inventory.sha256_file) as hashed:
            record = self.compiler()
        self.assertEqual(hashed.call_count, 11)
        clang = str(self.fixture.root / 'bin/clang')
        self.assertEqual(sum(call.args == (clang,) for call in hashed.call_args_list), 1)
        for role in ('cxx', 'cpp', 'as'):
            self.assertEqual(record['binaries'][role]['sha256'], record['binaries']['cc']['sha256'])

    def test_distinct_executable_keeps_its_own_hash(self):
        cxx = self.fixture.root / 'bin/clang++'
        cxx.unlink()
        content = header(62, 2) + b'cxx'
        self.fixture.write('clang++', content)
        def capture(argv, *, env):
            result = self.capture(argv, env=env)
            if '-###' in argv and argv[0] == str(cxx):
                result.stderr = result.stderr.replace(b'/bin/clang"', b'/bin/clang++"')
            return result
        with patch('emberbsd_sdk.host_prefix.process.capture', side_effect=capture), \
                patch('emberbsd_sdk.target_toolchain.inventory.sha256_file', wraps=inventory.sha256_file) as hashed:
            record = self.builder.compiler()
        self.assertEqual(hashed.call_count, 12)
        self.assertEqual(record['binaries']['cxx']['sha256'], hashlib.sha256(content).hexdigest())
        self.assertNotEqual(record['binaries']['cc']['sha256'], record['binaries']['cxx']['sha256'])

    def test_shared_executable_change_between_roles_is_rehashed(self):
        clang = self.fixture.root / 'bin/clang'
        member = target_toolchain._member
        content = header(62, 2) + b'changed'
        def change_cxx(selected, *args, **kwargs):
            if selected == str(self.fixture.root / 'bin/clang++'):
                clang.write_bytes(content)
            return member(selected, *args, **kwargs)
        with patch('emberbsd_sdk.target_toolchain._member', side_effect=change_cxx), \
                patch('emberbsd_sdk.target_toolchain.inventory.sha256_file', wraps=inventory.sha256_file) as hashed:
            record = self.compiler()
        self.assertEqual(sum(call.args == (str(clang),) for call in hashed.call_args_list), 2)
        self.assertNotEqual(record['binaries']['cc']['sha256'], record['binaries']['cxx']['sha256'])
        for role in ('cxx', 'cpp', 'as'):
            self.assertEqual(record['binaries'][role]['sha256'], hashlib.sha256(content).hexdigest())

    def test_each_identity_rehashes_same_size_changes_with_restored_mtime(self):
        clang = self.fixture.root / 'bin/clang'
        before = clang.stat()
        with patch('emberbsd_sdk.target_toolchain.inventory.sha256_file', wraps=inventory.sha256_file) as hashed:
            first = self.compiler()
            content = bytearray(clang.read_bytes())
            content[-1] = 1
            clang.write_bytes(content)
            os.utime(clang, ns=(before.st_atime_ns, before.st_mtime_ns))
            second = self.compiler()
        self.assertEqual(hashed.call_count, 22)
        self.assertNotEqual(first['binaries']['cc']['sha256'], second['binaries']['cc']['sha256'])
        self.assertEqual(second['binaries']['cc']['sha256'], hashlib.sha256(content).hexdigest())

    def test_portable_clang_identity_rejects_mixed_commands_without_reading_tools(self):
        record = self.compiler()
        with patch('emberbsd_sdk.paths.entry', side_effect=AssertionError('mutable tool inspection')):
            validate_clang_identity(record, 'aarch64--netbsd')
        record['commands']['cc'][-1] = '--ld-path=/other/ld.lld'
        with self.assertRaisesRegex(SDKError, 'compiler command'):
            validate_clang_identity(record, 'aarch64--netbsd')

    def test_missing_llvm_tool_does_not_fall_back_to_gnu(self):
        (self.fixture.root / 'bin/llvm-ar').unlink()
        with self.assertRaisesRegex(SDKError, 'llvm-ar'):
            self.compiler()

    def test_linker_cannot_escape_the_selected_prefix(self):
        self.fixture.linker = self.write('outside-linker', header(62, 2), 0o755)
        with self.assertRaisesRegex(SDKError, 'escapes host prefix'):
            self.compiler()

    def test_default_runtime_cannot_reuse_gcc_stage(self):
        sdk = self.root / 'sdk'; create(sdk, target='aarch64')
        with self.assertRaisesRegex(SDKError, '--llvm-source'):
            SysrootBuilder({'sdk': str(sdk), 'target': 'aarch64', 'jobs': '1',
                            'runtime-work': '/gcc-stage'}).preflight()

    def test_unknown_family_is_rejected_before_selecting_inputs(self):
        sdk = self.root / 'sdk'; create(sdk, target='aarch64')
        with self.assertRaisesRegex(SDKError, 'compiler family'):
            SysrootBuilder({'compiler-family': 'automatic', 'sdk': str(sdk),
                            'target': 'aarch64', 'jobs': '1'}).preflight()

    def test_explicit_gcc_requires_its_runtime_archive(self):
        sdk = self.root / 'sdk'; create(sdk, target='aarch64')
        with self.assertRaisesRegex(SDKError, '--gcc-archive'):
            SysrootBuilder({'compiler-family': 'gcc', 'sdk': str(sdk),
                            'llvm-source': '/selected-common-source',
                            'libarchive-source': '/selected-libarchive-source',
                            'libevent-source': '/selected-libevent-source',
                            'target': 'aarch64', 'jobs': '1'}).preflight()

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
