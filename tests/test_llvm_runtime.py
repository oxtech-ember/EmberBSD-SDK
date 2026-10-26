# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Portable LLVM runtime stage contracts; synthetic ELF is not ABI acceptance."""

import copy
import struct
from unittest.mock import patch

from test_core import TemporaryCase, header, archive
from emberbsd_sdk import inventory, jsonio, llvm_runtime
from emberbsd_sdk.errors import SDKError


def shared_elf(soname, *, needed=('libc.so.12',), runpath=None):
    """Real-shaped ELF dynamic metadata, without compiled machine code."""
    strings = bytearray(b'\0')
    def string(value):
        offset = len(strings); strings.extend(value.encode() + b'\0'); return offset
    tags = [(14, string(soname)), *[(1, string(name)) for name in needed]]
    if runpath is not None:
        tags.append((29, string(runpath)))
    dynamic = b''.join(struct.pack('<qQ', tag, value) for tag, value in
                       [(5, 0x1000 + 400), (10, len(strings)), *tags, (0, 0)])
    data = bytearray(header(183, 3))
    struct.pack_into('<Q', data, 32, 64)
    struct.pack_into('<HH', data, 54, 56, 2)
    size = max(512, 400 + len(strings))
    data.extend(struct.pack('<IIQQQQQQ', 1, 4, 0, 0x1000, 0, size, size, 4096))
    data.extend(struct.pack('<IIQQQQQQ', 2, 4, 256, 0x1100, 0, len(dynamic), len(dynamic), 8))
    data.extend(b'\0' * (size - len(data)))
    data[256:256 + len(dynamic)] = dynamic
    data[400:400 + len(strings)] = strings
    return bytes(data)


