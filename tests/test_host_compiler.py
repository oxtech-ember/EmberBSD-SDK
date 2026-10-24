# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD; AI-assisted native GCC16 selector contracts.
"""Native host compiler selection refuses PATH, system and Clang fallbacks."""

import os
from pathlib import Path
import subprocess
from unittest.mock import patch

from test_core import TemporaryCase
from host_compiler_fixture import fake_compiler, gcc16_pair
from emberbsd_sdk import host_compiler
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.sysroot_builder import SysrootBuilder


def capture(argv):
    result = subprocess.run(argv, capture_output=True, check=False)
    if result.returncode:
        raise SDKError(f'command failed: {argv}')
    return result.stdout.decode(), result.stderr.decode()


class HostCompilerTests(TemporaryCase):
    def test_accepts_explicit_gcc16_pair(self):
        tools = host_compiler.select_pair(gcc16_pair(self.root / 'gcc'), capture)
        self.assertEqual(set(tools), {'cc', 'c++'})
        self.assertEqual(tools['cc']['gcc_version'], '16.2.0')
        self.assertEqual(tools['cc']['path'], os.path.realpath(self.root / 'gcc/gcc-16'))
        self.assertEqual(tools['c++']['programs'], {'as': 'as', 'ld': 'ld'})
        host_compiler.check_record(tools)

    def test_missing_selector_has_no_path_fallback(self):
        fake_compiler(self.root / 'path', 'cc'); fake_compiler(self.root / 'path', 'c++')
        with patch.dict(os.environ, {'PATH': str(self.root / 'path')}):
            for options in ({}, {'host-cc': fake_compiler(self.root / 'gcc', 'gcc-16')}):
                with self.assertRaisesRegex(SDKError, 'required.*no PATH, cc or Clang fallback'):
                    host_compiler.select_pair(options, capture)

    def test_refuses_relative_and_missing_paths(self):
        with self.assertRaisesRegex(SDKError, 'absolute path'):
            host_compiler.select('cc', 'gcc-16', capture)
        with self.assertRaisesRegex(SDKError, 'does not exist'):
            host_compiler.select('cc', str(self.root / 'absent/gcc-16'), capture)

    def test_refuses_clang_identity(self):
        clang = fake_compiler(self.root / 'clang', 'cc', clang=True)
        with self.assertRaisesRegex(SDKError, 'is Clang'):
            host_compiler.select('cc', clang, capture)

    def test_refuses_clang_behind_a_gcc_name(self):
        path = Path(fake_compiler(self.root / 'x', 'gcc-16'))
        path.write_text(path.read_text().replace('#define __GNUC__ 16', '#define __clang__ 1\\n#define __GNUC__ 16'))
        with self.assertRaisesRegex(SDKError, 'is Clang'):
            host_compiler.select('cc', str(path), capture)

    def test_refuses_other_gcc_versions(self):
        with self.assertRaisesRegex(SDKError, 'not GCC 16'):
            host_compiler.select('cc', fake_compiler(self.root / 'gcc15', 'gcc-15', version='15.2.0'), capture)

    def test_refuses_mixed_installations(self):
        options = {'host-cc': fake_compiler(self.root / 'a', 'gcc-16'),
                   'host-cxx': fake_compiler(self.root / 'b', 'g++-16', version='16.1.0')}
        with self.assertRaisesRegex(SDKError, 'different GCC installations'):
            host_compiler.select_pair(options, capture)

    def test_refuses_ambient_system_compilers(self):
        for path in ('/usr/bin/cc', '/usr/bin/c++', '/opt/homebrew/bin/gcc-16', '/usr/local/bin/gcc-16'):
            if os.path.lexists(path):
                with self.assertRaisesRegex(SDKError, 'ambient system or package-manager compiler'):
                    host_compiler.select('cc', path, capture)
        link = self.root / 'links/gcc-16'
        link.parent.mkdir()
        if os.path.exists('/usr/bin/cc'):
            link.symlink_to('/usr/bin/cc')
            with self.assertRaisesRegex(SDKError, 'ambient system or package-manager compiler'):
                host_compiler.select('cc', str(link), capture)

    def test_recorded_path_selection_is_refused(self):
        legacy = {name: dict(path='/usr/bin/' + name, sha256='0' * 64, version='Apple clang version 21.0.0') for name in ('cc', 'c++')}
        with self.assertRaisesRegex(SDKError, 'not selected as native GCC 16'):
            host_compiler.check_record(legacy)
        tools = host_compiler.select_pair(gcc16_pair(self.root / 'gcc'), capture)
        tools['cc'] = dict(tools['cc'], path='/usr/bin/cc')
        with self.assertRaisesRegex(SDKError, 'not native GCC 16'):
            host_compiler.check_record(tools)

    def test_builder_refuses_missing_selector_before_host_queries(self):
        builder = SysrootBuilder({'sdk': str(self.root), 'target': 'aarch64', 'jobs': '1'})
        with patch('emberbsd_sdk.workspace.inspect_layout', return_value={}), \
                patch('emberbsd_sdk.workspace.require_target'), \
                patch.object(builder, 'find_command', side_effect=AssertionError('host query')):
            with self.assertRaisesRegex(SDKError, '--host-cc is required'):
                builder.preflight()
