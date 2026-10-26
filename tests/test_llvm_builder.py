# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""LLVM first-base dependency ordering; synthetic outputs are not ABI evidence."""

from pathlib import Path
import json
import copy
from unittest.mock import patch

from test_core import TemporaryCase
from emberbsd_sdk import inventory, jsonio
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.sysroot_builder import SysrootBuilder
from emberbsd_sdk.llvm_sysroot import runtime_paths, llvm_flags, LLVMCoordinator, LLVMPhaseBuilder
from emberbsd_sdk.libedit import prepare_source
from emberbsd_sdk.target_toolchain import CLANG_BINARIES
from emberbsd_sdk import sysroot_transfer
from test_builder import B, fixture, write, git, commit
import test_llvm_runtime as runtime_tests


class ClangBuilder(B):
    """Dispatch only: synthetic driver identity is not compiler qualification."""
    def compiler(self):
        prefix = self.paths['compiler']
        names = {'cc': 'clang', 'cxx': 'clang++', 'cpp': 'clang', 'as': 'clang', 'ld': 'ld.lld'}
        binaries, commands = {}, {}
        for role in CLANG_BINARIES:
            path = prefix + '/bin/' + names.get(role, 'llvm-' + role)
            binaries[role] = dict(path=path, selected_path=path, sha256='f' * 64)
            commands[role] = [path]
        for role in ('cc', 'cxx'):
            commands[role] += ['--target=aarch64--netbsd', '--ld-path=' + binaries['ld']['path']]
        commands['cpp'] += ['-E', '--target=aarch64--netbsd']
        commands['as'] += ['--target=aarch64--netbsd', '-c', '-x', 'assembler']
        return dict(family='clang', version='23.1.4', triplet='aarch64--netbsd',
                    reported_triplet='aarch64-unknown-netbsd', resource_dir=prefix + '/lib/clang/23',
                    binaries=binaries, commands=commands)


