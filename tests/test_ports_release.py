# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Release preparation and tagging use temporary Git roots."""

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
from emberbsd_sdk import ports_release as release
from test_core import git_environment


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

    def git(self, component, *args):
        return subprocess.check_output(['git', '-C', str(self.repos[component]), *args], stderr=subprocess.PIPE, env=git_environment()).decode().strip()

    def commit(self, component):
        self.git(component, 'add', '.')
        self.git(component, 'commit', '-qm', 'Fixture input')

    def write(self, name, value):
        path = self.root / name
        path.write_bytes(json_bytes(value))
        return path

    def create(self):
        return release.create_release(self.sources_path, self.repos)

    def no_tags(self):
        for name in self.repos:
            self.assertEqual(self.git(name, 'tag', '--list'), '')

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
        self.no_tags()

    def test_cli_prepare_and_finalize(self):
        command = [sys.executable, '-B', str(SDK_SOURCE / 'scripts/ports-release.py')]
        repos = [arg for name, path in self.repos.items() for arg in ('--repo', name + '=' + str(path))]
        result = subprocess.run(command + ['prepare', '--version', '1.2.3', '--output', str(self.root / 'cli-sources.json'), *repos], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run(command + ['finalize', '--sources', str(self.sources_path), *repos], capture_output=True, text=True)
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
        self.assertEqual(set(result['manifest']), {'schema', 'version', 'sources_sha256', 'sources', 'manifest_sha256'})
        self.assertEqual(result['manifest']['sources_sha256'], sha256_file(self.sources_path))

    def test_finalize_needs_no_build_evidence(self):
        command = [sys.executable, '-B', str(SDK_SOURCE / 'scripts/ports-release.py'), 'finalize']
        repos = [arg for name, path in self.repos.items() for arg in ('--repo', name + '=' + str(path))]
        for option in ('--plan', '--receipt', '--checks', '--logs'):
            with self.subTest(option=option):
                result = subprocess.run(command + ['--sources', str(self.sources_path), *repos, option, str(self.root)],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 2)
                self.assertIn('unrecognized arguments', result.stderr)
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
            release.create_release(self.sources_path, repos)
        self.no_tags()

    def test_existing_tag_is_preserved_and_no_other_refs_created(self):
        self.git('ports', 'tag', 'emberbsd-1.2.3')
        original = self.git('ports', 'rev-parse', 'emberbsd-1.2.3')
        with self.assertRaisesRegex(SDKError, 'already exists'):
            self.create()
        self.assertEqual(self.git('ports', 'rev-parse', 'emberbsd-1.2.3'), original)
        self.assertEqual(self.git('os', 'tag', '--list'), '')
        self.assertEqual(self.git('sdk', 'tag', '--list'), '')

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
