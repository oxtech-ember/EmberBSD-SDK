# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Map successful Ports state rows to an existing producer graph."""

from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk import package_publish
from emberbsd_sdk.package_publish import packages_from_state


def graph(nodes):
    return dict(schema='ports-graph-v1', nodes=[
        dict(id=identity, version=version, input_sha256='a' * 64, dependencies=[])
        for identity, version in nodes
    ])


class PackagesFromStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / 'state'
        self.state.mkdir()
        self.graph_path = self.root / 'graph.json'
        self.packages = self.root / 'All'
        self.packages.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def write_state(self, *, packages=(), done=(), failed=(), blocked=(), skipped=()):
        for name, rows in (('packages', packages), ('done', done), ('failed', failed),
                           ('blocked', blocked), ('skipped', skipped)):
            (self.state / name).write_text(''.join(row + '\n' for row in rows), encoding='utf-8')

    def target_spec(self):
        self.graph_path.write_text(json.dumps(graph([
            ('target:net/app', '1.2'),
            ('target:net/failed', '2.0'),
            ('target:net/blocked', '3.0'),
            ('target:net/skipped', '4.0'),
            ('host:devel/generator', '5.0'),
        ])), encoding='utf-8')
        return dict(schema='emberbsd.package-build.v1', repositories=[dict(
            id='target', host=None, target={'arch': 'aarch64'}, graph=str(self.graph_path),
            roots=['net/original'], packages={'target:net/original': '/old.tgz'})])

    def test_selects_only_matching_successes_and_preserves_exact_graph_identity(self):
        spec = self.target_spec()
        self.write_state(
            packages=['net/app app-1.2', 'net/failed failed-2.0', 'net/blocked blocked-3.0',
                      'net/skipped skipped-4.0', 'net/notdone notdone-1.0',
                      'net/wrong-name actual-1.0', 'devel/generator@native generator-5.0'],
            done=['net/app app-1.2 ' + '1' * 64, 'net/failed failed-2.0 ' + '2' * 64,
                  'net/blocked blocked-3.0 ' + '3' * 64, 'net/skipped skipped-4.0 ' + '4' * 64,
                  'net/wrong-name different-1.0 ' + '5' * 64,
                  'devel/generator@native generator-5.0 ' + '6' * 64],
            failed=['net/failed failed-2.0'], blocked=['net/blocked blocked-3.0'],
            skipped=['net/skipped skipped-4.0'])

        result, checksums = packages_from_state(spec, self.state, self.packages)

        app_path = str((self.packages / 'app-1.2.tgz').absolute())
        self.assertEqual(result['repositories'][0]['packages'], {'target:net/app': app_path})
        self.assertEqual(result['repositories'][0]['roots'], ['net/app', 'net/original'])
        self.assertEqual(checksums, {app_path: '1' * 64})
        self.assertNotIn('host:devel/generator', result['repositories'][0]['packages'])

    def test_native_role_uses_native_state_root_and_host_graph_id(self):
        self.graph_path.write_text(json.dumps(graph([('host:devel/generator', '5.0')])), encoding='utf-8')
        spec = dict(schema='emberbsd.package-build.v1', repositories=[dict(
            id='native', host={'os': 'Darwin'}, target=None, graph=str(self.graph_path),
            roots=[], packages={})])
        self.write_state(packages=['devel/generator@native generator-5.0'],
                         done=['devel/generator@native generator-5.0 ' + 'a' * 64])

        result, checksums = packages_from_state(spec, self.state, self.packages)

        path = str((self.packages / 'generator-5.0.tgz').absolute())
        self.assertEqual(result['repositories'][0]['packages'], {'host:devel/generator': path})
        self.assertEqual(result['repositories'][0]['roots'], ['devel/generator@native'])
        self.assertEqual(checksums, {path: 'a' * 64})

    def test_latest_successful_rebuild_supplies_package_name_and_checksum(self):
        self.graph_path.write_text(json.dumps(graph([('target:net/app', '2.0')])), encoding='utf-8')
        spec = dict(schema='emberbsd.package-build.v1', repositories=[dict(
            id='target', host=None, target={'arch': 'aarch64'}, graph=str(self.graph_path),
            roots=[], packages={})])
        self.write_state(packages=['net/app app-2.0'],
                         done=['net/app app-1.0 ' + '1' * 64,
                               'net/app app-2.0 ' + '2' * 64])

        result, checksums = packages_from_state(spec, self.state, self.packages)

        path = str((self.packages / 'app-2.0.tgz').absolute())
        self.assertEqual(result['repositories'][0]['packages'], {'target:net/app': path})
        self.assertEqual(checksums, {path: '2' * 64})

    def test_missing_or_mismatched_graph_version_is_rejected(self):
        for nodes in ([], [('target:net/app', '9.9')]):
            with self.subTest(nodes=nodes):
                spec = self.target_spec()
                self.graph_path.write_text(json.dumps(graph(nodes)), encoding='utf-8')
                self.write_state(packages=['net/app app-1.2'], done=['net/app app-1.2 ' + 'b' * 64])
                with self.assertRaises(SDKError):
                    packages_from_state(spec, self.state, self.packages)

    def test_rejects_invalid_state_and_multiple_repositories(self):
        spec = self.target_spec()
        self.write_state(packages=['net/app app-1.2'], done=['net/app app-1.2 not-a-sha'])
        with self.assertRaisesRegex(SDKError, 'SHA256'):
            packages_from_state(spec, self.state, self.packages)

        spec['repositories'].append(deepcopy(spec['repositories'][0]))
        with self.assertRaisesRegex(SDKError, 'exactly one'):
            packages_from_state(spec, self.state, self.packages)

    def test_does_not_mutate_input_spec(self):
        spec = self.target_spec()
        original = deepcopy(spec)
        self.write_state(packages=['net/app app-1.2'], done=['net/app app-1.2 ' + 'c' * 64])

        packages_from_state(spec, self.state, self.packages)

        self.assertEqual(spec, original)

    def test_unowned_workdir_result_does_not_delete_existing_release(self):
        self.graph_path.write_text(json.dumps(graph([('target:net/app', '1.0')])), encoding='utf-8')
        spec = dict(schema='emberbsd.package-build.v1', release_id='fixture', created_at=1,
                    repositories=[dict(id='target', host=None, target={'arch': 'aarch64'},
                                       graph=str(self.graph_path), roots=[], packages={})])
        workdir = self.root / 'publisher-work'
        release = workdir / 'release'
        release.mkdir(parents=True)
        important = release / 'important'
        important.write_bytes(b'keep this unrelated output')
        (workdir / 'result.json').write_text('{"unrelated": true}', encoding='utf-8')

        with patch.object(package_publish, 'fetch_release') as fetch:
            with self.assertRaisesRegex(SDKError, 'not owned'):
                package_publish.update_packages(Mock(), Mock(), Mock(), spec,
                    self.root / 'ports', workdir, Mock(), Mock())

        fetch.assert_not_called()
        self.assertTrue(important.is_file())
        self.assertEqual(important.read_bytes(), b'keep this unrelated output')


if __name__ == '__main__':
    unittest.main()
