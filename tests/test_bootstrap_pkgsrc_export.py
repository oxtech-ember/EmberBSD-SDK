# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Contracts for the source-bound current Ports pkgsrc exporter."""

import os
from pathlib import Path
import subprocess

from test_core import TemporaryCase
from emberbsd_sdk.bootstrap_pkgsrc_export import (
    describe_export_source, export_source_manifest, create_export_plan,
    validate_export_source, validate_export_plan, validate_export_transfer_acceptance,
    worker_main, run_export, _tools,
)
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.inventory import sha256_file, snapshot_tree, transfer_inventory_equal
from emberbsd_sdk.jsonio import read_json, write_json
from unittest.mock import patch


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.STDOUT).decode().strip()


def write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data)
    return path


def commit(root, *paths):
    git(root, 'add', '-f', *(paths or ('.',)))
    git(root, '-c', 'user.name=SDK fixture', '-c', 'user.email=fixture@example.invalid',
        'commit', '-qm', 'Fixture source')


class PkgsrcExportToolTests(TemporaryCase):
    def tools(self, patch_name='patch'):
        tools = {}
        for role, name in (('git', 'git'), ('patch', patch_name)):
            path = write(self.root / name, f'{role} fixture bytes\n')
            path.chmod(0o755)
            tools[role] = dict(path=str(path), sha256=sha256_file(path))
        return tools

    def test_patch_gpatch_and_nbpatch_are_exact_selected_patch_names(self):
        for name in ('patch', 'gpatch', 'nbpatch'):
            tools = self.tools(name)
            for inspect_inputs in (False, True):
                with self.subTest(name=name, inspect_inputs=inspect_inputs):
                    self.assertEqual(_tools(tools, inspect_inputs=inspect_inputs), tools)

    def test_other_patch_names_and_gpatch_for_git_are_refused(self):
        for role, name in (('patch', 'other-patch'), ('patch', 'cc'),
                           ('patch', 'make'), ('git', 'gpatch')):
            tools = self.tools()
            tools[role]['path'] = str(self.root / name)
            with self.subTest(role=role, name=name), \
                    self.assertRaisesRegex(SDKError, 'utility|compiler|make'):
                _tools(tools, inspect_inputs=False)

    def test_gpatch_hash_and_executable_checks_remain_required(self):
        tools = self.tools('gpatch')
        path = Path(tools['patch']['path'])
        path.write_text('changed fixture bytes\n')
        with self.assertRaisesRegex(SDKError, 'utility changed'):
            _tools(tools, inspect_inputs=True)
        tools['patch']['sha256'] = sha256_file(path)
        path.chmod(0o644)
        with self.assertRaisesRegex(SDKError, 'utility changed'):
            _tools(tools, inspect_inputs=True)

    def test_gpatch_symlink_and_directory_are_refused(self):
        tools = self.tools('gpatch')
        path = Path(tools['patch']['path'])
        path.unlink()
        path.symlink_to('git')
        with self.assertRaisesRegex(SDKError, 'utility changed'):
            _tools(tools, inspect_inputs=True)
        path.unlink()
        path.mkdir()
        with self.assertRaisesRegex(SDKError, 'utility changed'):
            _tools(tools, inspect_inputs=True)

    def test_gpatch_still_requires_normalized_absolute_path_and_exact_roles(self):
        tools = self.tools('gpatch')
        for path in ('gpatch', str(self.root / 'child') + '/../gpatch'):
            changed = {**tools, 'patch': {**tools['patch'], 'path': path}}
            with self.subTest(path=path), self.assertRaisesRegex(SDKError, 'absolute|normalized'):
                _tools(changed, inspect_inputs=False)
        for changed in ({'patch': tools['patch']}, {**tools, 'cc': tools['git']}):
            with self.assertRaisesRegex(SDKError, 'exactly selected'):
                _tools(changed, inspect_inputs=False)


