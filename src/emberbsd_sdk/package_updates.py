# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Check binary replacements which preserve installed package consumers."""

from functools import cache
from pathlib import Path
import tarfile

from .binary_release import _matches, _package_payload
from .errors import SDKError
from .package_install import install_paths
from .package_abi import consumers_unbound, providers_preserved
from .release_signing import read_bytes


def _metadata(contents, build_info):
    lines = contents.decode('utf-8').splitlines()
    names = [line[6:] for line in lines if line.startswith('@name ')]
    prefixes = [line[5:] for line in lines if line.startswith('@cwd ')]
    if len(names) != 1 or not prefixes:
        raise SDKError('package update requires a name and prefix in +CONTENTS')
    prefix = prefixes[0].rstrip('/') + '/'
    # Native archives and installed packages can use different prefixes.
    providers = {value.removeprefix(prefix) for line in build_info.decode('utf-8').splitlines()
                 if line.startswith('PROVIDES=') for value in [line[9:]]}
    return dict(name=names[0], providers=providers,
                depends=[line[8:] for line in lines if line.startswith('@pkgdep ')])


def _installed(database, name):
    if Path(name).name != name or name in ('.', '..'):
        raise SDKError('invalid installed package name')
    directory = database / name
    return _metadata(read_bytes(directory / '+CONTENTS', 8 * 1024 * 1024),
                     read_bytes(directory / '+BUILD_INFO', 1024 * 1024))


def _archive(path):
    fields = {}
    with Path(path).open('rb') as stream, tarfile.open(fileobj=_package_payload(stream), mode='r|*') as archive:
        for member in archive:
            name = member.name.removeprefix('./')
            if name not in ('+CONTENTS', '+BUILD_INFO'):
                continue
            if name in fields or not member.isfile() or member.size > 8 * 1024 * 1024:
                raise SDKError('invalid package update metadata')
            fields[name] = archive.extractfile(member).read()
            if len(fields) == 2:
                break
    if len(fields) != 2:
        raise SDKError('package update requires +CONTENTS and +BUILD_INFO')
    return _metadata(fields['+CONTENTS'], fields['+BUILD_INFO'])


def reuse_compatible_dependencies(crypto, config, packages, archives, installed, roots=()):
    """Reuse a newer installed package when every selected direct consumer accepts it.

    Catalog edges identify exact artifacts, not pkg_tools version constraints. Use
    authenticated package metadata and pkg_admin's matcher to avoid replacing a
    compatible installed dependency with an older catalog artifact. Explicit roots
    are never elided; ambiguous or undeclared edges remain selected for the normal
    update checks to handle.
    """
    _, database = install_paths(config)
    by_id = {package['artifact']['id']: package for package in packages}
    incoming = {identity: [] for identity in by_id}
    for consumer in packages:
        for dependency in consumer['artifact']['depends']:
            if dependency in incoming:
                incoming[dependency].append(consumer)

    installed_by_base = {}
    for name in installed:
        installed_by_base.setdefault(name.rsplit('-', 1)[0], []).append(name)

    @cache
    def metadata(name):
        return _installed(database, name)

    @cache
    def matches(pattern, name):
        return _matches(crypto, pattern, name)

    @cache
    def archive_metadata(identity):
        return _archive(archives[identity])

    roots = set(roots)
    reused = []
    for identity, package in by_id.items():
        name = package['pkgname']
        if package['action'] == 'installed' or package.get('root') in roots:
            continue
        base, version = name.rsplit('-', 1)
        candidates = installed_by_base.get(base, [])
        if len(candidates) != 1:
            continue
        installed_name = candidates[0]
        if not matches(base + '>=' + version, installed_name):
            continue
        candidate_metadata = archive_metadata(identity)
        consumers = incoming[identity]
        if not consumers:
            continue
        installed_providers = metadata(installed_name)['providers']
        if not providers_preserved(config, archives[identity], candidate_metadata['providers'],
                                   installed_providers, installed_is_old=False):
            # The same proof as for a replacement: installed consumers whose
            # images load none of the catalog providers that the installed
            # package lacks. A consumer from an archive keeps the strict rule.
            if any(consumer['action'] != 'installed' for consumer in consumers) or \
                    not consumers_unbound(config, database, sorted(c['pkgname'] for c in consumers),
                                          candidate_metadata['providers'] - installed_providers):
                continue
        compatible = True
        for consumer in consumers:
            if consumer['action'] == 'installed':
                requirements = metadata(consumer['pkgname'])['depends']
            else:
                requirements = archive_metadata(consumer['artifact']['id'])['depends']
            accepted = [pattern for pattern in requirements if matches(pattern, name)]
            if not accepted or not all(matches(pattern, installed_name) for pattern in accepted):
                compatible = False
                break
        if compatible:
            reused.append(dict(root=package.get('root'), pkgname=installed_name,
                               catalog_candidate=name))
            package['pkgname'] = installed_name
            package['action'] = 'installed'
            package['replaces'] = []
            # This catalog artifact was authenticated but was not installed. Do
            # not retain it in the resulting package ledger as an installed row.
            package['artifact'] = None
    return reused