class LLVMBuilderTests(TemporaryCase):
    def test_base_cxx_option_type_is_checked_before_compiler_selection(self):
        with self.assertRaisesRegex(SDKError, 'must be boolean'):
            SysrootBuilder({'compiler-family': 'gcc', 'base-cxx': 'false'}).preflight()

    def builder(self, options=None, builder_type=ClangBuilder):
        options = fixture(self.root) if options is None else options
        source = self.root / 'llvm-selected-source'; source.mkdir()
        runtime = runtime_tests.LLVMRuntimeTests()
        runtime.setUp(); self.addCleanup(runtime.doCleanups)
        runtime.full()
        source_record = dict(format='emberbsd-llvm-source', version=1, llvm_version='23.1.4',
                             source=runtime.record['source'])
        write(source / 'llvm-source.json', json.dumps(source_record))
        script = 'pkgsrc/lang/compiler-rt/files/runtime.py'
        write(Path(options['ports']) / script, '''import json,sys
from pathlib import Path
a=sys.argv[1:]
key='--source' if 'verify-source' in a else '--stage'
print((Path(a[a.index(key)+1])/('llvm-source.json' if key=='--source' else 'runtime.json')).read_text())
''')
        commit(options['ports'])
        options['libedit-source'] = str(self.root / 'libedit-current')
        prepare_source(options['ports'], self.root / 'libedit.tar.gz', options['libedit-source'])
        runtime.record['recipe'] = dict(ports_revision=git(options['ports'], 'rev-parse', 'HEAD'),
            files={script: inventory.sha256_file(Path(options['ports']) / script)})
        runtime.record['base']['os_revision'] = git(options['os'], 'rev-parse', 'HEAD')
        runtime.save()
        options.update({'compiler-family': 'clang', 'llvm-source': str(source),
                        'llvm-runtime-stage': str(runtime.stage),
                        'destination': str(Path(options['sdk']) / 'stages/clang')})
        return builder_type(options), runtime

    def manifest(self):
        return {'prefix': 'usr/pkg/llvm', 'llvm_version': '23.1.4', 'artifacts': {
            'builtins': 'usr/pkg/llvm/lib/clang/23/lib/netbsd/libclang_rt.builtins-aarch64.a',
            'libunwind': {'static': 'usr/pkg/llvm/lib/libunwind.a'}}}

    def test_resource_location_comes_from_measured_builtins(self):
        stage = str(self.root / 'stage')
        selected = runtime_paths(stage, self.manifest())
        self.assertEqual(selected['llvm-resource'], stage + '/usr/pkg/llvm/lib/clang/23')
        self.assertEqual(selected['llvm-unwind'], stage + '/usr/pkg/llvm/lib/libunwind.a')
        bad = self.manifest()
        bad['artifacts']['builtins'] = 'usr/pkg/llvm/lib/libbuiltins.a'
        with self.assertRaisesRegex(SDKError, 'resource directory'):
            runtime_paths(stage, bad)

    def test_base_and_final_have_distinct_runtime_policies(self):
        selected = {'compiler': '/compiler', 'llvm-source': '/source',
                    **runtime_paths('/stage', self.manifest())}
        base = llvm_flags(selected, phase='base')
        self.assertIn('MKCXX=no', base)
        self.assertIn('EMBER_LIBUNWIND_ARCHIVE=/stage/usr/pkg/llvm/lib/libunwind.a', base)
        self.assertIn('EMBER_EXTERNAL_LLVM_RUNTIME=/stage/usr/pkg/llvm', base)
        self.assertIn('EMBER_LLVM_RUNTIME_PHASE=bootstrap', base)
        self.assertIn('EMBER_LLVM_RESOURCE_DIR=/stage/usr/pkg/llvm/lib/clang/23', base)
        final = llvm_flags(selected, phase='final')
        self.assertIn('MKCXX=yes', final)
        self.assertIn('MKLIBCXX=no', final)
        self.assertIn('EMBER_EXTERNAL_LLVM_RUNTIME=/stage/usr/pkg/llvm', final)
        self.assertIn('EMBER_LLVM_RUNTIME_PHASE=full', final)
        self.assertFalse(any('GCC' in v or 'STDCXX' in v for v in final))
        opted_in = llvm_flags(selected, phase='final', base_cxx=True)
        self.assertIn('MKLIBCXX=yes', opted_in)
        self.assertIn('MKLIBCXX=no', llvm_flags(selected, phase='final', base_cxx=False))

    def test_bootstrap_phases_do_not_require_a_final_runtime(self):
        options = {'sdk': str(self.root / 'sdk'), 'runtime-work': str(self.root / 'sdk/obj/runtime'),
                   'target': 'aarch64', 'destination': str(self.root / 'sdk/stages/final'),
                   'jobs': '2', 'host-prefix': '/host', 'compiler': '/compiler',
                   'llvm-source': '/source', 'sdk-source': '/sdk-source', 'os': '/os', 'ports': '/ports'}
        coordinator = LLVMCoordinator(options, SysrootBuilder)
        events = []
        class Stage:
            def __init__(self, selected, phase):
                self.options, self.phase = selected, phase
            def run(self):
                events.append(self.phase)
                return {'phase': self.phase}
        class Final:
            def __init__(self, selected):
                self.options = selected
            def run(self):
                events.append('final')
                self_stage = self.options['llvm-runtime-stage']
                return {'runtime': self_stage}
        coordinator.builder_type = Final
        with patch.object(coordinator, 'prepare', return_value=None), \
             patch.object(coordinator, 'lock'), \
             patch('emberbsd_sdk.llvm_sysroot.LLVMPhaseBuilder', Stage), \
             patch.object(coordinator, 'runtime', side_effect=lambda phase, base: events.append(phase)):
            result = coordinator.run()
        self.assertEqual(events, ['headers', 'bootstrap', 'base', 'full', 'final'])
        self.assertEqual(result['runtime'], options['runtime-work'] + '/full-stage')

    def test_headers_use_the_limited_os_operation_without_runtime_headers(self):
        class Headers(LLVMPhaseBuilder, ClangBuilder):
            pass
        builder, _ = self.builder(builder_type=lambda options:Headers(options, 'headers'))
        receipt = builder.preflight()
        self.assertEqual(len(receipt['plan']), 1)
        self.assertEqual(receipt['plan'][0][-2:], ['tools', 'bootstrap-headers'])
        for name in ('stdio.h','sys/types.h','sys/param.h','stdint.h','limits.h','math.h','pthread.h'):
            write(Path(builder.paths['destination'])/'usr/include'/name, '/* base header */\n')
        builder.inspect_payload()
        self.assertFalse((Path(builder.paths['destination'])/'usr/include/unwind.h').exists())

    def test_final_dispatch_copies_only_the_selected_llvm_runtime(self):
        builder, runtime = self.builder()
        result = builder.run()
        self.assertEqual(result['version'], 3)
        self.assertEqual(result['qualification'], 'built-and-inspected')
        self.assertEqual(result['signature']['runtime']['manifest'], runtime.record)
        output = Path(builder.paths['destination'])
        self.assertTrue((output / 'usr/pkg/llvm/lib/libc++.so').is_symlink())
        self.assertFalse((output / 'usr/pkg/gcc16').exists())
        self.assertIn('EMBER_COMPILER_FAMILY=clang', result['signature']['flags'])
        self.assertIn('EMBER_HOST_COMPILER_FAMILY=gcc', result['signature']['flags'])

    def test_opted_in_base_cxx_aliases_export_import_and_relocate(self):
        from emberbsd_sdk import llvm_runtime_install
        builder, runtime = self.builder()
        builder.options['base-cxx'] = True
        receipt = builder.run()
        root = Path(builder.paths['destination'])
        plan = llvm_runtime_install.base_alias_plan(runtime.record)
        self.assertIn('MKLIBCXX=yes', receipt['signature']['flags'])
        self.assertIn('usr/include/c++/vector', plan)
        self.assertTrue((root / 'usr/include/c++/vector').is_symlink())
        self.assertEqual(receipt['outputs'], inventory.sysroot(root, 183,
            exclude=(builder.RECEIPT, builder.LOG, builder.MARKER)))
        bundle = self.root / 'base-cxx-bundle'
        exported = sysroot_transfer.export_sysroot(root, bundle, target='aarch64')
        manifest = jsonio.read_json(bundle / 'manifest.json')
        self.assertEqual(manifest['provenance']['base_cxx'], 'libcxx')
        alias = 'usr/include/c++/vector'
        missing = copy.deepcopy(manifest)
        del missing['inventory'][alias]
        with self.assertRaises(SDKError):
            sysroot_transfer.validate_manifest(missing, target='aarch64')
        changed = copy.deepcopy(manifest)
        changed['inventory'][alias]['target'] = '../../pkg/llvm/include/c++/v1'
        with self.assertRaisesRegex(SDKError, 'base alias'):
            sysroot_transfer.validate_manifest(changed, target='aarch64')
        extra = copy.deepcopy(manifest)
        extra['inventory']['usr/include/c++/legacy'] = {'kind': 'directory', 'mode': 0o755}
        extra['inventory']['usr/include/c++/legacy/old.h'] = {'kind': 'file', 'mode': 0o644,
            'size': 0, 'sha256': '0' * 64}
        with self.assertRaisesRegex(SDKError, r'base C\+\+ namespace'):
            sysroot_transfer.validate_manifest(extra, target='aarch64')
        forged = copy.deepcopy(manifest)
        forged['provenance']['base_cxx'] = 'other'
        with self.assertRaisesRegex(SDKError, 'provenance'):
            sysroot_transfer.validate_manifest(forged, target='aarch64')
        restored = self.root / 'base-cxx-restored'
        sysroot_transfer.import_sysroot(bundle, restored, target='aarch64',
            manifest_sha256=exported['manifest_sha256'])
        llvm_runtime_install.validate_base_aliases(restored, runtime.record)

    def test_base_alias_conflict_preflights_before_runtime_copy(self):
        builder, runtime = self.builder()
        builder.options['base-cxx'] = True
        builder.preflight()
        conflict = Path(builder.paths['destination']) / 'usr/include/c++/vector'
        conflict.parent.mkdir(parents=True)
        conflict.write_text('conflict')
        with self.assertRaisesRegex(SDKError, 'alias conflict'):
            builder.copy_runtime()
        self.assertFalse((Path(builder.paths['destination']) / 'usr/pkg/llvm').exists())

    def test_runtime_aliases_follow_configured_flags_not_raw_option(self):
        builder, _ = self.builder()
        builder.options['base-cxx'] = False
        builder.preflight()
        Path(builder.paths['destination']).mkdir(parents=True)
        builder.flags = [flag for flag in builder.flags if not flag.startswith('MKLIBCXX=')]
        builder.flags.append('MKLIBCXX=yes')
        builder.copy_runtime()
        self.assertTrue((Path(builder.paths['destination']) / 'usr/include/c++/vector').is_symlink())

    def test_copy_runtime_rejects_base_cxx_flags_outside_final_clang_before_io(self):
        builder, _ = self.builder()
        builder.preflight()
        builder.flags = [flag for flag in builder.flags if not flag.startswith(('MKCXX=', 'MKLIBCXX='))]
        builder.flags.extend(('MKCXX=yes', 'MKLIBCXX=yes'))
        builder.options['base-cxx'] = False
        with patch.object(builder, 'verify_runtime', side_effect=AssertionError('must reject before runtime access')):
            builder.options['compiler-family'] = 'gcc'
            with self.assertRaisesRegex(SDKError, 'final Clang'):
                builder.copy_runtime()
            builder.options['compiler-family'] = 'clang'
            builder.llvm_phase = 'base'
            with self.assertRaisesRegex(SDKError, 'final Clang'):
                builder.copy_runtime()

    def test_runtime_of_another_llvm_version_fails_before_outputs(self):
        builder, runtime = self.builder()
        runtime.record['llvm_version'] = '22.1.0'; runtime.save()
        with self.assertRaisesRegex(SDKError, 'runtime compiler'):
            builder.run()
        self.assertFalse(Path(builder.options['destination']).exists())

    def test_compatible_runtime_survives_unrelated_ports_commit(self):
        builder, runtime = self.builder()
        builder.preflight()
        builder.sources['ports']['commit'] = '1' * 40
        self.assertEqual(builder.verify_runtime()['manifest'], runtime.record)
        recipe = Path(builder.paths['ports']) / next(iter(runtime.record['recipe']['files']))
        recipe.write_text(recipe.read_text() + '\n# changed recipe input\n')
        with self.assertRaisesRegex(SDKError, 'recipe input changed'):
            builder.verify_runtime()

    def test_clang_transfer_is_explicit_and_preserves_historical_format(self):
        builder, runtime = self.builder()
        receipt = builder.run()
        bundle = self.root / 'bundle'
        exported = sysroot_transfer.export_sysroot(builder.paths['destination'], bundle, target='aarch64')
        manifest = jsonio.read_json(bundle / 'manifest.json')
        self.assertEqual(manifest['version'], 2)
        self.assertEqual(manifest['provenance']['compiler']['family'], 'clang')
        sysroot_transfer.import_sysroot(bundle, self.root / 'restored', target='aarch64',
            manifest_sha256=exported['manifest_sha256'])
        advanced = copy.deepcopy(receipt)
        advanced['sources']['ports']['commit'] = '1' * 40
        sysroot_transfer._provenance(advanced, 'aarch64')
        mixed = copy.deepcopy(receipt)
        mixed['signature']['llvm_source']['source']['sha512'] = 'b' * 128
        with self.assertRaisesRegex(SDKError, 'runtime differs'):
            sysroot_transfer._provenance(mixed, 'aarch64')
        receipt['version'] = 2
        with self.assertRaisesRegex(SDKError, 'GCC16'):
            sysroot_transfer._provenance(receipt, 'aarch64')

    def test_target_runtime_changes_do_not_rebuild_native_tool_context(self):
        builder, runtime = self.builder()
        builder.preflight()
        before = builder.context_signature(builder.paths['tools'])
        builder.signature['runtime'] = {'changed': True}
        builder.signature['llvm_phase'] = 'base'
        self.assertEqual(builder.context_signature(builder.paths['tools']), before)
        self.assertNotEqual(builder.context_signature(builder.paths['objects']), before)
        builder.signature['native_toolset'] = {'different': True}
        self.assertNotEqual(builder.context_signature(builder.paths['tools']), before)
