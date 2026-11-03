# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Composition fixtures test provenance and selection, not target execution."""
import copy
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from test_core import header
from test_llvm_builder import ClangBuilder
from test_llvm_runtime import LLVMRuntimeFixture
from emberbsd_sdk import inventory, jsonio, llvm_runtime_install, sysroot_composition as composition
from emberbsd_sdk import application_toolchain, sysroot_transfer
from emberbsd_sdk.errors import SDKError


class SysrootCompositionTests(LLVMRuntimeFixture):
    def setUp(self):
        super().setUp()
        self.full()
        self.kit = self.root / 'kit'; self.kit.mkdir()
        self.base = self.kit / 'seed'; self.base.mkdir()
        for name in ('stdio.h', 'sys/types.h', 'math.h', 'pthread.h'):
            path = self.base / 'usr/include' / name
            path.parent.mkdir(parents=True, exist_ok=True); path.write_text('// base\n')
        (self.base / 'usr/lib').mkdir(parents=True)
        for name in ('crt0.o', 'crti.o', 'crtn.o'):
            (self.base / 'usr/lib' / name).write_bytes(header(183, 1))
        for name in ('libc.so', 'libm.so', 'libpthread.so'):
            (self.base / 'usr/lib' / name).write_bytes(header(183, 3))
        (self.base / 'libexec').mkdir()
        (self.base / 'libexec/ld.elf_so').write_bytes(header(183, 3))
        self.record['base']['files'] = {'usr/include/stdio.h': inventory.sha256_file(self.base / 'usr/include/stdio.h')}
        self.save()
        shutil.copytree(self.prefix, self.base / 'usr/pkg/llvm', symlinks=True)
        llvm_runtime_install.install_base_aliases(self.base, self.record)
        old_runtime = copy.deepcopy(self.record)
        (self.base / 'usr/bin').mkdir(); (self.base / 'usr/bin/unneeded').write_text('not SDK input')
        # The extractor is a separate Ports owner with its own archive tests.
        self.ports = self.root / 'ports'
        extractor = self.ports / 'profiles/common-build-tools/cross/sysroot_archive.py'
        extractor.parent.mkdir(parents=True)
        extractor.write_text('import shutil\ndef extract(archives, root):\n'
                             '    shutil.copytree(archives[0].parent / "seed", root, symlinks=True)\n')
        files = []
        for name in ('base.tar.xz', 'comp.tar.xz'):
            path = self.kit / name; path.write_bytes(name.encode())
            files.append(dict(name=name, size=path.stat().st_size, sha256=inventory.sha256_file(path)))
        self.manifest = dict(repository='oxtech-ember/EmberBSD', architecture='aarch64',
            source_commit=self.record['base']['os_revision'], files=files,
            build_profile=dict(sdk_receipt=dict(signature=dict(runtime=dict(manifest=old_runtime)))))
        jsonio.write_json(self.kit / 'manifest.json', self.manifest)
        self.pin = inventory.sha256_file(self.kit / 'manifest.json')
        self.compiler = ClangBuilder.compiler(SimpleNamespace(paths={'compiler': '/compiler'}))
        # The new runtime replaces bytes while preserving the C base.
        (self.prefix / 'include/c++/v1/vector').write_text('// new C++ runtime\n')
        self.save()

    def compose(self):
        return composition.compose(self.kit, self.root / 'bundle', manifest_sha256=self.pin,
            runtime_stage=self.stage, ports_source=self.ports, target='aarch64',
            compiler=self.compiler, sdk_revision='a' * 40)

    def test_composed_bundle_keeps_base_and_new_runtime_with_truthful_origin(self):
        result = self.compose()
        destination = self.root / 'installed'
        sysroot_transfer.import_sysroot(self.root / 'bundle', destination, target='aarch64',
                                       manifest_sha256=result['manifest_sha256'])
        self.assertEqual((destination / 'usr/include/c++/vector').read_text(), '// new C++ runtime\n')
        self.assertFalse((destination / 'usr/bin').exists())
        self.assertFalse((destination / sysroot_transfer.RECEIPT).exists())
        origin = jsonio.read_json(destination / 'usr/share/emberbsd/sysroot-origin.json')
        self.assertEqual(origin['base']['manifest_sha256'], self.pin)
        self.assertEqual(origin['execution_acceptance'], 'not-assessed')
        self.assertEqual(application_toolchain.load_clang_runtime(destination, target='aarch64'), self.record)

    def test_changed_set_or_manifest_is_refused_before_extraction(self):
        original = (self.kit / 'base.tar.xz').read_bytes()
        (self.kit / 'base.tar.xz').write_bytes(b'changed')
        with self.assertRaisesRegex(SDKError, 'release set'):
            self.compose()
        (self.kit / 'base.tar.xz').write_bytes(original)
        self.pin = '0' * 64
        with self.assertRaisesRegex(SDKError, 'manifest SHA256'):
            self.compose()
        self.assertFalse((self.root / 'bundle').exists())

    def test_runtime_built_for_another_base_is_refused(self):
        self.record['base']['os_revision'] = 'f' * 40; self.save()
        with self.assertRaisesRegex(SDKError, 'runtime differs from release'):
            self.compose()
        self.assertFalse((self.root / 'bundle').exists())

    def test_runtime_base_bytes_must_match_pinned_sets(self):
        self.record['base']['files']['usr/include/stdio.h'] = 'f' * 64; self.save()
        with self.assertRaisesRegex(SDKError, 'base inputs changed'):
            self.compose()
