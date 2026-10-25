# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Release checks use temporary Git roots and synthetic package artifacts."""

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
SDK_SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SDK_SOURCE / 'src'))
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.inventory import sha256_file
from emberbsd_sdk.jsonio import json_bytes, read_json
from emberbsd_sdk.ports_plan import lock_graph, make_plan, record_result
from emberbsd_sdk import ports_release as release
from test_ports_plan import graph


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sdk-ports-release-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repos = {}
        for name in release.COMPONENTS:
            root = self.root / name
            root.mkdir()
            self.repos[name] = root
            self.git(name, 'init', '-q')
            # Global excludes must not hide source files in these Git fixtures.
            self.git(name, 'config', 'core.excludesFile', os.devnull)
            self.git(name, 'config', 'user.name', 'Release Test')
            self.git(name, 'config', 'user.email', 'release@example.invalid')
            (root / 'source').write_text(name, encoding='utf-8')
            if name == 'os':
                version_file = root / release.VERSION_FILE
                version_file.parent.mkdir(parents=True)
                version_file.write_text('EMBER_VERSION?=\t1.2.3\n_NETBSD_ABI=1100000000\n', encoding='utf-8')
            self.commit(name)
        self.sources_path = self.write('release-sources.json', dict(schema='ports-release-sources-v1', version='1.2.3',
            sources={name: self.git(name, 'rev-parse', 'HEAD') for name in release.COMPONENTS}))
        candidate = graph()
        candidate['context']['shared_inputs']['release-sources'] = sha256_file(self.sources_path)
        candidate['provenance']['revision'] = self.git('ports', 'rev-parse', 'HEAD')
        locked = lock_graph(candidate)
        self.plan = make_plan(locked, locked)
        self.plan_path = self.write('plan.json', self.plan)
        self.receipts = []
        self.artifacts = []
        for node in candidate['nodes']:
            artifact = self.root / (node['id'] + '.tgz')
            artifact.write_text(node['id'], encoding='utf-8')
            log = self.root / (node['id'] + '.log')
            log.write_text('producer completed', encoding='utf-8')
            receipt = record_result(self.plan, node['id'], 'success', str(log), [str(artifact)])
            self.receipts.append(self.write(node['id'] + '.json', receipt))
            self.artifacts.append(artifact)

    def git(self, component, *args):
        return subprocess.check_output(['git', '-C', str(self.repos[component]), *args], stderr=subprocess.PIPE).decode().strip()

    def commit(self, component):
        self.git(component, 'add', '.')
        self.git(component, 'commit', '-qm', 'Fixture input')

    def write(self, name, value):
        path = self.root / name
        path.write_bytes(json_bytes(value))
        return path

    def create(self, **kwargs):
        return release.create_release(self.sources_path, self.repos, [self.plan_path], self.receipts, **kwargs)

    def no_tags(self):
        for name in self.repos:
            self.assertEqual(self.git(name, 'tag', '--list'), '')

    def checks(self, code, *args):
        return self.write('checks.json', dict(schema='ports-release-checks-v1', checks=[
            dict(name='regression', component='sdk', argv=[sys.executable, '-c', code, *map(str, args)])]))

    def test_prepare_commits_only_version_and_preserves_abi(self):
        before = {name: self.git(name, 'rev-parse', 'HEAD') for name in self.repos}
        output = self.root / 'prepared.json'
        result = release.prepare_release('2.0-rc1', self.repos, output)
        self.assertEqual(result['version'], '2.0-rc1')
        self.assertEqual(result['sources']['ports'], before['ports'])
        self.assertEqual(result['sources']['sdk'], before['sdk'])
        self.assertEqual(self.git('os', 'diff-tree', '--no-commit-id', '--name-only', '-r', 'HEAD'), release.VERSION_FILE)
        text = (self.repos['os'] / release.VERSION_FILE).read_text()
        self.assertEqual(text, 'EMBER_VERSION?=\t2.0-rc1\n_NETBSD_ABI=1100000000\n')
        self.assertEqual(read_json(output), result)
        self.assertEqual(self.git('os', 'status', '--porcelain'), '')
        self.no_tags()

    def test_prepare_preserves_crlf_and_refuses_symlink_output(self):
        path = self.repos['os'] / release.VERSION_FILE
        path.write_bytes(b'EMBER_VERSION?= 1.2.3\r\n# ABI stays unchanged\r\n')
        self.commit('os')
        output = self.root / 'new.json'
        release.prepare_release('2.0', self.repos, output)
        self.assertEqual(path.read_bytes(), b'EMBER_VERSION?= 2.0\r\n# ABI stays unchanged\r\n')
        alias = self.root / 'alias.json'
        alias.symlink_to(self.root / 'missing.json')
        with self.assertRaisesRegex(SDKError, 'symlink'):
            release.prepare_release('3.0', self.repos, alias)
        self.assertFalse((self.root / 'missing.json').exists())

    def test_prepare_commit_failure_preserves_edit_without_tags(self):
        before = self.git('os', 'rev-parse', 'HEAD')
        original = release._git
        def fault(root, *args, **kwargs):
            if args[0] == 'commit':
                raise SDKError('injected commit refusal')
            return original(root, *args, **kwargs)
        output = self.root / 'not-created.json'
        with patch.object(release, '_git', side_effect=fault):
            with self.assertRaisesRegex(SDKError, 'preserve and inspect the OS version edit'):
                release.prepare_release('2.0', self.repos, output)
        self.assertEqual(self.git('os', 'rev-parse', 'HEAD'), before)
        self.assertIn('2.0', (self.repos['os'] / release.VERSION_FILE).read_text())
        self.assertFalse(output.exists())
        self.no_tags()

    def test_prepare_same_version_and_output_preflight(self):
        before = self.git('os', 'rev-parse', 'HEAD')
        output = self.root / 'prepared.json'
        release.prepare_release('1.2.3', self.repos, output)
        self.assertEqual(self.git('os', 'rev-parse', 'HEAD'), before)
        with self.assertRaisesRegex(SDKError, 'new file'):
            release.prepare_release('2.0', self.repos, output)
        with self.assertRaisesRegex(SDKError, 'outside'):
            release.prepare_release('2.0', self.repos, self.repos['os'] / 'output.json')
        self.assertEqual(self.git('os', 'rev-parse', 'HEAD'), before)
        self.assertIn('1.2.3', (self.repos['os'] / release.VERSION_FILE).read_text())

    def test_prepare_refuses_oversize_source_without_truncation(self):
        path = self.repos['os'] / release.VERSION_FILE
        content = b'EMBER_VERSION?=1.2.3\n' + b'#' * (2 * 1024 * 1024)
        path.write_bytes(content)
        self.commit('os')
        with self.assertRaisesRegex(SDKError, 'size limit'):
            release.prepare_release('2', self.repos, self.root / 'new.json')
        self.assertEqual(path.read_bytes(), content)

    def test_ignored_untracked_version_cannot_bind_a_release(self):
        self.git('os', 'rm', '--cached', release.VERSION_FILE)
        (self.repos['os'] / '.gitignore').write_text(release.VERSION_FILE + '\n', encoding='utf-8')
        self.commit('os')
        self.assertEqual(self.git('os', 'status', '--porcelain'), '')
        with self.assertRaisesRegex(SDKError, 'tracked in HEAD'):
            release.prepare_release('1.2.3', self.repos, self.root / 'new.json')
        declared = read_json(self.sources_path)
        declared['sources']['os'] = self.git('os', 'rev-parse', 'HEAD')
        self.sources_path.write_bytes(json_bytes(declared))
        with self.assertRaisesRegex(SDKError, 'tracked in HEAD'):
            self.create()
        self.no_tags()

    def test_prepare_refuses_ambiguous_version_source(self):
        path = self.repos['os'] / release.VERSION_FILE
        path.write_text('EMBER_VERSION?=1\nEMBER_VERSION?=2\n', encoding='utf-8')
        self.commit('os')
        with self.assertRaisesRegex(SDKError, 'exactly one'):
            release.prepare_release('3', self.repos, self.root / 'new.json')
        self.no_tags()

    def test_version_and_prepared_hash_must_match(self):
        declared = read_json(self.sources_path)
        declared['version'] = '9.0'
        self.sources_path.write_bytes(json_bytes(declared))
        with self.assertRaisesRegex(SDKError, 'EMBER_VERSION'):
            self.create()
        declared['version'] = '1.2.3'
        self.sources_path.write_bytes(json_bytes(declared) + b' ')
        with self.assertRaisesRegex(SDKError, 'prepared release sources hash'):
            self.create()
        self.no_tags()

    def test_cli_prepare_and_finalize(self):
        command = [sys.executable, '-B', str(SDK_SOURCE / 'scripts/ports-release.py')]
        repos = [arg for name, path in self.repos.items() for arg in ('--repo', name + '=' + str(path))]
        result = subprocess.run(command + ['prepare', '--version', '1.2.3', '--output', str(self.root / 'cli-sources.json'), *repos], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipts = [arg for path in self.receipts for arg in ('--receipt', str(path))]
        result = subprocess.run(command + ['finalize', '--sources', str(self.sources_path), '--plan', str(self.plan_path), *repos, *receipts], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(json.loads(result.stdout)['created_refs']), 3)

    def test_three_annotated_tags_share_portable_manifest(self):
        result = self.create()
        self.assertEqual(len(result['created_refs']), 3)
        for name in self.repos:
            self.assertEqual(self.git(name, 'cat-file', '-t', 'emberbsd-1.2.3'), 'tag')
            self.assertEqual(self.git(name, 'rev-parse', 'emberbsd-1.2.3^{commit}'), result['manifest']['sources'][name])
            message = self.git(name, 'cat-file', '-p', 'emberbsd-1.2.3').split('\n\n', 1)[1]
            self.assertEqual(json.loads(message), result['manifest'])
        self.assertNotIn(str(self.root), json.dumps(result['manifest']))
        self.assertEqual(result['manifest']['qualification'], 'artifact-integrity-only')

    def test_missing_unchanged_candidate_evidence_refuses(self):
        self.assertEqual(self.plan['impacted'], [])
        self.receipts.pop()
        with self.assertRaisesRegex(SDKError, 'every candidate'):
            self.create()
        self.no_tags()

    def test_failure_stale_and_modified_artifact_refuse(self):
        path = self.receipts[-1]
        original = read_json(path)
        failed = dict(original, outcome='failure', artifacts=[])
        stale = dict(original, plan_sha256='0' * 64)
        for receipt in (failed, stale):
            path.write_bytes(json_bytes(receipt))
            with self.assertRaises(SDKError):
                self.create()
            self.no_tags()
        path.write_bytes(json_bytes(original))
        self.artifacts[-1].write_text('changed', encoding='utf-8')
        with self.assertRaisesRegex(SDKError, 'hash mismatch'):
            self.create()
        self.no_tags()

    def test_dirty_and_changed_ports_commit_refuse(self):
        (self.repos['ports'] / 'source').write_text('changed', encoding='utf-8')
        with self.assertRaisesRegex(SDKError, 'clean Git'):
            self.create()
        self.no_tags()
        self.commit('ports')
        with self.assertRaisesRegex(SDKError, 'source commits'):
            self.create()
        self.no_tags()

    def test_duplicate_repos_and_bad_version_refuse(self):
        for version in ('', 'v1', '1/2', '1..2', '1 lock', '1.lock', '-1', '1\n2'):
            with self.subTest(version=version), self.assertRaises(SDKError):
                release.prepare_release(version, self.repos, self.root / 'new-sources.json')
        repos = dict(self.repos, sdk=self.repos['os'])
        with self.assertRaisesRegex(SDKError, 'distinct'):
            release.create_release(self.sources_path, repos, [self.plan_path], self.receipts)
        self.no_tags()

    def test_existing_tag_is_preserved_and_no_other_refs_created(self):
        self.git('ports', 'tag', 'emberbsd-1.2.3')
        original = self.git('ports', 'rev-parse', 'emberbsd-1.2.3')
        with self.assertRaisesRegex(SDKError, 'already exists'):
            self.create()
        self.assertEqual(self.git('ports', 'rev-parse', 'emberbsd-1.2.3'), original)
        self.assertEqual(self.git('os', 'tag', '--list'), '')
        self.assertEqual(self.git('sdk', 'tag', '--list'), '')

    def test_duplicate_plan_and_scope_refuse(self):
        with self.assertRaisesRegex(SDKError, 'duplicate release plan'):
            release.create_release(self.sources_path, self.repos, [self.plan_path, self.plan_path], self.receipts)
        candidate = deepcopy(self.plan['candidate']['graph'])
        candidate['nodes'][0]['version'] = '2'
        other = self.write('other.json', make_plan(self.plan['baseline'], lock_graph(candidate)))
        with self.assertRaisesRegex(SDKError, 'ambiguous'):
            release.create_release(self.sources_path, self.repos, [self.plan_path, other], self.receipts)
        self.no_tags()

    def test_empty_plans_and_graphs_refuse(self):
        with self.assertRaisesRegex(SDKError, 'nonempty'):
            release.create_release(self.sources_path, self.repos, [], [])
        candidate = deepcopy(self.plan['candidate']['graph'])
        candidate['nodes'] = []
        empty = lock_graph(candidate)
        self.plan_path.write_bytes(json_bytes(make_plan(empty, empty)))
        with self.assertRaisesRegex(SDKError, 'nonempty'):
            self.create()
        self.no_tags()

    def test_passing_check_qualifies_and_failed_check_creates_no_tags(self):
        checks = self.checks('print("checked")')
        result = self.create(checks_path=checks, logs=self.root / 'logs')
        self.assertEqual(result['manifest']['qualification'], 'explicit-checks-passed')
        self.assertEqual(len(result['manifest']['checks']), 1)
        self.assertNotIn(str(self.root), json.dumps(result['manifest']))

    def test_failed_check_creates_no_tags(self):
        checks = self.checks('print("failed"); raise SystemExit(7)')
        with self.assertRaises(SDKError):
            self.create(checks_path=checks, logs=self.root / 'logs')
        self.no_tags()
        self.assertIn('failed', (self.root / 'logs' / '000-regression.log').read_text())

    def test_check_changes_artifacts_refuses(self):
        checks = self.checks('from pathlib import Path; import sys; Path(sys.argv[1]).write_text("changed")', self.artifacts[0])
        with self.assertRaisesRegex(SDKError, 'hash mismatch'):
            self.create(checks_path=checks, logs=self.root / 'logs')
        self.no_tags()

    def test_check_changes_sources_or_head_refuses(self):
        checks = self.checks('from pathlib import Path; Path("source").write_text("changed")')
        with self.assertRaisesRegex(SDKError, 'clean Git'):
            self.create(checks_path=checks, logs=self.root / 'logs')
        self.no_tags()
        (self.repos['sdk'] / 'source').write_text('sdk', encoding='utf-8')
        checks = self.checks('import subprocess; subprocess.run(["git","commit","--allow-empty","-qm","Changed HEAD"],check=True)')
        with self.assertRaisesRegex(SDKError, 'changed during validation'):
            self.create(checks_path=checks, logs=self.root / 'more-logs')
        self.no_tags()

    def test_check_changes_receipt_bytes_refuses(self):
        checks = self.checks('from pathlib import Path; import sys; p=Path(sys.argv[1]); p.write_bytes(p.read_bytes()+b" ")', self.receipts[0])
        with self.assertRaisesRegex(SDKError, 'changed during validation'):
            self.create(checks_path=checks, logs=self.root / 'logs')
        self.no_tags()

    def test_logs_must_be_new_and_outside_repositories(self):
        checks = self.checks('pass')
        with self.assertRaisesRegex(SDKError, 'outside'):
            self.create(checks_path=checks, logs=self.repos['sdk'] / 'logs')
        with self.assertRaisesRegex(SDKError, 'outside'):
            self.create(checks_path=checks, logs=self.root)
        existing = self.root / 'existing-logs'
        existing.mkdir()
        with self.assertRaises(FileExistsError):
            self.create(checks_path=checks, logs=existing)
        self.no_tags()

    def test_partial_ref_failure_preserves_and_reports_created_refs(self):
        original = release._git
        def fault(root, *args, **kwargs):
            if args[0] == 'update-ref' and Path(root) == self.repos['ports']:
                raise SDKError('injected second ref failure')
            return original(root, *args, **kwargs)
        with patch.object(release, '_git', side_effect=fault):
            with self.assertRaises(release.ReleaseError) as failure:
                self.create()
        self.assertEqual([r['component'] for r in failure.exception.created_refs], ['os'])
        self.assertEqual(self.git('os', 'tag', '--list'), 'emberbsd-1.2.3')
        self.assertEqual(self.git('ports', 'tag', '--list'), '')
        self.assertEqual(self.git('sdk', 'tag', '--list'), '')

    def test_interrupted_successful_ref_is_reconciled(self):
        original = release._git
        def fault(root, *args, **kwargs):
            result = original(root, *args, **kwargs)
            if args[0] == 'update-ref':
                raise SDKError('injected error after ref write')
            return result
        with patch.object(release, '_git', side_effect=fault):
            with self.assertRaises(release.ReleaseError) as failure:
                self.create()
        self.assertEqual([r['component'] for r in failure.exception.created_refs], ['os'])
        self.assertEqual(self.git('os', 'tag', '--list'), 'emberbsd-1.2.3')


if __name__ == '__main__':
    unittest.main()
