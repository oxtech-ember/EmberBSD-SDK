# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Finite initial pkg_install controls. No selected executable runs."""

import copy
import fcntl
import hashlib
import io
import os
from pathlib import Path
import shlex
import struct
import subprocess
import tarfile
import unittest
from unittest.mock import patch

import test_bootstrap_bsdinstall as bsd_tests
from emberbsd_sdk import bootstrap_pkg_install as pkg, bootstrap_program as program
from emberbsd_sdk import bootstrap_producer as producer, bootstrap_pilot
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.inventory import snapshot_tree


class PkgInstallTests(unittest.TestCase):
    def setUp(self):
        self.old = bsd_tests.BSDInstallTests()
        self.old.setUp()
        self.addCleanup(self.old.doCleanups)
        self.base, self.f = self.old.base, self.old.f
        self.descriptor, self.commands, self.flags = self.old.descriptor, self.old.commands, self.old.flags
        self.work = self.base / 'pkg-work'
        self.source, self.owner = self.base / 'pkg-source', self.base / 'pkg-owner'
        self.manifest = dict(format='emberbsd-native-bootstrap-program', version=1, name='pkg-install',
            initial_only=True, build_system='pkgsrc-bootstrap', compiler=dict(family='gcc', major=16, abi='native'),
            output_programs=pkg._OUTPUTS, infrastructure_patches=[pkg._PATCH], files={'payload/input': 'data\n'},
            required_tool_roles=sorted(pkg._DRIVERS | {'sh', 'install', 'cat'}),
            components=[dict(name='libarchive', source_directory='archivers/libarchive/files',
                configure_args=['--enable-static', '--disable-shared'], make_targets=['all']),
                dict(name='pkg_install', source_directory='pkgtools/pkg_install/files',
                     configure_args=['--enable-bootstrap'], make_targets=['all'])],
            runtime_cases=[dict(name='fixed-' + str(i), invocation='pkg_admin', args=['-V'], stdin='', exit=0,
                                stdout='20260227\n', stderr='') for i in range(16)])
        for relative, raw in (('archivers/libarchive/files/configure', b'synthetic configure\n'),
            ('pkgtools/pkg_install/files/configure', b'synthetic configure\n'),
            ('bootstrap/bootstrap', b'--enable-static \\\n --disable-shared --enable-bootstrap STATIC_LIBARCHIVE=fixture\n')):
            self.old.put(self.source / relative, raw)
        for relative in pkg._GNU_CONFIG_OVERRIDES.values():
            self.old.put(self.source / relative, ('canonical current ' + relative + '\n').encode())
            os.utime(self.source / relative, ns=(1700000000123456789, 1700000000123456789))
        for relative in pkg._GNU_CONFIG_OVERRIDES:
            component, name = relative.split('/', 1)
            directory = 'archivers/libarchive/files' if component == 'libarchive' else 'pkgtools/pkg_install/files'
            self.old.put(self.source / directory / name, b'old config rejects aarch64-apple-darwin25\n')
        self.old.readonly(self.source)
        self.old.put(self.owner / pkg._PATCH, b'synthetic canonical patch\n')
        self.old.put(self.owner / 'pkgsrc/export.conf', b'synthetic canonical order\n')
        raw_ref = self.f.document(self.manifest)
        self.old.put(self.owner / pkg.MANIFEST_RELATIVE, Path(raw_ref['path']).read_bytes())
        self.manifest_ref = dict(path=str(self.owner / pkg.MANIFEST_RELATIVE), sha256=raw_ref['sha256'])
        mocked = patch.object(pkg, 'MANIFEST_SHA256', raw_ref['sha256'])
        mocked.start()
        self.addCleanup(mocked.stop)
        for role in set(self.manifest['required_tool_roles']) | pkg._CONFIGURE_UTILITIES:
            if role not in self.commands:
                self.commands[role] = (str(self.base / ('selected-' + role)),)
                self.descriptor['tools'][role] = dict(kind='native')
        self.commands['make'] = (str(self.base / 'selected-bmake'), '-m', str(self.base / 'selected-mk'),
                                 'SHELL=' + self.commands['sh'][0], 'MAKECONF=' + str(self.base / 'selected.conf'))
        self.profile = self.f.raw(b'synthetic fixed profile\n')
        export = self.base / 'export'
        export.mkdir()
        profile_path = export / producer.PROFILE_EXPORT
        self.old.put(profile_path, Path(self.profile['path']).read_bytes())
        self.profile['path'] = str(profile_path)
        self.plan = dict(format=producer.PRODUCER_FORMAT, version=1, scope='gcc16-bootstrap', stage=pkg.STAGE,
            seed_descriptor={}, host=self.descriptor['host'], sources=self.descriptor['sources'], work_root=str(self.work),
            required_roles=sorted(set(self.manifest['required_tool_roles']) | pkg._CONFIGURE_UTILITIES |
                                  set(self.descriptor['driver_components'].values())),
            resources=dict(cpu_jobs=1, link_jobs=1, runtime_seconds=1200, lock_root=str(self.base),
                           heavy_link_context=str(self.base / 'heavy')),
            program=dict(source=self.old.tree(self.source, owner=False), owner=self.old.tree(self.owner, owner=True),
                manifest=self.manifest_ref, compat=self.old.plan['program']['compat'], profile=self.profile,
                export=dict(root=str(export), inventory=self.f.document(snapshot_tree(str(self.source))), source_proof=self.f.document({}))))
        self.plan['commands'] = program.program_steps(self.plan, self.descriptor, self.commands, self.flags)

    def validate(self):
        # The unchanged export proof validator is covered by metadata tests.
        with patch.object(producer, '_export_proof'), patch('subprocess.Popen', side_effect=AssertionError('no child')):
            return producer.validate_producer_plan(self.plan, self.descriptor, self.commands, self.flags)

    def test_fixed_commands_preserve_bound_make_driver_abi_and_serial_cases(self):
        self.assertIs(self.validate(), self.plan)
        rows = self.plan['commands']
        self.assertEqual(len(rows), 24)
        self.assertEqual([r['phase'] for r in rows[:4]], ['pkg-configure', 'pkg-build'] * 2)
        for row in rows[:4]:
            self.assertEqual(shlex.split(row['env']['CC']), list(self.commands['cc']))
            self.assertEqual(row['env']['PATH'], str(self.work / 'bin'))
            self.assertIn('-Wl,--threads=1', shlex.split(row['env']['LDFLAGS']))
            self.assertEqual(shlex.split(row['env']['MAKE']), list(self.commands['make']))
        for row in (rows[1], rows[3]):
            self.assertEqual(row['argv'][:len(self.commands['make'])], list(self.commands['make']))
            self.assertIn('MAKE=' + shlex.join(self.commands['make']), row['argv'])
        self.assertIn('-lnbcompat', rows[2]['env']['LIBS'])
        self.assertEqual(rows[0]['env']['LIBS'], '')
        self.assertTrue(all(row['argv'][:len(self.commands['sh'])] == list(self.commands['sh']) for row in rows[4:20]))
        self.assertTrue(all(row['env']['DYLD_PRINT_LIBRARIES'] == '1' for row in rows[20:]))

    def test_parallel_build_budget_does_not_parallelize_cases(self):
        self.plan['resources']['cpu_jobs'] = 18
        self.plan['commands'] = program.program_steps(self.plan, self.descriptor, self.commands, self.flags)
        with patch('os.cpu_count', return_value=24):
            self.assertIs(self.validate(), self.plan)
        self.assertIn('-j18', self.plan['commands'][1]['argv'])
        self.assertTrue(all('-j18' not in row['argv'] for row in self.plan['commands'][4:]))

    def test_missing_role_changed_command_and_source_refused(self):
        for mutation in ('role', 'argv', 'source'):
            with self.subTest(mutation=mutation):
                saved = copy.deepcopy(self.plan)
                if mutation == 'role':
                    self.plan['required_roles'].remove('install')
                elif mutation == 'argv':
                    self.plan['commands'][0]['argv'].append('--disable-checks')
                else:
                    inventory = snapshot_tree(str(self.source))
                    inventory.pop('pkgtools/pkg_install/files/configure')
                    self.plan['program']['export']['inventory'] = self.f.document(inventory)
                with self.assertRaises(SDKError):
                    self.validate()
                self.plan = saved

    def test_compat_binding_still_refuses_changed_abi(self):
        self.descriptor['compiler']['triplet'] += '-changed'
        with self.assertRaises(SDKError):
            self.validate()

    def test_common_gnu_configs_replace_all_four_old_files_exactly(self):
        self.work.mkdir()
        pkg.prepare_pkg_install_work(self.plan, self.commands)
        for relative, common in pkg._GNU_CONFIG_OVERRIDES.items():
            with self.subTest(relative=relative):
                target, canonical = self.work / 'source' / relative, self.source / common
                component, name = relative.split('/', 1)
                original = self.source / ('archivers/libarchive/files' if component == 'libarchive'
                                          else 'pkgtools/pkg_install/files') / name
                self.assertNotEqual(original.read_bytes(), canonical.read_bytes())
                self.assertEqual(target.read_bytes(), canonical.read_bytes())
                self.assertEqual(target.stat().st_mode & 0o7777, canonical.stat().st_mode & 0o7777)
                self.assertEqual(target.stat().st_mtime_ns, canonical.stat().st_mtime_ns)
                self.assertIn('source/' + relative, program.program_outputs(self.plan))

    def test_common_gnu_config_source_members_cannot_be_missing_or_replaced(self):
        for mutation in ('missing', 'changed'):
            with self.subTest(mutation=mutation):
                value = self.plan['program']['export']
                saved = value['inventory']
                inventory = snapshot_tree(str(self.source))
                if mutation == 'missing':
                    del inventory['mk/gnu-config/config.sub']
                else:
                    inventory['mk/gnu-config/config.sub']['sha256'] = '0' * 64
                value['inventory'] = self.f.document(inventory)
                with self.assertRaises(SDKError):
                    self.validate()
                value['inventory'] = saved

    def test_configure_diff_is_required_native_and_exact_alias(self):
        # The source-owner manifest is a minimum; configure needs selected diff too.
        self.assertNotIn('diff', self.manifest['required_tool_roles'])
        self.assertIs(self.validate(), self.plan)
        for mutation in ('required-role', 'missing-command', 'wrong-kind'):
            with self.subTest(mutation=mutation):
                roles = list(self.plan['required_roles'])
                command = self.commands['diff']
                record = dict(self.descriptor['tools']['diff'])
                if mutation == 'required-role':
                    self.plan['required_roles'].remove('diff')
                elif mutation == 'missing-command':
                    del self.commands['diff']
                else:
                    self.descriptor['tools']['diff']['kind'] = 'source'
                with self.assertRaises(SDKError):
                    self.validate()
                self.plan['required_roles'] = roles
                self.commands['diff'] = command
                self.descriptor['tools']['diff'] = record
        self.work.mkdir()
        pkg.prepare_pkg_install_work(self.plan, self.commands)
        alias = self.work / 'bin/diff'
        self.assertTrue(alias.is_symlink())
        self.assertEqual(os.readlink(alias), self.commands['diff'][0])

    def test_preparation_preserves_source_mtimes_and_has_no_bare_make_alias(self):
        self.work.mkdir()
        pkg.prepare_pkg_install_work(self.plan, self.commands)
        self.assertEqual((self.work / 'source/libarchive/configure').stat().st_mtime_ns,
                         (self.source / 'archivers/libarchive/files/configure').stat().st_mtime_ns)
        self.assertFalse((self.work / 'bin/make').exists())
        self.assertTrue((self.work / 'bin/install').is_symlink())
        self.assertTrue((self.work / 'payload').is_dir())

    def pc(self, text):
        path = self.work / 'source/libarchive/build/pkgconfig/libarchive.pc'
        self.old.put(path, text.encode())

    def test_actual_libarchive_private_libraries_are_exported_to_nested_make(self):
        self.pc('prefix=/fixture\nLibs.private: -lm -lpthread\n')
        result = pkg._private_libraries(str(self.work))
        self.assertEqual(result['private_libraries'], ['-lm', '-lpthread'])
        self.assertEqual((self.work / 'libarchive-private.mk').read_text(),
                         'STATIC_LIBARCHIVE_LDADD= -lm -lpthread\n.export STATIC_LIBARCHIVE_LDADD\n')
        with self.assertRaises(FileExistsError):
            pkg._private_libraries(str(self.work))

    def test_private_library_injection_and_duplicate_line_refused(self):
        for value in ('Libs.private: -L/ambient -lx\n', 'Libs.private: $(command)\n',
                      'Libs.private: -lm\nLibs.private: -lpthread\n'):
            with self.subTest(value=value):
                self.pc(value)
                with self.assertRaises(SDKError):
                    pkg._private_libraries(str(self.work))
                self.assertFalse((self.work / 'libarchive-private.mk').exists())

    def test_strong_missing_library_diagnostic_is_required(self):
        case = dict(name='missing', invocation='pkg_add', args=[], stdin='', exit=1, stdout='',
                    stderr_nonempty=True, stderr_contains='missing required library: /missing', checks=[])
        self.manifest['runtime_cases'][0] = case
        self.work.mkdir()
        (self.work / 'missing.stdout').write_bytes(b'')
        (self.work / 'missing.stderr').write_bytes(b'unrelated failure\n')
        with patch.object(pkg, 'pkg_install_manifest', return_value=self.manifest):
            with self.assertRaises(SDKError):
                program.verify_program_step(dict(id='case-missing', phase='program-case'), str(self.work), self.plan)
            (self.work / 'missing.stderr').write_bytes(b'pkg_add: missing required library: /missing\n')
            self.assertIn('stderr', program.verify_program_step(dict(id='case-missing', phase='program-case'),
                                                               str(self.work), self.plan))

    def test_version_create_has_no_archive_postcheck(self):
        self.work.mkdir()
        case = dict(name='version-create', invocation='pkg_create', args=['-V'], stdin='', exit=0,
                    stdout='20260227\n', stderr='')
        self.manifest['runtime_cases'][0] = case
        (self.work / 'version-create.stdout').write_bytes(b'20260227\n')
        (self.work / 'version-create.stderr').write_bytes(b'')
        with patch.object(pkg, 'pkg_install_manifest', return_value=self.manifest):
            result = pkg.verify_pkg_install_step(dict(id='case-version-create', phase='program-case'),
                                                 str(self.work), self.plan)
        self.assertEqual(result['checks'], [])

    def test_package_is_uncompressed_and_contains_metadata_and_payload(self):
        self.work.mkdir()
        case = dict(name='create-cache', invocation='pkg_create', args=[], stdin='', exit=0, stdout='', stderr='',
                    checks=[dict(path='fixture.tgz', kind='file')])
        self.manifest['runtime_cases'][0] = case
        for stream in ('stdout', 'stderr'):
            (self.work / ('create-cache.' + stream)).write_bytes(b'')
        for mode in ('w:gz', 'w:'):
            with tarfile.open(self.work / 'fixture.tgz', mode) as output:
                for name in ('+CONTENTS', '+BUILD_INFO', 'share/initial-pkgtools.txt'):
                    row = tarfile.TarInfo(name)
                    row.size = 4
                    output.addfile(row, io.BytesIO(b'data'))
            with patch.object(pkg, 'pkg_install_manifest', return_value=self.manifest):
                if mode == 'w:gz':
                    with self.assertRaises(tarfile.ReadError):
                        pkg.verify_pkg_install_step(dict(id='case-create-cache', phase='program-case'), str(self.work), self.plan)
                else:
                    self.assertTrue(pkg.verify_pkg_install_step(dict(id='case-create-cache', phase='program-case'), str(self.work), self.plan))

    def test_runner_holds_source_compat_export_work_and_build_context(self):
        descriptor_ref = self.f.document(self.descriptor)
        self.plan['seed_descriptor'] = descriptor_ref
        plan_ref = self.f.document(self.plan)
        accepted = self.f.document({})
        case = self
        class Selection:
            commands = case.commands
            def as_dict(self):
                return dict(plan=case.validate())
        def locked(context):
            name = '.prebuild-sdk-lock-' + hashlib.sha256(str(context).encode()).hexdigest()
            with open(self.base / name, 'rb') as handle:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        def fake_command(argv, log, **kwargs):
            step = next(row for row in self.plan['commands'] if row['id'] == Path(log).stem)
            for root in (self.source, self.owner, self.old.compat, self.work, self.plan['program']['export']['root']):
                locked(root)
            if step['phase'] in ('pkg-configure', 'pkg-build'):
                locked(self.plan['resources']['heavy_link_context'])
            Path(log).write_bytes(b'synthetic drained child; no executable ran\n')
            if step['id'] == 'libarchive-configure':
                self.pc('Libs.private: -lm\n')
            if step['id'] == 'libarchive-build':
                self.old.put(self.work / 'source/libarchive/.libs/libarchive.a', b'!<arch>\n')
            if step['id'] == 'pkg_install-build':
                for relative in pkg._OUTPUTS.values():
                    path = self.work / 'source' / relative
                    self.old.put(path, struct.pack('<8I', 0xfeedfacf, 0x100000c, 0, 2, 0, 0, 0, 0))
                    path.chmod(0o755)
            if step['phase'] == 'program-case':
                row = next(r for r in self.manifest['runtime_cases'] if step['id'] == 'case-' + r['name'])
                for name in ('stdout', 'stderr'):
                    (self.work / (row['name'] + '.' + name)).write_text(row[name])
            return subprocess.CompletedProcess(argv, 0)
        with patch('emberbsd_sdk.bootstrap_seed.load_bootstrap_seed', side_effect=lambda *a, **kw: Selection()) as load, \
             patch.object(bootstrap_pilot, 'run_logged', side_effect=fake_command), \
             patch('subprocess.Popen', side_effect=AssertionError('no child')):
            result = bootstrap_pilot.run_producer(descriptor_ref['path'], descriptor_sha256=descriptor_ref['sha256'],
                accepted_seed_receipt=accepted, bootstrap_plan=plan_ref, required_roles=self.plan['required_roles'])
        self.assertEqual(result['status'], 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW', result.get('error'))
        self.assertEqual(len(result['commands']), 24)
        self.assertTrue(result['all_commands_drained'] and result['inputs_revalidated_after_drain'] and result['all_leases_closed'])
        self.assertFalse(result['ordinary_provider_qualified'])
        self.assertEqual(load.call_count, 2)

    def test_profile_addition_retains_legacy_classes(self):
        self.assertTrue(producer._native_provider_profile(producer.BOOTSTRAP_PKGTOOLS_PROFILE_SHA256))
        self.assertTrue(producer._native_provider_profile(producer.HOST_RUBY_PROFILE_SHA256))
        self.assertTrue(producer._native_provider_profile(producer.FOUNDATION_PROFILE_SHA256))
        self.assertTrue(producer._native_provider_profile(producer.BSDINSTALL_PROFILE_SHA256))
        self.assertTrue(producer._recognized_profile(producer.PROFILE_SHA256))
        self.assertFalse(producer._recognized_profile('0' * 64))


if __name__ == '__main__':
    unittest.main()