class SourceFixture(TemporaryCase):
    def setUp(self):
        super().setUp()
        self.ports = self.root / 'Ports'
        self.pkgsrc = self.ports / 'upstream/pkgsrc'
        self.ports.mkdir()
        self.pkgsrc.mkdir(parents=True)
        (self.root / 'inputs').mkdir()
        git(self.pkgsrc, 'init', '-q')
        write(self.pkgsrc / 'README', 'fixture pkgsrc tree\n')
        commit(self.pkgsrc)
        git(self.pkgsrc, 'remote', 'add', 'origin',
            'https://github.com/NetBSD/pkgsrc.git')
        pkgsrc_commit = git(self.pkgsrc, 'rev-parse', 'HEAD')
        git(self.ports, 'init', '-q')
        self.exporter = write(self.ports / 'scripts/prepare-pkgsrc.py',
            '# fixture exporter API\n'
            'def export_pkgsrc(root, destination, *, selection, runner, scratch):\n'
            '    runner([*selection.command("git"), "--version"])\n'
            '    from pathlib import Path\n'
            '    Path(scratch).mkdir()\n'
            '    (Path(scratch) / "pkgsrc.tar").write_bytes(b"fixture archive")\n'
            '    Path(destination).mkdir()\n'
            '    (Path(destination) / "fixture.txt").write_text("exported")\n'
            '    return ' + repr(pkgsrc_commit) + '\n')
        write(self.ports / 'pkgsrc/export.conf', '# empty fixture\n')
        write(self.ports / 'profiles/common-build-tools/bmake.md', 'Shared bmake 20260912 source\n')
        write(self.ports / 'profiles/common-build-tools/sources.tsv', 'fixture\t' + '0' * 64 + '\turl\n')
        write(self.ports / 'profiles/common-build-tools/patches/pkgsrc-bmake-20260912.patch',
              'diff --git a/bmake/Makefile b/bmake/Makefile\n')
        commit(self.ports, 'scripts', 'pkgsrc', 'profiles')
        pin = git(self.pkgsrc, 'rev-parse', 'HEAD')
        git(self.ports, 'update-index', '--add', '--cacheinfo', f'160000,{pin},upstream/pkgsrc')
        git(self.ports, '-c', 'user.name=SDK fixture', '-c', 'user.email=fixture@example.invalid',
            'commit', '-qm', 'Pin pkgsrc')
        git(self.ports, 'remote', 'add', 'origin',
            'https://github.com/oxtech-ember/EmberBSD-Ports.git')
        self.git = os.path.realpath(subprocess.check_output(['which', 'git'], text=True).strip())
        self.patch = os.path.realpath(subprocess.check_output(['which', 'patch'], text=True).strip())
        self.python = os.path.realpath(os.sys.executable)
        self.manifest_path = self.root / 'inputs/ports-manifest.json'
        self.descriptor_path = self.root / 'inputs/export-source.json'
        self.manifest = export_source_manifest(str(self.ports), self.git, str(self.manifest_path))
        self.source = describe_export_source(str(self.ports), self.manifest,
                                             git_path=self.git)
        self.source_path = self.descriptor_path
        write_json(self.source_path, self.source)
        self.source_ref = dict(path=str(self.source_path), sha256=sha256_file(self.source_path))
        self.work = self.root / 'sdk-work/pkgsrc-export'
        self.lock_root = self.root / 'sdk-locks'
        self.python_ref = dict(path=self.python, sha256=sha256_file(self.python))
        self.tools = {
            'git': dict(path=self.git, sha256=sha256_file(self.git)),
            'patch': dict(path=self.patch, sha256=sha256_file(self.patch)),
        }


