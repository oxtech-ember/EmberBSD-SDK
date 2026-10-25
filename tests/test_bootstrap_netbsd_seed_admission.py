# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Narrow provenance checks for the NetBSD first-native seed extension."""

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from emberbsd_sdk.bootstrap_netbsd_seed_admission import (
    validate_retained_base_history, validate_retained_local_history,
)
from emberbsd_sdk.bootstrap_binding import seed_commands, seed_external_tools
from emberbsd_sdk.bootstrap_seed import _history
from emberbsd_sdk.bootstrap_seed import _dynamic_closure, _environment, load_bootstrap_seed
from emberbsd_sdk import bootstrap_seed
from emberbsd_sdk.bootstrap_pilot import pilot_steps
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.native_toolset import _driver_queries
from test_bootstrap_seed import SeedFixture


class RetainedLocalHistoryFixture:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.serial = 0
        self.bundle = self.root / 'owned-inputs'
        self.bundle.mkdir()
        self.base_original_hashes = {}
        self.tools = {}
        for role in ('cc', 'cxx', 'cpp', 'cc1', 'cc1plus', 'collect2',
                     'lto1', 'lto-wrapper', 'as', 'ld'):
            fixture_path = self.bundle / role
            fixture_path.write_bytes(('fixture:' + role).encode())
            fixture_path.chmod(0o755)
            path = Path('/usr/bin') / role if role in ('as', 'ld') else fixture_path
            provider = 'base' if role in ('as', 'ld') else 'gcc'
            self.tools[role] = dict(path=str(path), sha256=self.digest(fixture_path), provider=provider)
        self.prefix_inventory = {role: dict(kind='file', sha256=row['sha256'])
                                 for role, row in self.tools.items()
                                 if role not in ('as', 'ld')}
        self.plugin_relative = ('libexec/gcc/x86_64-unknown-netbsd11.0/16.2.0/'
                                'liblto_plugin.so')
        self.plugin = self.bundle / self.plugin_relative
        self.plugin.parent.mkdir(parents=True)
        self.plugin.write_bytes(b'fixture LTO shared object')
        self.plugin.chmod(0o755)
        self.prefix_inventory[self.plugin_relative] = dict(
            kind='file', sha256=self.digest(self.plugin))
        archive = self.raw('archive', filename='gcc.tar.xz')
        patch = self.raw('patch', filename='patch-netbsd')
        self.producer = dict(archives={'gcc.tar.xz': archive['sha256']},
                             patches={'patch-netbsd': patch['sha256']},
                             bootstrap='existing GCC/G++12.5.0 (native package bootstrap only)')
        self.prefix_manifest = self.raw('prefix manifest')
        self.accepted_record = self.document(dict(
            accepted_at_utc='2026-10-21T16:55:42Z', version='16.2.0', host='NetBSD11.0-amd64',
            triplet='x86_64-unknown-netbsd11.0', manifest_sha256=self.prefix_manifest['sha256'],
            source_plan=self.producer))
        self.audit_report = self.document(dict(status='PASS', scope='bmake-bootstrap'))
        self.prefix_inventory_ref = self.document(dict(
            format='emberbsd-bootstrap-compiler-prefix-inventory', version=1,
            scope='bmake-bootstrap', prefix='/retired/prefix', entries=self.prefix_inventory))
        self.legacy = dict(format='emberbsd-bootstrap-compiler-seed', version=1,
                           scope='bmake-bootstrap', host='NetBSD11.0-amd64',
                           compiler=dict(version='16.2.0', triplet='x86_64-unknown-netbsd11.0'),
                           prefix='/retired/prefix', accepted_record=self.accepted_record,
                           prefix_inventory=self.prefix_inventory_ref,
                           prefix_manifest=self.prefix_manifest,
                           host_runtime={},
                           driver_components={role: dict(
                               path='/usr/bin/' + role, sha256=self.tools[role]['sha256'])
                               for role in ('as', 'ld')})
        self.legacy_descriptor = self.document(self.legacy)
        self.legacy_acceptance = self.document(dict(
            format='emberbsd-bootstrap-bmake-compiler-seed-acceptance', version=1,
            scope='bmake-bootstrap', status='PASS', accepted_record=self.accepted_record,
            audit_report=self.audit_report, prefix_inventory=self.prefix_inventory_ref,
            prefix_manifest=self.prefix_manifest, host='NetBSD11.0-amd64',
            prefix='/retired/prefix', compiler=self.legacy['compiler'],
            host_runtime=self.legacy['host_runtime'],
            driver_components=self.legacy['driver_components'],
            source_provenance=self.producer))
        self.legacy['acceptance_receipt'] = self.legacy_acceptance
        self.legacy_descriptor = self.document(self.legacy)
        self.producer_plan = self.document(self.producer, 'native-gcc16-producer-plan.json')
        self.scripts = [self.raw('producer-' + str(index), filename=name) for index, name in enumerate((
            'native-gcc16-producer.py', 'native-gcc16-resume-makeinfo.py',
            'native-gcc16-resume-target-dialect.py', 'native-gcc16-resume-runtime-flags.py',
            'native-gcc16-resume-native-rpath.py'))]
        fingerprint_lines = [f'SHA256 (/retained/compiler-inputs/{Path(row["path"]).name}) = {row["sha256"]}'
                            for row in self.scripts]
        fingerprint_lines.append(
            f'SHA256 (/retained/compiler-inputs/archives/native-gcc16-producer-plan.json) = '
            f'{self.producer_plan["sha256"]}')
        self.producer_fingerprints = self.raw('\n'.join(fingerprint_lines) + '\n',
                                              filename='legacy-producer-fingerprints.txt')
        self.archives = [dict(name='gcc.tar.xz', file=archive)]
        self.patches = [dict(name='patch-netbsd', file=patch)]
        self.logs = [self.raw(name, filename=name) for name in (
            'gcc-configure.log', 'gcc-build.log', 'gcc-build-resume.log',
            'gcc-build-target-default.log', 'gcc-build-runtime-flags.log',
            'gcc-build-native-rpath.log', 'gcc-install.log', 'accept-c-build.log',
            'accept-c-run.log', 'accept-cxx-build.log', 'accept-cxx-run.log')]
        mappings = []
        for role in ('cc', 'cxx', 'cpp', 'cc1', 'cc1plus', 'collect2',
                     'lto1', 'lto-wrapper'):
            selected = self.tools[role]
            mappings.append(dict(legacy_relative=role, role=role,
                                 selected_path=selected['path'],
                                 sha256=selected['sha256']))
        self.descriptor = dict(
            admission='netbsd11-amd64-first-native-v1',
            bundle_root=str(self.bundle), driver_prefix=str(self.bundle),
            host=dict(os='NetBSD', cpu='amd64'),
            providers={'gcc': dict(origin='retained-local-netbsd-build')}, tools=self.tools,
            abi_flags={key: [] for key in ('cppflags', 'cflags', 'cxxflags', 'ldflags')},
            driver_components={role: role for role in
                ('cc1', 'cc1plus', 'collect2', 'lto1', 'lto-wrapper', 'as', 'ld')})
        self.history = dict(
            kind='retained-local-netbsd-build',
            legacy_seed=dict(descriptor=self.legacy_descriptor,
                             acceptance=self.legacy_acceptance,
                             accepted_record=self.accepted_record,
                             prefix_inventory=self.prefix_inventory_ref),
            producer_plan=self.producer_plan, producer_scripts=self.scripts,
            producer_fingerprints=self.producer_fingerprints,
            source_archives=self.archives, patches=self.patches, build_logs=self.logs,
            historical_compiler='GCC/G++ 12.5.0', member_mappings=mappings,
            shared_object_mappings=[dict(role='lto_plugin',
                legacy_relative=self.plugin_relative, selected_path=str(self.plugin),
                sha256=self.digest(self.plugin))],
            unknowns=['historical-command-environment', 'historical-producer-attestation',
                      'historical-publisher-signature', 'historical-recipe-commit',
                      'historical-source-commit', 'ordinary-provider-qualification'])

    @staticmethod
    def digest(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def raw(self, value, filename=None):
        path = self.root / (filename or ('file-' + str(self.serial)))
        self.serial += 1
        path.write_text(value)
        return dict(path=str(path), sha256=self.digest(path))

    def document(self, value, filename=None):
        path = self.root / (filename or ('doc-' + str(self.serial) + '.json'))
        self.serial += 1
        path.write_text(json.dumps(value, sort_keys=True))
        return dict(path=str(path), sha256=self.digest(path))

    def file(self, reference):
        role = {'/usr/bin/as': 'as', '/usr/bin/ld': 'ld'}.get(reference['path'])
        if role is not None and self.tools[role]['sha256'] == reference['sha256']:
            return
        if self.base_original_hashes.get(reference['path']) == reference['sha256']:
            return
        path = Path(reference['path'])
        if self.digest(path) != reference['sha256']:
            raise SDKError('fixture file changed')


class RetainedLocalHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = RetainedLocalHistoryFixture(self.temporary.name)

    def validate(self):
        return validate_retained_local_history(
            self.fixture.history, 'gcc', self.fixture.descriptor,
            self.fixture.file, inspect_inputs=True)

    def test_accepts_complete_retained_local_history_with_explicit_unknowns(self):
        self.assertEqual(self.validate(), self.fixture.history['unknowns'])

    def test_refuses_omitted_lto_driver_member(self):
        self.fixture.history['member_mappings'] = [
            row for row in self.fixture.history['member_mappings'] if row['role'] != 'lto-wrapper']
        with self.assertRaisesRegex(SDKError, 'member mappings'):
            self.validate()

    def test_refuses_relabelled_legacy_seed_scope(self):
        legacy = json.loads(Path(self.fixture.legacy_descriptor['path']).read_text())
        legacy['scope'] = 'gcc16-bootstrap'
        self.fixture.legacy_descriptor = self.fixture.document(legacy)
        self.fixture.history['legacy_seed']['descriptor'] = self.fixture.legacy_descriptor
        with self.assertRaisesRegex(SDKError, 'legacy compiler seed scope'):
            self.validate()

    def test_refuses_empty_historical_unknowns(self):
        self.fixture.history['unknowns'] = []
        with self.assertRaisesRegex(SDKError, 'historical unknowns'):
            self.validate()

    def test_seed_history_dispatches_retained_local_provider_only_in_netbsd_scope(self):
        providers = {'gcc': dict(origin='retained-local-netbsd-build')}
        histories = {'gcc': self.fixture.history}
        self.assertEqual(_history(histories, providers, descriptor=self.fixture.descriptor),
                         {'gcc': self.fixture.history['unknowns']})

    def test_retained_local_provider_is_refused_outside_netbsd_plan_scope(self):
        self.fixture.descriptor['admission'] = 'ordinary-native'
        with self.assertRaisesRegex(SDKError, 'limited to the NetBSD first-native seed'):
            _history({'gcc': self.fixture.history},
                     {'gcc': dict(origin='retained-local-netbsd-build')},
                     descriptor=self.fixture.descriptor)

    def test_ordinary_source_build_history_does_not_gain_netbsd_unknowns(self):
        with self.assertRaisesRegex(SDKError, 'seed provider history kind mismatch'):
            _history({'gcc': self.fixture.history},
                     {'gcc': dict(origin='source-build')},
                     descriptor=self.fixture.descriptor)


class RetainedBaseHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = RetainedLocalHistoryFixture(self.temporary.name)
        f = self.fixture
        self.history = dict(
            kind='retained-netbsd-base-tools',
            legacy_seed=f.history['legacy_seed'],
            release_identity='NetBSD 11.0 amd64',
            tool_members=[dict(role=role, original_path='/usr/bin/' + role,
                               selected_path=f.tools[role]['path'],
                               sha256=f.tools[role]['sha256']) for role in ('as', 'ld')],
            unknowns=['base-build-record', 'base-source-commit', 'base-source-signature'])
        f.descriptor['providers']['base'] = dict(origin='retained-netbsd-base-tools')
        f.descriptor['sources'] = {'ports': dict(
            repository='https://github.com/oxtech-ember/EmberBSD-Ports', commit='a' * 40)}
        f.descriptor['history'] = {'gcc': f.history, 'base': self.history}
        template = f.raw('*link_libgcc:\n%D\n',
                         filename='netbsd11-configured-base-seed.specs.in')
        specs_path = f.bundle / 'netbsd11-configured-base-seed.specs'
        specs_path.write_bytes(Path(template['path']).read_bytes())
        instantiated = dict(path=str(specs_path), sha256=f.digest(specs_path))
        self.route_evidence = f.raw(
            'Configured with: configure --with-as=/usr/bin/as --with-ld=/usr/bin/ld\n'
            '/usr/bin/as -o object.o input.s\n'
            'collect2 -plugin ' + str(f.plugin) + ' -static-libstdc++ -static-libgcc '
            '-specs=' + str(specs_path) + '\n', filename='configured-driver-route.txt')
        f.descriptor['driver_binding'] = dict(
            kind='netbsd11-configured-base-as-ld-seed-v1',
            ports_source=f.descriptor['sources']['ports'],
            reported_programs={'as': '/usr/bin/as', 'ld': '/usr/bin/ld'},
            configured_route_evidence=self.route_evidence,
            template_relative='profiles/development-toolchain/bootstrap/netbsd11-configured-base-seed.specs.in',
            template=template, instantiated=instantiated,
            lto_plugin=dict(path=str(f.plugin), sha256=f.digest(f.plugin)),
            lto_plugin_stale_rpath=['/usr/pkg/gcc12/x86_64--netbsd/lib/.',
                                    '/usr/pkg/gcc12/lib/.'],
            lto_plugin_elf_metadata=f.raw(
                ' 0x0000000000000001 (NEEDED) Shared library: [libc.so.12]\n'
                ' 0x000000000000000f (RPATH) Library rpath: '
                '[/usr/pkg/gcc12/x86_64--netbsd/lib/.:/usr/pkg/gcc12/lib/.]\n',
                filename='liblto_plugin-readelf-d.txt'))

    def validate(self):
        return validate_retained_base_history(
            self.history, 'base', self.fixture.descriptor,
            self.fixture.file, inspect_inputs=True)

    def test_accepts_finite_base_tool_mapping_with_disclosed_source_unknowns(self):
        self.assertEqual(self.validate(), self.history['unknowns'])

    def test_refuses_a_copied_base_tool_with_different_bytes(self):
        self.history['tool_members'][0]['sha256'] = 'f' * 64
        with self.assertRaisesRegex(SDKError, 'differs from its selected tool record'):
            self.validate()

    def test_refuses_a_base_role_from_an_unrecorded_system_path(self):
        self.history['tool_members'][0]['original_path'] = '/tmp/unpinned-as'
        with self.assertRaisesRegex(SDKError, 'exact release-tool route'):
            self.validate()

    def test_accepts_only_finite_netbsd_gzip_and_xz_alias_paths(self):
        f = self.fixture
        aliases = ('gzip', 'gunzip', 'gzcat', 'xz', 'xzcat')
        for role in aliases:
            selected = f.bundle / role
            selected.write_bytes(b'NetBSD source archive decompressor fixture\n')
            selected.chmod(0o755)
            digest = f.digest(selected)
            original = '/usr/bin/' + role
            f.base_original_hashes[original] = digest
            f.tools[role] = dict(path=str(selected), sha256=digest, provider='base')
            self.history['tool_members'].append(dict(
                role=role, original_path=original, selected_path=str(selected), sha256=digest))

        self.assertEqual(self.validate(), self.history['unknowns'])

    def test_compression_alias_copy_preserves_argv0_basename(self):
        f = self.fixture
        selected = f.bundle / 'gzip-selected'
        selected.write_bytes(b'gzip alias fixture\n')
        selected.chmod(0o755)
        digest = f.digest(selected)
        f.base_original_hashes['/usr/bin/gunzip'] = digest
        f.tools['gunzip'] = dict(path=str(selected), sha256=digest, provider='base')
        self.history['tool_members'].append(dict(
            role='gunzip', original_path='/usr/bin/gunzip', selected_path=str(selected),
            sha256=digest))
        with self.assertRaisesRegex(SDKError, 'argv0 basename'):
            self.validate()

    def test_external_driver_allowlist_requires_both_fixed_base_members(self):
        f = self.fixture
        self.assertEqual(seed_external_tools(f.descriptor),
                         {'as': '/usr/bin/as', 'ld': '/usr/bin/ld'})
        f.descriptor['tools']['ld']['path'] = '/tmp/ld'
        with self.assertRaisesRegex(SDKError, 'exact base executable'):
            seed_external_tools(f.descriptor)

    def test_external_driver_binding_checks_pinned_configured_route_evidence(self):
        f = self.fixture
        f.descriptor['driver_binding']['configured_route_evidence']['sha256'] = 'f' * 64
        with self.assertRaisesRegex(SDKError, 'SHA256 mismatch'):
            seed_external_tools(f.descriptor)

    def test_external_driver_route_must_use_the_selected_specs_path(self):
        f = self.fixture
        path = f.descriptor['driver_binding']['instantiated']['path']
        route = Path(self.route_evidence['path']).read_text().replace(path, '/tmp/arbitrary.specs')
        self.route_evidence = f.raw(route, filename='wrong-configured-driver-route.txt')
        f.descriptor['driver_binding']['configured_route_evidence'] = self.route_evidence
        with self.assertRaisesRegex(SDKError, 'isolated runtime route'):
            seed_external_tools(f.descriptor)

    def test_external_driver_binding_requires_exact_lto_plugin_and_retained_history(self):
        f = self.fixture
        f.descriptor['driver_binding']['lto_plugin']['sha256'] = 'f' * 64
        with self.assertRaisesRegex(SDKError, 'not bound to its retained prefix inventory'):
            seed_external_tools(f.descriptor)
        f.descriptor['driver_binding']['lto_plugin']['sha256'] = f.digest(f.plugin)
        f.history['shared_object_mappings'][0]['sha256'] = 'e' * 64
        with self.assertRaisesRegex(SDKError, 'LTO plugin differs from its old prefix inventory'):
            validate_retained_local_history(f.history, 'gcc', f.descriptor, f.file)

    def test_external_driver_binding_rejects_unrecorded_plugin_dso_closure(self):
        f = self.fixture
        metadata = Path(f.descriptor['driver_binding']['lto_plugin_elf_metadata']['path'])
        evidence = metadata.read_text().replace('Shared library: [libc.so.12]',
                                                'Shared library: [libc.so.99]')
        f.descriptor['driver_binding']['lto_plugin_elf_metadata'] = f.raw(
            evidence, filename='wrong-lto-plugin-runtime.txt')
        with self.assertRaisesRegex(SDKError, 'ELF metadata differs'):
            seed_external_tools(f.descriptor)

    def test_readonly_route_normalizes_gcc_bin_dotdot_plugin_spelling(self):
        f = self.fixture
        (f.bundle / 'bin').mkdir()
        route = Path(self.route_evidence['path']).read_text()
        route = route.replace(str(f.plugin), str(f.bundle / 'bin' / '..' / f.plugin_relative))
        evidence = f.raw(route, filename='dotdot-plugin-route.txt')
        f.descriptor['driver_binding']['configured_route_evidence'] = evidence
        self.assertEqual(seed_external_tools(f.descriptor), {'as': '/usr/bin/as', 'ld': '/usr/bin/ld'})

    def test_netbsd_driver_commands_bind_fixed_specs_and_static_runtime_policy(self):
        f = self.fixture
        f.descriptor['driver_prefix'] = str(f.bundle)
        template = f.raw('*link_libgcc:\n%D\n', filename='netbsd11-configured-base-seed.specs.in')
        instantiated_path = f.bundle / 'netbsd11-configured-base-seed.specs'
        instantiated_path.write_bytes(Path(template['path']).read_bytes())
        instantiated = dict(path=str(instantiated_path), sha256=f.digest(instantiated_path))
        f.route_evidence = f.raw(
            'Configured with: configure --with-as=/usr/bin/as --with-ld=/usr/bin/ld\n'
            '/usr/bin/as -o object.o input.s\n'
            'collect2 -plugin ' + str(f.plugin) + ' -static-libstdc++ -static-libgcc '
            '-specs=' + str(instantiated_path) + '\n', filename='netbsd-static-route.txt')
        binding = f.descriptor['driver_binding']
        binding.update(template_relative=
                       'profiles/development-toolchain/bootstrap/netbsd11-configured-base-seed.specs.in',
                       template=template, instantiated=instantiated,
                       configured_route_evidence=f.route_evidence)

        commands = seed_commands(f.descriptor)

        self.assertEqual(commands['cc'][-2:], ('-static-libstdc++', '-static-libgcc'))
        self.assertEqual(commands['cxx'][-2:], ('-static-libstdc++', '-static-libgcc'))
        self.assertEqual(commands['cpp'][-1], '-specs=' + str(instantiated_path))
        self.assertIn('-specs=' + str(instantiated_path), commands['cc'])

    def test_netbsd_fixed_pilot_path_excludes_external_base_driver_directory(self):
        f = self.fixture
        commands = seed_commands(f.descriptor)
        steps = pilot_steps(f.descriptor, commands, f.descriptor['abi_flags'],
                            str(f.bundle / 'pilot-work'), static_runtime=True)
        path_entries = steps[0]['env']['PATH'].split(os.pathsep)
        self.assertNotIn('/usr/bin', path_entries)
        self.assertTrue(any(entry.startswith(str(f.bundle)) for entry in path_entries))
        shell = f.bundle / 'sh'
        tools = {role: row['path'] for role, row in f.tools.items()}
        tools['sh'] = str(shell)
        environment_commands = dict(commands, sh=(str(shell),))
        environment = _environment(tools, environment_commands, f.descriptor['abi_flags'],
                                   f.descriptor)
        self.assertNotIn('/usr/bin', environment['PATH'].split(os.pathsep))

    def test_metadata_commands_do_not_reopen_retired_seed_runtime_inputs(self):
        f = self.fixture
        commands = seed_commands(f.descriptor)
        binding = f.descriptor['driver_binding']
        for key in ('template', 'instantiated', 'configured_route_evidence',
                    'lto_plugin_elf_metadata', 'lto_plugin'):
            Path(binding[key]['path']).unlink()
        steps = pilot_steps(f.descriptor, commands, f.descriptor['abi_flags'],
                            str(f.bundle / 'retired-pilot'), static_runtime=True)
        self.assertNotIn('/usr/bin', steps[0]['env']['PATH'].split(os.pathsep))
        shell = str(f.bundle / 'retired-shell')
        tools = {role: row['path'] for role, row in f.tools.items()}
        environment = _environment(dict(tools, sh=shell), dict(commands, sh=(shell,)),
                                   f.descriptor['abi_flags'], f.descriptor)
        self.assertNotIn('/usr/bin', environment['PATH'].split(os.pathsep))
        with self.assertRaises((SDKError, OSError)):
            seed_commands(f.descriptor)

    def test_dynamic_closure_requires_runtime_lto_plugin_edge_from_configured_ld(self):
        f = self.fixture
        plugin = f.bundle / 'liblto_plugin.so'
        plugin.write_bytes(b'plugin shared object fixture')
        plugin.chmod(0o755)
        ld = '/usr/bin/ld'
        ld_sha = f.digest(f.bundle / 'ld')
        graph = {
            str(plugin): {'sha256': f.digest(plugin), 'dependencies': []},
            ld: {'sha256': ld_sha,
                 'dependencies': [],
                 'runtime_loads': [dict(path=str(plugin),
                     basis='gcc-driver--###-plugin-argument')]},
        }
        descriptor = dict(
            bundle_root=str(f.bundle), host={'os': 'NetBSD', 'cpu': 'amd64'},
            host_abi={'libraries': []}, tools={'ld': dict(path=ld, kind='native',
                sha256=ld_sha)},
            driver_binding={'kind': 'netbsd11-configured-base-as-ld-seed-v1',
                            'lto_plugin': dict(path=str(plugin), sha256=f.digest(plugin))})
        missing_edge = {key: dict(row) for key, row in graph.items()}
        missing_edge[ld]['runtime_loads'] = []
        with patch('emberbsd_sdk.bootstrap_seed._native_image'), \
             patch('emberbsd_sdk.bootstrap_seed.canonical_existing', side_effect=lambda path: str(path)), \
             patch('emberbsd_sdk.bootstrap_seed.sha256_file',
                   side_effect=lambda path: ld_sha if str(path) == ld else f.digest(path)):
            with self.assertRaisesRegex(SDKError, 'runtime LTO plugin edge'):
                _dynamic_closure(missing_edge, descriptor,
                                 allowed_external_tools={'ld': ld})
            _dynamic_closure(graph, descriptor, allowed_external_tools={'ld': ld})

    def test_refuses_system_shell_as_a_base_tool_fallback(self):
        f = self.fixture
        path = f.bundle / 'sh'
        path.write_text('unqualified shell')
        path.chmod(0o755)
        role = dict(path=str(path), sha256=f.digest(path), provider='base')
        f.descriptor['tools']['sh'] = role
        self.history['tool_members'].append(dict(role='sh', original_path='/bin/sh',
            selected_path=role['path'], sha256=role['sha256']))
        with self.assertRaisesRegex(SDKError, 'disclose source unknowns'):
            self.validate()


class RetainedR13DigestHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = RetainedLocalHistoryFixture(self.temporary.name)
        f = self.fixture
        prefix = f.root / 'r13-prefix'
        (prefix / 'bin').mkdir(parents=True)
        digest_path = prefix / 'bin' / 'digest'
        digest_path.write_bytes(b'r13 source-built digest executable\n')
        digest_path.chmod(0o755)
        selected_path = f.bundle / 'digest'
        selected_path.write_bytes(digest_path.read_bytes())
        selected_path.chmod(0o755)
        self.digest_tool = dict(path=str(selected_path), sha256=f.digest(digest_path), provider='digest')
        self.plan = f.document(dict(
            format='emberbsd-bootstrap-pkgsrc-provider-plan', version=1,
            scope='pkgsrc-bootstrap-tools', host='NetBSD11.0-amd64',
            purpose='bootstrap-current-pkgsrc-tools', prefix_root=str(prefix), output_root=str(prefix),
            export_acceptance=f.raw('export acceptance'),
            transfer_acceptance=f.raw('transfer acceptance'),
            compiler_seed=f.raw('compiler seed'), sdk_source=f.raw('sdk source'),
            commands=[dict(id='install-digest', phase='package', cwd=str(prefix),
                           argv=['/r13/bin/gmake', '-C', str(prefix), 'install'],
                           env={'PATH': '/r13/bin'}, expected_exit=0)]))
        self.output_inventory = f.document({'bin/digest': dict(
            kind='file', mode=0o755, size=digest_path.stat().st_size,
            sha256=self.digest_tool['sha256'])}, 'r13-output-inventory.json')
        log = f.raw('digest package command completed\n')
        command_log = f.raw('digest package command completed\n')
        self.result = f.document(dict(
            format='emberbsd-bootstrap-pkgsrc-provider-result', version=1,
            scope='pkgsrc-bootstrap-tools', status='COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW',
            bootstrap_plan=self.plan, output_root=str(prefix), output_inventory=self.output_inventory,
            export_acceptance=json.loads(Path(self.plan['path']).read_text())['export_acceptance'],
            transfer_acceptance=json.loads(Path(self.plan['path']).read_text())['transfer_acceptance'],
            compiler_seed=json.loads(Path(self.plan['path']).read_text())['compiler_seed'],
            commands=[dict(id='install-digest', phase='package', cwd=str(prefix),
                           argv=['/r13/bin/gmake', '-C', str(prefix), 'install'],
                           env={'PATH': '/r13/bin'}, expected_exit=0,
                           actual_exit=0, log=command_log)],
            all_commands_drained=True, inputs_revalidated_after_drain=True,
            all_leases_closed=True, ordinary_provider_qualified=False))
        unknowns = ['r13-result-not-an-ordinary-provider', 'source-tree-not-reconstructed']
        self.bootstrap_tools_acceptance = f.raw('existing three-role pkgsrc-tools acceptance')
        self.bootstrap_tools_plan = self.plan
        self.bootstrap_tools_result = self.result
        self.bootstrap_tools_inventory = self.output_inventory
        fixture_path = f.root / 'digest-golden.txt'
        fixture_path.write_bytes(b'emberbsd digest golden\0bytes\n')
        self.semantic_report = f.document(dict(
            format='emberbsd-netbsd-r13-digest-semantic-report', version=1,
            tool=dict(path=str(digest_path), sha256=self.digest_tool['sha256']),
            fixture=dict(path=str(fixture_path), sha256=f.digest(fixture_path)),
            outputs={'sha256': '39f4f0f48162c272a826d628eb22d134a91061e3d75c3736ef691c1ad0447d79',
                     'sha512': '119f333142809b7cf3e86c2d7b36e020157053b91d651db574397694c48ea510ed07a10773de4e8549307f031d80094d6fcd1002b33df21f932f64b94aff16b8',
                     'blake2s': '68042c7a00313f7cbd544f9fed8673a9cd8d1c5fdf71ff2d89f3b9b9d08fd2c2'}))
        self.history = dict(
            kind='retained-r13-pkgsrc-bootstrap-artifact',
            bootstrap_tools_acceptance=self.bootstrap_tools_acceptance,
            provider_acceptance=f.document(dict(
                format='emberbsd-bootstrap-pkgsrc-provider-acceptance', version=1,
                scope='pkgsrc-bootstrap-tools', status='PASS', plan=self.plan,
                result=self.result, output_inventory=self.output_inventory,
                bootstrap_tools_acceptance=self.bootstrap_tools_acceptance,
                semantic_report=self.semantic_report,
                export_acceptance=json.loads(Path(self.plan['path']).read_text())['export_acceptance'],
                transfer_acceptance=json.loads(Path(self.plan['path']).read_text())['transfer_acceptance'],
                compiler_seed=json.loads(Path(self.plan['path']).read_text())['compiler_seed'],
                accepted_roles=['digest'], accepted_history_unknowns=unknowns,
                checks=dict(all_commands_drained=True, inputs_revalidated_after_drain=True,
                            all_leases_closed=True, source_export_matches=True,
                            output_inventory_matches=True))),
            provider_plan=self.plan, provider_result=self.result,
            output_inventory=self.output_inventory,
            semantic_report=self.semantic_report,
            member=dict(original_path=str(digest_path), selected_path=str(selected_path),
                        sha256=self.digest_tool['sha256']),
            unknowns=unknowns)
        self.base_tools = SimpleNamespace(
            plan=self.bootstrap_tools_plan, result=self.bootstrap_tools_result,
            output_inventory=self.bootstrap_tools_inventory)
        f.descriptor['tools']['digest'] = self.digest_tool
        f.descriptor['providers']['digest'] = dict(origin='retained-r13-pkgsrc-bootstrap-artifact')
        f.descriptor['history'] = {}
        f.descriptor['history']['digest'] = self.history

    def validate(self):
        with patch('emberbsd_sdk.bootstrap_netbsd_gcc16.load_pkgsrc_bootstrap_tools',
                   return_value=self.base_tools):
            return _history({'digest': self.history},
                            {'digest': dict(origin='retained-r13-pkgsrc-bootstrap-artifact')},
                            descriptor=self.fixture.descriptor)['digest']

    def test_accepts_r13_digest_only_with_fresh_exact_artifact_receipt(self):
        f = self.fixture
        self.assertEqual(self.validate(), self.history['unknowns'])

    def test_refuses_new_record_refs_with_the_same_source_pins(self):
        self.base_tools.plan = self.fixture.document(
            json.loads(Path(self.plan['path']).read_text()))
        with self.assertRaisesRegex(SDKError, 'original.*records'):
            self.validate()

    def test_refuses_r13_digest_when_receipt_omits_digest_role(self):
        accepted = json.loads(Path(self.history['provider_acceptance']['path']).read_text())
        accepted['accepted_roles'] = ['gmake', 'make', 'sh']
        self.history['provider_acceptance'] = self.fixture.document(accepted)
        self.fixture.descriptor['history']['digest'] = self.history
        with self.assertRaisesRegex(SDKError, 'digest-only'):
            self.validate()

    def test_refuses_symlink_in_place_of_r13_digest_output(self):
        path = Path(self.history['member']['selected_path'])
        payload = path.read_bytes()
        path.unlink()
        external = self.fixture.bundle / 'external-digest'
        external.write_bytes(payload)
        external.chmod(0o755)
        path.symlink_to(external)
        with self.assertRaisesRegex(SDKError, 'regular file'):
            self.validate()

    def test_refuses_actual_digest_command_that_differs_from_exact_plan(self):
        result = json.loads(Path(self.history['provider_result']['path']).read_text())
        result['commands'][0]['argv'][0] = '/usr/bin/gmake'
        self.history['provider_result'] = self.fixture.document(result)
        accepted = json.loads(Path(self.history['provider_acceptance']['path']).read_text())
        accepted['result'] = self.history['provider_result']
        self.history['provider_acceptance'] = self.fixture.document(accepted)
        self.fixture.descriptor['history']['digest'] = self.history
        with self.assertRaisesRegex(SDKError, 'original.*records'):
            self.validate()

    def test_refuses_digest_plan_with_different_accepted_source_inputs(self):
        plan = json.loads(Path(self.history['provider_plan']['path']).read_text())
        plan['transfer_acceptance'] = self.fixture.raw('another transfer receipt')
        self.history['provider_plan'] = self.fixture.document(plan)
        self.fixture.descriptor['history']['digest'] = self.history
        with self.assertRaisesRegex(SDKError, 'original.*records'):
            self.validate()

    def test_refuses_digest_semantic_report_that_misses_a_golden(self):
        report = json.loads(Path(self.history['semantic_report']['path']).read_text())
        report['outputs']['blake2s'] = '0' * 64
        self.history['semantic_report'] = self.fixture.document(report)
        accepted = json.loads(Path(self.history['provider_acceptance']['path']).read_text())
        accepted['semantic_report'] = self.history['semantic_report']
        self.history['provider_acceptance'] = self.fixture.document(accepted)
        self.fixture.descriptor['history']['digest'] = self.history
        with self.assertRaisesRegex(SDKError, 'semantic report does not match pinned digest goldens'):
            self.validate()

class NetBSDAdmissionBoundaryTests(unittest.TestCase):
    def test_configured_netbsd_driver_query_accepts_only_an_in_bundle_path_alias(self):
        with tempfile.TemporaryDirectory() as temporary:
            seed = SeedFixture(temporary)
            descriptor = seed.descriptor
            descriptor['host'] = {'os': 'NetBSD', 'cpu': 'amd64'}
            descriptor['admission'] = 'netbsd11-amd64-first-native-v1'
            descriptor['driver_binding'] = {
                'kind': 'netbsd11-configured-base-as-ld-seed-v1'}
            # The host GCC reports this lexical shape for a member under libexec.
            prefix = Path(descriptor['driver_prefix'])
            alias = str(prefix / 'bin' / '..' / 'cc1')
            (prefix / 'bin').mkdir()
            checks = json.loads(json.dumps(seed.acceptance['checks']))
            checks['driver_components']['cc1']['stdout'] = seed.native.raw(
                (alias + '\n').encode())
            selected = {role: row['path'] for role, row in descriptor['tools'].items()}

            _driver_queries(descriptor, checks, selected, seed.commands)

            checks['driver_components']['cc1']['stdout'] = seed.native.raw(
                b'/tmp/unlisted-cc1\n')
            with self.assertRaisesRegex(SDKError, 'undeclared or implicit member'):
                _driver_queries(descriptor, checks, selected, seed.commands)

    def test_configured_netbsd_driver_alias_cannot_follow_a_symlink_out_of_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            seed = SeedFixture(temporary)
            descriptor = seed.descriptor
            descriptor['host'] = {'os': 'NetBSD', 'cpu': 'amd64'}
            descriptor['admission'] = 'netbsd11-amd64-first-native-v1'
            descriptor['driver_binding'] = {
                'kind': 'netbsd11-configured-base-as-ld-seed-v1'}
            prefix = Path(descriptor['driver_prefix'])
            outside = Path(temporary) / 'outside'
            (outside / 'childdir').mkdir(parents=True)
            (outside / 'cc1').write_bytes(b'outside driver')
            (prefix / 'bin').symlink_to(outside / 'childdir', target_is_directory=True)
            alias = str(prefix / 'bin' / '..' / 'cc1')
            checks = json.loads(json.dumps(seed.acceptance['checks']))
            checks['driver_components']['cc1']['stdout'] = seed.native.raw(
                (alias + '\n').encode())
            selected = {role: row['path'] for role, row in descriptor['tools'].items()}

            with self.assertRaisesRegex(SDKError, 'undeclared or implicit member'):
                _driver_queries(descriptor, checks, selected, seed.commands)

    def test_driver_alias_exception_does_not_apply_to_ordinary_or_external_roles(self):
        for role, binding in (
                ('cc1', None),
                ('as', {'kind': 'netbsd11-configured-base-as-ld-seed-v1'}),
                ('ld', {'kind': 'netbsd11-configured-base-as-ld-seed-v1'})):
            with self.subTest(role=role):
                with tempfile.TemporaryDirectory() as temporary:
                    seed = SeedFixture(temporary)
                    descriptor = seed.descriptor
                    if binding is not None:
                        descriptor['host'] = {'os': 'NetBSD', 'cpu': 'amd64'}
                        descriptor['admission'] = 'netbsd11-amd64-first-native-v1'
                        descriptor['driver_binding'] = binding
                    if role in ('as', 'ld'):
                        descriptor['tools'][role]['path'] = '/usr/bin/' + role
                        alias = '/usr/bin/../bin/' + role
                    else:
                        prefix = Path(descriptor['driver_prefix'])
                        (prefix / 'bin').mkdir()
                        alias = str(prefix / 'bin' / '..' / role)
                    checks = json.loads(json.dumps(seed.acceptance['checks']))
                    checks['driver_components'][role]['stdout'] = seed.native.raw(
                        (alias + '\n').encode())
                    selected = {name: row['path'] for name, row in descriptor['tools'].items()}

                    with self.assertRaisesRegex(SDKError, 'undeclared or implicit member'):
                        _driver_queries(descriptor, checks, selected, seed.commands)

    def test_configured_base_drivers_are_the_only_prefix_exceptions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            prefix = root / 'gcc-prefix'
            prefix.mkdir()
            components = {name: name for name in
                          ('cc1', 'cc1plus', 'collect2', 'lto1', 'lto-wrapper', 'as', 'ld')}
            tools = {name: str(prefix / name) for name in components}
            tools['as'] = '/usr/bin/as'
            tools['ld'] = '/usr/bin/ld'
            descriptor = dict(bundle_root=str(root), driver_prefix=str(prefix),
                              driver_components=components)
            external = {'as': '/usr/bin/as', 'ld': '/usr/bin/ld'}
            bootstrap_seed._validate_driver_prefix(descriptor, tools, external)
            with self.assertRaisesRegex(SDKError, 'explicit prefix'):
                bootstrap_seed._validate_driver_prefix(descriptor, tools, {})
            tools['cc1'] = '/usr/bin/cc1'
            with self.assertRaisesRegex(SDKError, 'explicit prefix'):
                bootstrap_seed._validate_driver_prefix(descriptor, tools, external)

    def test_retained_history_origin_cannot_enter_an_ordinary_seed_plan(self):
        with tempfile.TemporaryDirectory() as temporary:
            seed = SeedFixture(temporary)
            seed.descriptor['providers']['ports']['origin'] = 'retained-local-netbsd-build'
            seed.descriptor['admission'] = 'netbsd11-amd64-first-native-v1'
            descriptor_ref = seed.native.document(seed.descriptor)
            seed.plan['seed_descriptor'] = descriptor_ref
            plan_ref = seed.native.document(seed.plan)
            acceptance = dict(seed.acceptance)
            acceptance['descriptor_sha256'] = descriptor_ref['sha256']
            acceptance['bootstrap_plan'] = plan_ref
            acceptance_ref = seed.native.document(acceptance)
            with patch('emberbsd_sdk.native_toolset.platform.system', return_value='Linux'), \
                 patch('emberbsd_sdk.native_toolset.platform.machine', return_value='x86_64'):
                with self.assertRaisesRegex(SDKError, 'limited to the NetBSD first-native plan'):
                    load_bootstrap_seed(descriptor_ref['path'],
                        descriptor_sha256=descriptor_ref['sha256'],
                        accepted_seed_receipt=acceptance_ref,
                        bootstrap_plan=plan_ref,
                        required_roles=('cc', 'cxx', 'make', 'sh'))
