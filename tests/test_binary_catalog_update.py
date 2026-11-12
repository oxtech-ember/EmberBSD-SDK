# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Sparse composition reuses authenticated current package metadata."""

from copy import deepcopy
import hashlib
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_binary_catalog import BinaryReleaseFixture, graph
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.binary_release import load_catalogs
from emberbsd_sdk.jsonio import json_bytes
from emberbsd_sdk.release_cli import seal_release


class BinaryCatalogUpdateTests(BinaryReleaseFixture):
    def baseline(self):
        repo = self.repository()
        release = self.compose()
        return repo, release, self.work / 'release'

    def load(self, directory):
        return load_catalogs(self.crypto, directory / 'release.p7m', directory,
                             self.ca.certificate, ())

    def test_addition_reuses_old_packages_without_old_payload_bodies(self):
        repo, _, current = self.baseline()
        for package in (current / 'arm64').glob('*.tgz'):
            package.unlink()

        value = graph()
        value['nodes'].append(dict(id='target:net/new', version='1.0',
            input_sha256='c' * 64, dependencies=[dict(id='target:devel/lib', kind='runtime')]))
        self.graph_path.write_bytes(json_bytes(value))
        repo['sets'] = []
        repo['roots'] = ['net/new']
        repo['packages'] = {'target:net/new': self.package('net/new', depends=('lib>=1',))}
        self.spec['repositories'] = [repo]
        self.spec.update(release_id='addition', created_at=2)

        result = self.compose(current=current, output=self.work / 'addition')

        _, catalogs = self.load(self.work / 'addition')
        self.assertEqual({item['root'] for item in catalogs[0]['packages']},
                         {'net/app', 'net/other', 'devel/lib', 'devel/lib-devel', 'net/new'})
        self.assertTrue((self.work / 'addition/arm64/new-1.0.tgz').is_file())
        self.assertFalse((self.work / 'addition/arm64/app-1.0.tgz').exists())
        new = next(item for item in catalogs[0]['packages'] if item['root'] == 'net/new')
        artifact = next(item for item in result['artifacts'] if item['id'] == new['artifact'])
        self.assertEqual(artifact['depends'], [next(item['artifact'] for item in catalogs[0]['packages']
                                                    if item['root'] == 'devel/lib')])

    def _add_bootstrap_artifact(self, directory, release):
        from emberbsd_sdk.binary_release import _artifact
        host = dict(os='Darwin', arch='arm64', abi='macos-27', runtime='libSystem')
        repo = dict(host=host, target=None)
        payload = directory / 'bootstrap' / 'tool.tar'
        payload.parent.mkdir()
        payload.write_bytes(b'unchanged bootstrap bytes\n')
        draft = deepcopy(release)
        draft['artifacts'].append(_artifact(repo, 'bootstrap-tool', 'bootstrap/tool.tar',
            'bootstrap', size=payload.stat().st_size))
        new_envelope = directory / 'release-with-bootstrap.p7m'
        seal_release(self.crypto, json_bytes(draft), directory, self.leaf,
                     self.ca.certificate, new_envelope)
        (directory / 'release.p7m').write_bytes(new_envelope.read_bytes())

    def source_graph_changed_addition(self, *, depends=('app>=1',)):
        repo, release, current = self.baseline()
        for package in (current / 'arm64').glob('*.tgz'):
            package.unlink()
        value = graph()
        # The current archive is unchanged. A different source profile no
        # longer records its dependency, which remains in the signed release.
        next(node for node in value['nodes'] if node['id'] == 'target:net/app')['dependencies'] = []
        value['nodes'].append(dict(id='target:net/new', version='1.0',
            input_sha256='c' * 64, dependencies=[dict(id='target:net/app', kind='runtime')]))
        self.graph_path.write_bytes(json_bytes(value))
        repo['sets'] = []
        repo['roots'] = ['net/new']
        repo['packages'] = {'target:net/new': self.package('net/new', depends=depends)}
        self.spec['repositories'] = [repo]
        self.spec.update(release_id='source-graph-changed', created_at=2)
        return release, current

    def test_reused_node_does_not_require_new_source_only_descendant(self):
        previous, current = self.source_graph_changed_addition()
        value = graph()
        value['nodes'].append(dict(id='target:lang/flang', version='1.0',
            input_sha256='d' * 64, dependencies=[]))
        next(node for node in value['nodes'] if node['id'] == 'target:net/app')['dependencies'].append(
            dict(id='target:lang/flang', kind='runtime'))
        value['nodes'].append(dict(id='target:net/new', version='1.0',
            input_sha256='c' * 64, dependencies=[dict(id='target:net/app', kind='runtime')]))
        self.graph_path.write_bytes(json_bytes(value))
        result = self.compose(current=current, output=self.work / 'pruned')
        old = next(item for item in previous['artifacts'] if item['path'].endswith('/app-1.0.tgz'))
        self.assertEqual(next(item for item in result['artifacts'] if item['id'] == old['id']), old)
        _, catalogs = self.load(self.work / 'pruned')
        self.assertNotIn('lang/flang', {item['root'] for item in catalogs[0]['packages']})

    def test_additions_same_name_also_prunes_source_only_descendant(self):
        from emberbsd_sdk.binary_release import compose
        _, current = self.source_graph_changed_addition()
        value = graph()
        value['nodes'].append(dict(id='target:lang/flang', version='1.0', input_sha256='d' * 64, dependencies=[]))
        next(node for node in value['nodes'] if node['id'] == 'target:net/app')['dependencies'].append(
            dict(id='target:lang/flang', kind='runtime'))
        value['nodes'].append(dict(id='target:net/new', version='1.0', input_sha256='c' * 64,
            dependencies=[dict(id='target:net/app', kind='runtime')]))
        self.graph_path.write_bytes(json_bytes(value))
        self.spec['repositories'][0]['packages']['target:net/app'] = str(self.work / 'app-1.0-NetBSD-aarch64/app-1.0.tgz')
        compose(self.crypto, self.spec, self.ports, self.work / 'addition-pruned',
                self.leaf, self.ca.certificate, current=current, additions=True)

    def test_reused_boundary_does_not_bypass_downgrade_or_signature(self):
        _, current = self.source_graph_changed_addition()
        value = graph()
        next(node for node in value['nodes'] if node['id'] == 'target:net/app')['version'] = '0.9'
        value['nodes'].append(dict(id='target:net/new', version='1.0', input_sha256='c' * 64,
            dependencies=[dict(id='target:net/app', kind='runtime')]))
        self.graph_path.write_bytes(json_bytes(value))
        with self.assertRaisesRegex(SDKError, 'downgrade'):
            self.compose(current=current, output=self.work / 'downgrade')
        envelope = current / 'release.p7m'
        envelope.write_bytes(envelope.read_bytes()[:-20] + b'corrupt-signature')
        with self.assertRaises(SDKError):
            self.compose(current=current, output=self.work / 'bad-signature')

    def test_explicit_descendant_is_not_pruned(self):
        _, current = self.source_graph_changed_addition()
        value = graph()
        value['nodes'].append(dict(id='target:lang/flang', version='1.0', input_sha256='d' * 64, dependencies=[]))
        next(node for node in value['nodes'] if node['id'] == 'target:net/app')['dependencies'].append(
            dict(id='target:lang/flang', kind='runtime'))
        value['nodes'].append(dict(id='target:net/new', version='1.0', input_sha256='c' * 64,
            dependencies=[dict(id='target:net/app', kind='runtime')]))
        self.graph_path.write_bytes(json_bytes(value))
        self.spec['repositories'][0]['roots'].append('lang/flang')
        with self.assertRaisesRegex(SDKError, 'missing runtime binary: target:lang/flang'):
            self.compose(current=current, output=self.work / 'explicit-missing')

    def test_new_node_missing_source_dependency_is_not_pruned(self):
        _, current = self.source_graph_changed_addition()
        value = graph()
        value['nodes'].append(dict(id='target:lang/flang', version='1.0', input_sha256='d' * 64, dependencies=[]))
        value['nodes'].append(dict(id='target:net/new', version='1.0', input_sha256='c' * 64,
            dependencies=[dict(id='target:lang/flang', kind='runtime')]))
        self.graph_path.write_bytes(json_bytes(value))
        with self.assertRaisesRegex(SDKError, 'missing runtime binary: target:lang/flang'):
            self.compose(current=current, output=self.work / 'new-missing')

    def test_reused_archive_retains_signed_edges_when_source_graph_changes(self):
        previous, current = self.source_graph_changed_addition()
        output = self.work / 'addition'
        result = self.compose(current=current, output=output)
        packages = {item['id']: item for item in result['artifacts']}
        for item in previous['artifacts']:
            if item['kind'] == 'target-package':
                self.assertEqual(packages[item['id']], item)
        # The authenticated dependency survives even outside the new source
        # closure; none of the previous package bodies was required.
        _, catalogs = self.load(output)
        self.assertIn('devel/lib', {item['root'] for item in catalogs[0]['packages']})
        self.assertEqual([path.name for path in (output / 'arm64').glob('*.tgz')], ['new-1.0.tgz'])

    def test_new_archive_still_requires_its_own_resolved_dependencies(self):
        _, current = self.source_graph_changed_addition(depends=('app>=2',))
        output = self.work / 'incompatible-addition'
        with self.assertRaisesRegex(SDKError, 'dependency absent or ambiguous'):
            self.compose(current=current, output=output)
        self.assertFalse(output.exists())

    def test_replacement_keeps_root_artifact_id_and_unrelated_repo_bootstrap(self):
        repo, _, current = self.baseline()
        # The unrelated repository and host bootstrap are deliberately omitted
        # from the sparse package-build spec; their signed records must survive.
        second_graph = deepcopy(graph())
        second_graph['context'].update(target='amd64', triplet='amd64--netbsd')
        second_path = self.work / 'amd64-graph.json'
        second_path.write_bytes(json_bytes(second_graph))
        other = self.repository('amd64', arch='amd64')
        other['graph'] = str(second_path)
        release = self.compose(output=self.work / 'baseline-with-two-repos')
        current = self.work / 'baseline-with-two-repos'
        self._add_bootstrap_artifact(current, release)
        old_release, _ = self.load(current)
        old_other = {item['id']: item for item in old_release['artifacts']
                     if item['id'].startswith('amd64-') or item['id'] == 'bootstrap-tool'}
        old_app = next(item for item in old_release['artifacts']
                       if item['path'].endswith('/app-1.0.tgz'))

        value = graph()
        next(node for node in value['nodes'] if node['id'] == 'target:net/app')['version'] = '2.0'
        self.graph_path.write_bytes(json_bytes(value))
        repo['sets'] = []
        repo['roots'] = ['net/app']
        repo['packages'] = {'target:net/app': self.package('net/app', version='2.0')}
        self.spec['repositories'] = [repo]
        self.spec.update(release_id='replacement', created_at=3)

        result = self.compose(current=current, output=self.work / 'replacement')

        new_app = next(item for item in result['artifacts'] if item['id'] == old_app['id'])
        self.assertEqual(new_app['path'], 'arm64/app-2.0.tgz')
        self.assertTrue((self.work / 'replacement/arm64/app-2.0.tgz').is_file())
        self.assertFalse((self.work / 'replacement/arm64/app-1.0.tgz').exists())
        self.assertFalse((self.work / 'replacement/bootstrap/tool.tar').exists())
        for identity, expected in old_other.items():
            self.assertEqual(next(item for item in result['artifacts'] if item['id'] == identity), expected)

    def test_incompatible_unchanged_reverse_consumer_is_rejected(self):
        repo = self.repository(other_options={'depends': ('lib<2',)})
        current_release = self.compose()
        current = self.work / 'release'
        old_lib = next(item for item in current_release['artifacts']
                       if item['path'].endswith('/lib-1.0.tgz'))
        value = graph()
        next(node for node in value['nodes'] if node['id'] == 'target:devel/lib')['version'] = '2.0'
        self.graph_path.write_bytes(json_bytes(value))
        repo['sets'] = []
        repo['roots'] = ['devel/lib']
        repo['packages'] = {'target:devel/lib': self.package('devel/lib', version='2.0')}
        self.spec.update(release_id='incompatible', created_at=2)

        with self.assertRaisesRegex(SDKError, 'dependency absent or ambiguous'):
            self.compose(current=current, output=self.work / 'incompatible')
        self.assertEqual(old_lib['id'], 'arm64-' + hashlib.sha256(
            b'target:devel/lib').hexdigest()[:24])
        self.assertFalse((self.work / 'incompatible').exists())

    def test_missing_runtime_closure_and_wrong_platform_are_rejected(self):
        repo = self.repository()
        repo['sets'] = []
        repo['roots'] = ['net/app']
        lib_package = repo['packages']['target:devel/lib']
        repo['packages'] = {'target:net/app': repo['packages']['target:net/app']}
        with self.assertRaisesRegex(SDKError, 'missing runtime binary'):
            self.compose()

        value = graph()
        next(node for node in value['nodes'] if node['id'] == 'target:net/app')['version'] = '1.1'
        self.graph_path.write_bytes(json_bytes(value))
        repo['packages'] = {
            'target:net/app': self.package('net/app', version='1.1', arch='amd64'),
            'target:devel/lib': lib_package,
        }
        with self.assertRaisesRegex(SDKError, 'platform differs'):
            self.compose(output=self.work / 'wrong-platform')
        self.assertFalse((self.work / 'wrong-platform').exists())

    def test_same_signed_archive_is_reused_without_resigning(self):
        repo, _, current = self.baseline()
        signed_app = current / 'arm64/app-1.0.tgz'
        repo['sets'] = []
        repo['roots'] = ['net/app']
        repo['packages'] = {'target:net/app': str(signed_app)}
        self.spec.update(release_id='repeat', created_at=2)
        output = self.work / 'repeat'
        with patch.object(self.crypto, 'sign_package', wraps=self.crypto.sign_package) as sign:
            self.compose(current=current, output=output)
        sign.assert_not_called()
        self.assertEqual((output / 'arm64/app-1.0.tgz').read_bytes(), signed_app.read_bytes())