class PkgsrcExportSourceTests(SourceFixture):
    def test_plan_selects_backup_policy_without_posix_path_selection(self):
        plan = create_export_plan(self.source_ref, str(self.work), str(self.lock_root),
                                  self.python_ref, self.tools, plan_path=str(self.root / 'inputs/export-plan.json'))
        self.assertEqual(plan['commands'][0]['env'].get('PATCH_VERSION_CONTROL'), 'none')
        changed = dict(plan)
        changed['commands'] = [dict(plan['commands'][0], env={
            key: value for key, value in plan['commands'][0]['env'].items()
            if key != 'PATCH_VERSION_CONTROL'})]
        with self.assertRaisesRegex(SDKError, 'fixed controller route'):
            validate_export_plan(changed, self.source, source_ref=self.source_ref,
                                 inspect_inputs=False)

    def test_shared_transfer_inventory_ignores_only_symlink_mode(self):
        source = {
            'regular': dict(kind='file', mode=0o755, size=4, sha256='a' * 64),
            'link': dict(kind='link', mode=0o777, target='regular'),
        }
        target = {
            'regular': dict(kind='file', mode=0o755, size=4, sha256='a' * 64),
            'link': dict(kind='link', mode=0o755, target='regular'),
        }
        self.assertTrue(transfer_inventory_equal(source, target))
        target['regular']['mode'] = 0o644
        self.assertFalse(transfer_inventory_equal(source, target))
        target['regular']['mode'] = 0o755
        target['link']['target'] = 'elsewhere'
        self.assertFalse(transfer_inventory_equal(source, target))

    def test_complete_source_manifest_binds_gitlink_and_export_inputs(self):
        rows = __import__('emberbsd_sdk.jsonio', fromlist=['read_json']).read_json(self.manifest_path)
        self.assertEqual(rows['upstream/pkgsrc']['kind'], 'gitlink')
        self.assertEqual(rows['upstream/pkgsrc']['commit'], git(self.pkgsrc, 'rev-parse', 'HEAD'))
        for relative in ('scripts/prepare-pkgsrc.py', 'pkgsrc/export.conf',
                         'profiles/common-build-tools/bmake.md',
                         'profiles/common-build-tools/sources.tsv'):
            self.assertEqual(rows[relative]['sha256'], sha256_file(self.ports / relative))
        self.assertEqual(validate_export_source(self.source, git_path=self.git), self.source)

    def test_changed_exporter_and_untracked_ignored_inputs_are_refused(self):
        self.exporter.write_text(self.exporter.read_text() + '# changed\n')
        with self.assertRaisesRegex(SDKError, 'source|manifest|changed'):
            validate_export_source(self.source, git_path=self.git)
        self.exporter.write_text(self.exporter.read_text().removesuffix('# changed\n'))
        (self.ports / 'pkgsrc/ignored-extra').mkdir(parents=True)
        (self.ports / 'pkgsrc/ignored-extra/Makefile').write_text('# untracked recipe\n')
        git(self.ports, 'config', 'core.excludesfile', str(write(self.root / 'ignore', 'pkgsrc/ignored-extra/\n')))
        with self.assertRaisesRegex(SDKError, 'complete|untracked|source'):
            validate_export_source(self.source, git_path=self.git)

    def test_gitlink_commit_must_match_clean_checked_out_pkgsrc(self):
        write(self.pkgsrc / 'README', 'new submodule content\n')
        commit(self.pkgsrc)
        with self.assertRaisesRegex(SDKError, 'pkgsrc|source|clean'):
            validate_export_source(self.source, git_path=self.git)

    def test_source_descriptor_must_name_the_official_ports_and_pkgsrc_repositories(self):
        for field in ('ports_source', 'pkgsrc_source'):
            with self.subTest(field=field):
                changed = dict(self.source)
                changed[field] = dict(self.source[field], repository='https://example.invalid/foreign')
                with self.assertRaisesRegex(SDKError, 'repository'):
                    validate_export_source(changed, git_path=self.git, inspect_inputs=False)

    def test_source_roots_must_have_the_official_git_origins(self):
        for root, repository in ((self.ports, 'https://github.com/oxtech-ember/EmberBSD-Ports.git'),
                                 (self.pkgsrc, 'https://github.com/NetBSD/pkgsrc.git')):
            with self.subTest(root=root.name):
                git(root, 'remote', 'set-url', 'origin', 'https://example.invalid/foreign')
                with self.assertRaisesRegex(SDKError, 'origin|repository'):
                    validate_export_source(self.source, git_path=self.git)
                git(root, 'remote', 'set-url', 'origin', repository)

    def test_run_export_cleans_exact_scratch_archive_after_drain(self):
        plan_path = self.root / 'inputs/export-plan.json'
        plan = create_export_plan(self.source_ref, str(self.work), str(self.lock_root),
                                  self.python_ref, self.tools, plan_path=str(plan_path))
        write_json(plan_path, plan)
        plan_ref = dict(path=str(plan_path), sha256=sha256_file(plan_path))
        self.work.parent.mkdir()
        self.lock_root.mkdir()

        def run_worker(argv, log, *, cwd, env, timeout_seconds):
            self.assertEqual(argv, plan['commands'][0]['argv'])
            self.assertEqual(cwd, plan['commands'][0]['cwd'])
            self.assertEqual(env, plan['commands'][0]['env'])
            self.assertGreater(timeout_seconds, 0)
            self.assertEqual(worker_main(str(self.source_path), self.source_ref['sha256'],
                                         str(plan_path)), 0)
            Path(log).write_bytes(b'')
            return subprocess.CompletedProcess(argv, 0)

        with patch('emberbsd_sdk.bootstrap_pkgsrc_export._sdk_identity',
                   return_value=plan['sdk_source']), \
                patch('emberbsd_sdk.bootstrap_pkgsrc_export.process.run_logged',
                      side_effect=run_worker):
            result = run_export(self.source_ref, plan_ref)
        self.assertEqual(result['status'], 'EXPORT_COMPLETED_PENDING_INDEPENDENT_REVIEW')
        self.assertTrue(result['inputs_revalidated_after_drain'])
        self.assertFalse(Path(plan['scratch_root']).exists())

    def test_plan_binds_exact_tools_and_lock_root_away_from_sources(self):
        plan = create_export_plan(self.source_ref, str(self.work), str(self.lock_root),
                                  self.python_ref, self.tools, plan_path=str(self.root / 'inputs/export-plan.json'))
        validate_export_plan(plan, self.source, source_ref=self.source_ref,
                             inspect_inputs=False)
        bad = dict(plan)
        bad['resources'] = dict(plan['resources'], lock_root=self.ports.parent.as_posix())
        with self.assertRaisesRegex(SDKError, 'lock root|overlap'):
            validate_export_plan(bad, self.source, source_ref=self.source_ref,
                                 inspect_inputs=False)

    def test_plan_refuses_system_make_or_compiler_path_aliases(self):
        for role, path in (('git', '/usr/bin/make'), ('patch', '/usr/bin/cc')):
            tools = dict(self.tools)
            tools[role] = dict(path=path, sha256='0' * 64)
            with self.subTest(role=role), self.assertRaisesRegex(SDKError, 'tool|utility|compiler|make'):
                create_export_plan(self.source_ref, str(self.work), str(self.lock_root),
                                   self.python_ref, tools, plan_path=str(self.root / 'inputs/export-plan.json'))

    def test_worker_imports_only_hashed_current_ports_exporter(self):
        plan_path = self.root / 'inputs/export-plan.json'
        plan = create_export_plan(self.source_ref, str(self.work), str(self.lock_root),
                                  self.python_ref, self.tools, plan_path=str(plan_path))
        write_json(plan_path, plan)
        plan_ref = dict(path=str(plan_path), sha256=sha256_file(plan_path))
        self.work.mkdir(parents=True)
        (self.work / 'logs').mkdir()
        write_json(plan['result_path'], dict(format='emberbsd-bootstrap-pkgsrc-export-result',
            version=1, scope='pkgsrc-source-export', status='RUNNING',
            source_descriptor=self.source_ref, export_plan=plan_ref, commands=[],
            export_root=plan['export_root'], output_inventory=None, source_proof=None,
            all_commands_drained=False, inputs_revalidated_after_drain=False,
            acceptance='PENDING_INDEPENDENT_REVIEW', scope_boundary=
            'current source-bound Ports pkgsrc export only; no package build or NativeToolset qualification'))
        with patch('emberbsd_sdk.bootstrap_pkgsrc_export._sdk_identity', return_value=plan['sdk_source']):
            self.assertEqual(worker_main(str(self.source_path), self.source_ref['sha256'], str(plan_path)), 0)
        result = read_json(plan['result_path'])
        self.assertEqual(result['status'], 'EXPORT_COMPLETED_PENDING_INDEPENDENT_REVIEW')
        self.assertEqual(result['exported_pkgsrc_commit'], git(self.pkgsrc, 'rev-parse', 'HEAD'))
        self.assertEqual(len(result['commands']), 1)
        self.assertEqual(result['commands'][0]['argv'], [self.git, '--version'])
        self.assertEqual(snapshot_tree(plan['export_root']), read_json(plan['inventory_path']))
        self.assertEqual(result['source_proof']['ports_source'], self.source['ports_source'])

    def test_transfer_acceptance_binds_independent_export_receipt_and_target_tree(self):
        output = self.root / 'target/pkgsrc'
        write(output / 'devel/bmake/Makefile', 'pinned recipe\n')
        (output / 'devel/bmake/Makefile.link').symlink_to('Makefile')
        inventory_path = self.root / 'target/inventory.json'
        write_json(inventory_path, snapshot_tree(str(output)))
        source_inventory = self.root / 'source/inventory.json'
        source_inventory.parent.mkdir()
        source_rows = snapshot_tree(str(output))
        source_rows['devel/bmake/Makefile.link']['mode'] = 0o755
        write_json(source_inventory, source_rows)
        source_result = dict(path=str(self.root / 'source/result.json'), sha256='1' * 64)
        source_plan = dict(path=str(self.root / 'source/plan.json'), sha256='2' * 64)
        source_descriptor = dict(path=str(self.root / 'source/descriptor.json'), sha256='3' * 64)
        export_acceptance = dict(
            format='emberbsd-bootstrap-pkgsrc-export-acceptance', version=1,
            scope='pkgsrc-source-export', status='PASS',
            evidence_level='independent-source-export-review',
            producer_result=source_result, export_plan=source_plan,
            source_descriptor=source_descriptor, output_root='/mac/export/pkgsrc',
            output_inventory=dict(path='/mac/export/inventory.json',
                                  sha256=sha256_file(str(source_inventory))),
            source_proof={'ports_source': self.source['ports_source']},
            commands=[], controller_command={}, all_commands_drained=True,
            inputs_revalidated_after_drain=True,
            scope_boundary='independently reviewed current Ports source export only; no pkgsrc build or NativeToolset qualification')
        acceptance_path = self.root / 'target/export-acceptance.json'
        write_json(acceptance_path, export_acceptance)
        acceptance_ref = dict(path=str(acceptance_path), sha256=sha256_file(str(acceptance_path)))
        transfer_report = self.root / 'target/transfer-report.json'
        write_json(transfer_report, {'method': 'fixture', 'status': 'PASS'})
        receipt = dict(
            format='emberbsd-bootstrap-pkgsrc-transfer-acceptance', version=1,
            scope='pkgsrc-source-transfer', status='PASS',
            evidence_level='independent-target-source-revalidation',
            source_export_acceptance=acceptance_ref,
            source_inventory=dict(path=str(source_inventory), sha256=sha256_file(str(source_inventory))),
            output_root=str(output),
            output_inventory=dict(path=str(inventory_path), sha256=sha256_file(str(inventory_path))),
            transfer_report=dict(path=str(transfer_report), sha256=sha256_file(str(transfer_report))),
            normalization_policy='symlink-mode-ignored-v1',
            source_proof=export_acceptance['source_proof'],
            inputs_revalidated_after_transfer=True,
            scope_boundary='independently revalidated current pkgsrc source tree at target; no package build or NativeToolset qualification')
        receipt_path = self.root / 'target/transfer-acceptance.json'
        write_json(receipt_path, receipt)
        validated_ref, validated = validate_export_transfer_acceptance(
            str(receipt_path), sha256_file(str(receipt_path)),
            export_acceptance_ref=acceptance_ref)
        self.assertEqual(validated_ref['sha256'], sha256_file(str(receipt_path)))
        self.assertEqual(validated['output_root'], str(output))

        write(output / 'devel/bmake/Makefile', 'changed after transfer\n')
        with self.assertRaisesRegex(SDKError, 'inventory|transfer|target'):
            validate_export_transfer_acceptance(str(receipt_path), sha256_file(str(receipt_path)),
                                                export_acceptance_ref=acceptance_ref)
        output.rename(self.root / 'target/pkgsrc-retired')
        source_inventory.rename(self.root / 'source/inventory-retired.json')
        _, offline = validate_export_transfer_acceptance(
            str(receipt_path), sha256_file(str(receipt_path)),
            export_acceptance_ref=acceptance_ref, inspect_inputs=False)
        self.assertEqual(offline['output_root'], str(output))