def check_updates(crypto, config, packages, archives, installed):
    """Run under the pkgdb lock after archive authentication, before mutation.

    Do not replace the resolver in pkg_tools. This bounded update path requires
    existing consumers to remain usable while dependencies change in place.
    ABI transitions which remove providers need a separate coordinated update.
    """
    _, database = install_paths(config)
    by_base = {}
    for name in installed:
        by_base.setdefault(name.rsplit('-', 1)[0], set()).add(name)
    for package in packages:
        name = package['pkgname']
        expected = {name} if package['action'] == 'installed' else set(package['replaces'])
        if by_base.get(name.rsplit('-', 1)[0], set()) != expected:
            raise SDKError(f'{name}: installed state changed; run the selection again')

    @cache
    def metadata(name):
        return _installed(database, name)

    @cache
    def matches(pattern, name):
        return _matches(crypto, pattern, name)

    # Installed packages that this batch replaces, and the install order.
    replaced_by = {old: other for other in packages for old in other['replaces']}
    position = {id(item): index for index, item in enumerate(packages)}

    for package in packages:
        if not package['replaces']:
            continue
        if len(package['replaces']) != 1:
            raise SDKError(f"{package['pkgname']}: multiple installed versions require package database repair")
        old_name = package['replaces'][0]
        base, version = old_name.rsplit('-', 1)
        if not matches(base + '>=' + version, package['pkgname']):
            raise SDKError(f"{old_name}: refusing package downgrade to {package['pkgname']}")
        new = _archive(archives[package['artifact']['id']])
        if new['name'] != package['pkgname']:
            raise SDKError('package update name differs from the signed catalog')
        old = metadata(old_name)
        required_by = database / old_name / '+REQUIRED_BY'
        dependents = set(read_bytes(required_by, 8 * 1024 * 1024).decode('utf-8').splitlines()) if required_by.exists() else set()
        # A split moves libraries to the package that conflicts with the old one
        # and removes it in this batch; those providers stay installed.
        providers = set(new['providers'])
        for other in packages:
            if old_name in other.get('removes', ()):
                providers |= set(_archive(archives[other['artifact']['id']])['providers'])
        if dependents and not providers_preserved(config, archives[package['artifact']['id']],
                                                 old['providers'], providers) and \
                not consumers_unbound(config, database, sorted(dependents), old['providers'] - providers):
            # REQUIRES omits dlopen users. Preserve exact providers unless the
            # payloads prove a versioned implementation of the retained SONAME,
            # or no file of an installed consumer loads a removed provider.
            raise SDKError(f'{old_name}: replacement removes library providers used by installed consumers; '
                           'a coordinated ABI update is required')
        for dependent in sorted(dependents):
            for pattern in metadata(dependent)['depends']:
                if matches(pattern, old_name) and not matches(pattern, new['name']):
                    successor = replaced_by.get(dependent)
                    if not _coordinated(matches, archives, package, new, old_name, successor, position):
                        raise SDKError(f"{dependent}: dependency {pattern} does not accept {new['name']}; "
                                       'a coordinated component update is required')
                    # pkg_add -D: the dependent's own replacement follows in this batch.
                    following = package.setdefault('coordinated', [])
                    if successor['pkgname'] not in following:
                        following.append(successor['pkgname'])


def _coordinated(matches, archives, package, new, old_name, successor, position):
    """True when SUCCESSOR, later in the batch, replaces the dependent and accepts NEW.

    The provider check above already proved that the old dependent's images
    load no removed library, so it stays usable until its replacement. Only
    its recorded dependency is unsatisfied between the two pkg_add calls and
    after a failure of the second; installing the same selection again
    replaces it.
    """
    if successor is None or position[id(successor)] < position[id(package)]:
        return False
    base = new['name'].rsplit('-', 1)[0]
    requirements = _archive(archives[successor['artifact']['id']])['depends']
    # Every requirement of this package, also one that matches neither version.
    return all(matches(pattern, new['name']) for pattern in requirements
               if matches(pattern, old_name) or matches(pattern, new['name']) or
               (pattern.startswith(base) and pattern[len(base):len(base) + 1] in tuple('-<>=[{')))
