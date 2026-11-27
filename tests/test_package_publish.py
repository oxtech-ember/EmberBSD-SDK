# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Publisher-only package updates reuse current binaries and prepared signatures."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import test_release_s3 as http_fixture
from test_binary_catalog import BinaryReleaseFixture, graph
from emberbsd_sdk.binary_release import load_catalogs
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.jsonio import json_bytes, read_json
from emberbsd_sdk.package_publish import follow_completions, packages_from_state, publication_candidates, update_packages
from emberbsd_sdk.release_catalog import parse_channel
import emberbsd_sdk.release_delivery as release_delivery
from emberbsd_sdk.release_delivery import publish_release


class PackagePublishTests(BinaryReleaseFixture):
    def setUp(self):
        http_fixture.HTTPTests.setUp(self)
        BinaryReleaseFixture.setUp(self)

    def publish_baseline(self):
        repo = self.repository()
        release_dir = self.work / 'baseline'
        self.compose(output=release_dir)
        pointer = publish_release(self.crypto, self.store, self.public,
            release_dir / 'release.p7m', self.leaf, self.ca.certificate)
        self.assertNotIn('generation', pointer)
        return repo

    def prepare_addition(self, *, root='net/new', release_id='package-update', created_at=2):
        repo = self.publish_baseline()
        value = graph()
        value['nodes'].append(dict(id='target:' + root, version='1.0',
            input_sha256='c' * 64,
            dependencies=[dict(id='target:devel/lib', kind='runtime')]))
        self.graph_path.write_bytes(json_bytes(value))
        archive = self.package(root, depends=('lib>=1',))
        repo['sets'] = []
        repo['roots'] = [root]
        repo['packages'] = {'target:' + root: archive}
        self.spec.update(release_id=release_id, created_at=created_at)
        return repo, Path(archive), self.work / 'publisher-work'

    def update(self, workdir, **kwargs):
        return update_packages(self.crypto, self.store, self.public, self.spec,
            self.ports, workdir, self.leaf, self.ca.certificate, **kwargs)

    def test_publication_candidates_compare_receipts_with_signed_names(self):
        repo, _, _ = self.prepare_addition()
        state = self.work / 'state'
        state.mkdir()
        rows = ['net/new new-1.0', 'net/app app-1.0', 'net/other renamed-1.0',
                'devel/lib lib-1.0']
        (state / 'packages').write_text('\n'.join(rows) + '\n')
        complete = ''.join(row + ' ' + 'a' * 64 + '\n' for row in rows[:3])
        (state / 'done').write_text(complete + rows[3] + '\n')
        current = self.work / 'baseline'

        def candidates():
            return publication_candidates(self.crypto, self.spec, state,
                self.work / 'no-archives', current, self.ca.certificate)

        result = candidates()
        self.assertEqual(result['missing'], ['net/new'])
        self.assertEqual(result['changed'], [dict(root='net/other', current='other-1.0', built='renamed-1.0')])
        (state / 'done').write_text(complete + 'net/new new-1.0\n')
        (state / 'failed').write_text('net/other build\n')
        self.assertEqual(candidates(), dict(release_id=result['release_id'], missing=[], changed=[]))
        (state / 'done').write_text('')
        self.assertEqual(candidates(), dict(release_id=result['release_id'], missing=[], changed=[]))
        repo['target']['abi'] = 'another-abi'
        with self.assertRaisesRegex(SDKError, 'platform differs'):
            candidates()

    def test_ready_batch_waits_for_runtime_receipts_and_an_explicit_hold(self):
        self.prepare_addition()
        graph = read_json(self.graph_path)
        template = next(node for node in graph['nodes'] if node['id'] == 'target:net/new')
        provider = 'net/unpublished'
        graph['nodes'].append(dict(template, id='target:' + provider, dependencies=[]))
        template['dependencies'].append(dict(id='target:' + provider, kind='runtime'))
        self.graph_path.write_bytes(json_bytes(graph))
        state = self.work / 'state'
        state.mkdir()
        names = {'net/new': 'new-1.0', provider: 'unpublished-1.0', 'net/other': 'renamed-1.0'}
        (state / 'packages').write_text(''.join(f'{root} {name}\n' for root, name in names.items()))

        def complete(roots):
            (state / 'done').write_text(''.join(f'{root} {names[root]} {"a" * 64}\n' for root in roots))

        def select(*held):
            return publication_candidates(self.crypto, self.spec, state, self.work / 'no-archives',
                self.work / 'baseline', self.ca.certificate, ready=True, held=held)

        complete(['net/new', 'net/other'])
        self.assertEqual(select()['ready'], ['net/other'])
        self.assertEqual(select()['waiting'], {'net/new': [provider]})
        complete(names)
        ready = select()['ready']
        self.assertEqual(set(ready), set(names))
        self.assertLess(ready.index(provider), ready.index('net/new'))
        self.assertEqual(select(provider)['ready'], ['net/other'])
        self.assertEqual(select('devel/lib')['ready'], [provider])
        # A published intermediate does not bypass a held provider.
        template['dependencies'] = [dict(id='target:net/app', kind='runtime')]
        self.graph_path.write_bytes(json_bytes(graph))
        self.assertEqual(select('devel/lib')['waiting']['net/new'], ['net/app'])
        template['dependencies'] = [dict(id='target:devel/lib', kind='runtime', patterns=['lib>=2'])]
        self.graph_path.write_bytes(json_bytes(graph))
        self.assertEqual(select()['waiting']['net/new'], ['devel/lib'])
        template['dependencies'][0]['patterns'] = ['lib>=1']
        self.graph_path.write_bytes(json_bytes(graph))
        self.assertIn('net/new', select()['ready'])
        spec = self.work / 'candidates.json'
        spec.write_bytes(json_bytes(self.spec))
        command = [sys.executable, str(Path(__file__).resolve().parents[1] / 'scripts/binary-catalog.py'),
                   '--host-prefix', os.environ['EMBERBSD_TEST_HOST_PREFIX'], 'publication-candidates',
                   str(spec), '--ready', '--state', str(state), '--packages', str(self.work / 'no-archives'),
                   '--current', str(self.work / 'baseline'), '--trust-root', str(self.ca.certificate)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), select())

    def test_follow_publishes_final_receipt_resumes_and_leaves_current_on_bad_checksum(self):
        _, archive, workdir = self.prepare_addition()
        state = self.work / 'follow-state'
        state.mkdir()
        (state / 'packages').write_text('net/new new-1.0\n')
        (state / 'done').write_text('net/new new-1.0 ' + '0' * 64 + '\n')
        archive_dir = self.work / 'follow-packages'
        archive_dir.mkdir()
        (archive_dir / 'new-1.0.tgz').write_bytes(archive.read_bytes())

        def run():
            return list(follow_completions(self.crypto, self.store, self.public, self.spec,
                self.ports, workdir, self.leaf, self.ca.certificate, state, archive_dir, interval=1))

        before = self.public.read('current.p7m', 1024 * 1024)[0]
        with self.assertRaises(SDKError):
            run()
        self.assertEqual(self.public.read('current.p7m', 1024 * 1024)[0], before)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        (state / 'done').write_text('net/new new-1.0 ' + digest + '\n')
        (state / 'build.lock').mkdir()
        with patch('emberbsd_sdk.package_publish.time') as clock:
            clock.sleep.side_effect = lambda _: (state / 'build.lock').rmdir()
            events = run()
            clock.sleep.assert_called_once_with(1)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['roots'], ['net/new'])
        self.assertEqual(events[0]['statistics']['uploaded_packages'], 1)
        committed = self.public.read('current.p7m', 1024 * 1024)[0]
        self.assertNotEqual(before, committed)
        self.assertEqual(run()[0]['roots'], [])
        self.assertEqual(self.public.read('current.p7m', 1024 * 1024)[0], committed)

    def test_follow_waits_for_a_concurrent_publisher_before_the_final_batch(self):
        _, archive, workdir = self.prepare_addition()
        state = self.work / 'completed'
        state.mkdir()
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        (state / 'packages').write_text('net/new new-1.0\n')
        (state / 'done').write_text(f'net/new new-1.0 {digest}\n')
        packages = self.work / 'archives'
        packages.mkdir()
        (packages / 'new-1.0.tgz').write_bytes(archive.read_bytes())

        lock = '/bucket/.publisher-lock'
        self.server.objects[lock] = b'peer'
        def finish_peer(seconds):
            self.assertEqual(seconds, 1)
            self.assertEqual(self.server.objects.pop(lock), b'peer')

        with patch('emberbsd_sdk.release_delivery.time') as clock:
            clock.time.side_effect = time.time
            clock.sleep.side_effect = finish_peer
            event, = follow_completions(self.crypto, self.store, self.public, self.spec,
                self.ports, workdir, self.leaf, self.ca.certificate, state, packages, interval=1)
        clock.sleep.assert_called_once_with(1)
        self.assertEqual(event['roots'], ['net/new'])
        self.assertNotIn(lock, self.server.objects)

    def test_follow_publishes_from_an_active_sdk_wave_before_merge(self):
        from emberbsd_sdk import source_wave

        _, archive, workdir = self.prepare_addition()
        state = self.work / 'source-state'
        lock = state / 'build.lock'
        lock.mkdir(parents=True)
        source_wave._write_stamp(lock, '012abc', os.getpid())
        (state / 'packages').write_text('net/new new-1.0\n')
        packages = self.work / 'packages'
        packages.mkdir()
        shutil.copyfile(archive, packages / 'new-1.0.tgz')
        receipt = 'net/new new-1.0 ' + hashlib.sha256(archive.read_bytes()).hexdigest() + '\n'
        private = Path(tempfile.mkdtemp(prefix='ember-wave-', dir=self.work))

        def start_wave(_):
            shutil.copyfile(state / 'packages', private / 'packages')
            (private / 'done').write_text(receipt)
            source_wave._write_controller(lock, os.getpid(), 'test controller', private)

        with patch('emberbsd_sdk.package_publish.time.sleep', side_effect=start_wave) as sleep:
            events = follow_completions(self.crypto, self.store, self.public, self.spec,
                self.ports, workdir, self.leaf, self.ca.certificate, state, packages, interval=1)
            self.assertEqual(next(events)['roots'], ['net/new'])
            self.assertFalse((state / 'done').exists())
            sleep.assert_called_once_with(1)
            (state / 'done').write_text(receipt)
            shutil.rmtree(lock)
            shutil.rmtree(private)
            sleep.side_effect = None
            self.assertEqual([event['roots'] for event in events], [[]])

    def test_follow_reselects_after_another_authenticated_publisher_advances_current(self):
        _, archive, workdir = self.prepare_addition()
        state = self.work / 'follow-state'
        state.mkdir()
        (state / 'packages').write_text('net/new new-1.0\n')
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        (state / 'done').write_text('net/new new-1.0 ' + digest + '\n')
        package_directory = self.work / 'follow-packages'
        package_directory.mkdir()
        (package_directory / 'new-1.0.tgz').write_bytes(archive.read_bytes())
        ids = []

        def advance_then_update(*args, **kwargs):
            ids.append(args[3]['release_id'])
            if len(ids) == 1:
                baseline = self.work / 'baseline' / 'release.p7m'
                metadata = json.loads(self.crypto.verify_cms(baseline.read_bytes(), self.ca.certificate))
                metadata.update(release_id='concurrent-publisher', created_at=2)
                peer = baseline.with_name('peer.p7m')
                peer.write_bytes(self.crypto.sign_cms(json_bytes(metadata), self.leaf, self.ca.certificate))
                publish_release(self.crypto, self.store, self.public, peer, self.leaf, self.ca.certificate,
                                expected_current=kwargs['expected_parent'], reuse_current=True)
            return update_packages(*args, **kwargs)

        with patch('emberbsd_sdk.package_publish.update_packages', side_effect=advance_then_update):
            result, = follow_completions(self.crypto, self.store, self.public, self.spec, self.ports,
                workdir, self.leaf, self.ca.certificate, state, package_directory)
        self.assertEqual(len(set(ids)), 2)
        self.assertEqual(result['release_id'], ids[-1])
        self.assertEqual(result['roots'], ['net/new'])

    def test_publish_one_completed_root_with_the_signed_runtime_provider(self):
        repo, archive, workdir = self.prepare_addition()
        repo['sets'] = ['app', 'other']
        state = self.work / 'state'
        state.mkdir()
        (state / 'packages').write_text('net/new new-1.0\nnet/other other-1.0\n')
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        (state / 'done').write_text(f'net/new new-1.0 {digest}\nnet/other other-1.0 {"0" * 64}\n')
        self.spec, checksums = packages_from_state(self.spec, state, archive.parent,
                                                   selected_roots=['net/new'])
        result = self.update(workdir, checksums=checksums)
        self.assertEqual(result['statistics']['added'], 1)
        self.assertEqual(result['statistics']['uploaded_packages'], 1)
        catalog = json.loads((workdir / 'release' / repo['id'] / 'catalog.json').read_text())
        self.assertEqual(catalog['sets']['app'], ['net/app'])

    def test_addition_fetch_compose_publish_preserves_old_packages_without_get(self):
        _, _, workdir = self.prepare_addition()
        original_read = self.public.read
        reads = []

        def track_read(key, *args, **kwargs):
            reads.append(key)
            return original_read(key, *args, **kwargs)

        with patch.object(self.public, 'read', side_effect=track_read):
            result = self.update(workdir)

        self.assertNotIn('generation', result['channel'])
        self.assertEqual(result['statistics']['added'], 1)
        self.assertEqual(result['statistics']['replaced'], 0)
        self.assertEqual(result['statistics']['uploaded_packages'], 1)
        self.assertEqual(result['statistics']['built'], 0)
        self.assertFalse(any(key.endswith('.tgz') for key in reads))

        release, catalogs = load_catalogs(self.crypto, workdir / 'release/release.p7m',
            workdir / 'release', self.ca.certificate, ())
        self.assertEqual({item['root'] for item in catalogs[0]['packages']},
            {'net/app', 'net/other', 'devel/lib', 'devel/lib-devel', 'net/new'})
        self.assertEqual(release['release_id'], 'package-update')

    def test_bad_successful_build_checksum_refuses_before_channel_switch(self):
        _, archive, workdir = self.prepare_addition(release_id='bad-checksum')
        pointer = self.server.objects['/bucket/current.p7m']
        current_puts = []
        original_put = self.store.put

        def count_current_put(key, *args, **kwargs):
            if key == 'current.p7m':
                current_puts.append(key)
            return original_put(key, *args, **kwargs)

        with patch.object(self.store, 'put', side_effect=count_current_put):
            with self.assertRaisesRegex(SDKError, 'archive differs from successful Ports result'):
                self.update(workdir, checksums={str(archive): '0' * 64})

        self.assertEqual(current_puts, [])
        self.assertEqual(self.server.objects['/bucket/current.p7m'], pointer)

    def test_graph_downgrade_from_current_version_is_rejected(self):
        value = graph()
        next(node for node in value['nodes'] if node['id'] == 'target:net/app')['version'] = '2.0'
        self.graph_path.write_bytes(json_bytes(value))
        repo = self.repository()
        version_one_archive = Path(repo['packages']['target:net/app'])
        repo['packages']['target:net/app'] = self.package('net/app', version='2.0')
        baseline = self.work / 'version-two-baseline'
        self.compose(output=baseline)
        initial = publish_release(self.crypto, self.store, self.public, baseline / 'release.p7m',
                                  self.leaf, self.ca.certificate)
        self.assertNotIn('generation', initial)
        pointer = self.server.objects['/bucket/current.p7m']

        value = graph()
        self.graph_path.write_bytes(json_bytes(value))
        repo['sets'] = []
        repo['roots'] = ['net/app']
        repo['packages'] = {'target:net/app': str(version_one_archive)}
        self.spec.update(release_id='attempt-downgrade', created_at=3)
        current_puts = []
        original_put = self.store.put

        def count_current_put(key, *args, **kwargs):
            if key == 'current.p7m':
                current_puts.append(key)
            return original_put(key, *args, **kwargs)

        with patch.object(self.store, 'put', side_effect=count_current_put):
            with self.assertRaisesRegex(SDKError, 'package would downgrade current: net/app'):
                self.update(self.work / 'downgrade-work')

        self.assertEqual(current_puts, [])
        self.assertEqual(self.server.objects['/bucket/current.p7m'], pointer)

    def _run_partial_retry(self, failure):
        _, _, workdir = self.prepare_addition(release_id='partial-' + failure)
        original_verify = self.public.verify
        failed = False

        def fail_current_once(key, *args, **kwargs):
            nonlocal failed
            if key == 'current.p7m' and not failed:
                failed = True
                raise SDKError('injected current verification failure')
            return original_verify(key, *args, **kwargs)

        with patch.object(self.crypto, 'sign_package', wraps=self.crypto.sign_package) as package_sign, \
                patch.object(self.crypto, 'sign_cms', wraps=self.crypto.sign_cms) as cms_sign:
            if failure == 'verify':
                injected = patch.object(self.public, 'verify', side_effect=fail_current_once)
            else:
                injected = patch.object(release_delivery, '_clean_current',
                    side_effect=SDKError('injected cleanup failure'))
            with injected:
                with self.assertRaisesRegex(SDKError, 'injected .* failure'):
                    self.update(workdir)
            calls = (package_sign.call_count, cms_sign.call_count)
            prepared = (workdir / 'release/release.p7m').read_bytes()
            self.assertTrue((workdir / 'snapshot/current.p7m').is_file())

            result = self.update(workdir)

            self.assertEqual((package_sign.call_count, cms_sign.call_count), calls)
            self.assertEqual((workdir / 'release/release.p7m').read_bytes(), prepared)
        self.assertNotIn('generation', result['channel'])

    def test_partial_current_verify_failure_retries_prepared_signature(self):
        self._run_partial_retry('verify')

    def test_partial_cleanup_failure_retries_prepared_signature(self):
        self._run_partial_retry('cleanup')

    def test_new_release_id_reuses_workdir_and_removes_only_operation_outputs(self):
        repo, first_archive, workdir = self.prepare_addition(release_id='first-update')
        first_archive_bytes = first_archive.read_bytes()
        first = self.update(workdir)
        self.assertNotIn('generation', first['channel'])
        old_snapshot_marker = workdir / 'snapshot/obsolete-marker'
        old_release_marker = workdir / 'release/obsolete-marker'
        old_snapshot_marker.write_bytes(b'owned snapshot output')
        old_release_marker.write_bytes(b'owned release output')

        root = 'net/second'
        value = json.loads(self.graph_path.read_text())
        value['nodes'].append(dict(id='target:' + root, version='1.0',
            input_sha256='d' * 64,
            dependencies=[dict(id='target:devel/lib', kind='runtime')]))
        self.graph_path.write_bytes(json_bytes(value))
        second_archive = Path(self.package(root, depends=('lib>=1',)))
        repo['roots'] = [root]
        repo['packages'] = {'target:' + root: str(second_archive)}
        self.spec.update(release_id='second-update', created_at=3)

        second = self.update(workdir)

        self.assertNotIn('generation', second['channel'])
        self.assertFalse(old_snapshot_marker.exists())
        self.assertFalse(old_release_marker.exists())
        self.assertEqual(first_archive.read_bytes(), first_archive_bytes)
        self.assertTrue(second_archive.is_file())
        self.assertTrue((workdir / 'snapshot/release.p7m').is_file())
        self.assertTrue((workdir / 'release/release.p7m').is_file())

    def test_same_root_version_with_renamed_pkgbase_is_replaced(self):
        repo = self.publish_baseline()
        baseline = self.work / 'baseline'
        _, catalogs = load_catalogs(self.crypto, baseline / 'release.p7m', baseline,
                                   self.ca.certificate, ())
        old_package = next(item for item in catalogs[0]['packages'] if item['root'] == 'net/app')

        renamed_archive = Path(self.package('net/app', pkgbase='renamed-app'))
        repo['sets'] = []
        repo['roots'] = ['net/app']
        repo['packages'] = {'target:net/app': str(renamed_archive)}
        self.spec.update(release_id='renamed-package', created_at=2)
        result = self.update(self.work / 'rename-work')

        self.assertNotIn('generation', result['channel'])
        self.assertEqual(result['statistics']['added'], 0)
        self.assertEqual(result['statistics']['replaced'], 1)
        self.assertEqual(result['statistics']['uploaded_packages'], 1)
        output = self.work / 'rename-work/release'
        _, updated_catalogs = load_catalogs(self.crypto, output / 'release.p7m', output,
                                            self.ca.certificate, ())
        new_package = next(item for item in updated_catalogs[0]['packages']
                           if item['root'] == 'net/app')
        self.assertEqual(new_package['pkgname'], 'renamed-app-1.0')
        self.assertEqual(new_package['artifact'], old_package['artifact'])
        self.assertTrue((output / 'arm64/renamed-app-1.0.tgz').is_file())
        self.assertNotIn('/bucket/current/target/aarch64/app-1.0.tgz', self.server.objects)
        self.assertIn('/bucket/current/target/aarch64/renamed-app-1.0.tgz', self.server.objects)

    def test_already_current_packages_are_noop_without_signing_or_pointer_put(self):
        _, _, workdir = self.prepare_addition(release_id='already-current')
        first = self.update(workdir)
        self.assertNotIn('generation', first['channel'])
        pointer = self.server.objects['/bucket/current.p7m']
        signed_input = workdir / 'release/arm64/new-1.0.tgz'

        # A fresh work area receives the exact already-current signed package.
        repo = self.spec['repositories'][0]
        repo['packages'] = {'target:net/new': str(signed_input)}
        noop_workdir = self.work / 'no-op-work'
        current_puts = []
        original_put = self.store.put
        def count_current_put(key, *args, **kwargs):
            if key == 'current.p7m':
                current_puts.append(key)
            return original_put(key, *args, **kwargs)

        with patch.object(self.crypto, 'sign_package', wraps=self.crypto.sign_package) as package_sign, \
                patch.object(self.crypto, 'sign_cms', wraps=self.crypto.sign_cms) as cms_sign, \
                patch.object(self.store, 'put', side_effect=count_current_put):
            result = self.update(noop_workdir)

        self.assertNotIn('generation', result['channel'])
        current = parse_channel(self.crypto.verify_cms(pointer, self.ca.certificate), 'current')
        self.assertEqual(result['channel'], current)
        self.assertEqual(result['statistics']['added'], 0)
        self.assertEqual(result['statistics']['replaced'], 0)
        self.assertEqual(result['statistics']['uploaded_packages'], 0)
        self.assertEqual(result['statistics']['upload_bytes'], 0)
        self.assertEqual(result['statistics']['built'], 0)
        self.assertEqual(package_sign.call_count, 0)
        self.assertEqual(cms_sign.call_count, 0)
        self.assertEqual(current_puts, [])
        self.assertEqual(self.server.objects['/bucket/current.p7m'], pointer)
