# SPDX-License-Identifier: BSD-2-Clause
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.host_identity import native_cpu
from emberbsd_sdk.package_install import PackageInstaller, inspect_install_paths, PORTABLE_PREFIX
from emberbsd_sdk.package_updates import check_updates
from emberbsd_sdk.binary_release import _arch, _package_payload
from emberbsd_sdk import bootstrap_bundle, package_relocation


@unittest.skipUnless(os.environ.get('EMBERBSD_TEST_HOST_PREFIX'), 'select native Ports pkg_tools')
class InstallTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from emberbsd_sdk.host_prefix import load_host_prefix
        from emberbsd_sdk.release_signing import ReleaseCrypto
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.root = Path(cls.tmp.name)
        cls.crypto = ReleaseCrypto(load_host_prefix(os.environ['EMBERBSD_TEST_HOST_PREFIX'],
            required_roles=('openssl', 'pkg_admin', 'pkg_info', 'pkg_create', 'pkg_add', 'pkg_delete')))
        cls.ca = cls.crypto.create_root(cls.root / 'root')
        cls.signer = cls.crypto.issue_signer(cls.ca, cls.root / 'signer')

    def setUp(self):
        self.work = self.root / self._testMethodName
        self.work.mkdir()
        self.destination = self.work / 'prefix'
        self.cfg = dict(prefix=str(self.destination), pkgdb=str(self.destination / 'pkgdb'),
                        host=dict(os=platform.system(), arch=native_cpu(), abi='test', runtime='native'))

    def package(self, *, target=False, link=None, commands='', arch='aarch64', requires_self=False, preserve=False,
                payload='installed data', provides=False, install_script=None, install_script_mode=0o755,
                package_name='fixture-1.0',
                payload_path='payload', executable=False, build_version=None, deinstall_script=None,
                sizes=None):
        stage = self.work / 'stage'
        stage.mkdir()
        payload_file = stage / payload_path
        payload_file.parent.mkdir(parents=True, exist_ok=True)
        if link:
            payload_file.symlink_to(link)
        else:
            payload_file.write_text(payload)
        if executable:
            payload_file.chmod(0o755)
        prefix = '/usr/pkg' if target else str(self.destination)
        (stage / 'PLIST').write_text('@name ' + package_name + '\n' + payload_path + '\n' + commands)
        (stage / 'INFO').write_text('PKGPATH=devel/fixture\nDISTNAME=fixture-1.0\nLICENSE=2-clause-bsd\n' +
            f'LOCALBASE={prefix}\nCFLAGS=-I{prefix}/include\n' +
            (f'REQUIRES={prefix}/payload\n' if requires_self else '') +
            (f'PROVIDES={prefix}/payload\n' if provides else '') +
            (f'OPSYS=NetBSD\nMACHINE_ARCH={arch}\nOS_VERSION=11.0\n' if target else
             f'OPSYS={platform.system()}\nMACHINE_ARCH={_arch(native_cpu())}\nOS_VERSION={platform.release()}\n'))
        unsigned, signed = self.work / 'unsigned.tgz', self.work / (package_name + '.tgz')
        extra = []
        if sizes is not None:
            for flag, name, value in (('-s', 'SIZE_PKG', sizes[0]), ('-S', 'SIZE_ALL', sizes[1])):
                (stage / name).write_text(str(value) + '\n')
                extra += [flag, stage / name]
        if build_version is not None:
            (stage / 'BUILD_VERSION').write_text(build_version)
            extra += ['-b', stage / 'BUILD_VERSION']
        if preserve:
            (stage / 'PRESERVE').write_text('bootstrap tool\n')
            (stage / 'INFO').chmod(0o444)
            extra = ['-n', stage / 'PRESERVE']
        if install_script is not None:
            script = stage / 'INSTALL'
            script.write_text(install_script); script.chmod(install_script_mode)
            extra += ['-i', script]
        if deinstall_script is not None:
            script = stage / 'DEINSTALL'
            script.write_text(deinstall_script)
            script.chmod(0o755)
            extra += ['-k', script]
        self.crypto.run_package('pkg_create', [*extra, '-c', '-fixture', '-d', '-fixture',
            '-f', stage / 'PLIST', '-B', stage / 'INFO', '-p', stage, '-I', prefix, unsigned])
        self.crypto.sign_package(unsigned, signed, self.signer, self.ca.certificate)
        return signed

    @unittest.skipUnless(platform.system() == 'NetBSD', 'native NetBSD lifecycle')
    def test_native_target_signed_package_hooks_and_replacement(self):
        arch = _arch(native_cpu())
        major = platform.release().split('.', 1)[0]
        self.cfg = dict(prefix=str(self.destination), pkgdb=str(self.destination / 'pkgdb'),
                        target=dict(arch=arch, triplet=arch + '--netbsd',
                                    abi='netbsd-' + major, runtime='libc++'),
                        install_mode='native', os_version=platform.release())
        installed = self.work / 'installed-hook'
        removed = self.work / 'removed-hook'
        post = self.work / 'post-install'
        deinstall = self.work / 'deinstall-hook'
        commands = '@exec touch ' + str(installed) + '\n@unexec touch ' + str(removed) + '\n'
        hook = '#!/bin/sh\n[ "$2" != POST-INSTALL ] || touch ' + str(post) + '\n'
        unhook = '#!/bin/sh\n[ "$2" != DEINSTALL ] || touch ' + str(deinstall) + '\n'
        first = self.package(package_name='fixture-1.0', commands=commands,
                             install_script=hook, deinstall_script=unhook)
        installer = PackageInstaller(self.crypto, self.ca.certificate)
        installer.install(first, self.cfg)
        self.assertTrue(installed.exists())
        self.assertTrue(post.exists())
        self.assertIn('fixture-1.0', installer.installed(self.cfg))
        shutil.rmtree(self.work / 'stage')
        second = self.package(package_name='fixture-2.0', payload='updated')
        installer.install(second, self.cfg)
        self.assertTrue(removed.exists())
        self.assertTrue(deinstall.exists())
        self.assertEqual((self.destination / 'payload').read_text(), 'updated')
        self.assertIn('fixture-2.0', installer.installed(self.cfg))
        self.assertNotIn('fixture-1.0', installer.installed(self.cfg))

    def test_real_pkgtools_preserve_automatic_state_and_recover(self):
        from emberbsd_sdk import package_automatic as automatic
        for state in ('yes', 'no', None):
            with self.subTest(state=state):
                case = self.work / ('state-' + str(state))
                case.mkdir()
                self.work = case
                self.destination = case / 'prefix'
                self.cfg = dict(prefix=str(self.destination),
                                pkgdb=str(self.destination / 'pkgdb'),
                                host=dict(os=platform.system(), arch=native_cpu(),
                                          abi='test', runtime='native'))
                installer = PackageInstaller(self.crypto, self.ca.certificate)
                first = self.package(package_name='fixture-1.0')
                installer.install(first, self.cfg, automatic=(state == 'yes'))
                if state == 'no':
                    self.crypto.run_package('pkg_admin', ['-K', self.cfg['pkgdb'],
                                            'set', 'automatic=no', 'fixture-1.0'])
                shutil.rmtree(case / 'stage')
                second = self.package(package_name='fixture-2.0')
                packages = [{'pkgname': 'fixture-2.0', 'root': 'devel/fixture',
                             'replaces': ['fixture-1.0']}]
                chosen = automatic.plan(self.cfg, packages, ['devel/fixture'])
                self.assertEqual(chosen['fixture-2.0'], state)
                automatic.prepare(self.cfg, 'fixture-2.0', state)
                installer.install(second, self.cfg, automatic=(state == 'yes'))
                failed = subprocess.CompletedProcess([], 1, b'', b'interrupted')
                with patch('emberbsd_sdk.package_automatic.subprocess.run',
                           return_value=failed):
                    with self.assertRaises(SDKError):
                        automatic.finish(self.crypto, self.cfg, 'fixture-2.0')
                automatic.recover(self.crypto, self.cfg, ['fixture-2.0'])
                info = Path(self.cfg['pkgdb']) / 'fixture-2.0/+INSTALLED_INFO'
                contents = info.read_text() if info.exists() else ''
                values = [line.split('=', 1)[1].lower() for line in contents.splitlines()
                          if line.lower().startswith('automatic=')]
                self.assertEqual(values, [] if state is None else [state])
                self.work = case.parent

        case = self.work / 'new-dependency'
        case.mkdir()
        self.work = case
        self.destination = case / 'prefix'
        self.cfg = dict(prefix=str(self.destination), pkgdb=str(self.destination / 'pkgdb'),
                        host=dict(os=platform.system(), arch=native_cpu(),
                                  abi='test', runtime='native'))
        package = self.package()
        self.destination.mkdir()  # pkgdb lock creates its parent before planning.
        value = automatic.plan(self.cfg, [{'pkgname': 'fixture-1.0',
                                'root': 'devel/fixture', 'replaces': []}], [])['fixture-1.0']
        automatic.prepare(self.cfg, 'fixture-1.0', value)
        PackageInstaller(self.crypto, self.ca.certificate).install(package, self.cfg,
                                                                  automatic=(value == 'yes'))
        automatic.finish(self.crypto, self.cfg, 'fixture-1.0')
        self.assertIn('automatic=yes', (Path(self.cfg['pkgdb']) /
                      'fixture-1.0/+INSTALLED_INFO').read_text().lower())

    def localbase(self, database):
        return self.crypto.run_package('pkg_info', ['-K', str(database), '-Q', 'LOCALBASE', 'fixture-1.0']).decode().strip()

    def test_native_package_is_owned_by_pkgdb_and_repeat_detected(self):
        installer = PackageInstaller(self.crypto, self.ca.certificate)
        package = self.package()
        installer.install(package, self.cfg)
        self.assertEqual((self.destination / 'payload').read_text(), 'installed data')
        self.assertIn('fixture-1.0', installer.installed(self.cfg))
        self.assertEqual(installer.inventory(self.cfg),
                         [dict(pkgname='fixture-1.0', root='devel/fixture@native')])

    def test_signed_pkg_tools_replacement_preserves_installed_consumer(self):
        installer = PackageInstaller(self.crypto, self.ca.certificate)
        original = self.package(package_name='fixture-1.0', provides=True)
        installer.install(original, self.cfg)
        original = original.with_name('original.tgz')
        (self.work / 'fixture-1.0.tgz').replace(original)
        shutil.rmtree(self.work / 'stage')
        consumer = self.package(package_name='consumer-1.0', payload_path='share/consumer',
                                commands='@pkgdep fixture>=1\n')
        installer.install(consumer, self.cfg)
        consumer = consumer.with_name('consumer.tgz')
        (self.work / 'consumer-1.0.tgz').replace(consumer)
        shutil.rmtree(self.work / 'stage')

        old_required_by = self.destination / 'pkgdb/fixture-1.0/+REQUIRED_BY'
        self.assertIn('consumer-1.0', old_required_by.read_text().splitlines())
        replacement = self.package(package_name='fixture-2.0', provides=True,
                                   payload='replacement data')
        packages = [dict(pkgname='fixture-2.0', replaces=['fixture-1.0'],
                         artifact={'id': 'replacement'}, action='download')]
        check_updates(self.crypto, self.cfg, packages,
                      {'replacement': replacement}, installer.installed(self.cfg))
        installer.install(replacement, self.cfg)

        self.assertEqual((self.destination / 'payload').read_text(), 'replacement data')
        self.assertNotIn('fixture-1.0', installer.installed(self.cfg))
        self.assertIn('fixture-2.0', installer.installed(self.cfg))
        self.assertIn('consumer-1.0',
                      (self.destination / 'pkgdb/fixture-2.0/+REQUIRED_BY').read_text().splitlines())
        self.assertIn('@pkgdep fixture>=1',
                      (self.destination / 'pkgdb/consumer-1.0/+CONTENTS').read_text())

    def test_split_package_removes_the_conflicting_old_package_and_keeps_its_consumer(self):
        installer = PackageInstaller(self.crypto, self.ca.certificate)
        installer.install(self.package(package_name='fixture-1.0', provides=True), self.cfg)
        shutil.rmtree(self.work / 'stage')
        installer.install(self.package(package_name='consumer-1.0', payload_path='share/consumer',
                                       commands='@pkgdep fixture>=1\n'), self.cfg)
        shutil.rmtree(self.work / 'stage')
        # The library moved to fixture-lib; fixture-2.0 keeps only the frontend.
        library = self.package(package_name='fixture-lib-1.0', provides=True, payload='moved library',
                               commands='@pkgcfl fixture<2\n')
        shutil.rmtree(self.work / 'stage')
        frontend = self.package(package_name='fixture-2.0', payload_path='bin/fixture',
                                commands='@pkgdep fixture-lib>=1\n')
        with self.assertRaisesRegex(SDKError, 'conflicts with'):
            installer.install(library, self.cfg)
        packages = [dict(pkgname='fixture-lib-1.0', replaces=[], removes=['fixture-1.0'],
                         artifact={'id': 'library'}, action='download'),
                    dict(pkgname='fixture-2.0', replaces=['fixture-1.0'], removes=[],
                         artifact={'id': 'frontend'}, action='download')]
        check_updates(self.crypto, self.cfg, packages, {'library': library, 'frontend': frontend},
                      installer.installed(self.cfg))
        with self.assertRaisesRegex(SDKError, 'coordinated ABI update'):
            check_updates(self.crypto, self.cfg, packages[1:], {'frontend': frontend}, installer.installed(self.cfg))
        installer.remove('fixture-1.0', self.cfg)
        installer.install(library, self.cfg)
        installer.install(frontend, self.cfg)
        installer.rebuild_tree(self.cfg)
        self.assertEqual(installer.installed(self.cfg), {'fixture-lib-1.0', 'fixture-2.0', 'consumer-1.0'})
        self.assertEqual((self.destination / 'payload').read_text(), 'moved library')
        self.assertIn('consumer-1.0',
                      (self.destination / 'pkgdb/fixture-2.0/+REQUIRED_BY').read_text().splitlines())
        self.assertIn('fixture-2.0',
                      (self.destination / 'pkgdb/fixture-lib-1.0/+REQUIRED_BY').read_text().splitlines())

    def test_target_scripts_do_not_run_on_host(self):
        marker = self.work / 'executed'
        package = self.package(target=True, commands=f'@exec touch {marker}\n')
        self.cfg = dict(prefix='/usr/pkg', pkgdb='/usr/pkg/pkgdb', sysroot=str(self.work / 'sysroot'),
                        target=dict(arch='aarch64', triplet='aarch64--netbsd', abi='11.0', runtime='test'),
                        os_version='11.0')
        installer = PackageInstaller(self.crypto, self.ca.certificate)
        installer.install(package, self.cfg)
        self.assertFalse(marker.exists())
        self.assertEqual((self.work / 'sysroot/usr/pkg/payload').read_text(), 'installed data')
        self.assertIn('fixture-1.0', installer.installed(self.cfg))
        self.assertIn('LOCALBASE=/usr/pkg\n', (self.work / 'sysroot/usr/pkg/pkgdb/fixture-1.0/+BUILD_INFO').read_text())

    def test_escaping_link_refused_before_install(self):
        package = self.package(link='../../outside')
        with self.assertRaisesRegex(SDKError, 'escape'):
            PackageInstaller(self.crypto, self.ca.certificate).install(package, self.cfg)
        self.assertFalse(self.destination.exists())

    def test_absolute_link_inside_the_package_prefix_is_accepted(self):
        # pkgsrc Qt6 modules link lib/pkgconfig/*.pc to the qt6 subprefix.
        package = self.package(payload_path='lib/pkgconfig/Qt6Svg.pc',
                               link=str(self.destination / 'qt6/lib/pkgconfig/Qt6Svg.pc'))
        inspect_install_paths(package, self.cfg)

    def test_absolute_link_outside_the_package_prefix_is_refused(self):
        for link in (str(self.work / 'outside'), str(self.destination) + '-sibling/x',
                     str(self.destination / 'qt6/../../outside')):
            with self.subTest(link=link):
                package = self.package(payload_path='lib/pkgconfig/Qt6Svg.pc', link=link)
                with self.assertRaisesRegex(SDKError, 'escape'):
                    inspect_install_paths(package, self.cfg)
                self.assertFalse(self.destination.exists())
                package.unlink()
                shutil.rmtree(self.work / 'stage')

    def test_absolute_link_in_a_sysroot_is_inspected_again_after_installation(self):
        package = self.package(target=True, payload_path='lib/pkgconfig/Qt6Svg.pc',
                               link='/usr/pkg/qt6/lib/pkgconfig/Qt6Svg.pc')
        self.cfg = dict(prefix='/usr/pkg', pkgdb='/usr/pkg/pkgdb', sysroot=str(self.work / 'sysroot'),
                        target=dict(arch='aarch64', triplet='aarch64--netbsd', abi='11.0', runtime='test'),
                        os_version='11.0')
        installer = PackageInstaller(self.crypto, self.ca.certificate)
        installer.install(package, self.cfg)
        installed = self.work / 'sysroot/usr/pkg/lib/pkgconfig/Qt6Svg.pc'
        self.assertEqual(os.readlink(installed), '/usr/pkg/qt6/lib/pkgconfig/Qt6Svg.pc')
        inspect_install_paths(package, self.cfg)
        installed.unlink()
        installed.symlink_to(self.work / 'outside')
        with self.assertRaisesRegex(SDKError, 'escape'):
            inspect_install_paths(package, self.cfg)

    def test_absolute_link_in_a_prefix_independent_package_is_refused(self):
        package = self.package(link=str(self.destination / 'qt6/target'), commands=PORTABLE_PREFIX + '\n')
        with self.assertRaisesRegex(SDKError, 'relocated'):
            inspect_install_paths(package, self.cfg)

    def test_target_amd64_uses_pkgsrc_machine_arch(self):
        package = self.package(target=True, arch='x86_64')
        self.cfg = dict(prefix='/usr/pkg', pkgdb='/usr/pkg/pkgdb', sysroot=str(self.work / 'sysroot'),
                        target=dict(arch='amd64', triplet='x86_64--netbsd', abi='11.0', runtime='test'),
                        os_version='11.0')
        installer = PackageInstaller(self.crypto, self.ca.certificate)
        installer.install(package, self.cfg)
        self.assertIn('fixture-1.0', installer.installed(self.cfg))

    def test_existing_escaping_directory_link_refused(self):
        package = self.package()
        self.destination.mkdir()
        (self.destination / 'payload').symlink_to(self.work / 'outside')
        with self.assertRaisesRegex(SDKError, 'escape'):
            inspect_install_paths(package, self.cfg)

    def test_unqualified_native_prefix_relocation_is_refused(self):
        package = self.package()
        self.cfg['prefix'] = str(self.work / 'different-prefix')
        with self.assertRaisesRegex(SDKError, 'prefix'):
            inspect_install_paths(package, self.cfg)

    def test_signed_prefix_independent_package_installs_into_selected_prefix(self):
        package = self.package(commands=PORTABLE_PREFIX + '\n')
        self.cfg['prefix'] = str(self.work / 'different-prefix')
        self.cfg['pkgdb'] = self.cfg['prefix'] + '/pkgdb'
        PackageInstaller(self.crypto, self.ca.certificate).install(package, self.cfg)
        self.assertEqual((Path(self.cfg['prefix']) / 'payload').read_text(), 'installed data')
        self.assertEqual(self.localbase(self.cfg['pkgdb']), self.cfg['prefix'])
        self.assertIn('CFLAGS=-I' + str(self.destination) + '/include',
                      (Path(self.cfg['pkgdb']) / 'fixture-1.0/+BUILD_INFO').read_text())

    def test_independent_metadata_recovery_does_not_rewrite_payload(self):
        package = self.package(commands=PORTABLE_PREFIX + '\n')
        destination = self.work / 'different-prefix'
        database = destination / 'pkgdb'
        self.cfg.update(prefix=str(destination), pkgdb=str(database))
        installer = PackageInstaller(self.crypto, self.ca.certificate)
        with patch.object(package_relocation, 'relocate_build_info', side_effect=OSError('interrupted')):
            with self.assertRaisesRegex(OSError, 'interrupted'):
                installer.install(package, self.cfg)
        payload = destination / 'payload'
        before = payload.stat().st_mtime_ns
        contents = (database / 'fixture-1.0/+CONTENTS').read_bytes()
        with patch.object(package_relocation, 'refresh_checksums', side_effect=AssertionError('payload scan')):
            self.assertIn('fixture-1.0', installer.installed(self.cfg))
            self.assertIn('fixture-1.0', installer.installed(self.cfg))
        self.assertEqual(self.localbase(database), str(destination))
        self.assertEqual(payload.stat().st_mtime_ns, before)
        self.assertEqual((database / 'fixture-1.0/+CONTENTS').read_bytes(), contents)
        self.crypto.run_package('pkg_admin', ['-K', database, 'check', 'fixture-1.0'])

    def native_stage_config(self):
        root = self.work / 'native-stage'
        logical = self.destination
        cfg = dict(self.cfg, install_mode='native-stage', stage_root=str(root))
        physical = root / str(logical).lstrip('/')
        return cfg, physical, physical / 'pkgdb'

    def test_native_stage_same_and_foreign_producer_signed_lifecycle(self):
        # Signed pkg_tools exercise both an original P and a third-party
        # producer; physical stage contains P as suffix, which must not cause
        # text relocation to treat the staging root as the intended prefix.
        logical = self.destination
        logical.mkdir()
        sentinel = logical / 'sentinel'
        sentinel.write_text('unchanged')
        cfg, physical, db = self.native_stage_config()
        installer = PackageInstaller(self.crypto, self.ca.certificate)
        for number, original in enumerate((logical, self.work / 'producer')):
            if number:
                shutil.rmtree(self.work / 'stage')
                (self.work / 'fixture-1.0.tgz').unlink()
                self.destination = original
            commands = (package_relocation.MARKER + '\n' +
                package_relocation.SOURCE + str(original) + '\n' +
                package_relocation.TEXT + 'payload\n' + package_relocation.HOOKS + '\n')
            hook = '#!/bin/sh\ntouch ' + str(self.work / 'hook-ran') + '\n'
            unhook = '#!/bin/sh\ntouch ' + str(self.work / 'unhook-ran') + '\n'
            package = self.package(package_name='fixture-1.0', payload=str(original) + '/value',
                                   provides=True, commands=commands, install_script=hook,
                                   deinstall_script=unhook)
            installer.install(package, cfg)
            installer.install(package, cfg)  # repeated replacement stays stage-local
            self.assertEqual((physical / 'payload').read_text(), str(logical) + '/value')
            metadata = db / 'fixture-1.0'
            info = (metadata / '+BUILD_INFO').read_text()
            self.assertIn('LOCALBASE=' + str(logical), info)
            self.assertIn('PROVIDES=' + str(logical) + '/payload', info)
            self.assertNotIn(str(cfg['stage_root']), info)
            self.assertTrue((metadata / package_relocation.READY).is_file())
            self.assertFalse((self.work / 'hook-ran').exists())
            self.assertIn('fixture-1.0', installer.installed(cfg))
            installer.configure(cfg)
            self.assertFalse((self.work / 'hook-ran').exists())
            self.crypto.run_package('pkg_admin', ['-P', cfg['stage_root'], '-K', db,
                                                  'check', 'fixture-1.0'])
            installer.remove('fixture-1.0', cfg)
            self.assertFalse((physical / 'payload').exists())
            self.assertFalse((self.work / 'unhook-ran').exists())
            self.assertEqual(sentinel.read_text(), 'unchanged')
        self.destination = logical

    def test_native_stage_raw_signed_package_never_executes_scripts(self):
        cfg, physical, _ = self.native_stage_config()
        self.destination.mkdir()
        sentinel = self.destination / 'sentinel'
        sentinel.write_text('unchanged')
        markers = {name: self.work / name for name in ('exec-ran', 'unexec-ran', 'install-ran', 'delete-ran')}
        commands = ('@exec touch ' + str(markers['exec-ran']) + '\n' +
                    '@unexec touch ' + str(markers['unexec-ran']) + '\n')
        package = self.package(commands=commands,
            install_script='#!/bin/sh\ntouch ' + str(markers['install-ran']) + '\n',
            deinstall_script='#!/bin/sh\ntouch ' + str(markers['delete-ran']) + '\n')
        installer = PackageInstaller(self.crypto, self.ca.certificate)
        installer.install(package, cfg)
        installer.install(package, cfg)
        self.assertTrue((physical / 'payload').is_file())
        installer.remove('fixture-1.0', cfg)
        self.assertFalse((physical / 'payload').exists())
        self.assertEqual(sentinel.read_text(), 'unchanged')
        for marker in markers.values():
            self.assertFalse(marker.exists(), marker)

    def test_native_stage_relocated_python_does_not_compile_bytecode(self):
        cfg, physical, db = self.native_stage_config()
        original = self.work / 'producer'
        self.destination = original
        commands = (package_relocation.MARKER + '\n' +
                    package_relocation.SOURCE + str(original) + '\n' +
                    package_relocation.TEXT + 'module.py\n')
        package = self.package(payload_path='module.py',
            payload='PREFIX = ' + repr(str(original)) + '\n', commands=commands)
        installer = PackageInstaller(self.crypto, self.ca.certificate)
        installer.install(package, cfg)
        self.assertIn(repr(cfg['prefix']), (physical / 'module.py').read_text())
        self.assertTrue((db / 'fixture-1.0' / package_relocation.READY).is_file())
        installer.installed(cfg)
        installer.configure(cfg)
        self.assertFalse(list(physical.rglob('__pycache__')))
        self.assertTrue((db / 'fixture-1.0' / package_relocation.READY).is_file())

    def test_native_stage_forbids_unqualified_hooks_and_commands(self):
        cfg, _, _ = self.native_stage_config()
        package = self.package(commands=PORTABLE_PREFIX + '\n@exec echo unsafe\n')
        with self.assertRaises(SDKError):
            PackageInstaller(self.crypto, self.ca.certificate).install(package, cfg)
        self.assertFalse((self.work / 'hook-ran').exists())
        shutil.rmtree(self.work / 'stage')
        package.unlink()
        package = self.package(commands=PORTABLE_PREFIX + '\n',
                               install_script='#!/bin/sh\ntouch ' + str(self.work / 'hook-ran') + '\n')
        with self.assertRaises(SDKError):
            PackageInstaller(self.crypto, self.ca.certificate).install(package, cfg)
        self.assertFalse((self.work / 'hook-ran').exists())

    def test_checked_archive_cannot_reuse_validation_for_other_stage(self):
        package = self.package()
        cfg, _, _ = self.native_stage_config()
        installer = PackageInstaller(self.crypto, self.ca.certificate)
        installer.check(package, cfg)
        other = dict(cfg, prefix=str(self.work / 'wrong-prefix'),
                     pkgdb=str(self.work / 'wrong-prefix/pkgdb'),
                     stage_root=str(self.work / 'other-stage'))
        with self.assertRaises(SDKError):
            installer.install(package, other, checked=True)
        self.assertFalse((self.work / 'other-stage').exists())

    def test_prefix_independent_commands_are_refused(self):
        package = self.package(commands=PORTABLE_PREFIX + '\n@exec echo unsafe\n')
        with self.assertRaisesRegex(SDKError, 'installation commands'):
            inspect_install_paths(package, self.cfg)

    def test_hooks_need_explicit_deferred_qualification(self):
        package = self.package(commands=PORTABLE_PREFIX + '\n', install_script='#!/bin/sh\nexit 0\n')
        with self.assertRaisesRegex(SDKError, 'installation scripts'):
            inspect_install_paths(package, self.cfg)

    def test_portable_text_must_be_regular_payload_of_same_archive(self):
        import io
        archive_path = self.work / 'claimed.tgz'
        prefix = str(self.destination)
        contents = ('@name claimed-1\n@cwd ' + prefix + '\n'
                    '@comment emberbsd-prefix-relocation-v1\n'
                    '@comment emberbsd-relocation-prefix=' + prefix + '\n'
                    '@comment emberbsd-relocation-text=other-owned\nother-owned\n')
        with tarfile.open(archive_path, 'w:gz') as archive:
            data = contents.encode(); member = tarfile.TarInfo('+CONTENTS'); member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
        with self.assertRaisesRegex(SDKError, 'regular payload owned by package'):
            inspect_install_paths(archive_path, self.cfg)

    @unittest.skipUnless(platform.system() == 'Darwin', 'Darwin packaging')
    def test_portable_size_tracks_payload_growth(self):
        import importlib.util
        from emberbsd_sdk.binary_release import inspect_package
        package = self.package(payload='abc', sizes=(3, 103))
        output = self.work / 'portable-growth.tgz'
        script = Path(__file__).resolve().parents[1] / 'scripts/package-portable.py'
        spec = importlib.util.spec_from_file_location('portable_size_test', script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        argv = [str(script), str(package), '--output', str(output), '--host-prefix',
                os.environ['EMBERBSD_TEST_HOST_PREFIX'], '--dependency-prefix', str(self.work),
                '--bootstrap-install-name-tool', '/usr/bin/true', '--codesign', '/usr/bin/true',
                '--relocate-text']
        def grow(root, *args, **kwargs):
            (root / 'payload').write_text('abcdefghij')
        with patch.object(sys, 'argv', argv), patch.object(module, 'make_relative', side_effect=grow):
            self.assertEqual(module.main(), 0)
        summary = self.crypto.run_package('pkg_info', ['-X', str(output)]).decode()
        self.assertIn('SIZE_PKG=10\n', summary)
        self.assertEqual(inspect_package(output, None, require_native_relocation=True), 10)
        with tarfile.open(output, 'r:gz') as archive:
            self.assertEqual(archive.extractfile('+SIZE_ALL').read(), b'110\n')

    @unittest.skipUnless(platform.system() == 'Darwin', 'Darwin packaging')
    def test_signed_deferred_hook_runs_after_relocation_and_recovers(self):
        original = str(self.destination)
        package = self.package(payload=original + '/value', install_script='#!/bin/sh\n'
            'if [ "$2" = POST-INSTALL ]; then\n'
            '  test "$(cat "' + original + '/payload")" = "$PKG_PREFIX/value" || exit 9\n'
            '  printf configured > "$PKG_METADATA_DIR/configured"\nfi\n')
        portable, signed = self.work / 'portable.tgz', self.work / 'portable-signed.tgz'
        subprocess.run([sys.executable, '-B', str(Path(__file__).resolve().parents[1] / 'scripts/package-portable.py'),
            str(package), '--host-prefix', os.environ['EMBERBSD_TEST_HOST_PREFIX'],
            '--dependency-prefix', str(self.work / 'unused'), '--output', str(portable), '--relocate-text',
            '--defer-hooks', '--bootstrap-install-name-tool', '/usr/bin/false', '--codesign', '/usr/bin/false'],
            check=True, capture_output=True)
        self.crypto.sign_package(portable, signed, self.signer, self.ca.certificate)
        self.cfg.update(prefix=str(self.work / 'relocated'), pkgdb=str(self.work / 'selected-pkgdb'))
        installer = PackageInstaller(self.crypto, self.ca.certificate)
        with patch.object(package_relocation, 'run_install_hooks', side_effect=SDKError('hook interrupted')):
            with self.assertRaisesRegex(SDKError, 'hook interrupted'):
                installer.install(signed, self.cfg)
        self.assertIn('fixture-1.0', installer.installed(self.cfg))
        metadata = Path(self.cfg['pkgdb']) / 'fixture-1.0'
        self.assertEqual((metadata / 'configured').read_text(), 'configured')
        self.assertNotIn(package_relocation.HOOKS, (metadata / '+CONTENTS').read_text())
        self.assertFalse((metadata / package_relocation.READY).exists())

    @unittest.skipUnless(platform.system() == 'Darwin', 'Darwin packaging')
    def test_batch_defers_hook_until_later_tool_is_installed_and_retries(self):
        hook = self.package(package_name='hook-1.0', install_script='#!/bin/sh\n'
            'if [ "$2" = POST-INSTALL ]; then\n'
            '  test -x "$PKG_PREFIX/bin/later-tool" || exit 9\n'
            '  printf configured > "$PKG_METADATA_DIR/configured"\n'
            'fi\n')
        portable, signed = self.work / 'hook-portable.tgz', self.work / 'hook-signed.tgz'
        subprocess.run([sys.executable, '-B', str(Path(__file__).resolve().parents[1] / 'scripts/package-portable.py'),
            str(hook), '--host-prefix', os.environ['EMBERBSD_TEST_HOST_PREFIX'],
            '--dependency-prefix', str(self.work / 'unused'), '--output', str(portable), '--relocate-text',
            '--defer-hooks', '--bootstrap-install-name-tool', '/usr/bin/false', '--codesign', '/usr/bin/false'],
            check=True, capture_output=True)
        self.crypto.sign_package(portable, signed, self.signer, self.ca.certificate)
        hook.unlink()
        installer = PackageInstaller(self.crypto, self.ca.certificate)
        installer.install(signed, self.cfg, configure=False)
        self.assertEqual(list(package_relocation.pending(self.destination, self.destination / 'pkgdb')),
                         ['hook-1.0'])

        # Configuration failure leaves the signed package registered and pending.
        with self.assertRaisesRegex(SDKError, 'package configuration failed: hook-1.0'):
            installer.configure(self.cfg)
        self.assertIn('hook-1.0', installer.installed(self.cfg, recover=False))

        shutil.rmtree(self.work / 'stage')
        tool = self.package(package_name='later-tool-1', payload_path='bin/later-tool', executable=True)
        installer.install(tool, self.cfg)
        self.assertTrue((self.destination / 'bin/later-tool').is_file())
        installer.configure(self.cfg)
        metadata = self.destination / 'pkgdb/hook-1.0'
        self.assertEqual((metadata / 'configured').read_text(), 'configured')
        self.assertEqual(list(package_relocation.pending(self.destination, self.destination / 'pkgdb')), [])

    @unittest.skipUnless(platform.system() == 'Darwin', 'Darwin packaging')
    def test_portable_package_revision_preserves_payload_and_build_provenance(self):
        package = self.package(package_name='fixture-1.0nb2', build_version='original build provenance\n',
                               commands='@pkgdep mksh>=59c\n')
        script = Path(__file__).resolve().parents[1] / 'scripts/package-portable.py'
        def command(revision, output):
            return [sys.executable, '-B', str(script), str(package), '--host-prefix',
                os.environ['EMBERBSD_TEST_HOST_PREFIX'], '--dependency-prefix', os.environ['EMBERBSD_TEST_HOST_PREFIX'],
                '--output', str(output), '--package-revision', revision,
                '--bootstrap-install-name-tool', '/usr/bin/false', '--codesign', '/usr/bin/false']
        for revision in ('0', '2', '1', '-1', '3.0', '03', 'no'):
            result = subprocess.run(command(revision, self.work / ('bad-' + revision + '.tgz')),
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0, revision)
            self.assertFalse((self.work / ('bad-' + revision + '.tgz')).exists())
        output = self.work / 'fixture-1.0nb3.tgz'
        subprocess.run(command('3', output), check=True, capture_output=True)
        with tarfile.open(output, 'r:gz') as archive:
            def member(name):
                return archive.extractfile(next(m for m in archive if m.name.removeprefix('./') == name)).read()
            contents = member('+CONTENTS').decode()
            self.assertIn('@name fixture-1.0nb3\n', contents)
            self.assertNotIn('@name fixture-1.0nb2\n', contents)
            self.assertIn('@pkgdep mksh>=59c\n', contents)
            self.assertIn(b'DISTNAME=fixture-1.0\n', member('+BUILD_INFO'))
            self.assertEqual(member('+BUILD_VERSION'), b'original build provenance\n')
            self.assertEqual(member('payload'), b'installed data')

    @unittest.skipUnless(platform.system() == 'Darwin', 'Darwin packaging')
    def test_repackage_portable_payload_with_replacement_hook(self):
        script = Path(__file__).resolve().parents[1] / 'scripts/package-portable.py'
        package = self.package(package_name='fixture-1.0nb1', payload_path='bin/fixture',
            payload='#!' + str(self.destination) + '/bin/sh\n', executable=True,
            build_version='original build provenance\n',
            install_script='#!/bin/sh\necho old > ' + str(self.work / 'old-hook') + '\n',
            install_script_mode=0o555)
        with package.open('rb') as source, tarfile.open(fileobj=_package_payload(source), mode='r|*') as archive:
            install_member = next(member for member in archive
                                  if member.name.removeprefix('./') == '+INSTALL' or
                                  member.name.removeprefix('./').endswith('/+INSTALL'))
            self.assertEqual(install_member.mode & 0o777, 0o555)
        common = [sys.executable, '-B', str(script), '--host-prefix', os.environ['EMBERBSD_TEST_HOST_PREFIX'],
            '--dependency-prefix', str(self.work / 'unused'), '--relocate-text', '--defer-hooks',
            '--bootstrap-install-name-tool', '/usr/bin/false', '--codesign', '/usr/bin/false']
        first = self.work / 'fixture-portable1.tgz'
        subprocess.run([*common, str(package), '--output', str(first)], check=True, capture_output=True)
        replacement_hook = self.work / 'replacement-install'
        replacement_hook.write_text('#!/bin/sh\necho new > ' + str(self.work / 'new-hook') + '\n')
        replacement_hook.chmod(0o755)
        portable2 = self.work / 'fixture-1.0nb2.tgz'
        result = subprocess.run([*common, str(first), '--output', str(portable2),
            '--package-revision', '2', '--install-script', str(replacement_hook)],
            check=True, capture_output=True)

        def members(archive_path):
            with tarfile.open(archive_path, 'r:gz') as archive:
                return {member.name.removeprefix('./'): archive.extractfile(member).read()
                        for member in archive if member.isfile()}
        first_members = members(first)
        second_members = members(portable2)
        contents = second_members['+CONTENTS'].decode().splitlines()
        self.assertIn('@name fixture-1.0nb2', contents)
        self.assertEqual(sum(line.startswith(package_relocation.SOURCE) for line in contents), 1)
        self.assertEqual(sum(line.startswith(package_relocation.TEXT) for line in contents), 1)
        self.assertIn(package_relocation.MARKER, contents)
        self.assertIn(package_relocation.HOOKS, contents)
        self.assertEqual(second_members['bin/fixture'], first_members['bin/fixture'])
        self.assertEqual(second_members['+BUILD_VERSION'], b'original build provenance\n')
        self.assertNotIn('+DEINSTALL', second_members)
        self.assertEqual(second_members['+INSTALL'], replacement_hook.read_bytes())
        self.assertNotIn(str(self.work / 'old-hook').encode(), second_members['+INSTALL'])

        unsigned, signed = self.work / 'signed-input.tgz', self.work / 'signed-portable2.tgz'
        shutil.copyfile(portable2, unsigned)
        self.crypto.sign_package(unsigned, signed, self.signer, self.ca.certificate)
        install_prefix = self.work / 'fresh-prefix'
        cfg = dict(self.cfg, prefix=str(install_prefix), pkgdb=str(install_prefix / 'pkgdb'))
        PackageInstaller(self.crypto, self.ca.certificate).install(signed, cfg)
        self.assertEqual((self.work / 'new-hook').read_text(), 'new\n')
        self.assertFalse((self.work / 'old-hook').exists())
        self.assertEqual((install_prefix / 'bin/fixture').read_text(), '#!' + str(install_prefix) + '/bin/sh\n')

        refused = self.work / 'revision-required.tgz'
        result = subprocess.run([*common, str(first), '--output', str(refused),
            '--install-script', str(replacement_hook)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--package-revision and --defer-hooks', result.stderr)
        self.assertFalse(refused.exists())

    @unittest.skipUnless(platform.system() == 'Darwin', 'Darwin packaging')
    def test_portable_producer_omits_empty_requires_before_real_pkg_add(self):
        package = self.package(requires_self=True, preserve=True)
        portable, signed = self.work / 'portable.tgz', self.work / 'portable-signed.tgz'
        subprocess.run([sys.executable, '-B', str(Path(__file__).resolve().parents[1] / 'scripts/package-portable.py'),
            str(package), '--host-prefix', os.environ['EMBERBSD_TEST_HOST_PREFIX'],
            '--dependency-prefix', str(self.work / 'unused'), '--output', str(portable),
            '--bootstrap-install-name-tool', '/usr/bin/false', '--codesign', '/usr/bin/false'],
            check=True, capture_output=True)
        self.crypto.sign_package(portable, signed, self.signer, self.ca.certificate)
        self.cfg['prefix'] = str(self.work / 'relocated')
        self.cfg['pkgdb'] = self.cfg['prefix'] + '/pkgdb'
        PackageInstaller(self.crypto, self.ca.certificate).install(signed, self.cfg)
        self.assertEqual((Path(self.cfg['prefix']) / 'payload').read_text(), 'installed data')
        self.assertEqual((Path(self.cfg['pkgdb']) / 'fixture-1.0/+PRESERVE').read_text(), 'bootstrap tool\n')

    @unittest.skipUnless(platform.system() == 'Darwin', 'Darwin packaging')
    def test_signed_text_relocation_recovers_after_pkg_add_and_keeps_checksums(self):
        original = str(self.destination)
        package = self.package(payload='#!' + original + '/bin/python3.15\n', provides=True)
        portable, signed = self.work / 'portable.tgz', self.work / 'portable-signed.tgz'
        subprocess.run([sys.executable, '-B', str(Path(__file__).resolve().parents[1] / 'scripts/package-portable.py'),
            str(package), '--host-prefix', os.environ['EMBERBSD_TEST_HOST_PREFIX'],
            '--dependency-prefix', str(self.work / 'unused'), '--output', str(portable), '--relocate-text',
            '--bootstrap-install-name-tool', '/usr/bin/false', '--codesign', '/usr/bin/false'],
            check=True, capture_output=True)
        self.crypto.sign_package(portable, signed, self.signer, self.ca.certificate)
        destination = self.work / 'relocated'
        database = self.work / 'selected-pkgdb'
        self.cfg.update(prefix=str(destination), pkgdb=str(database))
        installer = PackageInstaller(self.crypto, self.ca.certificate)
        replace = bootstrap_bundle._replace_file
        def interrupted(path, data):
            if path.name == '+BUILD_INFO':
                raise OSError('interrupted after pkg_add')
            return replace(path, data)
        with patch.object(bootstrap_bundle, '_replace_file', side_effect=interrupted):
            with self.assertRaisesRegex(OSError, 'interrupted'):
                installer.install(signed, self.cfg)
        self.assertEqual((destination / 'payload').read_text(), '#!' + str(destination) + '/bin/python3.15\n')
        self.assertIn('fixture-1.0', installer.installed(self.cfg))
        info = (database / 'fixture-1.0/+BUILD_INFO').read_text()
        self.assertIn('PROVIDES=' + str(destination) + '/payload', info)
        self.assertIn('CFLAGS=-I' + original + '/include', info)
        self.assertEqual(self.localbase(database), str(destination))
        self.assertFalse(list(package_relocation.pending(destination, database)))
        self.crypto.run_package('pkg_admin', ['-K', database, 'check', 'fixture-1.0'])
        with patch.object(package_relocation, 'refresh_checksums', side_effect=AssertionError('repeat payload scan')):
            self.assertIn('fixture-1.0', installer.installed(self.cfg))


if __name__ == '__main__':
    unittest.main()
