# SPDX-License-Identifier: BSD-2-Clause
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.package_install import PackageInstaller
import test_sdk_client as fixtures


def installer_factory(installer):
    class Factory:
        def __new__(cls, *_args):
            return installer
    return Factory


class InventoryTests(unittest.TestCase):
    def test_inventory_uses_one_pkg_summary_query_without_configuring_packages(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'pkgdb'
            database.mkdir()
            cfg = dict(host={}, prefix=directory, pkgdb=str(database))
            installer = PackageInstaller(unittest.mock.Mock(), Path(directory) / 'unused.pem')
            installer.crypto.prefix.tool.return_value = '/selected/pkg_info'
            summary = b'PKGNAME=app-1.2nb3\nPKGPATH=net/app\n\nPKGNAME=legacy-1\n\n'
            with patch('emberbsd_sdk.package_install.subprocess.run',
                       return_value=subprocess.CompletedProcess([], 0, summary, b'')) as run:
                self.assertEqual(installer.inventory(cfg), [
                    dict(pkgname='app-1.2nb3', root='net/app@native'),
                    dict(pkgname='legacy-1', root=None)])
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0], ['/selected/pkg_info', '-K', str(database), '-X', '-a'])

    def test_missing_database_is_empty_without_a_process(self):
        installer = PackageInstaller(unittest.mock.Mock(), '/unused.pem')
        with tempfile.TemporaryDirectory() as directory, patch('subprocess.run') as run:
            cfg = dict(prefix=directory, pkgdb=str(Path(directory) / 'absent'), host={})
            self.assertEqual(installer.inventory(cfg), [])
            run.assert_not_called()


