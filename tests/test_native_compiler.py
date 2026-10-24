# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Native compiler input contracts. No fixture builds a real compiler."""

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from test_core import TemporaryCase, SDK_SOURCE
from test_builder import fixture as builder_fixture, snapshot, write
from native_compiler_fixture import fixture
from emberbsd_sdk import inventory, jsonio, native_compiler as native, process
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.sysroot_builder import SysrootBuilder
from emberbsd_sdk.os_release_builder import OSReleaseBuilder


class NativeCompilerTests(TemporaryCase):
    def setUp(self):
        super().setUp()
        self.options = fixture(self.root)
        self.environment = process.sanitized_environment()
        self.manifest = Path(self.options['host-toolchain-manifest'])

    def inspect(self, **changes):
        return native.inspect(self.options | changes, self.environment)

    def control(self, **changes):
        (self.root / 'native-control.json').write_text(json.dumps(changes))

    def test_explicit_compiler_manifest_and_portable_identity(self):
        before = snapshot(self.root)
        record = self.inspect()
        native.validate_record(record)
        self.assertEqual(record['manifest']['sha256'], inventory.sha256_file(self.manifest))
        self.assertEqual(record['binaries']['cc']['path'], self.options['host-cc'])
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(native.host_tools(record)['c++']['path'], self.options['host-cxx'])

    def test_missing_relative_and_system_selections(self):
        for option in native.OPTIONS:
            missing = self.options.copy(); del missing[option]
            with self.subTest(option=option), self.assertRaisesRegex(SDKError, option):
                native.inspect(missing, self.environment)
            with self.subTest(relative=option), self.assertRaisesRegex(SDKError, 'absolute'):
                self.inspect(**{option: 'gcc-16'})
        for root in ('/', '/usr', '/USR', '/usr/local', '/opt/homebrew', '/opt/local', '/Applications',
                     '/Library', '/LiBrArY', '/Applications/Xcode.app', '/library/developer'):
            with self.subTest(root=root), self.assertRaisesRegex(SDKError, 'provider|system tool'):
                native.provider_root(root)
        native.provider_root('/usr/local/emberbsd-sdk/native')
        alias = self.root / 'system-provider'; alias.symlink_to('/usr')
        with self.assertRaisesRegex(SDKError, 'provider'):
            self.inspect(**{'host-toolchain': str(alias)})
        with self.assertRaisesRegex(SDKError, 'selector differs'):
            self.inspect(**{'host-cc': '/usr/bin/cc'})

    def test_gcc14_clang_cross_target_and_host_abi_refusals(self):
        cases = [dict(version='14.3.0'), dict(triplet='aarch64--netbsd'),
                 dict(macros={'__clang__': '1'}), dict(macros={'__GNUC__': '14'}),
                 dict(macros={'__SIZEOF_POINTER__': '2'}), dict(macros={'__BYTE_ORDER__': 'invalid'})]
        other_os = '__linux__' if native.current_host()['system'] != 'Linux' else '__APPLE__'
        cases.append(dict(macros={other_os: '1'}))
        for case in cases:
            with self.subTest(case=case), self.assertRaises(SDKError):
                self.control(**case); self.inspect()

    def test_swapped_and_identical_default_driver_roles_are_rejected(self):
        baseline = jsonio.read_json(self.manifest)
        for swapped in (True, False):
            changed = copy.deepcopy(baseline)
            changed['tools']['cxx'] = baseline['tools']['cc']
            if swapped: changed['tools']['cc'] = baseline['tools']['cxx']
            jsonio.write_json(self.manifest, changed, exclusive=False)
            selected = {option: self.options['host-toolchain'] + '/' + changed['tools'][name]['path']
                        for name, option in (('cc', 'host-cc'), ('cxx', 'host-cxx'))}
            with self.subTest(swapped=swapped), self.assertRaisesRegex(SDKError, 'C/C\\+\\+ frontend mismatch'):
                self.inspect(**selected)

    def test_probe_content_is_checked_without_plan_writes(self):
        probe = self.root / 'native_probe.c'; probe.write_bytes(native.PROBE_CONTENT)
        with patch.object(native, '__file__', str(self.root / 'native_compiler.py')):
            before = snapshot(self.root); self.inspect(); self.assertEqual(snapshot(self.root), before)
            probe.write_bytes(b'/* altered */\n')
            with self.assertRaisesRegex(SDKError, 'probe bytes differ'): self.inspect()
            probe.write_bytes(native.PROBE_CONTENT)
            capture = process.capture
            def change_after_query(argv, **kwargs):
                result = capture(argv, **kwargs)
                if '-dM' in argv: probe.write_bytes(b'/* altered during query */\n')
                return result
            with patch.object(process, 'capture', change_after_query), self.assertRaisesRegex(SDKError, 'probe changed'):
                self.inspect()

    def test_paths_hashes_and_driver_subprocesses_are_bound(self):
        record = self.inspect()
        tool = Path(self.options['host-cc'])
        original = tool.read_bytes()
        tool.write_bytes(original + b'\n# stale bytes\n')
        with self.assertRaisesRegex(SDKError, 'tool hash differs'):
            self.inspect()
        tool.write_bytes(original)
        outside = self.root / 'outside'; outside.write_bytes(original); outside.chmod(0o755)
        tool.unlink(); tool.symlink_to(outside)
        with self.assertRaisesRegex(SDKError, 'escapes provider'):
            self.inspect()
        tool.unlink(); tool.write_bytes(original); tool.chmod(0o755)
        for path in ('as', str(outside)):
            self.control(programs={'as': path})
            with self.assertRaisesRegex(SDKError, 'implicit subprocess|different provider'):
                self.inspect()
        self.control(programs={'lto-wrapper': 'lto-wrapper'})
        with self.assertRaisesRegex(SDKError, 'implicit subprocess'): self.inspect()
        self.control()
        abi = Path(record['references']['abi_inputs'][0]['path']); abi.write_text('changed')
        with self.assertRaisesRegex(SDKError, 'abi_inputs hash differs'):
            self.inspect()

    def test_manifest_provenance_schema_and_portable_tampering(self):
        baseline = jsonio.read_json(self.manifest)
        for label in ('missing-producer', 'revision', 'abi', 'hash', 'escape', 'host'):
            changed = copy.deepcopy(baseline)
            if label == 'missing-producer': del changed['producer']
            elif label == 'revision': changed['producer']['revision'] = 'main'
            elif label == 'abi': changed['abi_inputs'] = []
            elif label == 'hash': changed['tools']['cc']['sha256'] = '0' * 64
            elif label == 'escape': changed['tools']['cc']['path'] = '../cc'
            else: changed['host']['machine'] = 'foreign'
            jsonio.write_json(self.manifest, changed, exclusive=False)
            with self.subTest(label=label), self.assertRaises(SDKError): self.inspect()
        jsonio.write_json(self.manifest, baseline, exclusive=False)
        record = self.inspect()
        for label in ('content', 'binary', 'query', 'cxx-role'):
            changed = copy.deepcopy(record)
            if label == 'content': changed['manifest']['content']['producer']['revision'] = 'b' * 40
            elif label == 'binary': changed['binaries']['cc']['path'] = '/usr/bin/cc'
            elif label == 'query': changed['compilers']['cc']['macros']['__clang__'] = '1'
            else: del changed['compilers']['cxx']['macros']['__cplusplus']
            with self.subTest(label=label), self.assertRaises(SDKError): native.validate_record(changed)
        Path(baseline['provenance'][0]['path']).write_text('changed provider receipt')
        with self.assertRaisesRegex(SDKError, 'provenance hash differs'): self.inspect()

    def test_native_platform_matching_does_not_infer_target(self):
        for system, machine, triplet in (('Darwin', 'aarch64', 'aarch64-apple-darwin27.0.0'),
                                        ('Darwin', 'x86_64', 'x86_64-apple-darwin27'),
                                        ('Linux', 'x86_64', 'x86_64-pc-linux-gnu'),
                                        ('Linux', 'aarch64', 'aarch64-unknown-linux-musl'),
                                        ('NetBSD', 'x86_64', 'x86_64--netbsd')):
            host = dict(system=system, machine=machine)
            with self.subTest(triplet=triplet):
                self.assertTrue(native.native_triplet(triplet, host))
                self.assertFalse(native.native_triplet('aarch64-none-elf', host))


