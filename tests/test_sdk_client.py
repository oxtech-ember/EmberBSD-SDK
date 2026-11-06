# SPDX-License-Identifier: BSD-2-Clause
from copy import deepcopy
import hashlib
import io
import json
import platform
from contextlib import redirect_stderr
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.sdk_client import installation_plan, apply_plan, include_source_dependencies, install_selection
from emberbsd_sdk.package_cache import PackageCache
from emberbsd_sdk.package_install import PackageInstaller
from test_package_cache import Store


class Installer:
    def __init__(self):
        self.present, self.actions = set(), []
        self.recover_args = []
    def installed(self, config, *, recover=True):
        self.recover_args.append(recover)
        return self.present
    def check(self, path, config):
        self.actions.append(('check', path.name))
    def install(self, path, config, *, checked=False, configure=True, automatic=False):
        self.actions.append(('install', path.name, configure))
        self.present.add(path.stem)
    def check_updates(self, packages, archives, config):
        pass
    def configure(self, config):
        self.actions.append(('configure',))


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

    def test_cli_passes_explicit_https_proxy_and_direct_default(self):
        from emberbsd_sdk.sdk_client import main
        host = dict(os=platform.system(), arch=platform.machine(), abi='test', runtime='native')
        config = dict(schema='emberbsd.sdk-client.v1', host_prefix=str(self.root),
                      cache_dir=str(self.root / 'cache'), state_dir=str(self.root / 'state'),
                      trust_root=str(self.root / 'trust.pem'), url='https://packages.example',
                      https_ca=str(self.root / 'https-ca.pem'),
                      repositories={'native': dict(host=host, prefix=str(self.root / 'prefix'),
                                                   pkgdb=str(self.root / 'pkgdb'))})
        path = self.root / 'client.json'
        proxy = 'http://127.0.0.1:12345'
        for value in (None, proxy):
            with self.subTest(proxy=value):
                if value is None:
                    config.pop('https_proxy', None)
                else:
                    config['https_proxy'] = value
                path.write_text(json.dumps(config))
                with patch('emberbsd_sdk.sdk_client.load_host_prefix'), \
                     patch('emberbsd_sdk.sdk_client.ReleaseCrypto'), \
                     patch('emberbsd_sdk.sdk_client.HTTPStore', side_effect=SDKError('fixture stop')) as store, \
                     redirect_stderr(io.StringIO()):
                    self.assertEqual(main(['--config', str(path), 'plan', '--package', 'net/app@native']), 1)
                store.assert_called_once_with('https://packages.example',
                    ca_file=str(self.root / 'https-ca.pem'), proxy_url=value)

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

    def test_replacement_is_planned_without_mutation(self):
        self.installer.present.add('lib-0')
        plan = self.plan(sets=['task'])
        self.assertEqual(plan['packages'][0]['replaces'], ['lib-0'])
        self.assertEqual(plan['packages'][0]['action'], 'download')
        self.assertEqual(self.installer.actions, [])

    def test_selected_batch_is_installed_before_hooks_and_retries_failed_configuration(self):
        plan = self.plan(sets=['task'])
        def configure_once(config):
            self.installer.actions.append(('configure',))
            if configure.call_count == 1:
                raise SDKError('hook interrupted')
        with patch.object(self.installer, 'configure', side_effect=configure_once) as configure:
            with self.assertRaisesRegex(SDKError, 'hook interrupted'):
                apply_plan(plan, self.cfg, self.installer, self.cache, self.store)
            self.assertEqual([action[0] for action in self.installer.actions],
                             ['check', 'check', 'install', 'install', 'configure'])
            self.assertEqual([action[2] for action in self.installer.actions if action[0] == 'install'],
                             [False, False])
            self.installer.present.update(package['pkgname'] for package in plan['packages'])
            retry = self.plan(sets=['task'])
            self.assertTrue(all(package['action'] == 'installed' for package in retry['packages']))
            apply_plan(retry, self.cfg, self.installer, self.cache, self.store)
            self.assertEqual(configure.call_count, 2)
        self.assertTrue(self.installer.recover_args)
        self.assertFalse(any(self.installer.recover_args))

    def test_incompatible_update_stops_before_any_package_mutation(self):
        self.installer.present.add('lib-0')
        with patch.object(self.installer, 'check_updates', side_effect=SDKError('incompatible update')):
            with self.assertRaisesRegex(SDKError, 'incompatible update'):
                apply_plan(self.plan(sets=['task']), self.cfg, self.installer, self.cache, self.store)
        self.assertEqual([a[0] for a in self.installer.actions], ['check', 'check'])

    def test_apply_reuses_compatible_newer_provider_and_source_selection_uses_installed_name(self):
        root = self.root
        prefix, database = root / 'prefix', root / 'pkgdb'
        prefix.mkdir()
        database.mkdir()
        installed_name = 'libxml2-2.15.4nb5'
        installed_dir = database / installed_name
        installed_dir.mkdir()
        (installed_dir / '+CONTENTS').write_text('@cwd /prefix\n@name ' + installed_name + '\n')
        (installed_dir / '+BUILD_INFO').write_text('PROVIDES=/prefix/lib/libxml2.so.2\n')

        def package_archive(path, name, *, depends=(), providers=()):
            contents = '@cwd /producer/prefix\n@name ' + name + '\n' + ''.join(
                '@pkgdep ' + value + '\n' for value in depends)
            info = ''.join('PROVIDES=/producer/prefix/' + value + '\n' for value in providers)
            with tarfile.open(path, 'w:gz') as archive:
                for filename, data in (('+CONTENTS', contents), ('+BUILD_INFO', info)):
                    content = data.encode()
                    member = tarfile.TarInfo(filename)
                    member.size = len(content)
                    archive.addfile(member, io.BytesIO(content))
            return path

        provider = package_archive(root / 'provider.tgz', 'libxml2-2.15.4nb4',
                                   providers=('lib/libxml2.so.2',))
        consumer = package_archive(root / 'consumer.tgz', 'consumer-1',
                                   depends=('libxml2>=2.14.6nb2',))
        archives = {'provider': provider, 'consumer': consumer}
        artifacts = {}
        for identity, path, deps in (('provider', provider, []), ('consumer', consumer, ['provider'])):
            payload = path.read_bytes()
            artifacts[identity] = dict(id=identity, path=f'native/{identity}.tgz', kind='host-package',
                host=self.host, target=None, size=len(payload), sha256=hashlib.sha256(payload).hexdigest(),
                unpacked_size=1, depends=deps)

        class Cache:
            def __init__(self):
                self.root = root / 'client-cache'
                self.root.mkdir()
            def trim(self, *_args, **_kwargs):
                pass
            def fetch(self, _store, _key, artifact):
                return archives[artifact['id']]

        class RealInstaller(PackageInstaller):
            def __init__(self):
                self.crypto = object()
                self.present = {installed_name}
                self.checked_updates = []
                self.installed_paths = []
                self.automatic_values = []
            def installed(self, _config, *, recover=True):
                return self.present
            def check(self, _path, _config):
                pass
            def check_updates(self, packages, _archives, _config):
                self.checked_updates.extend(packages)
            def install(self, path, _config, *, checked=False, configure=True, automatic=False):
                self.installed_paths.append(path.name)
                self.automatic_values.append(automatic)
            def configure(self, _config):
                pass

        cfg = dict(host=self.host, prefix=str(prefix), pkgdb=str(database))
        installer, cache = RealInstaller(), Cache()
        plan = dict(roots=['cad/consumer@native'], build_roots=[], packages=[
            dict(root='textproc/libxml2@native', repository='native', pkgname='libxml2-2.15.4nb4',
                 action='download', replaces=[installed_name], artifact=artifacts['provider']),
            dict(root='cad/consumer@native', repository='native', pkgname='consumer-1',
                 action='download', replaces=[], artifact=artifacts['consumer']),
        ], download_size=sum(item['size'] for item in artifacts.values()), unpacked_size=2)
        selected = []
        def pmatch(_crypto, pattern, name):
            if pattern.startswith('libxml2>='):
                return name in ('libxml2-2.15.4nb4', installed_name)
            return False
        with patch('emberbsd_sdk.sdk_client.check_space'), \
             patch('emberbsd_sdk.package_install.inspect_install_paths'), \
             patch('emberbsd_sdk.package_updates._matches', side_effect=pmatch), \
             patch('emberbsd_sdk.package_automatic.finish') as finish, \
             patch('emberbsd_sdk.sdk_client.build_missing', side_effect=lambda _cfg, _roots, rows: selected.extend(rows)):
            install_selection(plan, dict(repositories={'native': cfg}), installer, cache, object())
        provider_row = next(item for item in installer.checked_updates if item['root'].startswith('textproc/'))
        self.assertEqual(provider_row['pkgname'], installed_name)
        self.assertIsNone(provider_row['artifact'])
        self.assertEqual(installer.installed_paths, [consumer.name])
        self.assertEqual(installer.automatic_values, [False])
        finish.assert_called_once_with(installer.crypto, cfg, 'consumer-1')
        self.assertIn(('textproc/libxml2@native', installed_name), selected)

    def test_all_archives_checked_before_any_install(self):
        plan = self.plan(sets=['task'])
        apply_plan(plan, self.cfg, self.installer, self.cache, self.store)
        self.assertEqual([a[0] for a in self.installer.actions],
                         ['check', 'check', 'install', 'install', 'configure'])
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
                self.assertEqual(self.installer.actions[-1][0], 'configure')
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

    def _source(self):
        self.cfg['source'] = {name: str(self.root / name) for name in ('ports_root', 'pkgsrc', 'makeconf', 'state')}
        (self.root / 'ports_root/profiles/package-sets').mkdir(parents=True, exist_ok=True)

    def _graph(self, order, edges, names):
        state = self.root / 'state'
        state.mkdir(parents=True, exist_ok=True)
        (state / 'order').write_text(''.join(root + '\n' for root in order))
        (state / 'edges').write_text(''.join(dep + ' ' + node + '\n' for dep, node in edges))
        (state / 'packages').write_text(''.join(root + ' ' + name + '\n' for root, name in names.items()))
        return state

    def _channel(self, packages):
        artifacts, entries = [], []
        for identity, root, pkgname, depends in packages:
            artifacts.append(dict(id=identity, path=f'native/{pkgname}.tgz', kind='host-package', host=self.host,
                                  target=None, size=13, sha256=hashlib.sha256(self.store.payload).hexdigest(),
                                  unpacked_size=20, depends=list(depends)))
            entries.append(dict(root=root, pkgname=pkgname, artifact=identity))
        release = dict(release_id='current', artifacts=artifacts)
        catalogs = [dict(repository='native', sets={}, packages=entries)]
        return release, catalogs

    def _waves(self, order, edges, names, refresh, *, installed=(), accepted='same', build=None):
        self._source()
        state = self._graph(order, edges, names)
        original_order = (state / 'order').read_bytes()
        original_edges = (state / 'edges').read_bytes()
        self.installer.present.update(installed)
        plan = dict(roots=list(order), build_roots=list(order), packages=[], source_packages=dict(names),
                    download_size=0, unpacked_size=0)
        calls = []

        def source(config, roots, command, binaries=(), controller_mode=None):
            calls.append((list(roots), command, controller_mode,
                          (state / 'order').read_text(), (state / 'edges').read_text()))
            if build is not None:
                build(roots)

        errors = io.StringIO()
        with patch('emberbsd_sdk.sdk_client.run_source', side_effect=source), redirect_stderr(errors):
            install_selection(plan, self.cfg, self.installer, self.cache, self.store,
                              refresh_current=refresh, accepted_channel=accepted)
        self.assertEqual((state / 'order').read_bytes(), original_order)
        self.assertEqual((state / 'edges').read_bytes(), original_edges)
        return calls, errors.getvalue()

    def test_running_install_adopts_new_current_between_source_waves(self):
        lib, app = 'devel/lib@native', 'net/app@native'
        published = self._channel([('app', app, 'app-1', [])])
        snapshots = iter(((*self._channel([]), 'same'), (*published, 'newer')))

        def refresh():
            return next(snapshots)

        calls, errors = self._waves(
            [lib, app], [(lib, lib), (lib, app), (app, app)], {lib: 'lib-1', app: 'app-1'}, refresh)
        self.assertEqual([call[0] for call in calls], [[lib]])
        self.assertEqual({call[2] for call in calls}, {'build'})
        self.assertEqual([call[1] for call in self.store.calls],
                         ['current/host/darwin-arm64/app-1.tgz'])
        self.assertIn('Current changed', errors)
        self.assertIn('installing them instead of building', errors)
        self.assertNotIn('generation', errors)

    def test_unchanged_current_does_not_rebuild_or_download(self):
        lib, app = 'devel/lib@native', 'net/app@native'
        empty = (*self._channel([]), 'same')

        def refresh():
            return empty

        calls, errors = self._waves(
            [lib, app], [(lib, lib), (lib, app), (app, app)], {lib: 'lib-1', app: 'app-1'}, refresh)
        self.assertEqual([call[0] for call in calls], [[lib], [app]])
        self.assertEqual(self.store.calls, [])
        self.assertNotIn('Current changed', errors)
        self.assertNotIn('installing them instead of building', errors)

    def test_expired_current_is_not_adopted_and_the_build_continues(self):
        lib, app = 'devel/lib@native', 'net/app@native'
        seen = {'n': 0}

        def refresh():
            seen['n'] += 1
            if seen['n'] > 1:
                raise SDKError('channel has expired; check the local clock or obtain a fresh channel')
            return (*self._channel([]), 'same')

        calls, errors = self._waves(
            [lib, app], [(lib, lib), (lib, app), (app, app)], {lib: 'lib-1', app: 'app-1'}, refresh)
        self.assertEqual([call[0] for call in calls], [[lib], [app]])
        self.assertEqual(self.store.calls, [])
        self.assertIn('expired', errors)
        self.assertNotIn('generation', errors)

    def test_untrusted_current_stops_without_adoption(self):
        lib, app = 'devel/lib@native', 'net/app@native'
        seen = {'n': 0}

        def refresh():
            seen['n'] += 1
            if seen['n'] > 1:
                raise SDKError('CMS signature/certificate verification failed; check trust')
            return (*self._channel([]), 'same')

        with self.assertRaisesRegex(SDKError, 'CMS signature'):
            self._waves([lib, app], [(lib, lib), (lib, app), (app, app)], {lib: 'lib-1', app: 'app-1'}, refresh,
                        build=lambda roots: self.installer.present.add('lib-1'))
        self.assertEqual(self.store.calls, [])
        self.assertNotIn('app-1', self.installer.present)

    def test_unreachable_current_continues_without_a_new_download(self):
        lib, app = 'devel/lib@native', 'net/app@native'
        seen = {'n': 0}

        def refresh():
            seen['n'] += 1
            if seen['n'] > 1:
                raise OSError('timed out')
            return (*self._channel([]), 'same')

        calls, errors = self._waves(
            [lib, app], [(lib, lib), (lib, app), (app, app)], {lib: 'lib-1', app: 'app-1'}, refresh)
        self.assertEqual([call[0] for call in calls], [[lib], [app]])
        self.assertEqual(self.store.calls, [])
        self.assertIn('could not be reread', errors)

    def test_pkgname_mismatch_stays_on_the_source_wave(self):
        app = 'net/app@native'
        published = (*self._channel([('app', app, 'app-2', [])]), 'newer')
        calls, _errors = self._waves([app], [(app, app)], {app: 'app-1'}, lambda: published,
                                     build=lambda roots: self.installer.present.add('app-1'))
        self.assertEqual([call[0] for call in calls], [[app]])
        self.assertEqual(self.store.calls, [])

    def test_newer_installed_native_is_not_replaced_or_rebuilt(self):
        lib = 'devel/lib@native'
        published = (*self._channel([('lib', lib, 'lib-1', [])]), 'newer')
        calls, errors = self._waves([lib], [(lib, lib)], {lib: 'lib-1'}, lambda: published, installed={'lib-2'})
        self.assertEqual([call[0] for call in calls], [[lib]])
        self.assertEqual(self.store.calls, [])
        self.assertNotIn('lib-1', self.installer.present)
        self.assertNotIn('without installing', errors)
        self.assertNotIn('generation', errors)

    def test_published_consumer_keeps_a_newer_installed_dependency(self):
        provider_root, consumer_root = 'textproc/libxml2@native', 'cad/consumer@native'
        installed_name = 'libxml2-2.15.4nb5'
        prefix, database = self.root / 'prefix', self.root / 'pkgdb'
        prefix.mkdir()
        installed_dir = database / installed_name
        installed_dir.mkdir(parents=True)
        (installed_dir / '+CONTENTS').write_text('@cwd /prefix\n@name ' + installed_name + '\n')
        (installed_dir / '+BUILD_INFO').write_text('PROVIDES=/prefix/lib/libxml2.so.2\n')

        def package_archive(path, name, *, depends=(), providers=()):
            contents = '@cwd /producer/prefix\n@name ' + name + '\n' + ''.join(
                '@pkgdep ' + value + '\n' for value in depends)
            info = ''.join('PROVIDES=/producer/prefix/' + value + '\n' for value in providers)
            with tarfile.open(path, 'w:gz') as archive:
                for filename, data in (('+CONTENTS', contents), ('+BUILD_INFO', info)):
                    content = data.encode()
                    member = tarfile.TarInfo(filename)
                    member.size = len(content)
                    archive.addfile(member, io.BytesIO(content))
            return path

        provider = package_archive(self.root / 'provider.tgz', 'libxml2-2.15.4nb4',
                                   providers=('lib/libxml2.so.2',))
        consumer = package_archive(self.root / 'consumer.tgz', 'consumer-1',
                                   depends=('libxml2>=2.14.6nb2',))
        payloads = {'libxml2-2.15.4nb4': provider.read_bytes(), 'consumer-1': consumer.read_bytes()}

        class CatalogStore(Store):
            def verify(self, key, size, sha256, *, sink):
                self.calls.append(('get', key))
                payload = payloads['libxml2-2.15.4nb4' if key.endswith('libxml2-2.15.4nb4.tgz') else 'consumer-1']
                if len(payload) != size or hashlib.sha256(payload).hexdigest() != sha256:
                    raise SDKError('invalid downloaded bytes')
                sink.write(payload)

        class ReuseInstaller(PackageInstaller):
            def __init__(self):
                self.crypto = object()
                self.present = {installed_name}
                self.checked_updates = []
                self.installed_paths = []
            def installed(self, _config, *, recover=True):
                return self.present
            def check(self, _path, _config):
                pass
            def check_updates(self, packages, _archives, _config):
                self.checked_updates.extend(packages)
            def install(self, path, _config, *, checked=False, configure=True, automatic=False):
                self.installed_paths.append(path.name)
            def configure(self, _config):
                pass

        artifacts = []
        for identity, filename, deps in (('provider', 'libxml2-2.15.4nb4.tgz', []),
                                        ('consumer', 'consumer-1.tgz', ['provider'])):
            payload = payloads['libxml2-2.15.4nb4' if identity == 'provider' else 'consumer-1']
            artifacts.append(dict(id=identity, path='native/' + filename, kind='host-package', host=self.host,
                                  target=None, size=len(payload), sha256=hashlib.sha256(payload).hexdigest(),
                                  unpacked_size=1, depends=deps))
        release = dict(release_id='current', artifacts=artifacts)
        catalogs = [dict(repository='native', sets={}, packages=[
            dict(root=provider_root, pkgname='libxml2-2.15.4nb4', artifact='provider'),
            dict(root=consumer_root, pkgname='consumer-1', artifact='consumer')])]
        self._source()
        cfg = dict(host=self.host, prefix=str(prefix), pkgdb=str(database))
        self.cfg = dict(repositories={'native': cfg}, source=self.cfg['source'])
        self._graph([provider_root, consumer_root],
                    [(provider_root, provider_root), (provider_root, consumer_root), (consumer_root, consumer_root)],
                    {provider_root: 'libxml2-2.15.4nb4', consumer_root: 'consumer-1'})
        plan = dict(roots=[provider_root, consumer_root], build_roots=[provider_root, consumer_root], packages=[],
                    source_packages={provider_root: 'libxml2-2.15.4nb4', consumer_root: 'consumer-1'},
                    download_size=0, unpacked_size=0)
        installer, store = ReuseInstaller(), CatalogStore(b'')
        built = []

        def pmatch(_crypto, pattern, name):
            if pattern.startswith('libxml2>='):
                return name in ('libxml2-2.15.4nb4', installed_name)
            return False

        with patch('emberbsd_sdk.sdk_client.check_space'), \
                patch('emberbsd_sdk.package_install.inspect_install_paths'), \
                patch('emberbsd_sdk.package_updates._matches', side_effect=pmatch), \
                patch('emberbsd_sdk.package_automatic.finish'), \
                patch('emberbsd_sdk.sdk_client.run_source', side_effect=lambda *_args, **_kwargs: built.append(_args)):
            install_selection(plan, self.cfg, installer, self.cache, store,
                              refresh_current=lambda: (release, catalogs, 'newer'), accepted_channel='same')
        self.assertEqual(built, [])
        self.assertEqual(len(installer.installed_paths), 1)
        provider_row = next(item for item in installer.checked_updates if item['root'] == provider_root)
        self.assertEqual(provider_row['pkgname'], installed_name)
        self.assertIsNone(provider_row['artifact'])
        self.assertNotIn('libxml2-2.15.4nb4', installer.present)

    def test_saved_edges_keep_target_fed_native_context(self):
        tool, app = 'cross/cross-libtool-base@native', 'x11/app'
        edges = [(tool, tool), (tool, app), (app, app)]

        def build(roots):
            if roots == [app]:
                self.installer.present.add('app-1')

        calls, _errors = self._waves(
            [tool, app], edges, {tool: 'libtool-base-1', app: 'app-1'},
            lambda: (*self._channel([]), 'same'), installed={'libtool-base-2'}, build=build)
        self.assertEqual([call[0] for call in calls], [[tool], [app]])
        self.assertTrue(all(call[1] == 'build' and call[2] == 'build' for call in calls))
        self.assertIn(f'{tool} {app}', calls[0][4])
        self.assertEqual(calls[0][3].split(), [tool])
        self.assertIn(f'{tool} {app}', calls[1][4])
        self.assertEqual(self.store.calls, [])
        self.assertNotIn('libtool-base-1', self.installer.present)

    def test_target_wave_requires_the_exact_pkgname(self):
        app = 'x11/app'
        with self.assertRaisesRegex(SDKError, 'without installing'):
            self._waves([app], [(app, app)], {app: 'app-1'}, lambda: (*self._channel([]), 'same'))

    def test_independent_source_packages_share_one_build_wave(self):
        lib, tool = 'devel/lib@native', 'devel/tool@native'
        calls, _errors = self._waves(
            [lib, tool], [(lib, lib), (tool, tool)], {lib: 'lib-1', tool: 'tool-1'},
            lambda: (*self._channel([]), 'same'))
        self.assertEqual(len(calls), 1)
        self.assertEqual(set(calls[0][0]), {lib, tool})
        self.assertEqual(calls[0][2], 'build')

    def test_missing_saved_graph_keeps_one_selected_build(self):
        self._source()
        plan = dict(roots=['devel/missing@native'], build_roots=['devel/missing@native'], packages=[],
                    source_packages={'devel/missing@native': 'missing-1'}, download_size=0, unpacked_size=0)
        with patch('emberbsd_sdk.sdk_client.build_missing') as build, \
                patch('emberbsd_sdk.sdk_client.run_source') as source:
            install_selection(plan, self.cfg, self.installer, self.cache, self.store,
                              refresh_current=lambda: (self.release, self.catalogs, 'same'),
                              accepted_channel='same')
        build.assert_called_once()
        self.assertEqual(build.call_args.args[1], ['devel/missing@native'])
        source.assert_not_called()

    def test_narrowed_order_is_restored_when_the_wave_fails(self):
        app = 'devel/app@native'
        self._source()
        state = self._graph([app], [(app, app)], {app: 'app-1'})
        original = (state / 'order').read_bytes()
        plan = dict(roots=[app], build_roots=[app], packages=[], source_packages={app: 'app-1'},
                    download_size=0, unpacked_size=0)

        def source(config, roots, command, binaries=(), controller_mode=None):
            self.assertEqual((state / 'order').read_text().split(), [app])
            raise SDKError('Ports build failed (1); compatible objects and logs were retained')

        with patch('emberbsd_sdk.sdk_client.run_source', side_effect=source):
            with self.assertRaisesRegex(SDKError, 'Ports build failed'):
                install_selection(plan, self.cfg, self.installer, self.cache, self.store,
                                  refresh_current=lambda: (*self._channel([]), 'same'),
                                  accepted_channel='same')
        self.assertEqual((state / 'order').read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
