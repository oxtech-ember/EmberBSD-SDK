# SPDX-License-Identifier: BSD-2-Clause
# Origin: EmberBSD; AI-assisted independent runtime refresh regressions.
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

SDK_SRC = Path(__file__).resolve().parents[1] / 'src'
sys.path.insert(0, str(SDK_SRC))
from emberbsd_sdk import prepared_refresh, runtime_refresh
from emberbsd_sdk.sysroot_builder import SysrootBuilder


class RuntimeRefreshTests(unittest.TestCase):
    def setUp(self):
        self.before = self.context()
        self.after = copy.deepcopy(self.before)
        self.after['runtime_prefix'] = '/candidate/runtime'
        old, new = self.before['runtime']['manifest'], self.after['runtime']['manifest']
        new['files']['usr/pkg/llvm/include/c++/v1/locale'] = '1' * 64
        new['files']['usr/pkg/llvm/lib/libc++.so.1'] = '2' * 64
        new['files']['usr/pkg/llvm/lib/libc++abi.so.1'] = '3' * 64

    @staticmethod
    def manifest():
        prefix = 'usr/pkg/llvm'
        files = {
            prefix + '/lib/clang/23/lib/netbsd/libclang_rt.builtins-aarch64.a': 'a' * 64,
            prefix + '/lib/libunwind.a': 'b' * 64,
            prefix + '/lib/libunwind.so.1': 'c' * 64,
            prefix + '/lib/libc++.a': 'd' * 64,
            prefix + '/lib/libc++abi.a': 'e' * 64,
            prefix + '/lib/libc++.so.1': 'f' * 64,
            prefix + '/lib/libc++abi.so.1': '0' * 64,
            prefix + '/include/c++/v1/vector': '1' * 64,
        }
        return {
            'format': 'emberbsd-llvm-runtime', 'version': 1, 'compiler_family': 'clang',
            'llvm_version': '23.1.4', 'components': ['builtins', 'libunwind', 'libcxxabi', 'libcxx'],
            'target': 'aarch64', 'triplet': 'aarch64--netbsd', 'machine_arch': 'aarch64',
            'elf_machine': 183, 'prefix': prefix,
            'source': {'archive': 'llvm-project-23.1.4.src.tar.xz', 'sha512': 'a' * 128},
            'recipe': {'ports_revision': 'b' * 40, 'files': {'pkgsrc/llvm/Makefile': 'c' * 64}},
            'base': {'os_revision': 'd' * 40, 'files': {'usr/include/stdint.h': 'e' * 64}, 'symlinks': {}},
            'artifacts': {
                'builtins': prefix + '/lib/clang/23/lib/netbsd/libclang_rt.builtins-aarch64.a',
                'libunwind': {'static': prefix + '/lib/libunwind.a', 'shared': prefix + '/lib/libunwind.so.1'},
                'libcxx': {'static': prefix + '/lib/libc++.a', 'shared': prefix + '/lib/libc++.so.1'},
                'libcxxabi': {'static': prefix + '/lib/libc++abi.a', 'shared': prefix + '/lib/libc++abi.so.1'},
                'cxx_headers': prefix + '/include/c++/v1',
            },
            'files': files,
            'symlinks': {
                prefix + '/lib/libc++.so': 'libc++.so.1',
                prefix + '/lib/libc++abi.so': 'libc++abi.so.1',
            },
        }

    @classmethod
    def context(cls):
        manifest = cls.manifest()
        return {
            'compiler_family': 'clang', 'runtime_prefix': '/previous/runtime',
            'llvm_source': {'llvm_version': '23.1.4', 'manifest_sha256': 'f' * 64},
            'runtime': {'manifest': manifest},
            'common_sources': {},
            'flags': ['CC=clang', 'CFLAGS=-O2', 'EMBER_EXTERNAL_LLVM_RUNTIME=/previous/runtime',
                      'EMBER_LLVM_RESOURCE_DIR=/previous/resource'],
        }

    def test_recorded_locale_dso_pair_and_prefix_move_refresh_without_tree_hashing(self):
        with patch('emberbsd_sdk.inventory.snapshot_tree', side_effect=AssertionError('unexpected tree hash')):
            update = runtime_refresh.cpp_update(self.before, self.after)
        self.assertEqual(update, {
            'headers': ('usr/pkg/llvm/include/c++/v1/', 'usr/include/c++/'),
            'linked': ('libc++.so', 'libc++abi.so'),
        })

    def test_metadata_only_os_revision_does_not_select_runtime_inputs(self):
        new = copy.deepcopy(self.before)
        new['runtime']['manifest']['base']['os_revision'] = '9' * 40
        self.assertEqual(runtime_refresh.cpp_update(self.before, new), {'headers': (), 'linked': ()})

    def test_metadata_only_transition_plans_no_object_refresh(self):
        old, new = copy.deepcopy(self.before), copy.deepcopy(self.before)
        new['runtime']['manifest']['base']['os_revision'] = '9' * 40
        fake = SimpleNamespace(
            paths={'os': '/os', 'objects': '/obj'}, context_signature=lambda path: new,
            COMMON_SOURCES=SysrootBuilder.COMMON_SOURCES,
            reuse_signature=SysrootBuilder.reuse_signature,
            compatible_context=lambda candidate, path: True,
            compiler_record={'binaries': {'ar': {'selected_path': '/tool/ar'}}}, environment={},
        )
        fake.reuse_signature = fake.reuse_signature.__get__(fake)
        with patch('emberbsd_sdk.sysroot_builder.prepared_refresh.plan', return_value=[]) as plan:
            self.assertTrue(SysrootBuilder.plan_prepared_refresh(fake, {'signature': old}, '/obj'))
        self.assertEqual(fake.refresh_dirs, [])
        self.assertEqual(plan.call_args.kwargs['extra_headers'], ())
        self.assertEqual(plan.call_args.kwargs['extra_linked'], ())

    def test_builtin_unwind_archive_version_target_base_and_symlink_changes_are_rejected(self):
        cases = []
        builtins = copy.deepcopy(self.after)
        builtins['runtime']['manifest']['files']['usr/pkg/llvm/lib/clang/23/lib/netbsd/libclang_rt.builtins-aarch64.a'] = '9' * 64
        cases.append(('builtins', builtins))
        unwind = copy.deepcopy(self.after)
        unwind['runtime']['manifest']['files']['usr/pkg/llvm/lib/libunwind.so.1'] = '9' * 64
        cases.append(('unwinder', unwind))
        static = copy.deepcopy(self.after)
        static['runtime']['manifest']['files']['usr/pkg/llvm/lib/libc++.a'] = '9' * 64
        cases.append(('static archive', static))
        archive = copy.deepcopy(self.after)
        archive['runtime']['manifest']['source']['sha512'] = '9' * 128
        cases.append(('source archive', archive))
        version = copy.deepcopy(self.after)
        version['runtime']['manifest']['llvm_version'] = '24.0.0'
        cases.append(('LLVM version', version))
        target = copy.deepcopy(self.after)
        target['runtime']['manifest']['target'] = 'riscv64'
        cases.append(('target', target))
        base = copy.deepcopy(self.after)
        base['runtime']['manifest']['base']['files']['usr/include/stdint.h'] = '9' * 64
        cases.append(('base ABI', base))
        link = copy.deepcopy(self.after)
        link['runtime']['manifest']['symlinks']['usr/pkg/llvm/lib/libc++.so'] = 'other.so'
        cases.append(('symlink map', link))
        for label, candidate in cases:
            with self.subTest(label=label):
                self.assertIsNone(runtime_refresh.cpp_update(self.before, candidate))

    def test_prepared_planner_unions_runtime_headers_and_links_with_common_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            os_root, obj = root / 'os', root / 'obj'
            contract = {
                'version': 1,
                'sources': {'EMBER_LIBCBOR_SOURCE': {
                    'producer_dirs': ['external/cbor'], 'consumer_dirs': ['external/fido'],
                    'header_prefixes': ['usr/include/cbor.h'], 'linked_names': ['libcbor.so']}},
                'header_independent_object_patterns': [], 'header_independent_archive_patterns': [],
            }
            (os_root / 'share/mk').mkdir(parents=True)
            (os_root / prepared_refresh.CONTRACT).write_text(json.dumps(contract))
            for directory in ('external/cbor', 'external/fido', 'other/cxx', 'other/base-cxx', 'other/c', 'other/linked'):
                (os_root / directory).mkdir(parents=True, exist_ok=True)
                (os_root / directory / 'Makefile').write_text('all:\n')
                (obj / directory).mkdir(parents=True, exist_ok=True)
            (obj / 'other/cxx/locale.d').write_text(
                'locale.o: /usr/pkg/llvm/include/c++/v1/locale\n')
            (obj / 'other/cxx/locale.o').write_bytes(b'obj')
            (obj / 'other/base-cxx/locale.d').write_text(
                'locale.o: /old/sysroot/usr/include/c++/__cxx03/locale\n')
            (obj / 'other/base-cxx/locale.o').write_bytes(b'obj')
            (obj / 'other/c/c.d').write_text('c.o: /usr/include/stdio.h\n')
            (obj / 'other/c/c.o').write_bytes(b'obj')
            (obj / 'other/linked/libuser.so').write_bytes(b'\x7fELFlibc++.so.1\0')
            calls = []
            real_plan = prepared_refresh.plan
            def count_plan(*args, **kwargs):
                calls.append((args, kwargs))
                return real_plan(*args, **kwargs)
            with patch.object(prepared_refresh, 'plan', side_effect=count_plan):
                result = prepared_refresh.plan(os_root, obj, {'libcbor'}, lambda archive: [],
                    extra_headers=runtime_refresh.cpp_update(self.before, self.after)['headers'],
                    extra_linked=('libc++.so', 'libc++abi.so'))
            self.assertEqual(len(calls), 1)
            self.assertEqual(result, ['external/cbor', 'external/fido', 'other/base-cxx', 'other/cxx', 'other/linked'])
            self.assertNotIn('other/c', result)

    def test_plan_prepared_refresh_keeps_compiler_and_unrelated_flags_in_compatibility_check(self):
        new = copy.deepcopy(self.after)
        new['flags'] = ['CC=clang', 'CFLAGS=-O2', 'LDFLAGS=-Wl,keep',
                        'EMBER_EXTERNAL_LLVM_RUNTIME=/candidate/runtime',
                        'EMBER_LLVM_RESOURCE_DIR=/candidate/resource']
        old = copy.deepcopy(self.before)
        old['flags'] = ['CC=clang', 'CFLAGS=-O2', 'LDFLAGS=-Wl,keep',
                        'EMBER_EXTERNAL_LLVM_RUNTIME=/previous/runtime',
                        'EMBER_LLVM_RESOURCE_DIR=/previous/resource']
        old['compiler'] = {'family': 'clang', 'version': '23.1.4'}
        new['compiler'] = copy.deepcopy(old['compiler'])
        record = {'signature': old}
        checks = []
        fake = SimpleNamespace(
            paths={'os': '/os', 'objects': '/obj'},
            context_signature=lambda path: new,
            COMMON_SOURCES=SysrootBuilder.COMMON_SOURCES,
            reuse_signature=SysrootBuilder.reuse_signature,
            compatible_context=lambda candidate, path: checks.append(copy.deepcopy(candidate['signature'])) or
                candidate['signature']['compiler'] == old['compiler'] and
                candidate['signature']['flags'] == new['flags'],
            compiler_record={'binaries': {'ar': {'selected_path': '/tool/ar'}}},
            environment={},
        )
        fake.reuse_signature = fake.reuse_signature.__get__(fake)
        with patch('emberbsd_sdk.sysroot_builder.prepared_refresh.plan', return_value=['other/cxx']) as plan:
            self.assertTrue(SysrootBuilder.plan_prepared_refresh(fake, record, '/obj'), repr(checks))
        self.assertEqual(fake.refresh_dirs, ['other/cxx'])
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]['compiler'], old['compiler'])
        self.assertEqual(checks[0]['flags'], new['flags'])
        plan.assert_called_once()

    def test_combined_source_and_runtime_refresh_normalizes_only_declared_flags(self):
        def source(package, tree):
            return {'format': 'emberbsd-prepared-source', 'version': 1,
                    'package': package, 'tree_sha256': tree}

        old, new = copy.deepcopy(self.before), copy.deepcopy(self.after)
        old['compiler'] = new['compiler'] = {'family': 'clang', 'version': '23.1.4'}
        old['llvm_source'] = {'llvm_version': '23.1.4', 'tree_sha256': 'a' * 64}
        new['llvm_source'] = {'llvm_version': '23.1.4', 'tree_sha256': 'b' * 64}
        old['common_sources'] = {
            'openssh': source('security/openssh', '1' * 64),
            'openssl': source('security/openssl', '2' * 64),
            'openldap': source('databases/openldap', '3' * 64),
        }
        new['common_sources'] = {
            'openssh': source('security/openssh', '4' * 64),
            'openssl': source('security/openssl', '2' * 64),
            'openldap': source('databases/openldap', '3' * 64),
        }
        old['flags'] = [
            'CC=clang', 'CFLAGS=-O2', 'LDFLAGS=-Wl,keep',
            'EMBER_EXTERNAL_LLVM_RUNTIME=/previous/runtime',
            'EMBER_LLVM_RESOURCE_DIR=/previous/resource',
            'EMBER_LLVM_SOURCE=/prepared/llvm-old',
            'EMBER_OPENSSH_SOURCE=/prepared/openssh-old',
            'EMBER_OPENSSL_SOURCE=/prepared/openssl-old',
            'EMBER_OPENLDAP_SOURCE=/prepared/openldap-old',
        ]
        new['flags'] = [
            'CC=clang', 'CFLAGS=-O2', 'LDFLAGS=-Wl,keep',
            'EMBER_EXTERNAL_LLVM_RUNTIME=/candidate/runtime',
            'EMBER_LLVM_RESOURCE_DIR=/candidate/resource',
            'EMBER_LLVM_SOURCE=/tmp/llvm-current',
            'EMBER_OPENSSH_SOURCE=/tmp/openssh-current',
            'EMBER_OPENSSL_SOURCE=/tmp/openssl-current',
            'EMBER_OPENLDAP_SOURCE=/tmp/openldap-current',
            'HOST_AUTOCONF=/native/bin/autoconf',
        ]
        record = {'signature': old}
        checks = []
        fake = SimpleNamespace(
            paths={'os': '/os', 'objects': '/obj', 'openssl-source': '/tmp/openssl-current',
                   'openldap-source': '/tmp/openldap-current'},
            context_signature=lambda path: new,
            COMMON_SOURCES=SysrootBuilder.COMMON_SOURCES,
            reuse_signature=SysrootBuilder.reuse_signature,
            compatible_context=lambda candidate, path:
                checks.append(copy.deepcopy(candidate['signature'])) or
                candidate['signature']['compiler'] == fake.context_signature(path)['compiler'] and
                candidate['signature']['flags'] == fake.context_signature(path)['flags'],
            compiler_record={'binaries': {'ar': {'selected_path': '/tool/ar'}}},
            environment={}, native=SimpleNamespace(tool=lambda role: '/native/bin/' + role),
        )
        fake.reuse_signature = fake.reuse_signature.__get__(fake)
        with patch('emberbsd_sdk.sysroot_builder.prepared_refresh.plan', return_value=['other/cxx']) as plan:
            self.assertTrue(SysrootBuilder.plan_prepared_refresh(fake, record, '/obj'), repr(checks))
        self.assertEqual(fake.refresh_dirs, ['other/cxx'])
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]['flags'], new['flags'])
        plan.assert_called_once()

        for label, mutate in (
                ('compiler', lambda candidate: candidate.update(compiler={'family': 'clang', 'version': '24'})),
                ('unrelated flag', lambda candidate: candidate['flags'].append('LDFLAGS=-Wl,changed')),
                ('wrong autoconf', lambda candidate: candidate['flags'].__setitem__(
                    candidate['flags'].index('HOST_AUTOCONF=/native/bin/autoconf'),
                    'HOST_AUTOCONF=/wrong/autoconf'))):
            with self.subTest(label=label):
                candidate = copy.deepcopy(new)
                mutate(candidate)
                fake.context_signature = lambda path, candidate=candidate: candidate
                self.assertFalse(SysrootBuilder.plan_prepared_refresh(fake, record, '/obj'))

        wrong_existing_host = copy.deepcopy(old)
        wrong_existing_host['flags'].append('HOST_AUTOCONF=/old/other-autoconf')
        fake.context_signature = lambda path: new
        self.assertFalse(SysrootBuilder.plan_prepared_refresh(
            fake, {'signature': wrong_existing_host}, '/obj'))

        path_only_old, path_only_new = copy.deepcopy(old), copy.deepcopy(old)
        path_only_new['flags'] = [flag.replace('/prepared/openssl-old', '/tmp/openssl-current')
                                  for flag in path_only_new['flags']]
        fake.context_signature = lambda path: path_only_new
        self.assertFalse(SysrootBuilder.plan_prepared_refresh(
            fake, {'signature': path_only_old}, '/obj'))

        no_refresh_old = copy.deepcopy(old)
        no_refresh_new = copy.deepcopy(old)
        no_refresh_new['llvm_source']['tree_sha256'] = 'c' * 64
        no_refresh_new['runtime']['manifest']['source']['sha512'] = '9' * 128
        no_refresh_new['flags'] = list(no_refresh_old['flags']) + ['EMBER_LLVM_SOURCE=/tmp/other-llvm']
        fake.context_signature = lambda path: no_refresh_new
        self.assertFalse(SysrootBuilder.plan_prepared_refresh(fake, {'signature': no_refresh_old}, '/obj'))


if __name__ == '__main__':
    unittest.main()