class NativeProducerTests(TemporaryCase):
    def setUp(self):
        super().setUp()
        self.options = builder_fixture(self.root)

    def test_both_producers_refuse_missing_selectors_without_writes(self):
        for cls in (SysrootBuilder, OSReleaseBuilder):
            for option in native.OPTIONS:
                selected = self.options | dict(destination=self.options['sdk'] + '/stages/refused')
                del selected[option]
                before = snapshot(self.root)
                with self.subTest(producer=cls.__name__, option=option), self.assertRaisesRegex(SDKError, option):
                    cls(selected).run()
                self.assertEqual(snapshot(self.root), before)

    def test_both_clis_require_each_native_input(self):
        for script in ('build-sysroot.py', 'build-os-release.py'):
            for missing in native.OPTIONS:
                selected = self.options | dict(destination=self.options['sdk'] + '/stages/refused')
                del selected[missing]
                if script == 'build-os-release.py':
                    selected['os-source'] = selected.pop('os'); selected.pop('target')
                    selected['profile'] = 'aarch64-sysroot'
                argv = [sys.executable, '-B', str(SDK_SOURCE / 'scripts' / script), '--plan']
                argv += [arg for pair in selected.items() for arg in ('--' + pair[0], pair[1])]
                before = snapshot(self.root)
                result = subprocess.run(argv, capture_output=True)
                with self.subTest(script=script, missing=missing):
                    self.assertEqual(result.returncode, 2)
                    self.assertIn(('--' + missing).encode(), result.stderr)
                    self.assertEqual(snapshot(self.root), before)

    def test_neither_producer_queries_ambient_cc(self):
        original = SysrootBuilder.find_command
        def checked(builder, name):
            self.assertNotIn(name, ('cc', 'c++', 'gcc', 'g++'))
            return original(builder, name)
        for cls in (SysrootBuilder, OSReleaseBuilder):
            # The release source version is needed only for plan composition.
            with self.subTest(producer=cls.__name__), patch.object(SysrootBuilder, 'find_command', checked):
                if cls is OSReleaseBuilder:
                    from test_builder import commit
                    write(Path(self.options['os']) / 'share/mk/bsd.own.mk', 'EMBER_VERSION?=1.0\n')
                    commit(self.options['os'])
                plan = cls(self.options | dict(plan=True, destination=self.options['sdk'] + '/stages/planned')).run()
                for command in plan['plan']:
                    if 'build.sh' not in ' '.join(command): continue
                    self.assertIn('HOST_CC=' + self.options['host-cc'], command)
                    self.assertIn('HOST_CXX=' + self.options['host-cxx'], command)

    def test_pre_dispatch_and_pre_output_mutations_refuse(self):
        builder = SysrootBuilder(self.options)
        builder.preflight()
        changed = Path(self.options['host-cxx']); changed.write_text(changed.read_text() + '\n# change\n')
        with patch.object(process, 'OwnedProcessGroup', side_effect=AssertionError('unexpected producer')):
            with self.assertRaisesRegex(SDKError, 'tool hash differs'): builder.dispatch(['never-run'])
        self.assertEqual(builder.commands[-1]['scope'], 'runtime-check')

    def test_native_drift_before_output_does_not_touch_object_contexts(self):
        builder = SysrootBuilder(self.options)
        original = builder.preflight
        def changed_after_plan():
            original()
            compiler = Path(self.options['host-cc'])
            compiler.write_text(compiler.read_text() + '\n# changed after plan\n')
        with patch.object(builder, 'preflight', changed_after_plan), self.assertRaisesRegex(SDKError, 'tool hash differs'):
            builder.run()
        self.assertFalse(Path(builder.paths['destination'], builder.MARKER).exists())
        for name in ('objects', 'tools'):
            self.assertFalse(Path(builder.paths[name]).exists())

    def test_stale_native_context_is_not_reused(self):
        SysrootBuilder(self.options).run()
        manifest = Path(self.options['host-toolchain-manifest'])
        value = jsonio.read_json(manifest); value['producer']['revision'] = 'b' * 40
        jsonio.write_json(manifest, value, exclusive=False)
        before = snapshot(self.root)
        with self.assertRaisesRegex(SDKError, 'incompatible object context'):
            SysrootBuilder(self.options | dict(plan=True, destination=self.options['sdk'] + '/stages/next')).run()
        self.assertEqual(snapshot(self.root), before)

    def test_detected_native_drift_quarantines_context_after_restoration(self):
        builder = SysrootBuilder(self.options)
        compiler = Path(self.options['host-cc']); original = compiler.read_bytes()
        def drift():
            write(Path(builder.paths['objects']) / 'partial-object', 'possible changed compiler output')
            compiler.write_bytes(original + b'\n# changed while building\n')
        with patch.object(builder, 'build_payload', drift), self.assertRaisesRegex(SDKError, 'tool hash differs'):
            builder.run()
        compiler.write_bytes(original)
        for name in ('objects', 'tools'):
            self.assertTrue(Path(builder.paths[name], builder.QUARANTINE).is_file())
        with self.assertRaisesRegex(SDKError, 'quarantined'):
            SysrootBuilder(self.options | dict(plan=True, destination=self.options['sdk'] + '/stages/retry')).run()
