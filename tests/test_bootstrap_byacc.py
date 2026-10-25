# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Real owner data and synthetic children. No native compiler or parser execution."""
import hashlib
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

import test_bootstrap_program as fixtures
from emberbsd_sdk import bootstrap_byacc as byacc, bootstrap_program as program
from emberbsd_sdk.bootstrap_program_history import _commands
from emberbsd_sdk.errors import SDKError, CommandError
from emberbsd_sdk.inventory import snapshot_tree


class ByaccContractTests(fixtures.ProgramTests):
    # Reuse the established seed/runner fixture without duplicating its tests.
    def setUp(self):
        super().setUp()
        required = ('EMBERBSD_TEST_PORTS_SOURCE', 'EMBERBSD_TEST_BYACC_ARCHIVE', 'EMBERBSD_TEST_BYACC_SOURCE')
        if any(not os.environ.get(key) for key in required):
            self.skipTest('select the real Ports owner, release archive and patched source')
        owner = self.base / 'byacc-owner'
        upstream = self.base / 'byacc-source'
        owner.mkdir()
        for name in byacc.OWNER_FILES:
            target = owner / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(Path(os.environ[required[0]], name), target)
        shutil.copytree(os.environ[required[2]], upstream)
        for path in upstream.rglob('*'):
            path.chmod(0o555 if path.is_dir() or path.stat().st_mode & 0o111 else 0o444)
        upstream.chmod(0o555)
        def tree(root, git):
            inventory = snapshot_tree(str(root))
            inv = self.f.document(inventory)
            members = {}
            for name, row in inventory.items():
                if git:
                    if row['kind'] != 'file': continue
                    raw = (root / name).read_bytes()
                    members[name] = dict(sha256=row['sha256'], git_blob=hashlib.sha1(
                        b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest())
                else:
                    members[name] = dict(mtime_ns=(root / name).stat().st_mtime_ns)
                    if row['kind'] == 'file': members[name]['sha256'] = row['sha256']
            source = self.descriptor['sources']['ports']
            proof = self.f.document(dict(format='emberbsd-bootstrap-program-source', version=1,
                source=source, inventory=inv, members=self.f.document(members)))
            return dict(root=str(root), source=source, inventory=inv, source_proof=proof)
        def ref(path):
            return dict(path=str(path), sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest())
        self.plan['stage'] = byacc.STAGE
        self.plan['program'] = dict(owner=tree(owner, True), source=tree(upstream, False),
            manifest=ref(owner / byacc.MANIFEST_RELATIVE), archive=ref(os.environ[required[1]]),
            patches=[ref(owner / byacc.RECIPE / 'patches' / name) for name in ('patch-aclocal.m4', 'patch-configure')])
        self.manifest = byacc.byacc_manifest(self.plan)
        self.plan['commands'] = program.program_steps(self.plan, self.descriptor, self.commands, self.flags)

    def fake_command(self, argv, log, **kwargs):
        step = next(row for row in self.plan['commands'] if row['id'] == Path(log).stem)
        if step['phase'] == 'generator':
            for name in self.manifest['generated']:
                (self.work / name).write_bytes(('synthetic generated ' + name).encode())
        return super().fake_command(argv, log, **kwargs)

    def test_real_manifest_builds_complete_finite_route(self):
        with patch('subprocess.Popen', side_effect=AssertionError('no native child')):
            self.validate()
        self.assertEqual(len(self.plan['commands']), 31)
        compiles = [row for row in self.plan['commands'] if row['phase'] == 'compile']
        self.assertEqual(len(compiles), 16)
        self.assertTrue(any('btyaccpar.c' in word for row in compiles for word in row['argv']))
        self.assertTrue(all('-DYYPATCH=20260126' in row['argv'] for row in compiles[1:-1]))
        self.assertEqual(byacc.byacc_sources(self.plan)['grammar.y'],
                         Path(self.plan['program']['owner']['root'], byacc.RECIPE, 'bootstrap/grammar.y').read_bytes())
        self.assertNotIn('config.h', byacc.byacc_sources(self.plan))
        for row in self.plan['commands']:
            self.assertEqual(row['env']['TMPDIR'], str(self.work))

    def test_release_makefile_and_active_capabilities_match_owner(self):
        root = Path(self.plan['program']['source']['root'])
        makefile = (root / 'makefile.in').read_text()
        table = makefile.split('C_FILES =', 1)[1].split('OBJS', 1)[0]
        names = table.replace('\\', '').replace('$(SKELETON)', 'btyaccpar').split()
        self.assertEqual(names, self.manifest['sources'])
        owner = Path(self.plan['program']['owner']['root'], byacc.RECIPE)
        template = (owner / self.manifest['configuration']['template']).read_text()
        self.assertIn('#define YYBTYACC 1', template)
        for macro in ('HAVE_GETOPT', 'HAVE_MKSTEMP', 'HAVE_VSNPRINTF', 'HAVE_STDNORETURN_H'):
            self.assertIn('#define ' + macro + ' 1', template)
        self.assertNotIn('#define HAVE_FCNTL_H', template)
        self.assertEqual((root / 'VERSION').read_text().strip(), '20260126')

    def test_history_patch_urls_use_canonical_github_pages(self):
        for repository in ('https://github.com/oxtech-ember/EmberBSD-Ports.git',
                           'https://github.com/oxtech-ember/EmberBSD-Ports'):
            self.plan['program']['owner']['source'] = dict(repository=repository, commit='5' * 40)
            archives, patches = byacc.history_origins(self.plan)
            self.assertEqual([row['url'] for row in patches], [
                'https://github.com/oxtech-ember/EmberBSD-Ports/blob/' + '5' * 40 +
                '/pkgsrc/devel/byacc/patches/patch-aclocal.m4',
                'https://github.com/oxtech-ember/EmberBSD-Ports/blob/' + '5' * 40 +
                '/pkgsrc/devel/byacc/patches/patch-configure'])
            self.assertEqual([row['file'] for row in patches], self.plan['program']['patches'])
            self.assertEqual(archives[0]['url'],
                             'https://invisible-island.net/archives/byacc/byacc-20260126.tgz')

    def test_changed_owner_manifest_refuses_without_child(self):
        ref = self.plan['program']['manifest']
        Path(ref['path']).write_text(Path(ref['path']).read_text().replace('14\\n', '13\\n'))
        with patch('subprocess.Popen', side_effect=AssertionError('no native child')):
            with self.assertRaises(SDKError): self.validate()

    def test_missing_patch_and_driver_refuse_without_child(self):
        original = self.plan['program']['patches']
        self.plan['program']['patches'] = original[:-1]
        with self.assertRaisesRegex(SDKError, 'patches'): self.validate()
        self.plan['program']['patches'] = original
        self.plan['required_roles'].remove('sh')
        with self.assertRaisesRegex(SDKError, 'roles'): self.validate()

    def test_changed_release_and_patch_output_refuse_without_child(self):
        root = Path(self.plan['program']['source']['root'])
        for name in ('main.c', 'configure'):
            path = root / name
            original = path.read_bytes()
            info = path.stat()
            path.chmod(0o644)
            path.write_bytes(original + b'\nchanged\n')
            path.chmod(0o444)
            with self.assertRaisesRegex(SDKError, 'changed'): self.validate()
            path.chmod(0o644); path.write_bytes(original); path.chmod(info.st_mode & 0o777)
            os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        # The complete archive comparison also refuses a changed asserted member map.
        expected = byacc.source_members(self.plan)
        expected['main.c'] = '0' * 64
        with patch.object(byacc, 'source_members', return_value=expected):
            with self.assertRaisesRegex(SDKError, 'mtime|exact release'): self.validate()

    def test_capability_failure_never_creates_configuration(self):
        fake = self.fake_command
        def fail(argv, log, **kwargs):
            if Path(log).stem == 'capability-run':
                Path(log).write_bytes(b'synthetic missing native capability\n')
                raise CommandError(1, argv, log=log)
            return fake(argv, log, **kwargs)
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=fail): result = self.invoke()
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(len(result['commands']), 3)
        self.assertFalse((self.work / 'config.h').exists())

    def test_success_requires_generated_parser_and_independent_semantics(self):
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=self.fake_command): result = self.invoke()
        self.assertEqual(result['status'], 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW')
        self.assertEqual((self.work / 'config.h').read_bytes(), byacc.configuration_bytes(self.plan, result['commands']))
        byacc.verify_byacc_history(self.plan, result)
        self.assertEqual(_commands(self.plan, result, self.manifest)['path'], str(self.work / 'yacc'))
        (self.work / 'parser.tab.c').write_bytes(b'changed parser\n')
        with self.assertRaises(SDKError): byacc.verify_byacc_history(self.plan, result)

    def test_changed_generated_input_refuses_before_parser_compiler(self):
        original = program.verify_program_step
        seen = []
        def verify(step, work, plan, **kwargs):
            value = original(step, work, plan, **kwargs)
            if step['id'] == 'regenerate':
                (self.work / 'parser.tab.c').write_bytes(b'changed after regeneration\n')
            return value
        def child(argv, log, **kwargs):
            seen.append(Path(log).stem)
            return self.fake_command(argv, log, **kwargs)
        with patch('emberbsd_sdk.bootstrap_program.verify_program_step', side_effect=verify), \
             patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=child):
            result = self.invoke()
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(seen[-1], 'regenerate')
        self.assertFalse((self.work / 'parser.o').exists())

    def test_changed_regeneration_stops_before_parser_compile(self):
        fake = self.fake_command
        def change(argv, log, **kwargs):
            value = fake(argv, log, **kwargs)
            if Path(log).stem == 'regenerate': (self.work / 'parser.tab.c').write_bytes(b'different\n')
            return value
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=change): result = self.invoke()
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(result['commands'][-1]['id'], 'regenerate')
        self.assertFalse((self.work / 'parser.o').exists())

    def test_wrong_parser_output_fails(self):
        fake = self.fake_command
        def change(argv, log, **kwargs):
            value = fake(argv, log, **kwargs)
            if Path(log).stem == 'case-precedence': (self.work / 'precedence.stdout').write_bytes(b'20\n')
            return value
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=change): result = self.invoke()
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(result['commands'][-1]['id'], 'case-precedence')

    def test_no_configuration_without_real_ordered_observations(self):
        with self.assertRaisesRegex(SDKError, 'actual successful'): byacc.configuration_bytes(self.plan, [])
        rows = [dict(id='capability-' + name, actual_exit=0) for name in ('compile', 'link', 'run')]
        rows[-1]['actual_exit'] = 1
        with self.assertRaisesRegex(SDKError, 'actual successful'): byacc.configuration_bytes(self.plan, rows)

# Do not inherit the grep-specific assertions; reuse only its fixture methods.
for _name in tuple(name for name in dir(fixtures.ProgramTests) if name.startswith('test_')):
    if _name not in ByaccContractTests.__dict__:
        setattr(ByaccContractTests, _name, None)
