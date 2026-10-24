# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Scoped first-bmake compiler admission controls."""

from pathlib import Path
import hashlib
import io
import json
import sys
import subprocess
import tarfile
import tempfile
import unittest

SDK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SDK_ROOT / 'src'))


def compiler_seed():
    prefix = '/tmp/accepted-gcc16/prefix'
    triplet = 'x86_64-unknown-netbsd11.0'
    version = '16.2.0'
    driver = Path(prefix) / 'libexec/gcc' / triplet / version
    library = Path(prefix) / 'lib/gcc' / triplet / version / 'libgcc.a'
    return {
        'format': 'emberbsd-bootstrap-compiler-seed',
        'version': 1,
        'scope': 'bmake-bootstrap',
        'host': 'NetBSD11.0-amd64',
        'prefix': prefix,
        'accepted_record': {'path': '/tmp/accepted-gcc16/accepted.json', 'sha256': 'a' * 64},
        'prefix_manifest': {'path': '/tmp/accepted-gcc16/prefix-sha256.json', 'sha256': 'b' * 64},
        'prefix_inventory': {'path': '/tmp/accepted-gcc16/prefix-inventory.json', 'sha256': '8' * 64},
        'acceptance_receipt': {'path': '/tmp/accepted-gcc16/acceptance.json', 'sha256': '7' * 64},
        'compiler': {
            'path': prefix + '/bin/gcc',
            'sha256': 'c' * 64,
            'version': version,
            'triplet': triplet,
        },
        'driver_components': {
            'cc1': {'path': str(driver / 'cc1'), 'sha256': 'd' * 64},
            'cc1plus': {'path': str(driver / 'cc1plus'), 'sha256': 'e' * 64},
            'collect2': {'path': str(driver / 'collect2'), 'sha256': 'f' * 64},
            'libgcc': {'path': str(library), 'sha256': '1' * 64},
            'as': {'path': '/usr/bin/as', 'sha256': '2' * 64},
            'ld': {'path': '/usr/bin/ld', 'sha256': '3' * 64},
        },
        'host_runtime': {
            'ldd': {'path': '/usr/bin/ldd', 'sha256': '4' * 64},
            'libraries': {'/usr/lib/libc.so.12': '5' * 64},
        },
    }


def source_descriptor():
    return {
        'format': 'emberbsd-bootstrap-bmake-source',
        'version': 1,
        'scope': 'bmake-bootstrap',
        'ports_source': {
            'repository': 'https://github.com/oxtech-ember/EmberBSD-Ports',
            'asserted_revision': '0' * 40,
        },
        'profile': {'path': '/tmp/ports/bmake.md', 'sha256': '4' * 64},
        'sources': {'path': '/tmp/ports/sources.tsv', 'sha256': '5' * 64},
        'patch': {'path': '/tmp/ports/pkgsrc-bmake-20260912.patch', 'sha256': '6' * 64},
        'archive': {
            'path': '/tmp/bmake-20260912.tar.gz',
            'sha256': 'b6bd32964cbe451be2838822c9d200b7c7e76a2a5947c03feb71dc6bd72988bd',
            'url': 'https://www.crufty.net/ftp/pub/sjg/bmake-20260912.tar.gz',
            'version': '20260912',
        },
        'root': '/tmp/bmake-source',
    }


def _write_json_ref(path, value):
    encoded = json.dumps(value, sort_keys=True).encode('utf-8')
    path = Path(path)
    path.write_bytes(encoded)
    return {'path': str(path), 'sha256': hashlib.sha256(encoded).hexdigest()}


def producer_result_fixture(directory, *, source=None):
    from emberbsd_sdk.bootstrap_bmake_seed import _HOST_TOOLS, create_plan

    root = Path(directory)
    seed, source = compiler_seed(), source or source_descriptor()
    seed_ref = _write_json_ref(root / 'seed.json', seed)
    source_ref = _write_json_ref(root / 'source.json', source)
    host_tools = {'sh': {'path': '/bin/sh', 'sha256': '8' * 64}}
    host_tools.update({name: {'path': '/usr/bin/' + name, 'sha256': '9' * 64}
                       for name in _HOST_TOOLS})
    producer_plan = create_plan(seed_ref, source_ref, str(root / 'producer-work'),
                                str(root / 'producer-locks'), host_tools)
    producer_plan_ref = _write_json_ref(root / 'producer-plan.json', producer_plan)
    result = {
        'format': 'emberbsd-bootstrap-bmake-result', 'version': 1,
        'scope': 'bmake-bootstrap',
        'status': 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW',
        'seed_descriptor': seed_ref, 'source_descriptor': source_ref,
        'bootstrap_plan': producer_plan_ref,
        'commands': [dict(step, actual_exit=0, log={
            'path': str(Path(producer_plan['work_root']) / (step['id'] + '.log')),
            'sha256': 'a' * 64}) for step in producer_plan['commands']],
        'all_commands_drained': True,
        'evidence_level': 'accepted-GCC16-command-routes-and-actual-exits',
        'ordinary_provider_qualified': False,
        'output_binary': producer_plan['output_binary'],
        'output': {'path': producer_plan['output_binary'], 'sha256': 'e' * 64, 'mode': 0o755},
        'inputs_revalidated_after_drain': True,
    }
    result_ref = _write_json_ref(root / 'result.json', result)
    return seed, source, seed_ref, source_ref, producer_plan, result, result_ref, host_tools


