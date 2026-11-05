# SPDX-License-Identifier: BSD-2-Clause
from pathlib import Path
import platform
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.package_updates import check_updates


class PackageUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.database = self.root / 'pkgdb'
        self.database.mkdir()
        self.archive = self.root / 'replacement.tgz'
        self.crypto = object()

    def installed(self, name, *, depends=(), providers=(), dependents=()):
        directory = self.database / name
        directory.mkdir()
        (directory / '+CONTENTS').write_text('@cwd /old/prefix\n@name ' + name + '\n' +
            ''.join('@pkgdep ' + value + '\n' for value in depends))
        (directory / '+BUILD_INFO').write_text(''.join('PROVIDES=/old/prefix/' + value + '\n'
            for value in providers))
        if dependents:
            (directory / '+REQUIRED_BY').write_text(''.join(value + '\n' for value in dependents))

    def replacement_archive(self, name, *, depends=(), providers=(), prefix='/producer/prefix'):
        contents = '@cwd ' + prefix + '\n@name ' + name + '\n' + ''.join(
            '@pkgdep ' + value + '\n' for value in depends)
        info = ''.join('PROVIDES=' + prefix + '/' + value + '\n' for value in providers)
        with tarfile.open(self.archive, 'w:gz') as archive:
            for filename, data in (('+CONTENTS', contents), ('+BUILD_INFO', info)):
                path = self.root / filename.lstrip('+')
                path.write_text(data)
                archive.add(path, arcname=filename)
        return self.archive

    def package(self, name, *, replaces=('lib-1',)):
        return dict(pkgname=name, replaces=list(replaces), artifact={'id': 'replacement'},
                    action='download' if replaces else 'installed')

    def check(self, packages, installed):
        return check_updates(self.crypto, self.database, packages,
            {'replacement': self.archive}, installed)

    def test_prefix_relocation_normalizes_provider_paths(self):
        self.installed('lib-1', providers=('lib/libexample.so.1',), dependents=('consumer-1',))
        self.installed('consumer-1', depends=('lib>=1',))
        self.replacement_archive('lib-2', providers=('lib/libexample.so.1',), prefix='/new/prefix')
        with patch('emberbsd_sdk.package_updates._matches', return_value=True):
            self.check([self.package('lib-2')], {'lib-1', 'consumer-1'})

    def test_provider_removal_with_installed_consumer_is_refused(self):
        self.installed('lib-1', providers=('lib/libexample.so.1',), dependents=('consumer-1',))
        self.installed('consumer-1', depends=('lib>=1',))
        self.replacement_archive('lib-2')
        with patch('emberbsd_sdk.package_updates._matches', return_value=True):
            with self.assertRaisesRegex(SDKError, 'removes library providers'):
                self.check([self.package('lib-2')], {'lib-1', 'consumer-1'})

    def test_dependency_constraint_rejects_incompatible_replacement(self):
        self.installed('lib-1', dependents=('consumer-1',))
        self.installed('consumer-1', depends=('lib<2',))
        self.replacement_archive('lib-2')
        with patch('emberbsd_sdk.package_updates._matches', side_effect=lambda _c, pattern, name:
                   pattern == 'lib>=1' or (name == 'lib-1' and pattern == 'lib<2')):
            with self.assertRaisesRegex(SDKError, 'does not accept lib-2'):
                self.check([self.package('lib-2')], {'lib-1', 'consumer-1'})

    def test_stale_installed_state_is_refused(self):
        self.installed('lib-3')
        self.replacement_archive('lib-2')
        with self.assertRaisesRegex(SDKError, 'installed state changed'):
            self.check([self.package('lib-2')], {'lib-3'})

    def test_update_without_dependents_allows_provider_removal(self):
        self.installed('lib-1', providers=('lib/libexample.so.1',))
        self.replacement_archive('lib-2')
        with patch('emberbsd_sdk.package_updates._matches', return_value=True):
            self.check([self.package('lib-2')], {'lib-1'})

    def test_repeated_noop_with_current_package(self):
        self.installed('lib-2')
        package = self.package('lib-2', replaces=())
        self.check([package], {'lib-2'})
        self.check([package], {'lib-2'})

    def test_signed_catalog_cannot_downgrade_newer_installed_package(self):
        self.installed('lib-3')
        self.replacement_archive('lib-2')
        with patch('emberbsd_sdk.package_updates._matches', side_effect=lambda _c, pattern, name:
                   pattern == 'lib>=3' and name == 'lib-3'):
            with self.assertRaisesRegex(SDKError, 'downgrade'):
                self.check([self.package('lib-2', replaces=('lib-3',))], {'lib-3'})


if __name__ == '__main__':
    unittest.main()