class UpdateSelectionTests(unittest.TestCase):
    setUp = fixtures.ClientTests.setUp
    def update(self, **kwargs):
        from emberbsd_sdk.package_state import update_plan
        return update_plan(self.release, self.catalogs, self.cfg, self.installer, self.cache, **kwargs)

    def inventory(self, entries):
        self.installer.inventory = unittest.mock.Mock(return_value=entries)

    def test_update_uses_pkgdb_including_packages_added_outside_client(self):
        self.inventory([dict(pkgname='app-0', root='net/app@native')])
        result = self.update()
        self.assertEqual(result['roots'], ['net/app@native'])
        self.assertEqual([p['pkgname'] for p in result['packages']], ['lib-1', 'app-1'])
        self.assertEqual(result['packages'][1]['replaces'], ['app-0'])
        self.assertEqual(result['build_roots'], [])

    def test_update_does_not_expand_sets_or_restore_removed_packages(self):
        self.inventory([dict(pkgname='lib-1', root='devel/lib@native')])
        first = self.update()
        self.catalogs[0]['sets']['task'].append('devel/lib@native')
        self.release['release_id'] = 'catalog-with-new-app'
        second = self.update()
        self.assertEqual(second['packages'], first['packages'])
        self.assertEqual(second['download_size'], 0)
        self.assertEqual([p['pkgname'] for p in second['packages']], ['lib-1'])

    def test_missing_and_unidentified_installed_packages_are_retained_without_builds(self):
        self.inventory([dict(pkgname='private-1', root='local/private@native'),
                        dict(pkgname='legacy-1', root=None)])
        result = self.update()
        self.assertEqual(result['packages'], [])
        self.assertEqual(result['build_roots'], [])
        self.assertEqual({p['pkgname'] for p in result['retained']}, {'private-1', 'legacy-1'})

    def test_explicit_update_cannot_install_an_uninstalled_root(self):
        self.inventory([dict(pkgname='lib-1', root='devel/lib@native')])
        with self.assertRaisesRegex(SDKError, 'not installed'):
            self.update(roots=['net/app@native'])

    def test_explicit_update_selects_only_requested_installed_root(self):
        self.inventory([dict(pkgname='lib-0', root='devel/lib@native'),
                        dict(pkgname='app-0', root='net/app@native')])
        result = self.update(roots=['devel/lib@native'])
        self.assertEqual([p['pkgname'] for p in result['packages']], ['lib-1'])

    def test_update_keeps_repository_scope_for_same_target_root(self):
        target = dict(arch='aarch64', triplet='aarch64--netbsd', abi='test', runtime='libc++')
        for item in self.release['artifacts']:
            item.update(host=None, target=target, kind='target-package')
        self.catalogs[0]['packages'] = [dict(p, root=p['root'].removesuffix('@native'))
                                       for p in self.catalogs[0]['packages']]
        self.cfg['repositories'] = {name: dict(target=target, prefix='/usr/pkg', pkgdb='/usr/pkg/pkgdb',
            sysroot=str(self.root / name), os_version='11') for name in ('first', 'second')}
        self.catalogs[0]['repository'] = 'first'
        other = deepcopy(self.catalogs[0])
        other['repository'] = 'second'
        for package in other['packages']:
            identity = package['artifact']
            original = next(a for a in self.release['artifacts'] if a['id'] == identity)
            self.release['artifacts'].append(dict(original, id=identity + '-second',
                depends=[d + '-second' for d in original['depends']]))
            package['artifact'] += '-second'
        self.catalogs.append(other)
        self.installer.inventory = unittest.mock.Mock(side_effect=lambda cfg:
            [dict(pkgname='app-0', root='net/app')] if cfg['sysroot'].endswith('first') else [])
        result = self.update()
        self.assertEqual({p['repository'] for p in result['packages']}, {'first'})

    def test_package_rename_requires_explicit_migration(self):
        self.inventory([dict(pkgname='previous-1', root='net/app@native')])
        with self.assertRaisesRegex(SDKError, 'rename'):
            self.update()

    def test_scoped_update_rejects_renamed_installed_dependency(self):
        self.inventory([dict(pkgname='app-0', root='net/app@native'),
                        dict(pkgname='previous-lib-0', root='devel/lib@native')])
        with self.assertRaisesRegex(SDKError, 'rename'):
            self.update(roots=['net/app@native'])

    def test_dependency_cannot_replace_retained_package_without_pkgpath(self):
        self.inventory([dict(pkgname='app-0', root='net/app@native'),
                        dict(pkgname='lib-0', root=None)])
        with self.assertRaisesRegex(SDKError, 'PKGPATH.*migration'):
            self.update()

    def test_dependency_cannot_replace_retained_package_from_another_pkgpath(self):
        self.inventory([dict(pkgname='app-0', root='net/app@native'),
                        dict(pkgname='lib-0', root='local/old-lib@native')])
        with self.assertRaisesRegex(SDKError, 'PKGPATH.*migration'):
            self.update(roots=['net/app@native'])

    def test_cli_dependency_conflict_stops_before_download_or_package_mutation(self):
        self.inventory([dict(pkgname='app-0', root='net/app@native'),
                        dict(pkgname='lib-0', root=None)])
        status, _ = self.run_client(['update', '--package', 'net/app@native'])
        self.assertEqual(status, 1)
        self.assertEqual(self.store.calls, [])
        self.assertEqual(self.installer.actions, [])

    def test_binary_update_needs_no_configured_source_checkout(self):
        self.inventory([dict(pkgname='lib-1', root='devel/lib@native')])
        self.cfg['source'] = {'ports_root': str(self.root / 'unmounted-ports')}
        result = self.update()
        self.assertEqual([p['pkgname'] for p in result['packages']], ['lib-1'])
        self.assertEqual(result['download_size'], 0)

    def test_installed_os_version_comes_from_target_base(self):
        from emberbsd_sdk.package_state import installed_state
        target = dict(arch='aarch64', triplet='aarch64--netbsd', abi='netbsd-11', runtime='libc++')
        cfg = dict(self.cfg, repositories={'target': dict(target=target, sysroot=str(self.root))})
        self.inventory([dict(pkgname='app-7nb2', root='net/app')])
        (self.root / 'etc').mkdir()
        (self.root / 'etc/release').write_text('EmberBSD 1.4/evbarm\nAttribution\n')
        state = installed_state(cfg, self.installer)['repositories']['target']
        self.assertEqual(state['os_release'], 'EmberBSD 1.4/evbarm')
        self.assertEqual(state['platform']['abi'], 'netbsd-11')
        self.assertEqual(state['packages'][0]['pkgname'], 'app-7nb2')

    def test_cli_installed_reads_versions_without_network_or_openssl(self):
        from emberbsd_sdk.sdk_client import main
        import io
        cfg = dict(self.cfg, state_dir=str(self.root / 'state'), cache_dir=str(self.cache.root),
                   host_prefix=str(self.root / 'tools'))
        self.inventory([dict(pkgname='app-1', root='net/app@native')])
        output = io.TextIOWrapper(io.BytesIO(), encoding='utf-8')
        with patch('emberbsd_sdk.sdk_client.load_config', return_value=cfg), \
             patch('emberbsd_sdk.sdk_client.load_host_prefix') as prefix, \
             patch('emberbsd_sdk.sdk_client.PackageInstaller', installer_factory(self.installer)), \
             patch('emberbsd_sdk.sdk_client.HTTPStore') as store, patch('sys.stdout', output):
            self.assertEqual(main(['--config', 'unused', 'installed']), 0)
            store.assert_not_called()
            self.assertEqual(prefix.call_args.kwargs['required_roles'], ('pkg_info',))
        output.seek(0)
        self.assertIn('app-1', output.read())

    def run_client(self, command):
        from emberbsd_sdk.sdk_client import main
        import io
        cfg = dict(self.cfg, state_dir=str(self.root / 'state'), cache_dir=str(self.cache.root),
                   host_prefix=str(self.root / 'tools'))
        snapshots = []
        @contextmanager
        def snapshot_context(_crypto, _store, _trust, state, **kwargs):
            with tempfile.TemporaryDirectory(prefix='.release-metadata-', dir=state) as work:
                snapshot = Path(work) / 'snapshot'
                snapshot.mkdir()
                (snapshot / 'release.p7m').write_bytes(b'authenticated fixture')
                snapshots.append(snapshot)
                yield self.release, snapshot
        output = io.TextIOWrapper(io.BytesIO(), encoding='utf-8')
        with patch('emberbsd_sdk.sdk_client.load_config', return_value=cfg), \
             patch('emberbsd_sdk.sdk_client.load_host_prefix'), \
             patch('emberbsd_sdk.sdk_client.PackageInstaller', installer_factory(self.installer)), \
             patch('emberbsd_sdk.sdk_client.HTTPStore') as store, \
             patch('emberbsd_sdk.sdk_client.release_snapshot', side_effect=snapshot_context) as fetched, \
             patch('emberbsd_sdk.sdk_client.load_release_catalogs', return_value=self.catalogs), \
             patch('sys.stdout', output):
            store.return_value.__enter__.return_value = self.store
            status = main(['--config', 'unused', *command])
            self.assertTrue(fetched.call_count)
            self.assertEqual(len(snapshots), fetched.call_count)
            self.assertTrue(all(call.args[3] == self.root / 'state'
                                for call in fetched.call_args_list))
            self.assertTrue(all(snapshot.name == 'snapshot' for snapshot in snapshots))
            self.assertFalse((self.root / 'state/channel.json').exists())
        output.seek(0)
        return status, output.read()

    def test_update_reuses_authenticated_release_without_a_second_cms_verification(self):
        self.inventory([dict(pkgname='lib-1', root='devel/lib@native')])
        with patch('emberbsd_sdk.release_signing.ReleaseCrypto.verify_cms',
                   side_effect=SDKError('release was already authenticated by fetch')) as verify:
            status, output = self.run_client(['update'])
        self.assertEqual(status, 0, output)
        verify.assert_not_called()

    def test_cli_plan_installed_uses_actual_pkgdb_without_install_or_build(self):
        import json
        self.inventory([dict(pkgname='lib-1', root='devel/lib@native')])
        status, output = self.run_client(['plan', '--installed'])
        self.assertEqual(status, 0)
        plan = json.loads(output)
        self.assertEqual([p['pkgname'] for p in plan['packages']], ['lib-1'])
        self.assertEqual(plan['download_size'], 0)
        self.assertEqual(self.installer.actions, [])
        self.assertEqual(self.store.calls, [])

    def test_cli_update_does_not_revive_saved_roots(self):
        self.inventory([dict(pkgname='lib-0', root='devel/lib@native')])
        state = self.root / 'state'
        state.mkdir()
        saved = state / 'selection.json'
        saved.write_text('{"roots":["net/app@native"]}\n')
        status, output = self.run_client(['update'])
        self.assertEqual(status, 0)
        self.assertIn('installed', output)
        self.assertEqual([a[0] for a in self.installer.actions], ['check', 'install', 'configure'])
        self.assertEqual(saved.read_text(), '{"roots":["net/app@native"]}\n')
        self.installer.inventory.assert_called_once()

    def test_inventory_failure_stops_before_download_or_install(self):
        self.installer.inventory = unittest.mock.Mock(side_effect=SDKError('unreadable pkgdb'))
        status, _ = self.run_client(['update'])
        self.assertEqual(status, 1)
        self.assertEqual(self.store.calls, [])
        self.assertEqual(self.installer.actions, [])


if __name__ == '__main__':
    unittest.main()