def producer_acceptance_fixture(directory, result, result_ref):
    receipt = {
        'format': 'emberbsd-bootstrap-bmake-producer-acceptance', 'version': 1,
        'scope': 'bmake-bootstrap', 'status': 'PASS',
        'evidence_level': 'independent-producer-evidence-review',
        'producer_result': result_ref, 'bootstrap_plan': result['bootstrap_plan'],
        'seed_descriptor': result['seed_descriptor'], 'source_descriptor': result['source_descriptor'],
        'output': result['output'], 'commands': result['commands'],
        'all_commands_drained': True, 'inputs_revalidated_after_drain': True,
        'ordinary_provider_qualified': False,
        'scope_boundary': 'independently reviewed source-bound bmake execution evidence only; no NativeToolset qualification',
    }
    return _write_json_ref(Path(directory) / 'producer-acceptance.json', receipt)


class BootstrapBmakeSeedTests(unittest.TestCase):
    def test_system_compiler_cannot_replace_the_accepted_gcc16_seed(self):
        from emberbsd_sdk.bootstrap_bmake_seed import validate_compiler_seed
        from emberbsd_sdk.errors import SDKError

        seed = compiler_seed()
        seed['compiler']['path'] = '/usr/bin/cc'
        with self.assertRaisesRegex(SDKError, 'compiler must be the accepted GCC16 seed'):
            validate_compiler_seed(seed, inspect_inputs=False)

    def test_only_the_receipt_bound_gcc16_driver_is_admitted(self):
        from emberbsd_sdk.bootstrap_bmake_seed import validate_compiler_seed

        seed = compiler_seed()
        admitted = validate_compiler_seed(seed, inspect_inputs=False)
        self.assertEqual(admitted['compiler']['path'], seed['prefix'] + '/bin/gcc')
        self.assertEqual(admitted['compiler']['version'], '16.2.0')
        self.assertEqual(admitted['compiler']['triplet'], 'x86_64-unknown-netbsd11.0')

    def test_self_hashed_historical_record_without_independent_acceptance_is_refused(self):
        from emberbsd_sdk.bootstrap_bmake_seed import validate_compiler_seed
        from emberbsd_sdk.errors import SDKError

        seed = compiler_seed()
        del seed['acceptance_receipt']
        with self.assertRaisesRegex(SDKError, 'unsupported bootstrap compiler seed'):
            validate_compiler_seed(seed, inspect_inputs=False)

    def test_prefix_inventory_binds_unselected_files_and_rejects_extras(self):
        from emberbsd_sdk.bootstrap_bmake_seed import verify_prefix_inventory
        from emberbsd_sdk.errors import SDKError
        from emberbsd_sdk.inventory import snapshot_tree

        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory).resolve() / 'prefix'
            prefix.mkdir()
            (prefix / 'bin').mkdir()
            (prefix / 'bin/gcc').write_bytes(b'gcc-driver')
            (prefix / 'share').mkdir()
            (prefix / 'share/specs').write_bytes(b'accepted-specs')
            inventory = snapshot_tree(str(prefix))
            manifest = {
                relative: {'sha256': row['sha256'], 'size': row['size']}
                for relative, row in inventory.items() if row['kind'] == 'file'
            }

            verify_prefix_inventory(str(prefix), manifest, inventory)
            (prefix / 'share/specs').write_bytes(b'modified-specs')
            with self.assertRaisesRegex(SDKError, 'complete GCC16 prefix inventory changed'):
                verify_prefix_inventory(str(prefix), manifest, inventory)

            inventory = snapshot_tree(str(prefix))
            manifest = {
                relative: {'sha256': row['sha256'], 'size': row['size']}
                for relative, row in inventory.items() if row['kind'] == 'file'
            }
            (prefix / 'extra').write_bytes(b'outside-receipt')
            with self.assertRaisesRegex(SDKError, 'complete GCC16 prefix inventory changed'):
                verify_prefix_inventory(str(prefix), manifest, inventory)

    def test_prefix_file_manifest_must_be_complete(self):
        from emberbsd_sdk.bootstrap_bmake_seed import verify_prefix_inventory
        from emberbsd_sdk.errors import SDKError
        from emberbsd_sdk.inventory import snapshot_tree

        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory).resolve() / 'prefix'
            prefix.mkdir()
            (prefix / 'gcc').write_bytes(b'driver')
            inventory = snapshot_tree(str(prefix))
            with self.assertRaisesRegex(SDKError, 'file manifest does not match every regular file'):
                verify_prefix_inventory(str(prefix), {}, inventory)

    def test_candidate_audit_writes_pending_full_inventory_and_runtime_evidence(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from emberbsd_sdk.bootstrap_bmake_seed import audit_compiler_seed_candidate
        from emberbsd_sdk.inventory import sha256_file, snapshot_tree
        from emberbsd_sdk.jsonio import write_json

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            prefix = root / 'prefix'
            paths = [
                prefix / 'bin/gcc',
                prefix / 'libexec/gcc/x86_64-unknown-netbsd11.0/16.2.0/cc1',
                prefix / 'libexec/gcc/x86_64-unknown-netbsd11.0/16.2.0/cc1plus',
                prefix / 'libexec/gcc/x86_64-unknown-netbsd11.0/16.2.0/collect2',
                prefix / 'lib/gcc/x86_64-unknown-netbsd11.0/16.2.0/libgcc.a',
            ]
            for index, path in enumerate(paths):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(f'compiler-member-{index}'.encode())
            entries = snapshot_tree(str(prefix))
            manifest = {
                relative: {'sha256': row['sha256'], 'size': row['size']}
                for relative, row in entries.items() if row['kind'] == 'file'
            }
            manifest_path = root / 'prefix-sha256.json'
            write_json(str(manifest_path), manifest)
            accepted_path = root / 'accepted.json'
            write_json(str(accepted_path), {
                'host': 'NetBSD11.0-amd64', 'prefix': str(prefix),
                'version': '16.2.0', 'triplet': 'x86_64-unknown-netbsd11.0',
                'manifest_sha256': sha256_file(str(manifest_path)),
                'source_plan': {'compiler': 'GCC16.2.0',
                                'kind': 'native compiler seed, no board objects',
                                'ports': '1' * 40, 'pkgsrc': '2' * 40},
                'checks': {'accept-c-run': 'native GCC16 C PASS\n',
                           'accept-cxx-run': 'native GCC16 C++ TLS thread exception PASS\n'},
                'boundary': 'AArch64 cross compiler and board kernel pending',
            })
            inventory_path, report_path = root / 'inventory.json', root / 'audit.json'
            actual_hash = sha256_file

            def hash_with_host_paths(path):
                if path in ('/usr/bin/as', '/usr/bin/ld', '/usr/bin/ldd', '/usr/lib/libc.so.12'):
                    return 'f' * 64
                return actual_hash(path)

            def query(argv, **_kwargs):
                stdout = {
                    '-dumpfullversion': b'16.2.0\n',
                    '-dumpmachine': b'x86_64-unknown-netbsd11.0\n',
                    '-print-prog-name=as': b'/usr/bin/as\n',
                    '-print-prog-name=ld': b'/usr/bin/ld\n',
                }[argv[1]]
                return SimpleNamespace(stdout=stdout, returncode=0)

            with patch('emberbsd_sdk.bootstrap_bmake_seed.platform.system', return_value='NetBSD'), \
                 patch('emberbsd_sdk.bootstrap_bmake_seed.platform.machine', return_value='x86_64'), \
                 patch('emberbsd_sdk.bootstrap_bmake_seed.sha256_file', side_effect=hash_with_host_paths), \
                 patch('emberbsd_sdk.bootstrap_bmake_seed._ldd_dependencies',
                       return_value={'/usr/lib/libc.so.12'}), \
                 patch('emberbsd_sdk.bootstrap_bmake_seed.capture', side_effect=query):
                report = audit_compiler_seed_candidate(
                    str(accepted_path), actual_hash(str(accepted_path)),
                    str(manifest_path), actual_hash(str(manifest_path)), str(prefix),
                    str(inventory_path), str(report_path))

            self.assertEqual(report['status'], 'PENDING_INDEPENDENT_REVIEW')
            self.assertEqual(report['checks'], {'prefix_inventory': 'PASS',
                                                'identity_queries': 'PASS',
                                                'runtime_closure': 'PASS'})
            self.assertEqual(len(report['queries']), 4)
            self.assertEqual(report['host_runtime']['libraries'], {'/usr/lib/libc.so.12': 'f' * 64})
            self.assertEqual(report['prefix_inventory']['sha256'], actual_hash(str(inventory_path)))

    def test_independent_compiler_receipt_binds_audit_report_and_source_record(self):
        from emberbsd_sdk.bootstrap_bmake_seed import (_validate_compiler_acceptance,
            COMPILER_ACCEPTANCE_FORMAT, COMPILER_AUDIT_FORMAT)
        from emberbsd_sdk.errors import SDKError
        from emberbsd_sdk.inventory import sha256_file
        from emberbsd_sdk.jsonio import write_json

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            seed = compiler_seed()
            accepted_path, manifest_path, inventory_path = (
                root / 'accepted.json', root / 'manifest.json', root / 'inventory.json')
            source_provenance = {'compiler': 'GCC16.2.0',
                                 'kind': 'native compiler seed, no board objects',
                                 'ports': '1' * 40, 'pkgsrc': '2' * 40}
            write_json(str(accepted_path), {'source_plan': source_provenance})
            write_json(str(manifest_path), {})
            write_json(str(inventory_path), {'entries': {'bin/gcc': {'kind': 'file'}}})
            accepted_ref = {'path': str(accepted_path), 'sha256': sha256_file(str(accepted_path))}
            manifest_ref = {'path': str(manifest_path), 'sha256': sha256_file(str(manifest_path))}
            inventory_ref = {'path': str(inventory_path), 'sha256': sha256_file(str(inventory_path))}
            seed.update(accepted_record=accepted_ref, prefix_manifest=manifest_ref,
                        prefix_inventory=inventory_ref)
            report = {
                'format': COMPILER_AUDIT_FORMAT, 'version': 1, 'scope': 'bmake-bootstrap',
                'status': 'PENDING_INDEPENDENT_REVIEW', 'host': seed['host'],
                'prefix': seed['prefix'], 'accepted_record': accepted_ref,
                'prefix_manifest': manifest_ref, 'prefix_inventory': inventory_ref,
                'compiler': seed['compiler'], 'driver_components': seed['driver_components'],
                'host_runtime': seed['host_runtime'], 'source_provenance': source_provenance,
                'checks': {'identity_queries': 'PASS', 'runtime_closure': 'PASS',
                           'prefix_inventory': 'PASS'},
                'queries': [
                    {'argv': [seed['compiler']['path'], '-dumpfullversion'], 'actual_exit': 0,
                     'stdout': '16.2.0'},
                    {'argv': [seed['compiler']['path'], '-dumpmachine'], 'actual_exit': 0,
                     'stdout': 'x86_64-unknown-netbsd11.0'},
                    {'argv': [seed['compiler']['path'], '-print-prog-name=as'], 'actual_exit': 0,
                     'stdout': seed['driver_components']['as']['path']},
                    {'argv': [seed['compiler']['path'], '-print-prog-name=ld'], 'actual_exit': 0,
                     'stdout': seed['driver_components']['ld']['path']},
                ],
                'scope_boundary': 'native GCC16 identity queries and runtime closure only; no NativeToolset qualification',
            }
            report_path = root / 'audit.json'
            write_json(str(report_path), report)
            audit_ref = {'path': str(report_path), 'sha256': sha256_file(str(report_path))}
            receipt = {
                'format': COMPILER_ACCEPTANCE_FORMAT, 'version': 1,
                'scope': 'bmake-bootstrap', 'status': 'PASS', 'audit_report': audit_ref,
                'accepted_record': accepted_ref, 'prefix_manifest': manifest_ref,
                'prefix_inventory': inventory_ref, 'host': seed['host'], 'prefix': seed['prefix'],
                'compiler': seed['compiler'], 'driver_components': seed['driver_components'],
                'host_runtime': seed['host_runtime'], 'source_provenance': source_provenance,
            }
            receipt_path = root / 'acceptance.json'
            write_json(str(receipt_path), receipt)
            seed['acceptance_receipt'] = {
                'path': str(receipt_path), 'sha256': sha256_file(str(receipt_path))}

            _validate_compiler_acceptance(seed, accepted_ref, manifest_ref, inventory_ref)
            seed['acceptance_receipt']['sha256'] = '0' * 64
            with self.assertRaisesRegex(SDKError, 'SHA256 mismatch'):
                _validate_compiler_acceptance(seed, accepted_ref, manifest_ref, inventory_ref)

    def test_ports_revision_is_explicitly_an_assertion(self):
        from emberbsd_sdk.bootstrap_bmake_seed import validate_source_descriptor
        from emberbsd_sdk.errors import SDKError

        source = source_descriptor()
        self.assertEqual(validate_source_descriptor(source, inspect_inputs=False), source)
        source['ports_source']['asserted_revision'] = 'main'
        with self.assertRaisesRegex(SDKError, 'Ports revision must be an asserted revision'):
            validate_source_descriptor(source, inspect_inputs=False)

    def test_ordinary_seed_or_native_toolset_receipt_cannot_be_relabelled(self):
        from emberbsd_sdk.bootstrap_bmake_seed import validate_compiler_seed
        from emberbsd_sdk.errors import SDKError

        seed = compiler_seed()
        seed['scope'] = 'qualified-native'
        with self.assertRaisesRegex(SDKError, 'unsupported bootstrap compiler seed'):
            validate_compiler_seed(seed, inspect_inputs=False)

    def test_compiler_seed_must_bind_host_runtime_closure_outside_usr_pkg(self):
        from emberbsd_sdk.bootstrap_bmake_seed import validate_compiler_seed
        from emberbsd_sdk.errors import SDKError

        seed = compiler_seed()
        seed['host_runtime']['libraries'] = {'/usr/pkg/lib/libgmp.so.10': '6' * 64}
        with self.assertRaisesRegex(SDKError, 'NetBSD base inputs'):
            validate_compiler_seed(seed, inspect_inputs=False)

    def test_static_libgcc_archive_is_not_sent_to_ldd(self):
        from emberbsd_sdk.bootstrap_bmake_seed import (_requires_executable_component,
            _runtime_binary_paths)

        seed = compiler_seed()
        paths = _runtime_binary_paths(seed['compiler']['path'], seed['driver_components'])
        self.assertIn(seed['compiler']['path'], paths)
        self.assertIn(seed['driver_components']['cc1']['path'], paths)
        self.assertIn(seed['driver_components']['as']['path'], paths)
        self.assertNotIn(seed['driver_components']['libgcc']['path'], paths)
        self.assertFalse(_requires_executable_component('libgcc'))
        self.assertTrue(_requires_executable_component('cc1'))

    def test_ldd_runtime_aliases_bind_the_real_base_library(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from emberbsd_sdk.bootstrap_bmake_seed import _ldd_dependencies
        from emberbsd_sdk.errors import SDKError

        output = SimpleNamespace(stdout=b'libm.so.0 => /usr/lib/libm.so.0 (0)\n')
        with patch('emberbsd_sdk.bootstrap_bmake_seed.capture', return_value=output), \
             patch('emberbsd_sdk.bootstrap_bmake_seed.os.path.realpath',
                   return_value='/lib/libm.so.0.14'):
            self.assertEqual(_ldd_dependencies('/usr/bin/ldd', '/usr/bin/ld'),
                             {'/lib/libm.so.0.14'})
        with patch('emberbsd_sdk.bootstrap_bmake_seed.capture', return_value=output), \
             patch('emberbsd_sdk.bootstrap_bmake_seed.os.path.realpath',
                   return_value='/usr/pkg/lib/libm.so.0.14'):
            with self.assertRaisesRegex(SDKError, 'escaped NetBSD base library directories'):
                _ldd_dependencies('/usr/bin/ldd', '/usr/bin/ld')

    def test_first_bmake_steps_use_the_receipt_bound_compiler_without_make(self):
        from emberbsd_sdk.bootstrap_bmake_seed import _HOST_TOOLS, bmake_steps

        seed = compiler_seed()
        plan = {
            'work_root': '/tmp/first-bmake',
            'utility_bin': '/tmp/first-bmake/bin',
            'machine_arch': 'x86_64',
            'host_tools': {'sh': {'path': '/bin/sh', 'sha256': '4' * 64}},
        }
        plan['host_tools'].update({name: {'path': '/usr/bin/' + name, 'sha256': '5' * 64}
                                   for name in _HOST_TOOLS})
        steps = bmake_steps(seed, plan)
        self.assertEqual([row['id'] for row in steps], ['configure', 'make-bootstrap'])
        self.assertEqual(steps[0]['env']['CC'], seed['compiler']['path'])
        self.assertEqual(steps[0]['env']['PATH'], plan['utility_bin'])
        self.assertEqual(steps[0]['argv'][1], './configure')
        self.assertEqual(steps[1]['argv'], ['/bin/sh', 'make-bootstrap.sh'])
        self.assertNotIn('make', steps[1]['argv'])
        self.assertNotIn('bmake', steps[1]['argv'])

    def test_system_compiler_cannot_enter_private_host_utility_path(self):
        from emberbsd_sdk.bootstrap_bmake_seed import _HOST_TOOLS, _tool_map
        from emberbsd_sdk.errors import SDKError

        host_tools = {'sh': {'path': '/bin/sh', 'sha256': '4' * 64}}
        host_tools.update({name: {'path': '/usr/bin/' + name, 'sha256': '5' * 64}
                           for name in _HOST_TOOLS})
        host_tools['gcc'] = {'path': '/usr/bin/gcc', 'sha256': '6' * 64}
        with self.assertRaisesRegex(SDKError, 'fixed host utility roster'):
            _tool_map(host_tools, inspect_inputs=False)

    def test_configure_utility_path_can_run_ls_without_host_path_fallback(self):
        from emberbsd_sdk.bootstrap_bmake_seed import select_host_utilities

        host_tools = select_host_utilities()
        self.assertIn('ls', host_tools)
        with tempfile.TemporaryDirectory() as directory:
            utility_bin = Path(directory) / 'bin'
            utility_bin.mkdir()
            (utility_bin / 'ls').symlink_to(host_tools['ls']['path'])
            result = subprocess.run(
                ['/bin/sh', '-c', 'ls -d .'], cwd=directory,
                env={'PATH': str(utility_bin), 'LC_ALL': 'C'},
                capture_output=True, text=True, check=False)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '.\n')

    def test_configured_script_binds_force_machine_arch_and_gcc16(self):
        from emberbsd_sdk.bootstrap_bmake_seed import _source_script_guard
        from emberbsd_sdk.errors import SDKError

        seed = compiler_seed()
        plan = {'machine_arch': 'x86_64'}
        expected = (
            b'DEFAULT_SYS_PATH="/usr/pkg/share/mk"\n'
            b'CC="' + seed['compiler']['path'].encode('ascii') + b'"\n'
            b'MDEFS="... -DFORCE_MACHINE_ARCH=\\"x86_64\\" ..."\n'
        )
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / 'make-bootstrap.sh'
            script.write_bytes(expected)
            _source_script_guard(str(script), seed, plan)

            script.write_bytes(expected.replace(b'\\"x86_64\\"', b'\\"amd64\\"'))
            with self.assertRaisesRegex(SDKError, 'accepted GCC16 driver and Ports recipe inputs'):
                _source_script_guard(str(script), seed, plan)

    def test_source_selector_requires_current_common_ports_owner_and_release(self):
        from emberbsd_sdk.bootstrap_bmake_seed import validate_source_descriptor
        from emberbsd_sdk.errors import SDKError

        source = source_descriptor()
        self.assertEqual(validate_source_descriptor(source, inspect_inputs=False), source)
        source['ports_source']['repository'] = 'https://example.invalid/other'
        with self.assertRaisesRegex(SDKError, 'common Ports owner'):
            validate_source_descriptor(source, inspect_inputs=False)

    def test_source_selector_refuses_unpinned_bmake_archive(self):
        from emberbsd_sdk.bootstrap_bmake_seed import validate_source_descriptor
        from emberbsd_sdk.errors import SDKError

        source = source_descriptor()
        source['archive']['sha256'] = '7' * 64
        with self.assertRaisesRegex(SDKError, '20260912 release pin'):
            validate_source_descriptor(source, inspect_inputs=False)

    def test_archive_metadata_becomes_an_exact_reference_before_source_unpack(self):
        from emberbsd_sdk.bootstrap_bmake_seed import _archive_reference, _source_archive_tree

        contents = b'20260912\n'
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / 'bmake-20260912.tar.gz'
            with tarfile.open(archive_path, 'w:gz') as archive:
                member = tarfile.TarInfo('bmake/VERSION')
                member.mode = 0o644
                member.size = len(contents)
                archive.addfile(member, io.BytesIO(contents))
            metadata = {
                'path': str(archive_path),
                'sha256': hashlib.sha256(archive_path.read_bytes()).hexdigest(),
                'url': 'https://www.crufty.net/ftp/pub/sjg/bmake-20260912.tar.gz',
                'version': '20260912',
            }

            files, directories = _source_archive_tree(_archive_reference(metadata))

        self.assertEqual(files, {'version': ('VERSION', hashlib.sha256(contents).hexdigest(), False, len(contents))})
        self.assertEqual(directories, set())

    def test_independent_audit_is_scoped_to_bmake_binary(self):
        from emberbsd_sdk.bootstrap_bmake_audit import audit_steps

        work = '/tmp/bmake-audit'
        plan = {
            'output_binary': '/tmp/bmake-producer/source/bmake',
            'work_root': work,
            'host_tools': {'sh': {'path': '/bin/sh', 'sha256': '8' * 64}},
        }
        steps = audit_steps(plan)
        self.assertEqual([row['id'] for row in steps], ['version', 'positive', 'expected-failure'])
        self.assertEqual(steps[0]['argv'], [plan['output_binary'], '-r', '-m', work + '/mk',
                                            '-V', '.MAKE.VERSION'])
        self.assertTrue(all(row['argv'][0] == plan['output_binary'] for row in steps))
        self.assertFalse(any(word in ('gcc', 'cc', 'make', 'gmake') for row in steps for word in row['argv']))

    def test_producer_plan_rejects_a_substituted_cc_even_with_a_pinned_seed(self):
        from emberbsd_sdk.bootstrap_bmake_seed import _HOST_TOOLS, bmake_steps, validate_plan
        from emberbsd_sdk.errors import SDKError

        seed, source = compiler_seed(), source_descriptor()
        host_tools = {'sh': {'path': '/bin/sh', 'sha256': '8' * 64}}
        host_tools.update({name: {'path': '/usr/bin/' + name, 'sha256': '9' * 64}
                           for name in _HOST_TOOLS})
        plan = dict(format='emberbsd-bootstrap-bmake-plan', version=1, scope='bmake-bootstrap',
                    purpose='produce-first-bmake',
                    seed_descriptor={'path': '/tmp/seed.json', 'sha256': 'a' * 64},
                    source_descriptor={'path': '/tmp/source.json', 'sha256': 'b' * 64},
                    host=seed['host'], machine_arch='x86_64', work_root='/tmp/first-bmake',
                    utility_bin='/tmp/first-bmake/bin',
                    output_binary='/tmp/first-bmake/source/bmake', host_tools=host_tools,
                    resources={'lock_root': '/var/tmp/ikar-producer-locks', 'runtime_seconds': 900}, commands=[])
        plan['commands'] = bmake_steps(seed, plan)
        validate_plan(plan, seed, source,
                      seed_ref=plan['seed_descriptor'], source_ref=plan['source_descriptor'],
                      inspect_inputs=False)
        plan['commands'][0]['env']['CC'] = '/usr/bin/cc'
        with self.assertRaisesRegex(SDKError, 'fixed Ports build route'):
            validate_plan(plan, seed, source,
                          seed_ref=plan['seed_descriptor'], source_ref=plan['source_descriptor'],
                          inspect_inputs=False)

    def test_producer_rejects_lock_root_overlapping_compiler_prefix(self):
        from emberbsd_sdk.bootstrap_bmake_seed import _HOST_TOOLS, bmake_steps, validate_plan
        from emberbsd_sdk.errors import SDKError

        seed, source = compiler_seed(), source_descriptor()
        host_tools = {'sh': {'path': '/bin/sh', 'sha256': '8' * 64}}
        host_tools.update({name: {'path': '/usr/bin/' + name, 'sha256': '9' * 64}
                           for name in _HOST_TOOLS})
        plan = dict(format='emberbsd-bootstrap-bmake-plan', version=1, scope='bmake-bootstrap',
                    purpose='produce-first-bmake',
                    seed_descriptor={'path': '/tmp/seed.json', 'sha256': 'a' * 64},
                    source_descriptor={'path': '/tmp/source.json', 'sha256': 'b' * 64},
                    host=seed['host'], machine_arch='x86_64', work_root='/tmp/first-bmake',
                    utility_bin='/tmp/first-bmake/bin',
                    output_binary='/tmp/first-bmake/source/bmake', host_tools=host_tools,
                    resources={'lock_root': seed['prefix'], 'runtime_seconds': 900}, commands=[])
        plan['commands'] = bmake_steps(seed, plan)
        with self.assertRaisesRegex(SDKError, 'lock root overlaps a protected input or work path'):
            validate_plan(plan, seed, source, seed_ref=plan['seed_descriptor'],
                          source_ref=plan['source_descriptor'], inspect_inputs=False)

    def test_producer_lock_root_cannot_contain_the_pinned_archive(self):
        from emberbsd_sdk.bootstrap_bmake_seed import _HOST_TOOLS, bmake_steps, validate_plan
        from emberbsd_sdk.errors import SDKError

        seed, source = compiler_seed(), source_descriptor()
        with tempfile.TemporaryDirectory() as directory:
            source['archive']['path'] = str(Path(directory) / 'bmake.tar.gz')
            host_tools = {'sh': {'path': '/bin/sh', 'sha256': '8' * 64}}
            host_tools.update({name: {'path': '/usr/bin/' + name, 'sha256': '9' * 64}
                               for name in _HOST_TOOLS})
            plan = dict(format='emberbsd-bootstrap-bmake-plan', version=1,
                        scope='bmake-bootstrap', purpose='produce-first-bmake',
                        seed_descriptor={'path': '/tmp/seed.json', 'sha256': 'a' * 64},
                        source_descriptor={'path': '/tmp/source.json', 'sha256': 'b' * 64},
                        host=seed['host'], machine_arch='x86_64', work_root='/tmp/first-bmake',
                        utility_bin='/tmp/first-bmake/bin',
                        output_binary='/tmp/first-bmake/source/bmake', host_tools=host_tools,
                        resources={'lock_root': directory, 'runtime_seconds': 900}, commands=[])
            plan['commands'] = bmake_steps(seed, plan)
            with self.assertRaisesRegex(SDKError, 'lock root overlaps a protected input or work path'):
                validate_plan(plan, seed, source, seed_ref=plan['seed_descriptor'],
                              source_ref=plan['source_descriptor'], inspect_inputs=False)

    def test_audit_plan_rejects_result_descriptor_substitution(self):
        from emberbsd_sdk.bootstrap_bmake_audit import create_audit_plan
        from emberbsd_sdk.bootstrap_bmake_seed import _HOST_TOOLS
        from emberbsd_sdk.errors import SDKError

        seed_ref = {'path': '/tmp/seed.json', 'sha256': 'a' * 64}
        source_ref = {'path': '/tmp/source.json', 'sha256': 'b' * 64}
        host_tools = {'sh': {'path': '/bin/sh', 'sha256': '8' * 64}}
        host_tools.update({name: {'path': '/usr/bin/' + name, 'sha256': '9' * 64}
                           for name in _HOST_TOOLS})
        with tempfile.TemporaryDirectory() as directory:
            result_path = Path(directory) / 'result.json'
            for field, wrong_ref in (
                ('seed_descriptor', {'path': '/tmp/other-seed.json', 'sha256': 'c' * 64}),
                ('source_descriptor', {'path': '/tmp/other-source.json', 'sha256': 'd' * 64}),
            ):
                result = {
                    'format': 'emberbsd-bootstrap-bmake-result', 'version': 1,
                    'scope': 'bmake-bootstrap',
                    'status': 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW',
                    'ordinary_provider_qualified': False,
                    'seed_descriptor': seed_ref, 'source_descriptor': source_ref,
                    'output': {'path': '/tmp/bmake', 'sha256': 'e' * 64, 'mode': 0o755},
                }
                result[field] = wrong_ref
                result_path.write_text(json.dumps(result), encoding='utf-8')
                result_ref = {'path': str(result_path),
                              'sha256': hashlib.sha256(result_path.read_bytes()).hexdigest()}
                with self.subTest(field=field), self.assertRaisesRegex(
                        SDKError, 'producer result descriptor reference differs'):
                    create_audit_plan(result_ref, seed_ref, source_ref,
                                      {'path': '/tmp/producer-acceptance.json', 'sha256': '7' * 64},
                                      '/tmp/audit-work', '/var/tmp/audit-locks', host_tools)

    def test_audit_rejects_result_descriptor_substitution_and_lock_overlap(self):
        from emberbsd_sdk.bootstrap_bmake_audit import create_audit_plan, validate_audit_plan
        from emberbsd_sdk.errors import SDKError

        with tempfile.TemporaryDirectory() as directory:
            seed, source, seed_ref, source_ref, producer_plan, result, result_ref, host_tools = (
                producer_result_fixture(directory))
            acceptance_ref = producer_acceptance_fixture(directory, result, result_ref)
            plan = create_audit_plan(result_ref, seed_ref, source_ref, acceptance_ref,
                                     str(Path(directory) / 'audit-work'),
                                     str(Path(directory) / 'audit-locks'), host_tools)
            for field, wrong_ref in (
                ('seed_descriptor', {'path': '/tmp/other-seed.json', 'sha256': 'c' * 64}),
                ('source_descriptor', {'path': '/tmp/other-source.json', 'sha256': 'd' * 64}),
            ):
                wrong_result = dict(result, **{field: wrong_ref})
                with self.subTest(field=field), self.assertRaisesRegex(
                        SDKError, 'producer result descriptor reference differs'):
                    validate_audit_plan(plan, wrong_result, seed, source,
                                        result_ref=result_ref, seed_ref=seed_ref,
                                        source_ref=source_ref, inspect_inputs=False)

            plan['resources']['lock_root'] = source['root']
            with self.assertRaisesRegex(SDKError, 'lock root overlaps a protected input or work path'):
                validate_audit_plan(plan, result, seed, source, result_ref=result_ref,
                                    seed_ref=seed_ref, source_ref=source_ref,
                                    inspect_inputs=False)

    def test_independent_audit_rejects_a_foreign_bmake_claimed_as_producer_output(self):
        from emberbsd_sdk.bootstrap_bmake_audit import create_audit_plan
        from emberbsd_sdk.errors import SDKError

        with tempfile.TemporaryDirectory() as directory:
            seed, source, seed_ref, source_ref, producer_plan, result, result_ref, host_tools = (
                producer_result_fixture(directory))
            acceptance_ref = producer_acceptance_fixture(directory, result, result_ref)
            # Simulate a foreign executable copied into the otherwise valid plan output path.
            result['output'] = dict(result['output'], sha256='f' * 64)
            foreign_result_ref = _write_json_ref(Path(directory) / 'foreign-result.json', result)
            self.assertEqual(result['output']['path'], producer_plan['output_binary'])
            with self.assertRaisesRegex(SDKError, 'producer acceptance receipt differs from the reviewed result'):
                create_audit_plan(foreign_result_ref, seed_ref, source_ref, acceptance_ref,
                                  str(Path(directory) / 'audit-work'),
                                  str(Path(directory) / 'audit-locks'), host_tools)

    def test_audit_lock_root_cannot_contain_the_pinned_archive(self):
        from emberbsd_sdk.bootstrap_bmake_audit import create_audit_plan
        from emberbsd_sdk.errors import SDKError

        with tempfile.TemporaryDirectory() as directory, \
             tempfile.TemporaryDirectory() as archive_directory:
            source = source_descriptor()
            source['archive']['path'] = str(Path(archive_directory) / 'bmake.tar.gz')
            _, _, seed_ref, source_ref, _, result, result_ref, host_tools = producer_result_fixture(
                directory, source=source)
            acceptance_ref = producer_acceptance_fixture(directory, result, result_ref)
            with self.assertRaisesRegex(SDKError, 'lock root overlaps a protected input or work path'):
                create_audit_plan(result_ref, seed_ref, source_ref, acceptance_ref,
                                  str(Path(directory) / 'audit-work'), archive_directory,
                                  host_tools)


if __name__ == '__main__':
    unittest.main()
