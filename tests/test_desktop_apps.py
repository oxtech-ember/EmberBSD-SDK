# SPDX-License-Identifier: BSD-2-Clause
"""Desktop binding fixtures for the signed catalog index."""

from pathlib import Path
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk.desktop_apps import bind_installed_desktops
from emberbsd_sdk.sdk_client import catalog_index


BROWSER = """[Desktop Entry]
Type=Application
Name=Ember Browser
Comment=Desktop browser
Exec=/usr/pkg/bin/ember-browser %U
Icon=org.emberbsd.browser
Terminal=false
Categories=Network;WebBrowser;
"""

VIEWER = """[Desktop Entry]
Type=Application
Name=Keyboard layout viewer
Exec=kbd-layout-viewer5
Icon=input-keyboard
Terminal=false
Categories=Qt;KDE;Utility;
"""

NODISPLAY = """[Desktop Entry]
Type=Application
Name=Hidden tool
Exec=/usr/pkg/bin/hidden-tool
NoDisplay=true
"""

TERMINAL = """[Desktop Entry]
Type=Application
Name=Curl
Exec=/usr/pkg/bin/curl
Terminal=true
Categories=Network;
"""

LINK = """[Desktop Entry]
Type=Link
Name=Not an application
Exec=/usr/pkg/bin/ember-browser
"""

GNOME_ONLY = """[Desktop Entry]
Type=Application
Name=Gnome Only
Exec=/usr/pkg/bin/ember-browser
OnlyShowIn=GNOME;
"""

OUTSIDE = """[Desktop Entry]
Type=Application
Name=Outside
Exec=/tmp/outside-bin
Terminal=false
"""


