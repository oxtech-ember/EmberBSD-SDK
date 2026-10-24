# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Finite metadata fixtures only. These bytes are not a qualified toolset."""

import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.inventory import snapshot_tree
from emberbsd_sdk.jsonio import MAX_JSON, json_bytes
from emberbsd_sdk.native_toolset import (NATIVE_TOOLSET_API, NATIVE_TOOL_ROLES,
                                        NATIVE_C_PROBE, NATIVE_CXX_PROBE, _gcc, load_native_toolset,
                                        provider_root, validate_selection)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Fixture:
    def __init__(self, temporary, bundle_name='bundle with spaces'):
        self.base = Path(temporary).resolve()
        self.bundle = self.base / bundle_name
        self.bundle.mkdir()
        self.evidence = self.base / 'evidence'
        self.evidence.mkdir()
        self.serial = 0
        self.host = dict(os='Linux', cpu='x86_64')
        image = bytearray(64)
        image[:7] = b'\x7fELF\x02\x01\x01'
        struct.pack_into('<HH', image, 16, 2, 62)
        self.image = bytes(image)
        self.compiler = dict(family='gcc', version='16.2.0', triplet='x86_64-pc-linux-gnu')
        self.sources = {name: dict(repository='https://example.invalid/' + name,
                                   commit='1' * 40) for name in ('sdk', 'ports')}
        self.providers = dict(ports=dict(source='ports', recipe='native/tools', recipe_sha256='2' * 64))
        self.records = {}
        for role in (*NATIVE_TOOL_ROLES, 'cc1', 'cc1plus', 'collect2', 'lto1', 'lto-wrapper'):
            path = self.bundle / role
            path.write_bytes(self.image)
            path.chmod(0o755)
            self.records[role] = dict(path=str(path), sha256=digest(path),
                                      version='16.2.0' if role in ('cc', 'cxx', 'cpp') else 'fixture-1',
                                      provider='ports', kind='native')
        self.build = dict(format='emberbsd-native-provider-build', version=1, scope='native-build',
                          status='PASS', host=self.host, source=self.sources['ports'],
                          recipe='native/tools', recipe_sha256='2' * 64, compiler=self.compiler,
                          accepted_input_scope='qualified-native', build_log=self.raw(b'finite fake build log'))
        abi = {}
        for name in ('headers', 'libraries'):
            root = self.base / name
            root.mkdir()
            (root / 'fixture-input').write_bytes(b'explicit ABI fixture')
            abi[name] = [dict(root=str(root), inventory=self.document(snapshot_tree(str(root))))]
        self.descriptor = dict(format='emberbsd-native-toolset', version=1, scope='native-build',
                               bundle_root=str(self.bundle), host=self.host, compiler=self.compiler,
                               sources=self.sources, providers=self.providers, tools=self.records,
                               host_abi=abi, abi_flags={name: [] for name in
                                                       ('cppflags', 'cflags', 'cxxflags', 'ldflags')},
                               driver_components={key: key for key in
                                                  ('cc1', 'cc1plus', 'collect2', 'lto1', 'lto-wrapper', 'as', 'ld')})
        checks = {}
        for role in ('cc', 'cxx'):
            source = self.base / ('probe.c' if role == 'cc' else 'probe.cc')
            source.write_bytes(NATIVE_C_PROBE if role == 'cc' else NATIVE_CXX_PROBE)
            obj = self.base / (role + '.o')
            object_image = bytearray(self.image)
            struct.pack_into('<H', object_image, 16, 1)
            obj.write_bytes(object_image)
            artifact = self.base / (role + '-native-probe')
            artifact.write_bytes(self.image)
            artifact.chmod(0o755)
            checks[role] = dict(
                source=dict(path=str(source), sha256=digest(source)),
                object=dict(path=str(obj), sha256=digest(obj)),
                artifact=dict(path=str(artifact), sha256=digest(artifact)),
                compile=self.command([self.records[role]['path'], '-c', str(source), '-o', str(obj)]),
                link=self.command([self.records[role]['path'], str(obj), '-o', str(artifact)]),
                run=self.command([str(artifact)], b'emberbsd-native-c-v1\n' if role == 'cc'
                                 else b'emberbsd-native-cxx-v1\n'))
        checks['identity'] = {
            role: {option: self.command([self.records[role]['path'], option], (value + '\n').encode())
                   for option, value in (('-dumpfullversion', '16.2.0'),
                                         ('-dumpmachine', 'x86_64-pc-linux-gnu'))}
            for role in ('cc', 'cxx')}
        checks['driver_components'] = {
            key: self.command([self.records['cxx' if key == 'cc1plus' else 'cc']['path'],
                               '-print-prog-name=' + key], (self.records[key]['path'] + '\n').encode())
            for key in self.descriptor['driver_components']}
        self.receipt = dict(format='emberbsd-native-toolset-qualification', version=1,
                            scope='native-build', status='PASS', host=self.host, compiler=self.compiler,
                            sources=self.sources, providers=self.providers, host_abi=abi,
                            abi_flags=self.descriptor['abi_flags'], checks=checks)
        self.seal()

    def raw(self, data):
        self.serial += 1
        path = self.evidence / str(self.serial)
        path.write_bytes(data)
        return dict(path=str(path), sha256=digest(path))

    def document(self, value):
        return self.raw(json_bytes(value))

    def command(self, argv, stdout=b''):
        return dict(argv=argv, actual_exit=0, stdout=self.raw(stdout), stderr=self.raw(b''))

    def seal(self):
        self.descriptor['inventory'] = self.document(snapshot_tree(str(self.bundle)))
        self.descriptor_ref = self.document(self.descriptor)
        self.receipt['descriptor_sha256'] = self.descriptor_ref['sha256']
        self.receipt['provider_builds'] = dict(ports=self.document(self.build))
        self.receipt['execution_audit'] = self.document(dict(
            format='emberbsd-native-execution-audit', version=1, scope='native-build', status='PASS',
            descriptor_sha256=self.descriptor_ref['sha256'], host=self.host, tools=self.records,
            undeclared_tools=[], path_searches=[], undeclared_abi_inputs=[]))
        self.accept_receipt()

    def bind_probe_flags(self):
        flags = self.descriptor['abi_flags']
        for role in ('cc', 'cxx'):
            probe = self.receipt['checks'][role]
            language = flags['cflags' if role == 'cc' else 'cxxflags']
            probe['compile']['argv'] = [self.records[role]['path'], *flags['cppflags'], *language,
                                         '-c', probe['source']['path'], '-o', probe['object']['path']]
            probe['link']['argv'] = [self.records[role]['path'], *language, probe['object']['path'],
                                      *flags['ldflags'], '-o', probe['artifact']['path']]

    def accept_receipt(self):
        self.receipt_ref = self.document(self.receipt)

    def load(self, **kwargs):
        options = dict(descriptor_sha256=self.descriptor_ref['sha256'],
                       accepted_producer_receipt=self.receipt_ref,
                       required_roles=NATIVE_TOOL_ROLES)
        options.update(kwargs)
        return load_native_toolset(self.descriptor_ref['path'], **options)


class NativeToolsetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture = Fixture(self.temp.name)
        self.system = patch('emberbsd_sdk.native_toolset.platform.system', return_value='Linux')
        self.machine = patch('emberbsd_sdk.native_toolset.platform.machine', return_value='x86_64')
        self.system.start()
        self.machine.start()
        self.addCleanup(self.system.stop)
        self.addCleanup(self.machine.stop)

    def refuse(self):
        with self.assertRaises(SDKError):
            self.fixture.load()

    def test_provider_log_larger_than_metadata_limit_is_streamed_and_hash_bound(self):
        from emberbsd_sdk.native_toolset import _bytes
        log = self.fixture.evidence / 'full-provider-build.log'
        block = b'complete compiler output\n' * 1024
        with log.open('wb') as stream:
            for _ in range(MAX_JSON // len(block) + 1):
                stream.write(block)
        self.assertGreater(log.stat().st_size, MAX_JSON)
        self.fixture.build['build_log'] = dict(path=str(log), sha256=digest(log))
        self.fixture.seal()
        # Replay the previous bounded-byte checker at the actual provider route.
        with patch('emberbsd_sdk.native_toolset._file', side_effect=_bytes):
            with self.assertRaisesRegex(SDKError, 'evidence exceeds size limit'):
                self.fixture.load()
        self.fixture.load()
        with log.open('ab') as stream:
            stream.write(b'changed log')
        with self.assertRaisesRegex(SDKError, 'referenced input changed'):
            self.fixture.load()

    def test_finite_selection_is_immutable_and_json_is_fresh(self):
        selected = self.fixture.load()
        self.assertEqual(NATIVE_TOOLSET_API, 1)
        self.assertEqual(selected.tool('cc'), self.fixture.records['cc']['path'])
        self.assertEqual(selected.abi_flags['cflags'], ())
        with self.assertRaises(TypeError):
            selected.tools['cc'] = '/usr/bin/cc'
        with self.assertRaises(TypeError):
            selected.abi_flags['cflags'] = ('-O3',)
        with self.assertRaises(AttributeError):
            selected.bundle_root = '/changed'
        with self.assertRaises(SDKError):
            selected.tool('undeclared')
        output = selected.as_dict()
        output['tools']['cc'] = '/changed'
        output['abi_flags']['cflags'].append('-O3')
        self.assertEqual(selected.tool('cc'), self.fixture.records['cc']['path'])
        self.assertEqual(selected.abi_flags['cflags'], ())
        self.assertEqual(output['format'], 'emberbsd-native-toolset-selection')

    def test_descriptor_and_receipt_pins_are_required(self):
        for options in (dict(descriptor_sha256='0' * 64),
                        dict(accepted_producer_receipt=None),
                        dict(accepted_producer_receipt=dict(path=self.fixture.receipt_ref['path'], sha256='0' * 64)),
                        dict(required_roles=[]), dict(required_roles=['not-installed'])):
            with self.subTest(options=options), self.assertRaises(SDKError):
                self.fixture.load(**options)

    def test_system_provider_roots_cannot_be_qualified(self):
        for root in ('/', '/usr', '/USR', '/usr/local', '/usr/bin', '/opt/homebrew', '/opt/local',
                     '/Library', '/library/developer', '/Applications', '/Applications/Xcode-beta.app',
                     '/home/linuxbrew/.linuxbrew'):
            with self.subTest(root=root), self.assertRaises(SDKError): provider_root(root)
        provider_root('/usr/local/emberbsd-sdk/native')
        if os.path.exists('/USR') and os.path.samefile('/USR', '/usr'):
            with self.assertRaises(SDKError): provider_root('/USR', live=True)
        self.fixture.descriptor['bundle_root'] = '/usr'
        self.fixture.seal()
        with self.assertRaisesRegex(SDKError, 'provider root|system'): self.fixture.load()

    def test_lto_wrapper_requires_pinned_native_member_and_query(self):
        original = copy.deepcopy(self.fixture.descriptor['driver_components'])
        del self.fixture.descriptor['driver_components']['lto-wrapper']
        self.fixture.seal()
        with self.assertRaisesRegex(SDKError, 'incomplete GCC driver closure'): self.fixture.load()
        self.fixture.descriptor['driver_components'] = original
        query = self.fixture.receipt['checks']['driver_components']['lto-wrapper']
        original_stdout = query['stdout']
        for value in ('lto-wrapper', '/usr/libexec/lto-wrapper'):
            query['stdout'] = self.fixture.raw((value + '\n').encode())
            self.fixture.seal()
            with self.subTest(value=value), self.assertRaisesRegex(SDKError, 'undeclared or implicit member'):
                self.fixture.load()
        query['stdout'] = original_stdout; self.fixture.seal()
        self.fixture.load()
        Path(self.fixture.records['lto-wrapper']['path']).write_bytes(b'changed wrapper')
        with self.assertRaisesRegex(SDKError, 'inventory changed'): self.fixture.load()

    def test_portable_selection_checks_grammar_without_local_files(self):
        selected = self.fixture.load().as_dict()
        with patch('emberbsd_sdk.native_toolset.canonical_existing', side_effect=AssertionError('filesystem read')):
            validate_selection(selected, required_roles=('python', 'sh'))
            for label in ('abi', 'version', 'path', 'pin'):
                changed = copy.deepcopy(selected)
                if label == 'abi': changed['abi_flags']['cflags'] = ['-B/usr/bin']
                elif label == 'version': changed['compiler']['version'] = '14.3.0'
                elif label == 'path': changed['tools']['cc'] = '/usr/bin/cc'
                else: changed['producer_receipt']['sha256'] = 'not-a-hash'
                with self.subTest(label=label), self.assertRaises(SDKError):
                    validate_selection(changed, required_roles=('python', 'sh'))

    def test_receipt_cannot_accept_another_descriptor(self):
        self.fixture.receipt['descriptor_sha256'] = '0' * 64
        self.fixture.accept_receipt()
        self.refuse()

    def test_bootstrap_and_clang_are_not_native_acceptance(self):
        for field, value in (('scope', 'bootstrap-seed'), ('format', 'bootstrap-seed')):
            with self.subTest(field=field):
                original = self.fixture.descriptor[field]
                self.fixture.descriptor[field] = value
                self.fixture.seal()
                self.refuse()
                self.fixture.descriptor[field] = original
        self.fixture.compiler['family'] = 'clang'
        self.fixture.seal()
        self.refuse()

    def test_gcc_major_and_cross_triplet_refused(self):
        for field, value in (('version', '15.2.0'), ('triplet', 'aarch64--netbsd'),
                             ('triplet', 'x86_64--netbsd'), ('triplet', 'x86_64-pc-linux-extra-netbsd')):
            with self.subTest(value=value):
                original = self.fixture.compiler[field]
                self.fixture.compiler[field] = value
                self.fixture.seal()
                self.refuse()
                self.fixture.compiler[field] = original

    def test_conflicting_os_triplet_fails_even_with_matching_query_evidence(self):
        for triplet in ('x86_64-linux-netbsd', 'x86_64-pc-linux-extra-netbsd',
                        'x86_64-netbsd-linux-gnu', 'x86_64-linux-linux-gnu',
                        'x86_64-pc-linux-gnu-netbsd'):
            with self.subTest(triplet=triplet):
                self.fixture.compiler['triplet'] = triplet
                for role in ('cc', 'cxx'):
                    self.fixture.receipt['checks']['identity'][role]['-dumpmachine']['stdout'] = \
                        self.fixture.raw((triplet + '\n').encode())
                self.fixture.seal()
                with self.assertRaisesRegex(SDKError, 'triplet does not describe the actual native host'):
                    self.fixture.load()

    def test_supported_native_tuple_forms(self):
        for host, triplets in (
                (dict(os='Linux', cpu='x86_64'),
                 ('x86_64-pc-linux-gnu', 'x86_64-linux-gnu', 'x86_64--linux-musl', 'x86_64-linux')),
                (dict(os='Darwin', cpu='arm64'),
                 ('aarch64-apple-darwin25.0.0', 'arm64-apple-darwin25', 'aarch64-darwin')),
                (dict(os='NetBSD', cpu='amd64'),
                 ('x86_64--netbsd', 'x86_64-unknown-netbsd10.1', 'amd64-netbsd'))):
            for triplet in triplets:
                with self.subTest(triplet=triplet):
                    _gcc(dict(family='gcc', version='16.2.0', triplet=triplet), host)

    def test_actual_host_and_source_pins_must_match(self):
        self.fixture.receipt['sources'] = copy.deepcopy(self.fixture.sources)
        self.fixture.receipt['sources']['ports']['commit'] = '3' * 40
        self.fixture.accept_receipt()
        self.refuse()
        self.fixture.receipt['sources'] = self.fixture.sources
        self.fixture.host['cpu'] = 'aarch64'
        self.fixture.seal()
        self.refuse()

    def test_provider_build_cannot_be_a_bootstrap_or_clang_claim(self):
        self.fixture.build['accepted_input_scope'] = 'bootstrap-seed'
        self.fixture.seal()
        self.refuse()
        self.fixture.build['accepted_input_scope'] = 'qualified-native'
        self.fixture.build['compiler'] = dict(family='clang', version='16.2.0', triplet='x86_64-pc-linux-gnu')
        self.fixture.seal()
        self.refuse()

    def test_changed_bundle_or_native_abi_bytes_refused(self):
        path = Path(self.fixture.records['make']['path'])
        path.write_bytes(path.read_bytes() + b'changed')
        self.refuse()
        path.write_bytes(self.fixture.image)
        abi = Path(self.fixture.descriptor['host_abi']['headers'][0]['root']) / 'fixture-input'
        abi.write_bytes(b'changed')
        self.refuse()

    def test_system_tool_and_external_symlink_refused(self):
        external = self.fixture.base / 'external'
        external.write_bytes(self.fixture.image)
        external.chmod(0o755)
        self.fixture.records['cc']['path'] = str(external)
        self.fixture.seal()
        self.refuse()
        self.fixture.records['cc']['path'] = str(self.fixture.bundle / 'cc')
        alias = self.fixture.bundle / 'escape'
        alias.symlink_to(external)
        with self.assertRaises(SDKError):
            self.fixture.seal()

    def test_renamed_compiler_script_and_target_image_refused(self):
        cc = Path(self.fixture.records['cc']['path'])
        for data in (b'#!/bin/sh\nexec /usr/bin/clang "$@"\n', b'\xcf\xfa\xed\xfe' + b'\0' * 60):
            with self.subTest(data=data[:8]):
                cc.write_bytes(data)
                self.fixture.records['cc']['sha256'] = digest(cc)
                self.fixture.seal()
                self.refuse()

    def test_script_interpreter_is_explicit_and_in_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(temporary, bundle_name='bundle')
            meson = Path(fixture.records['meson']['path'])
            python = fixture.records['python']['path']
            meson.write_text('#!' + python + '\n# finite fixture\n')
            fixture.records['meson'].update(kind='script', interpreter='python', sha256=digest(meson))
            fixture.seal()
            fixture.load()
            meson.write_text('#!/usr/bin/env python3\n# implicit interpreter\n')
            fixture.records['meson']['sha256'] = digest(meson)
            fixture.seal()
            with self.assertRaises(SDKError):
                fixture.load()

    def test_script_interpreter_whitespace_path_is_refused(self):
        meson = Path(self.fixture.records['meson']['path'])
        python = self.fixture.records['python']['path']
        self.assertIn(' ', python)
        meson.write_text('#!' + python + '\n# finite fixture\n')
        self.fixture.records['meson'].update(kind='script', interpreter='python', sha256=digest(meson))
        self.fixture.seal()
        with self.assertRaisesRegex(SDKError, 'interpreter path must not contain whitespace'):
            self.fixture.load()

    def test_failed_or_missing_actual_probe_is_refused(self):
        for phase in ('compile', 'link', 'run'):
            command = self.fixture.receipt['checks']['cxx'][phase]
            command['actual_exit'] = 1
            self.fixture.accept_receipt()
            self.refuse()
            command['actual_exit'] = 0
        del self.fixture.receipt['checks']['cc']['run']
        self.fixture.accept_receipt()
        self.refuse()

    def test_identity_and_driver_queries_bind_exact_outputs(self):
        query = self.fixture.receipt['checks']['identity']['cc']['-dumpfullversion']
        query['stdout'] = self.fixture.raw(b'clang 16.2.0\n')
        self.fixture.accept_receipt()
        self.refuse()
        query['stdout'] = self.fixture.raw(b'16.2.0\n')
        driver = self.fixture.receipt['checks']['driver_components']['ld']
        for selected in (b'ld\n', b'/usr/bin/ld\n', self.fixture.records['as']['path'].encode() + b'\n'):
            driver['stdout'] = self.fixture.raw(selected)
            self.fixture.accept_receipt()
            self.refuse()

    def test_execution_audit_cannot_admit_path_or_external_tools(self):
        audit = json.loads(Path(self.fixture.receipt['execution_audit']['path']).read_text())
        for key in ('undeclared_tools', 'path_searches', 'undeclared_abi_inputs'):
            changed = copy.deepcopy(audit)
            changed[key] = ['/usr/bin/cc']
            self.fixture.receipt['execution_audit'] = self.fixture.document(changed)
            self.fixture.accept_receipt()
            self.refuse()

    def test_abi_flags_are_separate_and_cannot_override_tool_selection(self):
        flags = self.fixture.descriptor['abi_flags']['cppflags']
        native = self.fixture.descriptor['host_abi']['headers'][0]['root']
        flags.extend(['-isystem', native, '-DFIXTURE=1'])
        self.fixture.bind_probe_flags()
        self.fixture.seal()
        selected = self.fixture.load()
        self.assertEqual(selected.abi_flags['cppflags'], tuple(flags))
        for flag in ('-B/usr/bin', '-I/usr/include', '@response', '-Wl,--sysroot=relative',
                     '-Wl,-L/usr/lib', '-Wp,-I/usr/include', '-Wa,-I/usr/include'):
            flags[:] = [flag]
            self.fixture.seal()
            self.refuse()

    def test_generic_cli_serializes_one_validated_selection(self):
        path = Path(__file__).resolve().parents[1] / 'scripts' / 'host-toolset.py'
        spec = importlib.util.spec_from_file_location('native_toolset_cli', path)
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        args = ['check', '--descriptor', self.fixture.descriptor_ref['path'],
                '--descriptor-sha256', self.fixture.descriptor_ref['sha256'],
                '--producer-receipt', self.fixture.receipt_ref['path'],
                '--producer-receipt-sha256', self.fixture.receipt_ref['sha256'],
                '--required-role', 'make']
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(cli.main(args), 0)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result, self.fixture.load().as_dict())
        self.assertEqual(stderr.getvalue(), '')
        args[args.index('--descriptor-sha256') + 1] = '0' * 64
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(cli.main(args), 1)
        self.assertEqual(stdout.getvalue(), '')
        self.assertIn('SHA256 mismatch', stderr.getvalue())

    def test_malformed_nested_records_fail_with_sdk_error(self):
        self.fixture.receipt['checks']['identity']['cc']['-dumpmachine'] = []
        self.fixture.accept_receipt()
        self.refuse()

    def test_metadata_only_probe_commands_are_not_compile_link_run(self):
        probe = self.fixture.receipt['checks']['cc']
        probe['compile'] = self.fixture.command([self.fixture.records['cc']['path'], '--version'])
        self.fixture.accept_receipt()
        self.refuse()

    def test_probe_files_and_commands_are_bound(self):
        probe = self.fixture.receipt['checks']['cc']
        original = copy.deepcopy(probe)
        for field, replacement in (
                ('source', self.fixture.raw(b'int main(void) { return 0; }\n')),
                ('object', original['artifact']),
                ('artifact', dict(path=self.fixture.records['cc']['path'],
                                  sha256=self.fixture.records['cc']['sha256']))):
            with self.subTest(field=field):
                probe[field] = replacement
                self.fixture.accept_receipt()
                self.refuse()
                probe[field] = original[field]
        for phase in ('compile', 'link', 'run'):
            with self.subTest(phase=phase):
                command = probe[phase]
                saved = copy.deepcopy(command)
                command['argv'].append('--version')
                self.fixture.accept_receipt()
                self.refuse()
                probe[phase] = saved

    def test_probe_result_output_must_match_fixed_source(self):
        probe = self.fixture.receipt['checks']['cxx']
        probe['run']['stdout'] = self.fixture.raw(b'GCC 16.2.0\n')
        self.fixture.accept_receipt()
        self.refuse()

    def test_selection_does_not_run_tools_or_use_path(self):
        with patch.dict(os.environ, {'PATH': '/undeclared/ambient/path'}), \
                patch('subprocess.run', side_effect=AssertionError('no subprocess allowed')), \
                patch('os.system', side_effect=AssertionError('no shell allowed')):
            self.fixture.load()


if __name__ == '__main__':
    unittest.main()
