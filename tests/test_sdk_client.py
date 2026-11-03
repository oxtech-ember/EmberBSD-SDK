# SPDX-License-Identifier: BSD-2-Clause
from copy import deepcopy
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.sdk_client import installation_plan, apply_plan, include_source_dependencies, install_selection
from emberbsd_sdk.package_cache import PackageCache
from test_package_cache import Store


class Installer:
    def __init__(self):
        self.present, self.actions = set(), []
    def installed(self, config):
        return self.present
    def check(self, path, config):
        self.actions.append(('check', path.name))
    def install(self, path, config, *, checked=False):
        self.actions.append(('install', path.name))


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.host = dict(os='Darwin', arch='arm64', abi='test', runtime='libSystem')
        self.cfg = dict(repositories={'native': dict(host=self.host,
            prefix=str(self.root / 'prefix'), pkgdb=str(self.root / 'pkgdb'))})
        self.installer = Installer()
        self.cache = PackageCache(self.root / 'cache')
        self.store = Store(b'package bytes')
        def artifact(name, deps):
            return dict(id=name, path=f'native/{name}-1.tgz', kind='host-package', host=self.host,
                target=None, size=13, sha256=hashlib.sha256(self.store.payload).hexdigest(),
                unpacked_size=20, depends=deps)
        self.release = dict(release_id='test', artifacts=[artifact('app', ['lib']), artifact('lib', [])])
        self.catalogs = [dict(repository='native', sets={'task': ['net/app@native']}, packages=[
            dict(root='net/app@native', pkgname='app-1', artifact='app'),
            dict(root='devel/lib@native', pkgname='lib-1', artifact='lib')])]

    def plan(self, **kw):
        return installation_plan(self.release, self.catalogs, self.cfg, self.installer, self.cache, **kw)

    def test_empty_selection_does_nothing(self):
        self.assertEqual(self.plan()['packages'], [])

    def test_shared_dependency_and_installed_component_subtracted(self):
        self.installer.present.add('lib-1')
        plan = self.plan(sets=['task'], roots=['devel/lib@native'])
        self.assertEqual([p['pkgname'] for p in plan['packages']], ['lib-1', 'app-1'])
        self.assertEqual(plan['download_size'], 13)
        self.assertEqual(plan['unpacked_size'], 20)
        self.assertEqual(plan['packages'][0]['action'], 'installed')

    def test_absent_root_is_a_source_build(self):
        plan = self.plan(roots=['devel/missing@native'])
        self.assertEqual(plan['build_roots'], ['devel/missing@native'])
        self.assertEqual(plan['packages'], [])

    def test_wrong_abi_is_not_a_source_fallback(self):
        self.cfg['repositories']['native']['host'] = dict(self.host, abi='different')
        with self.assertRaisesRegex(SDKError, 'platform'):
            self.plan(sets=['task'])

    def test_replacement_requires_the_update_path_before_any_mutation(self):
        self.installer.present.add('lib-0')
        with self.assertRaisesRegex(SDKError, 'update'):
            self.plan(sets=['task'])
        self.assertEqual(self.installer.actions, [])

    def test_all_archives_checked_before_any_install(self):
        plan = self.plan(sets=['task'])
        apply_plan(plan, self.cfg, self.installer, self.cache, self.store)
        self.assertEqual([a[0] for a in self.installer.actions], ['check', 'check', 'install', 'install'])
        self.assertEqual(len(self.store.calls), 1)

    def test_download_failure_does_not_install_or_build(self):
        self.store.fail = True
        with self.assertRaises(OSError):
            apply_plan(self.plan(sets=['task']), self.cfg, self.installer, self.cache, self.store)
        self.assertEqual(self.installer.actions, [])

    def test_no_source_configuration_fails_before_binary_mutation(self):
        plan = self.plan(sets=['task'], roots=['devel/missing@native'])
        with self.assertRaisesRegex(SDKError, 'Ports'):
            apply_plan(plan, self.cfg, self.installer, self.cache, self.store)
        self.assertEqual(self.installer.actions, [])

    def test_missing_roots_use_existing_ports_after_binary_install(self):
        self.cfg['source'] = {name: str(self.root / name) for name in ('ports_root', 'pkgsrc', 'makeconf', 'state')}
        sets = self.root / 'ports_root/profiles/package-sets'
        sets.mkdir(parents=True)
        (sets / 'mixed.txt').write_text('net/app@native\ndevel/missing@native\n')
        plan = self.plan(sets=['mixed'])
        with patch('emberbsd_sdk.sdk_client.build_missing') as build:
            def check(config, roots, binaries):
                self.assertEqual(roots, ['devel/missing@native'])
                self.assertEqual(binaries, [('devel/lib@native', 'lib-1'), ('net/app@native', 'app-1')])
                self.assertEqual(self.installer.actions[-1][0], 'install')
                self.assertFalse((self.root / 'state/build.lock').exists())
                self.assertFalse((self.cache.root / '.lock').exists())
            build.side_effect = check
            install_selection(plan, self.cfg, self.installer, self.cache, self.store)
        build.assert_called_once()

    def test_not_enough_space_stops_before_download(self):
        with patch('emberbsd_sdk.package_cache.shutil.disk_usage') as usage:
            usage.return_value.free = 1
            with self.assertRaisesRegex(SDKError, 'space'):
                apply_plan(self.plan(sets=['task']), self.cfg, self.installer, self.cache, self.store)
        self.assertEqual(self.store.calls, [])
        self.assertEqual(self.installer.actions, [])

    def test_missing_root_downloads_available_dependencies_of_source_graph(self):
        self.cfg['source'] = {name: str(self.root / name) for name in ('ports_root', 'pkgsrc', 'makeconf', 'state')}
        (self.root / 'ports_root/profiles/package-sets').mkdir(parents=True)
        (self.root / 'state').mkdir()
        (self.root / 'state/packages').write_text('devel/missing@native missing-1\ndevel/lib@native lib-1\n')
        plan = self.plan(roots=['devel/missing@native'])
        with patch('emberbsd_sdk.sdk_client.run_source') as source:
            result = include_source_dependencies(plan, self.release, self.catalogs, self.cfg,
                                                  self.installer, self.cache)
        source.assert_called_once_with(self.cfg, ['devel/missing@native'], 'plan')
        self.assertEqual([p['pkgname'] for p in result['packages']], ['lib-1'])
        self.assertEqual(result['build_roots'], ['devel/missing@native'])
        self.assertEqual(result['roots'], ['devel/missing@native'])

    def test_source_dependency_version_mismatch_remains_for_ports(self):
        self.cfg['source'] = {name: str(self.root / name) for name in ('ports_root', 'pkgsrc', 'makeconf', 'state')}
        (self.root / 'ports_root/profiles/package-sets').mkdir(parents=True)
        (self.root / 'state').mkdir()
        (self.root / 'state/packages').write_text('devel/missing@native missing-1\ndevel/lib@native lib-2\n')
        plan = self.plan(roots=['devel/missing@native'])
        with patch('emberbsd_sdk.sdk_client.run_source'):
            result = include_source_dependencies(plan, self.release, self.catalogs, self.cfg,
                                                  self.installer, self.cache)
        self.assertEqual(result['packages'], [])

    def test_requested_binary_conflicting_with_source_dependency_moves_to_source(self):
        self.cfg['source'] = {name: str(self.root / name) for name in ('ports_root', 'pkgsrc', 'makeconf', 'state')}
        (self.root / 'ports_root/profiles/package-sets').mkdir(parents=True)
        (self.root / 'state').mkdir()
        path = self.root / 'state/packages'
        def source(config, roots, command):
            path.write_text('devel/missing@native missing-1\ndevel/lib@native lib-2\n' +
                            ('net/app@native app-2\n' if 'net/app@native' in roots else ''))
        plan = self.plan(roots=['net/app@native', 'devel/missing@native'])
        with patch('emberbsd_sdk.sdk_client.run_source', side_effect=source):
            result = include_source_dependencies(plan, self.release, self.catalogs, self.cfg,
                                                  self.installer, self.cache)
        self.assertEqual(result['packages'], [])
        self.assertEqual(set(result['build_roots']), {'net/app@native', 'devel/missing@native'})


if __name__ == '__main__':
    unittest.main()