def _exe(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/sh\n', encoding='utf-8')
    path.chmod(0o755)


def _contents(pkgdb, prefix, pkgname, lines):
    package = pkgdb / pkgname
    package.mkdir(parents=True)
    text = '@cwd %s\n@name %s\n%s\n' % (prefix, pkgname, '\n'.join(lines))
    (package / '+CONTENTS').write_text(text, encoding='utf-8')


class DesktopBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prefix = Path(self.tmp.name) / 'pkg'
        self.pkgdb = self.prefix / 'pkgdb'
        self.prefix.mkdir()
        self.pkgdb.mkdir()
        _exe(self.prefix / 'bin' / 'ember-browser')
        _exe(self.prefix / 'bin' / 'kbd-layout-viewer5')
        _exe(self.prefix / 'bin' / 'curl')
        _exe(self.prefix / 'bin' / 'hidden-tool')
        apps = self.prefix / 'share' / 'applications'
        apps.mkdir(parents=True)
        (apps / 'org.emberbsd.browser.desktop').write_text(BROWSER, encoding='utf-8')
        (apps / 'kbd-layout-viewer5.desktop').write_text(VIEWER, encoding='utf-8')
        (apps / 'hidden.desktop').write_text(NODISPLAY, encoding='utf-8')
        (apps / 'curl.desktop').write_text(TERMINAL, encoding='utf-8')
        (apps / 'link.desktop').write_text(LINK, encoding='utf-8')
        (apps / 'gnome.desktop').write_text(GNOME_ONLY, encoding='utf-8')
        (apps / 'outside.desktop').write_text(OUTSIDE, encoding='utf-8')
        outside = Path(self.tmp.name) / 'secret.desktop'
        outside.write_text(BROWSER, encoding='utf-8')
        (apps / 'escaped.desktop').symlink_to(outside)
        _contents(self.pkgdb, self.prefix, 'ember-browser-1.46.0nb1', [
            'share/applications/org.emberbsd.browser.desktop',
            'share/applications/hidden.desktop',
            'share/applications/link.desktop',
            'share/applications/gnome.desktop',
            'share/applications/outside.desktop',
            'share/applications/escaped.desktop',
            'share/applications/../../secret.desktop',
        ])
        _contents(self.pkgdb, self.prefix, 'kbd-layout-viewer-1.0', [
            'share/applications/kbd-layout-viewer5.desktop',
        ])
        _contents(self.pkgdb, self.prefix, 'curl-8.20.0', [
            'share/applications/curl.desktop',
            'bin/curl',
        ])
        _contents(self.pkgdb, self.prefix, 'libxml2-2.15.4nb5', ['lib/libxml2.so'])
        _contents(self.pkgdb, self.prefix, 'not-in-catalog-1.0', [
            'share/applications/org.emberbsd.browser.desktop',
        ])

    def test_multiple_resources_and_rejected_files(self):
        rows = [
            dict(repository='netbsd-amd64', root='www/blanc', pkgname='ember-browser-1.46.0nb1',
                 role='package', action='installed', installed_pkgname='ember-browser-1.46.0nb1'),
            dict(repository='netbsd-amd64', root='x11/viewer', pkgname='kbd-layout-viewer-1.0',
                 role='package', action='installed', installed_pkgname='kbd-layout-viewer-1.0'),
            dict(repository='netbsd-amd64', root='www/curl', pkgname='curl-8.20.0',
                 role='package', action='installed', installed_pkgname='curl-8.20.0'),
            dict(repository='netbsd-amd64', root='textproc/libxml2', pkgname='libxml2-2.15.4nb5',
                 role='library', action='installed', installed_pkgname='libxml2-2.15.4nb5'),
        ]
        # The Exec lines in the fixture name /usr/pkg. Point the files at this
        # temporary prefix by rewriting the absolute browser command.
        browser = self.prefix / 'share' / 'applications' / 'org.emberbsd.browser.desktop'
        browser.write_text(BROWSER.replace('/usr/pkg', str(self.prefix)), encoding='utf-8')
        outside = self.prefix / 'share' / 'applications' / 'outside.desktop'
        outside.write_text(OUTSIDE.replace('/tmp/outside-bin', '/tmp/outside-bin'), encoding='utf-8')
        hidden = self.prefix / 'share' / 'applications' / 'hidden.desktop'
        hidden.write_text(NODISPLAY.replace('/usr/pkg', str(self.prefix)), encoding='utf-8')
        terminal = self.prefix / 'share' / 'applications' / 'curl.desktop'
        terminal.write_text(TERMINAL.replace('/usr/pkg', str(self.prefix)), encoding='utf-8')
        link = self.prefix / 'share' / 'applications' / 'link.desktop'
        link.write_text(LINK.replace('/usr/pkg', str(self.prefix)), encoding='utf-8')
        gnome = self.prefix / 'share' / 'applications' / 'gnome.desktop'
        gnome.write_text(GNOME_ONLY.replace('/usr/pkg', str(self.prefix)), encoding='utf-8')
        status = bind_installed_desktops(rows, {
            'netbsd-amd64': dict(prefix=str(self.prefix), pkgdb=str(self.pkgdb)),
        })
        self.assertEqual(status, 'installed-pkgdb')
        by_root = {row['root']: row for row in rows}
        browser_row = by_root['www/blanc']
        self.assertEqual(browser_row['role'], 'desktop')
        self.assertEqual(len(browser_row['desktops']), 1)
        identity = browser_row['desktops'][0]
        self.assertEqual(identity['name'], 'Ember Browser')
        self.assertEqual(identity['icon'], 'org.emberbsd.browser')
        self.assertEqual(identity['exec'], [str((self.prefix / 'bin' / 'ember-browser').resolve())])
        self.assertEqual(identity['categories'], ['Network', 'WebBrowser'])
        self.assertEqual(identity['file'], 'share/applications/org.emberbsd.browser.desktop')
        self.assertEqual(identity['source'], 'installed-pkgdb')
        self.assertEqual(identity['pkgname'], 'ember-browser-1.46.0nb1')
        viewer = by_root['x11/viewer']['desktops']
        self.assertEqual(len(viewer), 1)
        self.assertEqual(viewer[0]['exec'], [str((self.prefix / 'bin' / 'kbd-layout-viewer5').resolve())])
        self.assertEqual(by_root['www/curl']['role'], 'package')
        self.assertEqual(by_root['www/curl']['desktops'], [])
        self.assertEqual(by_root['textproc/libxml2']['role'], 'library')
        self.assertEqual(by_root['textproc/libxml2']['desktops'], [])
        self.assertEqual(len(rows), 4)

    def test_empty_application_list_is_not_replaced_with_packages(self):
        empty = Path(self.tmp.name) / 'empty'
        prefix = empty / 'pkg'
        pkgdb = prefix / 'pkgdb'
        prefix.mkdir(parents=True)
        pkgdb.mkdir()
        rows = [dict(repository='netbsd-amd64', root='www/curl', pkgname='curl-8.20.0',
                     role='package', action='installed', installed_pkgname='curl-8.20.0')]
        status = bind_installed_desktops(rows, {'netbsd-amd64': dict(prefix=str(prefix), pkgdb=str(pkgdb))})
        self.assertEqual(status, 'installed-pkgdb')
        self.assertEqual(rows[0]['role'], 'package')
        self.assertEqual(rows[0]['desktops'], [])

    def test_missing_pkgdb_does_not_invent_applications(self):
        rows = [dict(repository='netbsd-amd64', root='www/blanc', pkgname='ember-browser-1.46.0nb1',
                     role='package', action='installed', installed_pkgname='ember-browser-1.46.0nb1')]
        status = bind_installed_desktops(rows, {
            'netbsd-amd64': dict(prefix='/absent/pkg', pkgdb='/absent/pkgdb'),
        })
        self.assertEqual(status, 'unavailable')
        self.assertEqual(rows[0]['role'], 'package')
        self.assertEqual(rows[0]['desktops'], [])

    def test_installed_newer_uses_the_installed_package_files(self):
        browser = self.prefix / 'share' / 'applications' / 'org.emberbsd.browser.desktop'
        browser.write_text(BROWSER.replace('/usr/pkg', str(self.prefix)), encoding='utf-8')
        _contents(self.pkgdb, self.prefix, 'ember-browser-1.46.0nb2', [
            'share/applications/org.emberbsd.browser.desktop',
        ])
        rows = [dict(repository='netbsd-amd64', root='www/blanc', pkgname='ember-browser-1.46.0nb1',
                     role='package', action='installed-newer', installed_pkgname='ember-browser-1.46.0nb2')]
        bind_installed_desktops(rows, {'netbsd-amd64': dict(prefix=str(self.prefix), pkgdb=str(self.pkgdb))})
        self.assertEqual(rows[0]['action'], 'installed-newer')
        self.assertEqual(rows[0]['role'], 'desktop')
        self.assertEqual(rows[0]['desktops'][0]['pkgname'], 'ember-browser-1.46.0nb2')
        self.assertEqual(rows[0]['pkgname'], 'ember-browser-1.46.0nb1')

    def test_catalog_index_counts_apps_separately_from_packages(self):
        browser = self.prefix / 'share' / 'applications' / 'org.emberbsd.browser.desktop'
        browser.write_text(BROWSER.replace('/usr/pkg', str(self.prefix)), encoding='utf-8')
        host = dict(os='NetBSD', arch='amd64', abi='netbsd-11', runtime='libc++')
        target = dict(arch='x86_64', triplet='x86_64--netbsd', abi='netbsd-11', runtime='libc++')
        cfg = dict(repositories={'netbsd-amd64': dict(
            target=target, prefix=str(self.prefix), pkgdb=str(self.pkgdb),
            os_version='11.0', install_mode='native')})
        artifacts = []
        packages = []
        for name, root, pkgname in (
                ('browser', 'www/blanc', 'ember-browser-1.46.0nb1'),
                ('curl', 'www/curl', 'curl-8.20.0'),
                ('lib', 'textproc/libxml2', 'libxml2-2.15.4nb5')):
            artifacts.append(dict(id=name, path=name + '.tgz', kind='target-package', host=None,
                                  target=target, size=10, sha256='ab' * 32, unpacked_size=20, depends=[]))
            packages.append(dict(root=root, pkgname=pkgname, artifact=name))
        release = dict(release_id='current-test', artifacts=artifacts)
        catalogs = [dict(repository='netbsd-amd64', sets={}, packages=packages)]

        class Installer:
            def installed(self, config, *, recover=True):
                return {'ember-browser-1.46.0nb1', 'curl-8.20.0', 'libxml2-2.15.4nb5'}

        index = catalog_index(release, catalogs, cfg, Installer())
        self.assertFalse(index['desktop_metadata'])
        self.assertEqual(index['desktop_binding'], 'installed-pkgdb')
        self.assertEqual(index['counts']['total'], 3)
        self.assertEqual(index['counts']['desktop'], 1)
        self.assertEqual(index['counts']['package'], 1)
        self.assertEqual(index['counts']['library'], 1)
        by_root = {row['root']: row for row in index['packages']}
        self.assertEqual(by_root['www/blanc']['desktops'][0]['name'], 'Ember Browser')
        self.assertEqual(by_root['www/curl']['desktops'], [])
        self.assertNotEqual(by_root['www/curl']['role'], 'desktop')
        self.assertEqual(by_root['textproc/libxml2']['role'], 'library')


class FailedVerificationStaysClosed(unittest.TestCase):
    def test_a_desktop_file_does_not_bypass_a_missing_catalog_row(self):
        """A file on disk cannot add a package the signed index did not list."""
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / 'pkg'
            pkgdb = prefix / 'pkgdb'
            apps = prefix / 'share' / 'applications'
            apps.mkdir(parents=True)
            pkgdb.mkdir(parents=True)
            _exe(prefix / 'bin' / 'ember-browser')
            desktop = apps / 'org.emberbsd.browser.desktop'
            desktop.write_text(BROWSER.replace('/usr/pkg', str(prefix)), encoding='utf-8')
            _contents(pkgdb, prefix, 'ember-browser-1.46.0nb1', [
                'share/applications/org.emberbsd.browser.desktop',
            ])
            rows = []
            status = bind_installed_desktops(rows, {'netbsd-amd64': dict(prefix=str(prefix), pkgdb=str(pkgdb))})
            self.assertEqual(status, 'installed-pkgdb')
            self.assertEqual(rows, [])
            self.assertTrue(desktop.is_file())
            self.assertEqual(stat.S_IMODE(desktop.stat().st_mode) & 0o444, 0o444)


if __name__ == '__main__':
    unittest.main()