class LLVMRuntimeTests(TemporaryCase):
    def setUp(self):
        super().setUp()
        self.stage = self.root / 'stage'
        self.prefix = self.stage / 'usr/pkg/llvm'
        self.builtins = 'usr/pkg/llvm/lib/clang/23/lib/netbsd/libclang_rt.builtins-aarch64.a'
        self.write('stage/' + self.builtins, archive(header(183, 1)))
        self.record = dict(format='emberbsd-llvm-runtime', version=1,
            compiler_family='clang', llvm_version='23.1.4', components=['builtins'],
            target='aarch64', triplet='aarch64--netbsd', machine_arch='aarch64', elf_machine=183,
            prefix='usr/pkg/llvm', source=dict(archive='llvm-project-23.1.4.src.tar.xz', sha512='a' * 128),
            recipe=dict(ports_revision='b' * 40, files={'pkgsrc/lang/llvm-runtimes/Makefile': 'c' * 64}),
            base=dict(os_revision='d' * 40, files={'usr/include/stdint.h': 'e' * 64}, symlinks={}),
            artifacts=dict(builtins=self.builtins), files={}, symlinks={})
        self.save()

    def save(self):
        rows = inventory.snapshot_tree(self.stage, exclude=('runtime.json',))
        self.record['files'] = {name: row['sha256'] for name, row in rows.items() if row['kind'] == 'file'}
        self.record['symlinks'] = {name: row['target'] for name, row in rows.items() if row['kind'] == 'link'}
        jsonio.write_json(self.stage / 'runtime.json', self.record, exclusive=False)

    def static_unwind(self):
        path = 'usr/pkg/llvm/lib/libunwind.a'
        self.write('stage/' + path, archive(header(183, 1)))
        self.record['components'].append('libunwind-static')
        self.record['artifacts']['libunwind'] = dict(static=path)
        self.save()

    def full(self):
        self.record['components'] = ['builtins', 'libunwind', 'libcxxabi', 'libcxx']
        self.record['artifacts']['cxx_headers'] = 'usr/pkg/llvm/include/c++/v1'
        self.write('stage/usr/pkg/llvm/include/c++/v1/vector', '// fixture\n')
        for component, name in (('libunwind', 'libunwind'), ('libcxxabi', 'libc++abi'), ('libcxx', 'libc++')):
            static, shared = 'usr/pkg/llvm/lib/' + name + '.a', 'usr/pkg/llvm/lib/' + name + '.so.1'
            self.write('stage/' + static, archive(header(183, 1)))
            self.write('stage/' + shared, shared_elf(name + '.so.1'))
            self.record['artifacts'][component] = dict(static=static, shared=shared)
            (self.prefix / ('lib/' + name + '.so')).symlink_to(name + '.so.1')
        self.save()

    def test_builtins_does_not_require_a_final_sdk_or_cpp_runtime(self):
        self.assertEqual(llvm_runtime.validate_stage(self.stage, required_components=('builtins',)), self.record)
        with self.assertRaisesRegex(SDKError, 'required runtime components'):
            llvm_runtime.validate_stage(self.stage, required_components=('libcxx',))

    def test_static_unwind_bootstrap_precedes_shared_runtime(self):
        self.static_unwind()
        llvm_runtime.validate_stage(self.stage, required_components=('builtins', 'libunwind-static'))
        with self.assertRaisesRegex(SDKError, 'required runtime components'):
            llvm_runtime.validate_stage(self.stage, required_components=('libunwind',))

    def test_full_stage_and_relocation(self):
        self.full()
        llvm_runtime.validate_stage(self.stage, target='aarch64', required_components=('builtins', 'libcxx', 'libcxxabi', 'libunwind'))
        relocated = self.root / 'relocated'
        self.stage.rename(relocated)
        self.assertEqual(llvm_runtime.validate_stage(relocated, required_components=('libunwind-static',)), self.record)

    def test_artifact_mutation_is_not_accepted(self):
        (self.stage / self.builtins).write_bytes(archive(header(62, 1)))
        with self.assertRaises(SDKError):
            llvm_runtime.validate_stage(self.stage)

    def test_matching_inventory_cannot_hide_the_wrong_machine(self):
        (self.stage / self.builtins).write_bytes(archive(header(62, 1)))
        self.save()
        with self.assertRaises(SDKError):
            llvm_runtime.validate_stage(self.stage)

    def test_unlisted_payload_and_external_link_are_rejected(self):
        extra = self.write('stage/usr/pkg/llvm/lib/unrecorded', 'unexpected')
        with self.assertRaisesRegex(SDKError, 'inventory'):
            llvm_runtime.validate_stage(self.stage)
        extra.unlink()
        (self.prefix / 'outside').symlink_to(self.root)
        with self.assertRaises(SDKError):
            llvm_runtime.validate_stage(self.stage)

    def test_portable_identity_rejects_mixed_or_unsafe_inputs(self):
        for field, value in (('compiler_family', 'gcc'), ('triplet', 'x86_64--netbsd'),
                             ('elf_machine', 62), ('prefix', '../outside'),
                             ('components', ['builtins', 'libcxx'])):
            bad = copy.deepcopy(self.record); bad[field] = value
            with self.subTest(field=field), self.assertRaises(SDKError):
                llvm_runtime.validate_identity(bad)
        bad = copy.deepcopy(self.record); bad['source']['archive'] = '/private/input.tar.xz'
        with self.assertRaises(SDKError):
            llvm_runtime.validate_identity(bad)

    def test_optional_base_check_uses_actual_inputs_without_sdk_manifest(self):
        base = self.root / 'base'
        file = self.write('base/usr/include/stdint.h', '// actual header\n')
        self.record['base']['files'] = {'usr/include/stdint.h': inventory.sha256_file(file)}
        self.save()
        llvm_runtime.validate_stage(self.stage, base=base)
        self.write('base/usr/lib/libc.so.12', shared_elf('libc.so.12', needed=()))
        llvm_runtime.validate_stage(self.stage, base=base)
        file.write_text('// changed\n')
        with self.assertRaisesRegex(SDKError, 'base inputs'):
            llvm_runtime.validate_stage(self.stage, base=base)

    def test_portable_inventory_cannot_have_a_file_as_ancestor(self):
        self.record['files']['usr/pkg/llvm/lib'] = 'f' * 64
        with self.assertRaisesRegex(SDKError, 'non-directory ancestor'):
            llvm_runtime.validate_identity(self.record)

    def test_empty_archive_is_not_a_builtins_provider(self):
        (self.stage / self.builtins).write_bytes(b'!<arch>\n')
        self.save()
        with self.assertRaisesRegex(SDKError, 'empty LLVM runtime archive'):
            llvm_runtime.validate_stage(self.stage)

    def test_shared_runtime_roles_reject_gcc_and_foreign_search_paths(self):
        self.full()
        path = self.stage / self.record['artifacts']['libcxx']['shared']
        for data in (shared_elf('libstdc++.so.7'),
                     shared_elf('libc++.so.1', needed=('libgcc_s.so.1',)),
                     shared_elf('libc++.so.1', runpath='/unrelated/runtime/lib')):
            path.write_bytes(data); self.save()
            with self.assertRaises(SDKError):
                llvm_runtime.validate_stage(self.stage)

    def test_recipe_checks_use_explicit_revision_without_git_discovery(self):
        source = self.write('ports/pkgsrc/lang/llvm-runtimes/Makefile', '# recipe\n')
        self.record['recipe']['files'] = {'pkgsrc/lang/llvm-runtimes/Makefile': inventory.sha256_file(source)}
        self.save()
        with patch('emberbsd_sdk.process.capture', side_effect=AssertionError('unexpected tool discovery')):
            llvm_runtime.validate_stage(self.stage, ports=self.root / 'ports', ports_revision='b' * 40)
        with self.assertRaisesRegex(SDKError, 'explicit Ports revision'):
            llvm_runtime.validate_stage(self.stage, ports=self.root / 'ports')
        with self.assertRaisesRegex(SDKError, 'Ports revision changed'):
            llvm_runtime.validate_stage(self.stage, ports_revision='f' * 40)

    def test_consumed_bootstrap_identity_has_no_local_path(self):
        self.full()
        self.record['bootstrap'] = dict(manifest_sha256='f' * 64,
            files={self.builtins: self.record['files'][self.builtins]})
        self.save()
        llvm_runtime.validate_stage(self.stage)
        for value in ({'manifest_sha256': 'not-a-digest', 'files': self.record['bootstrap']['files']},
                      {'manifest_sha256': 'f' * 64, 'files': {'/private/stage/builtins.a': 'a' * 64}}):
            self.record['bootstrap'] = value
            with self.assertRaises(SDKError):
                llvm_runtime.validate_identity(self.record)
