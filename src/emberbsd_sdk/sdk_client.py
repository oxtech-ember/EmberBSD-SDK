# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Binary-first component installation, with source builds owned by Ports."""

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import ExitStack, redirect_stdout
import hashlib
from importlib import resources
from itertools import islice
import os
from pathlib import Path
import platform
import subprocess
import sys
import tarfile
import tempfile

from . import sysroot_package, package_automatic, package_runtime, prefix_lock
from .binary_catalog import load_release_catalogs, select_packages
from .desktop_apps import bind_installed_desktops
from .binary_release import _arch, _matches
from .errors import ChannelExpired, SDKError, TransportError
from .host_prefix import load_host_prefix
from .host_identity import native_cpu
from .jsonio import json_bytes, read_json, write_json
from .package_cache import PackageCache, check_space, directory_lock
from .package_install import PackageInstaller, install_mode, install_paths, target_sysroot
from .package_updates import reuse_compatible_dependencies
from .package_selection import _package_sets, _parse_root, _read_roots
from .release_delivery import MetadataCache, artifact_key, release_snapshot, _key
from .release_signing import ReleaseCrypto
from .release_s3 import HTTPStore, HTTPStatusError

DEFAULT_URL = 'https://emberbsd-pkg.oxteam.me'


LIBRARY_CATEGORIES = frozenset({
    'archivers', 'converters', 'databases', 'devel', 'fonts', 'lang',
    'print', 'security', 'textproc',
})


def package_role(root, pkgname):
    """Classify a signed package that has no verified desktop file.

    This is not a GUI-application test. ``library`` is a library category or a
    ``lib``/``py-`` name. Everything else stays ``package``, including curl.
    ``desktop`` is assigned only by an installed desktop file.
    """
    category = root.split('@', 1)[0].split('/', 1)[0]
    base = pkgname.rsplit('-', 1)[0]
    if category in LIBRARY_CATEGORIES or base.startswith(('lib', 'py-', 'py3')):
        return 'library'
    return 'package'


def catalog_index(release, catalogs, config, installer):
    """Read-only index of one verified release for the configured repositories."""
    artifacts = {item['id']: item for item in release['artifacts']}
    installed_by_repo = {}
    rows = []
    for catalog in catalogs:
        repository = catalog['repository']
        if repository not in config['repositories']:
            continue
        cfg = config['repositories'][repository]
        if repository not in installed_by_repo:
            installed_by_repo[repository] = installer.installed(cfg, recover=False)
        present = installed_by_repo[repository]
        for package in catalog['packages']:
            name = package['pkgname']
            base, version = name.rsplit('-', 1)
            artifact = artifacts[package['artifact']]
            siblings = sorted(item for item in present if '-' in item and item.rsplit('-', 1)[0] == base)
            if name in present:
                action = 'installed'
                installed_pkgname = name
            elif not siblings:
                action = 'available'
                installed_pkgname = None
            elif _replacement_is_upgrade(installer, name, present):
                action = 'upgrade'
                installed_pkgname = siblings[0]
            elif _installed_is_current(installer, name, present):
                action = 'installed-newer'
                installed_pkgname = siblings[0]
            else:
                action = 'version-uncompared'
                installed_pkgname = siblings[0]
            role = package_role(package['root'], name)
            rows.append(dict(
                repository=repository, root=package['root'], pkgname=name, base=base,
                version=version, role=role, catalog_role=role, action=action,
                installed_pkgname=installed_pkgname, size=artifact['size'],
                prefix=cfg.get('prefix')))
    rows.sort(key=lambda row: (row['repository'], row['root'], row['pkgname']))
    binding = bind_installed_desktops(rows, config['repositories'])
    counts = dict(total=len(rows), package=0, library=0, desktop=0, installed=0, upgrade=0,
                  available=0, installed_newer=0, version_uncompared=0)
    for row in rows:
        counts[row['role']] += 1
        key = row['action'].replace('-', '_')
        counts[key] = counts.get(key, 0) + 1
    counts['desktop'] = sum(len(row['desktops']) for row in rows)
    return dict(schema='emberbsd.catalog-index.v1', release_id=release['release_id'],
                desktop_metadata=False, desktop_binding=binding, counts=counts, packages=rows)


def _read_only_directories(config, state_dir, cache_dir, command):
    """Keep a read-only caller off the configuration's state and cache."""
    if state_dir is None and cache_dir is None:
        return config
    if command in ('install', 'update', 'auto-update', 'cache-clean'):
        raise SDKError('directory overrides are only for read-only commands')
    config = dict(config)
    for key, path in (('state_dir', state_dir), ('cache_dir', cache_dir)):
        if path is None:
            continue
        if not Path(path).is_absolute():
            raise SDKError(f'{key} override must be absolute')
        config[key] = str(Path(path))
    return config


def available_sets(catalogs, config):
    sets = {}
    for catalog in catalogs:
        if catalog['repository'] in config['repositories']:
            for name, roots in catalog['sets'].items():
                sets.setdefault(name, []).extend(roots)
    source = config.get('source')
    if source:
        for name, path in _package_sets(Path(source['ports_root'])).items():
            sets[name] = _read_roots(path)
    return {name: list(dict.fromkeys(roots)) for name, roots in sorted(sets.items())}


def installation_plan(release, catalogs, config, installer, cache, *, sets=(), roots=(), installed=None):
    catalogs = [c for c in catalogs if c['repository'] in config['repositories']]
    all_sets = available_sets(catalogs, config) if sets else {}
    requested = list(roots)
    for name in sets:
        if name not in all_sets:
            raise SDKError(f'unknown package set: {name}; configure Ports to select an unpublished set')
        requested.extend(all_sets[name])
    requested = list(dict.fromkeys(_parse_root(r, 'selection') for r in requested))
    records, platforms = {}, {}
    installed = {} if installed is None else dict(installed)
    artifacts = {a['id']: a for a in release['artifacts']}
    for catalog in catalogs:
        repository = catalog['repository']
        cfg = config['repositories'][repository]
        if repository not in installed:
            installed[repository] = installer.installed(cfg, recover=False)
        for package in catalog['packages']:
            artifact = artifacts[package['artifact']]
            if (artifact['host'] != cfg.get('host') or artifact['target'] != cfg.get('target')):
                raise SDKError(f'binary repository platform differs from configuration: {repository}')
            records[artifact['id']] = dict(package, repository=repository,
                                           conflicts=catalog.get('conflicts', {}).get(package['pkgname'], []))
            platforms.setdefault(package['root'], []).append(artifact['id'])
    missing = [root for root in requested if root not in platforms]
    for root in requested:
        role = 'host' if root.endswith('@native') else 'target'
        if not any(cfg.get(role) for cfg in config['repositories'].values()):
            raise SDKError(f'no {role} destination configured for {root}')
    selected = [r for r in requested if r in platforms]
    while True:
        packages, downloads, unpacked = _order_packages(release, catalogs, records, installed, cache, selected)
        # A split or renamed package conflicts with an installed one that pkg_add
        # cannot replace by itself. Its current successor joins the same selection.
        additions = _conflict_replacements(packages, records, installed, installer)
        if not additions:
            break
        selected = selected + additions
    return dict(release_id=release['release_id'], roots=requested, packages=packages,
                build_roots=missing, download_size=sum(downloads.values()), unpacked_size=unpacked)


def _order_packages(release, catalogs, records, installed, cache, roots):
    selection = select_packages(release, catalogs, roots=roots)
    by_id = {p['id']: p for p in selection['packages']}
    ordered, visited = [], set()
    def visit(identity):
        if identity in visited:
            return
        visited.add(identity)
        for dependency in by_id[identity]['depends']:
            visit(dependency)
        ordered.append(identity)
    for identity in sorted(by_id):
        visit(identity)
    packages, downloads, unpacked = [], {}, 0
    for identity in ordered:
        item, record = by_id[identity], records[identity]
        base = record['pkgname'].rsplit('-', 1)[0]
        replaces = sorted(name for name in installed[record['repository']]
                          if name.rsplit('-', 1)[0] == base and name != record['pkgname'])
        if record['pkgname'] in installed[record['repository']]:
            action = 'installed'
        else:
            action = 'cache' if cache.path(item).exists() else 'download'
            unpacked += item['unpacked_size']
            if action == 'download':
                downloads[item['sha256']] = item['size']
        packages.append(dict(record, artifact=item, action=action, replaces=replaces, removes=[]))
    return packages, downloads, unpacked


def _conflict_replacements(packages, records, installed, installer):
    """Resolve explicit CONFLICTS of planned packages against installed ones.

    pkg_add forgives a conflict only with the version it replaces. Any other
    installed match must leave before that pkg_add. It may leave only when
    this current carries a newer package of its PKGBASE; that successor is
    returned as an additional root and the planned package records the removal.
    Without pkg_tools (test installers) conflicts are left to pkg_add.
    """
    crypto = getattr(installer, 'crypto', None)
    if crypto is None:
        return []
    planned = {package['root'] for package in packages}
    additions = []
    for package in packages:
        if package['action'] == 'installed':
            continue
        names = installed[package['repository']]
        for pattern in package['conflicts']:
            for name in sorted(names):
                if name in package['replaces'] or not _matches(crypto, pattern, name):
                    continue
                base = name.rsplit('-', 1)[0]
                successors = [r for r in records.values() if r['repository'] == package['repository']
                              and r['pkgname'].rsplit('-', 1)[0] == base]
                if (len(successors) != 1 or successors[0]['pkgname'] == name or
                        not _replacement_is_upgrade(installer, successors[0]['pkgname'], {name})):
                    raise SDKError(f"{package['pkgname']} conflicts with installed {name}; "
                                   f'current has no newer {base} to replace it')
                root = successors[0]['root']
                if root not in planned and root not in additions:
                    additions.append(root)
                if name not in package['removes']:
                    package['removes'].append(name)
    return additions


def _source_config(config, roots):
    if not roots:
        return
    if any(install_mode(c) == 'native-stage' for c in config['repositories'].values()):
        raise SDKError('native stage installs signed binaries only; build and sign missing packages '
                       'with the explicit Ports stage configuration before installing them')
    if any(c.get('target') and not target_sysroot(c) for c in config['repositories'].values()):
        raise SDKError('live target package installation requires published binaries; build with Ports on the developer host')
    source = config.get('source')
    if not source:
        raise SDKError('binary package absent; configure Ports source (ports_root, pkgsrc, makeconf, state) '
                       'to build: ' + ', '.join(roots))
    for key in ('ports_root', 'pkgsrc', 'makeconf', 'state'):
        if not isinstance(source.get(key), str) or not Path(source[key]).is_absolute():
            raise SDKError(f'Ports source requires an absolute {key}')


def plan_source_selection(config, *, sets=(), roots=()):
    """Plan local Ports inputs without loading a signed release or installing.

    This deliberately does not use run_source: installation destinations and
    signed-binary-only modes are not conditions on a local dependency query.
    Ports still owns evaluation, state locking and dependency validation.
    """
    if not sets and not roots:
        print('Nothing selected; no download, install or build requested.')
        return 0
    from . import package_selection
    source = config.get('source')
    if not isinstance(source, dict):
        raise SDKError('source-plan requires a configured local Ports source')
    for key in ('ports_root', 'pkgsrc', 'makeconf', 'state'):
        if not isinstance(source.get(key), str) or not Path(source[key]).is_absolute():
            raise SDKError(f'Ports source requires an absolute {key}')
    native = load_host_prefix(config['host_prefix'], required_roles={'make', 'sh'})
    values = dict(BMAKE=native.tool('make'), MAKECONF=source['makeconf'],
                  PKGSRCDIR=source['pkgsrc'])
    arguments = ['--ports-root', source['ports_root'], 'plan',
                 '--host-prefix', config['host_prefix'], '--state', source['state']]
    for name in sets:
        arguments.extend(('--set', name))
    for root in roots:
        arguments.extend(('--package', root))
    # Never inherit a binary-install selection into this source-only plan.
    previous = {key: os.environ.get(key) for key in (*values, 'CROSS_BULK_BINARY_INSTALLED')}
    try:
        os.environ.update(values)
        os.environ.pop('CROSS_BULK_BINARY_INSTALLED', None)
        return package_selection.main(arguments)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def run_source(config, roots, command, *, binaries=(), controller_mode=None, controller_ready=None):
    """Invoke the existing selector; Ports owns dependency solving and reuse."""
    if not roots:
        return
    _source_config(config, roots)
    from . import package_selection
    source = config['source']
    native = load_host_prefix(config['host_prefix'], required_roles={'make', 'sh'})
    values = dict(BMAKE=native.tool('make'), MAKECONF=source['makeconf'], PKGSRCDIR=source['pkgsrc'])
    # Validate the producer's effective destinations before it can change them.
    for role in ('host', 'target'):
        configs = [c for c in config['repositories'].values() if c.get(role)]
        if not configs:
            continue
        args = [native.tool('make'), '-C', str(Path(source['pkgsrc']) / 'pkgtools/pkg_install')]
        if role == 'host':
            args += ['USE_CROSS_COMPILE=no']
        result = subprocess.run(args + ['show-vars', 'VARNAMES=PREFIX PKG_DBDIR CROSS_DESTDIR OPSYS MACHINE_ARCH'],
            env=dict(os.environ, **values, PATH=native.path, SHELL=native.tool('sh')),
            capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise SDKError('cannot query Ports installation destinations: ' + result.stderr[-2048:])
        fields = result.stdout.splitlines()
        if len(fields) != 5:
            raise SDKError('Ports returned invalid installation destinations')
        prefix, database, sysroot, opsys, arch = fields
        for cfg in configs:
            expected_os = cfg['host']['os'] if role == 'host' else 'NetBSD'
            if (prefix != cfg['prefix'] or database != cfg['pkgdb'] or opsys != expected_os or
                    _arch(arch) != _arch(cfg[role]['arch']) or
                    (role == 'target' and Path(sysroot).resolve() != Path(cfg['sysroot']).resolve())):
                raise SDKError('Ports platform/prefix/database differs from binary installation configuration')
    previous = {key: os.environ.get(key) for key in values}
    try:
        os.environ.update(values)
        arguments = ['--ports-root', source['ports_root'], command, '--host-prefix', config['host_prefix'],
                     '--state', source['state']]
        for root in roots:
            arguments += ['--package', root]
        with tempfile.TemporaryDirectory(prefix='ember-binary-selection-') as directory:
            selected = Path(directory) / 'packages'
            selected.write_text(''.join(root + ' ' + name + '\n' for root, name in binaries))
            binary_variable = 'CROSS_BULK_BINARY_INSTALLED'
            old_binary = os.environ.get(binary_variable)
            os.environ[binary_variable] = str(selected)
            try:
                with redirect_stdout(sys.stderr):
                    if controller_ready is None:
                        from .source_wave import current_controller_ready
                        controller_ready = current_controller_ready()
                    status = package_selection.main(arguments, controller_stdout=sys.stderr,
                                                    controller_mode=controller_mode,
                                                    controller_ready=controller_ready)
            finally:
                if old_binary is None:
                    os.environ.pop(binary_variable, None)
                else:
                    os.environ[binary_variable] = old_binary
        if status:
            raise SDKError(f'Ports {command} failed ({status}); compatible objects and logs were retained')
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def build_missing(config, roots, binaries=()):
    if roots:
        run_source(config, roots, 'build', binaries=binaries)


def _required_source_inputs(roots, dependencies, supplied):
    """Follow Ports build edges only through nodes whose build is still required."""
    needed = set()
    todo = list(roots)
    while todo:
        root = todo.pop()
        if root in needed:
            continue
        needed.add(root)
        if root not in supplied:
            todo.extend(dependencies.get(root, ()))
    return needed


def include_source_dependencies(plan, release, catalogs, config, installer, cache):
    """Read the owner's resolved graph once, selecting exact available binaries."""
    if not plan['build_roots'] or not config.get('source'):
        return plan
    _source_config(config, plan['build_roots'])
    path = Path(config['source']['state']) / 'packages'
    selected = [c for c in catalogs if c['repository'] in config['repositories']]
    records = {p['artifact']: p for c in selected for p in c['packages']}
    build_roots = set(plan['build_roots'])
    while True:
        run_source(config, sorted(build_roots), 'plan')
        if not path.is_file():
            raise SDKError('update Ports: source plan did not produce package identities')
        names = {}
        for line in path.read_text().splitlines():
            root, name = line.split()
            names[_parse_root(root, 'Ports graph')] = name
        conflicts = set()
        for root in set(plan['roots']) - build_roots:
            closure = select_packages(release, selected, roots=[root])['packages']
            if any(records[p['id']]['root'] in names and
                   names[records[p['id']]['root']] != records[p['id']]['pkgname'] for p in closure):
                conflicts.add(root)
        if not conflicts:
            break
        # Each pass moves at least one original root to the owner's graph.
        # A binary dependent must not survive replacement of its library.
        build_roots.update(conflicts)
    candidates = []
    for package in records.values():
        root = package['root']
        if names.get(root) != package['pkgname']:
            continue
        closure = select_packages(release, selected, roots=[root])['packages']
        if all(names.get(records[p['id']]['root']) == records[p['id']]['pkgname'] for p in closure):
            candidates.append(root)
    graph = _read_source_graph(path.parent)
    if graph and set(graph[0]) == names.keys() and build_roots <= names.keys():
        installed = {role: _installed_names(config, installer, role) for role in ('host', 'target')}
        supplied = {root for root, name in names.items() if
                    _installed_is_current(installer, name, installed['host' if root.endswith('@native') else 'target'])}
        needed = _required_source_inputs(build_roots, graph[1], supplied.union(candidates))
        candidates = [root for root in candidates if root in needed]
    result = installation_plan(release, catalogs, config, installer, cache,
                               roots=[r for r in plan['roots'] if r not in build_roots] + candidates)
    result['roots'] = plan['roots']
    result['build_roots'] = sorted(build_roots)
    result['source_packages'] = names
    return result


class ArchiveUnavailable(SDKError):
    """An archive GET returned HTTP 403 or 404 for this verified release."""

    def __init__(self, release_digest, error):
        super().__init__(str(error))
        self.release_digest = release_digest


def _fetch_archives(items, cache, store, prefix):
    """One writer per SHA256 under the client's shared cache lock."""
    unique = {}
    for item in items:
        digest = item['sha256']
        if unique.setdefault(digest, item)['size'] != item['size']:
            raise SDKError(f'conflicting signed sizes for archive {digest}')

    def fetch(item):
        path = cache.fetch(store, _key(prefix, artifact_key(item, '')), item)
        return item['sha256'], path

    remaining = iter(unique.values())
    limit = 4
    active = set()
    paths = {}
    pool = ThreadPoolExecutor(max_workers=limit)
    try:
        while True:
            for item in islice(remaining, limit - len(active)):
                active.add(pool.submit(fetch, item))
            if not active:
                return paths
            done, active = wait(active, return_when=FIRST_COMPLETED)
            paths.update(future.result() for future in done)
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


def apply_plan(plan, config, installer, cache, store, *, source_locked=False, release_digest=None):
    _source_config(config, plan['build_roots'])
    pending = [p for p in plan['packages'] if p['action'] != 'installed']
    selected_repositories = {p['repository'] for p in plan['packages']}
    sysroot = plan.get('sysroot')
    new_sysroot = sysroot and sysroot['action'] != 'installed'
    keep = {p['artifact']['sha256'] for p in pending}
    if new_sysroot:
        keep.add(sysroot['artifact']['sha256'])
    cache.trim(config.get('cache_limit', 512 * 1024 * 1024), keep=keep)
    # Conservatively include download + unpacked space, without copying a prefix.
    check_space(cache.root, plan['download_size'] + plan['unpacked_size'])
    for repository in {p['repository'] for p in pending}:
        size = sum(p['artifact']['unpacked_size'] for p in pending if p['repository'] == repository)
        prefix, _ = install_paths(config['repositories'][repository])
        check_space(prefix, size)
    items = [package['artifact'] for package in pending]
    if new_sysroot:
        check_space(Path(sysroot['destination']).parent, sysroot['artifact']['unpacked_size'])
        items.append(sysroot['artifact'])
    try:
        paths = _fetch_archives(items, cache, store, config.get('url_prefix', ''))
    except HTTPStatusError as exc:
        if exc.status not in (403, 404):
            raise
        raise ArchiveUnavailable(release_digest, exc) from exc
    next_root = Path(sysroot['destination']).resolve() if new_sysroot else None
    archives = {}

    def lock_repositories(locks, repositories):
        for repository in repositories:
            _, database = install_paths(config['repositories'][repository])
            locks.enter_context(prefix_lock.writing(database))

    def retain_installed(repositories):
        for repository in repositories:
            cfg = config['repositories'][repository]
            packages = [p for p in plan['packages'] if p['repository'] == repository]
            plan['compatible_reuse'].extend(reuse_compatible_dependencies(installer.crypto, cfg,
                packages, archives, installer.installed(cfg, recover=False), plan['roots']))

    with ExitStack() as locks:
        # Same whole-build lock as cross-bulk. A source-wave supervisor already
        # holds it across adoption, so this install must not take it again.
        if config.get('source') and (new_sysroot or selected_repositories) and not source_locked:
            locks.enter_context(directory_lock(Path(config['source']['state']) / 'build.lock'))
        # A pkgdb lock creates its parent. Only the new base has to be
        # published before its package database can be locked.
        fresh = sorted(repo for repo in selected_repositories
                       if next_root is not None and target_sysroot(config['repositories'][repo])
                       and Path(config['repositories'][repo]['sysroot']).resolve() == next_root)
        existing = sorted(selected_repositories.difference(fresh))
        lock_repositories(locks, existing)
        staged = None
        if new_sysroot:
            archive = paths[sysroot['artifact']['sha256']]
            staged = locks.enter_context(sysroot_package.staged_install(archive, sysroot))
        overlays = {}
        for package in pending:
            repository = package['repository']
            cfg = config['repositories'][repository]
            if repository not in overlays and isinstance(installer, PackageInstaller):
                overlays[repository] = package_runtime.selected(cfg, staged if repository in fresh else None)
            item = package['artifact']
            path = paths[item['sha256']]
            if overlay := overlays.get(repository):
                installer.check(path, cfg, visit=overlay.visitor(package['pkgname']))
            else:
                installer.check(path, cfg)
            archives[item['id']] = path
        if isinstance(installer, PackageInstaller):
            plan['compatible_reuse'] = []
            retain_installed(existing)
            for repository, overlay in overlays.items():
                if overlay:
                    overlay.check([p for p in plan['packages'] if p['repository'] == repository])
        if new_sysroot:
            sysroot_package.publish_install(staged[0], sysroot)
        lock_repositories(locks, fresh)
        if isinstance(installer, PackageInstaller):
            retain_installed(fresh)
            pending = [p for p in plan['packages'] if p['action'] != 'installed']
            for repository in sorted(selected_repositories):
                cfg = config['repositories'][repository]
                package_automatic.recover(installer.crypto, cfg, installer.installed(cfg, recover=False))
        automatic_states = {}
        # Check every replacement before changing any package. pkg_add remains
        # the dependency and file owner; this path preserves installed consumers.
        for repository in sorted(selected_repositories):
            cfg = config['repositories'][repository]
            selected = [p for p in plan['packages'] if p['repository'] == repository]
            installer.check_updates(selected, archives, cfg)
            if isinstance(installer, PackageInstaller):
                automatic_states[repository] = package_automatic.plan(cfg,
                    [p for p in selected if p['action'] != 'installed'], plan['roots'])
        removed = set()
        try:
            for package in pending:
                cfg = config['repositories'][package['repository']]
                # Recheck paths after earlier packages have populated the prefix.
                from .package_install import inspect_install_paths
                if isinstance(installer, PackageInstaller):
                    archive = archives[package['artifact']['id']]
                    if overlay := overlays.get(package['repository']):
                        overlay.recheck(package, archive)
                    else:
                        inspect_install_paths(archive, cfg)
                    for name in package.get('removes', []):
                        _remove_conflicting(installer, cfg, package['repository'], name, pending,
                                            automatic_states[package['repository']], removed)
                    automatic = automatic_states[package['repository']][package['pkgname']]
                    package_automatic.prepare(cfg, package['pkgname'], automatic)
                    installer.install(archives[package['artifact']['id']], cfg, checked=True,
                                      configure=False, automatic=automatic == 'yes')
                    package_automatic.finish(installer.crypto, cfg, package['pkgname'])
                else:
                    installer.install(archives[package['artifact']['id']], cfg, checked=True, configure=False)
        finally:
            # Dependents of a removed package keep their files; the dependency
            # tree is rebuilt even when a later package of the batch failed.
            for repository in sorted({repository for repository, _ in removed}):
                installer.rebuild_tree(config['repositories'][repository])
        # Configure only after every selected payload has been installed and
        # relocated. Pending hooks are retried even when this selection reused
        # every exact installed package.
        for repository in sorted(selected_repositories):
            installer.configure(config['repositories'][repository])
    cache.trim(config.get('cache_limit', 512 * 1024 * 1024))


def _remove_conflicting(installer, cfg, repository, name, pending, automatic_states, removed):
    """Delete one installed package that the next pkg_add conflicts with.

    The plan proved a newer successor of its PKGBASE is in this batch. When
    that successor was installed first, pkg_add -U already replaced the old
    package and nothing remains to delete. Otherwise the successor's automatic
    state intent is recorded first: it was read from the old package, which is
    gone after this call.
    """
    if (repository, name) in removed or name not in installer.installed(cfg, recover=False):
        return
    for successor in pending:
        if successor['repository'] == repository and name in successor['replaces']:
            package_automatic.prepare(cfg, successor['pkgname'], automatic_states[successor['pkgname']])
    installer.remove(name, cfg)
    removed.add((repository, name))


def _read_source_graph(state):
    """Return the saved build order and dependency edges, or None when either is absent.

    Edges are `dependency dependent`, including a self-edge for every node.
    The order file is the producer's schedule. Both must stay the full graph:
    a later wave may build only the ready nodes, but target-fed native tools
    are identified from edges to target packages that are not in that wave.
    """
    order_path, edges_path = state / 'order', state / 'edges'
    if not order_path.is_file() or not edges_path.is_file():
        return None
    order = [line.strip() for line in order_path.read_text().splitlines() if line.strip()]
    deps = {root: [] for root in order}
    for line in edges_path.read_text().splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[0] == parts[1]:
            continue
        deps.setdefault(parts[1], []).append(parts[0])
    return order, deps


def _catalog_closure_matches(root, names, release, catalogs, config):
    """True when the signed catalog names this graph node and its runtime closure exactly."""
    selected = [catalog for catalog in catalogs if catalog['repository'] in config['repositories']]
    records, by_root = {}, {}
    for catalog in selected:
        for package in catalog['packages']:
            records[package['artifact']] = package
            by_root.setdefault(package['root'], package)
    record = by_root.get(root)
    if record is None or record['pkgname'] != names.get(root):
        return False
    try:
        closure = select_packages(release, selected, roots=[root])['packages']
    except KeyError as exc:
        raise SDKError('authenticated catalog closure is incomplete') from exc
    for item in closure:
        closed = records.get(item['id'])
        if closed is None:
            raise SDKError(f"authenticated catalog omits closure package {item['id']}")
        if names.get(closed['root']) != closed['pkgname']:
            return False
    return True


def _refresh_channel(refresh_current):
    """Reread authenticated current.

    An expired pointer, an HTTP status, or a transport failure keeps the last
    verified catalog. Certificate, signature, schema, hash and not-yet-valid
    failures are not transport errors and stop the install.
    """
    try:
        return refresh_current()
    except (HTTPStatusError, TransportError, ChannelExpired) as exc:
        print(f'Current channel could not be reread ({exc}); continuing with the last verified catalog.',
              file=sys.stderr)
        return None


# Producer/consumer packages that cross-bulk compiles from one staged work
# directory when both are in the same build. Keeping only one of the pair in
# the schedule disables that handoff and builds each package alone.
_SHARED_STAGE_PAIRS = (
    ('lang/llvm', 'lang/llvm-devel'),
    ('x11/qt6-qtbase', 'x11/qt6-qtbase-devel'),
    ('textproc/icu', 'textproc/icu-devel'),
)


def _ready_wave(order, deps, pending):
    ready = [root for root in order if root in pending and
             not any(dep in pending for dep in deps.get(root, ()))]
    selected = set(ready)
    # Target and native copies of one PKGPATH are different packages. Walk
    # both roles explicitly so set iteration order cannot pick one of them.
    changed = True
    while changed:
        changed = False
        for producer, consumer in _SHARED_STAGE_PAIRS:
            for suffix in ('', '@native'):
                producer_root = producer + suffix
                consumer_root = consumer + suffix
                if producer_root not in selected or consumer_root not in pending or consumer_root in selected:
                    continue
                if all(dep in selected or dep not in pending for dep in deps.get(consumer_root, ())):
                    selected.add(consumer_root)
                    changed = True
    return [root for root in order if root in selected]


def _installed_names(config, installer, role):
    names = set()
    for cfg in config['repositories'].values():
        cfg_role = 'host' if cfg.get('host') else 'target'
        if cfg_role != role:
            continue
        names.update(installer.installed(cfg, recover=False))
    return names


def _version_pattern(graph_name):
    base, version = graph_name.rsplit('-', 1)
    return base, base + '>=' + version


def _installed_is_current(installer, graph_name, installed):
    """True when this role's database already has the graph package or a newer one."""
    if graph_name in installed:
        return True
    crypto = getattr(installer, 'crypto', None)
    if crypto is None or '-' not in graph_name:
        return False
    base, pattern = _version_pattern(graph_name)
    return any(name.rsplit('-', 1)[0] == base and _matches(crypto, pattern, name) for name in installed)


def _unproven_target_replacement(installer, graph_name, installed):
    """True when a different installed target of this PKGBASE might not be older."""
    if '-' not in graph_name or graph_name in installed:
        return False
    base = graph_name.rsplit('-', 1)[0]
    siblings = [name for name in installed if name != graph_name and '-' in name and name.rsplit('-', 1)[0] == base]
    return bool(siblings) and not _replacement_is_upgrade(installer, graph_name, installed)


def _replacement_is_upgrade(installer, graph_name, installed):
    """True only when pkg_tools shows every installed sibling is older than the catalog package."""
    crypto = getattr(installer, 'crypto', None)
    if crypto is None or '-' not in graph_name:
        return False
    base, pattern = _version_pattern(graph_name)
    siblings = [name for name in installed if name.rsplit('-', 1)[0] == base and name != graph_name]
    return bool(siblings) and all(not _matches(crypto, pattern, name) for name in siblings)


def _adopt_published(config, installer, cache, store, release, catalogs, names, order, pending, binaries,
                     *, roots, dependencies, release_digest):
    """Download exact not-yet-started packages, including an older installed package.

    A newer installed package stays on the source wave. An installer that cannot
    ask pkg_tools whether the installed package is older does not download a
    replacement.
    """
    candidates = []
    for root in order:
        if root not in pending or not _catalog_closure_matches(root, names, release, catalogs, config):
            continue
        one = installation_plan(release, catalogs, config, installer, cache, roots=[root])
        own = [package for package in one['packages'] if package['root'] == root]
        # A newer installed package stays with Ports. An older installed package
        # is an exact catalog upgrade and is downloaded. Dependencies may still
        # be reused by apply_plan when the installer can prove that.
        if one['build_roots'] or len(own) != 1 or own[0]['pkgname'] != names[root]:
            continue
        if own[0]['replaces']:
            role = 'host' if root.endswith('@native') else 'target'
            if not _replacement_is_upgrade(installer, names[root], _installed_names(config, installer, role)):
                continue
        candidates.append(root)
    supplied = set(order).difference(pending).union(candidates)
    needed = _required_source_inputs(roots, dependencies, supplied)
    candidates = [root for root in candidates if root in needed]
    if not candidates:
        return binaries, []
    merged = installation_plan(release, catalogs, config, installer, cache, roots=candidates)
    if merged['build_roots']:
        return binaries, []
    dependency_replacements = [package for package in merged['packages']
                               if package['replaces'] and package['root'] not in candidates]
    if dependency_replacements and not isinstance(installer, PackageInstaller):
        return binaries, []
    fresh = [package for package in merged['packages'] if package['action'] != 'installed']
    reused = []
    if fresh:
        with directory_lock(cache.root / '.lock'):
            apply_plan(merged, config, installer, cache, store, source_locked=True, release_digest=release_digest)
        reused = list(merged.get('compatible_reuse') or [])
        binaries = list(binaries) + [(package['root'], package['pkgname'])
                                     for package in merged['packages'] if package.get('pkgname')]
        # Reuse can keep a newer installed dependency, so count only packages
        # that were actually added from this pointer.
        added = [package for package in merged['packages'] if package['action'] != 'installed' and package.get('artifact')]
        if added:
            size = sum(package['artifact']['size'] for package in added)
            label = 'package' if len(added) == 1 else 'packages'
            print(f"Current supplies {len(added)} {label} ({size} bytes); "
                  f"installing them instead of building.", file=sys.stderr)
    for package in merged['packages']:
        pending.discard(package['root'])
    return binaries, reused


def _receipt_roots(rows):
    return [row[0] for row in rows if row]


def _retain_current(config, installer, names, pending):
    """Drop a package the role's database already accepts at the graph version or newer.

    Ports replaces any installed target of the same PKGBASE, including a newer
    one. That replacement is a downgrade, so a current target never enters the
    schedule. A current native package is the same ``BASE>=version`` result
    Ports already treats as done.
    """
    for root in list(pending):
        name = names.get(root)
        if not name:
            continue
        role = 'host' if root.endswith('@native') else 'target'
        installed = _installed_names(config, installer, role)
        if _installed_is_current(installer, name, installed):
            pending.discard(root)
            continue
        # Ports replaces any target of the same PKGBASE. Scheduling that is a
        # downgrade unless pkg_tools has shown the installed package is older.
        if role == 'target' and _unproven_target_replacement(installer, name, installed):
            installed_names = ', '.join(sorted(
                sibling for sibling in installed if sibling.rsplit('-', 1)[0] == name.rsplit('-', 1)[0]))
            raise SDKError(
                f'{root} is installed as {installed_names}; refusing to replace it with {name} '
                'without proof it is older')


def _accept_built_wave(config, installer, names, ready, pending, receipts):
    """Honor this wave's done, skipped and failed receipts.

    A done row is ``PKGPATH PKGNAME ...``. The PKGNAME has to be the graph
    package; a newer or older name in that row is not success. A native
    package Ports reused through ``pkg_info -e BASE>=version`` has no done
    row and is accepted from the host database. A target package is accepted
    from an exact target-database name or a matching done row, never from
    the host database and never from a newer target that Ports would replace.
    """
    skipped = set(_receipt_roots(receipts['skipped']))
    failed = set(_receipt_roots(receipts['failed'])) | set(_receipt_roots(receipts['blocked']))
    blocked = [root for root in ready if root in skipped or root in failed]
    if blocked:
        raise SDKError('Ports did not build ' + ', '.join(blocked))
    done = {}
    mismatched = []
    for row in receipts['done']:
        if not row or row[0] not in ready:
            continue
        pkgname = row[1] if len(row) > 1 else None
        expected = names.get(row[0])
        if pkgname != expected:
            mismatched.append(f'{row[0]} receipt {pkgname or "without a PKGNAME"} is not {expected or "the graph package"}')
        else:
            done[row[0]] = pkgname
    if mismatched:
        raise SDKError('Ports receipt does not match the graph: ' + '; '.join(mismatched))
    missing = []
    for root in ready:
        if root in done:
            pending.discard(root)
            continue
        role = 'host' if root.endswith('@native') else 'target'
        installed = _installed_names(config, installer, role)
        name = names.get(root)
        if role == 'host' and name and _installed_is_current(installer, name, installed):
            pending.discard(root)
            continue
        if role == 'target' and name and name in installed:
            pending.discard(root)
            continue
        missing.append(f'{root} ({name or "unknown"})')
    if missing:
        raise SDKError('Ports build finished without installing ' + ', '.join(missing))


def _install_source_waves(plan, config, installer, cache, store, refresh_current, accepted_release, binaries):
    """Re-read current between waves. Return reuse rows, or None to keep one selected build."""
    source = config.get('source')
    names = plan.get('source_packages')
    if refresh_current is None or not names or not source:
        return None
    state = Path(source['state'])
    from .source_wave import SourceWaveLock, drop_resolved_receipts
    # The lock covers the graph read, every channel reread and every controller.
    # The controller writes a private copy and locks only that copy.
    session = SourceWaveLock(state)
    try:
        graph = _read_source_graph(state)
        if graph is None:
            return None
        order, deps = graph
        if any(root not in order for root in plan['build_roots']):
            return None
        handled = {package['root'] for package in plan['packages']}
        nodes = set(order)
        pending = (nodes - handled) & _required_source_inputs(plan['build_roots'], deps, handled)
        reused, seen = [], accepted_release
        while pending:
            snapshot = _refresh_channel(refresh_current)
            scheduled = set(pending)
            _retain_current(config, installer, names, pending)
            retained = scheduled - pending
            if retained:
                resolved = [(root, names[root]) for root in retained]
                drop_resolved_receipts(state, resolved, planned=names)
                pending.intersection_update(_required_source_inputs(plan['build_roots'], deps, nodes - pending))
            if snapshot is not None:
                release, catalogs, identity = snapshot
                if seen != identity:
                    if seen is not None:
                        print('Current changed; checking for published packages.', file=sys.stderr)
                    seen = identity
                binaries, extra = _adopt_published(config, installer, cache, store, release, catalogs,
                                                   names, order, pending, binaries,
                                                   roots=plan['build_roots'], dependencies=deps, release_digest=identity)
                reused.extend(extra)
                pending.intersection_update(_required_source_inputs(plan['build_roots'], deps, nodes - pending))
            if not pending:
                break
            ready = _ready_wave(order, deps, pending)
            if not ready:
                raise SDKError('source graph has no ready package')
            receipts = session.build(config, ready, binaries, planned=names)
            _accept_built_wave(config, installer, names, ready, pending, receipts)
        return reused
    finally:
        session.close()


def _read_channel(crypto, store, trust, state, prefix, metadata):
    """Load one authenticated current into memory. The snapshot directory does not outlive this call."""
    with release_snapshot(crypto, store, trust, state, prefix=prefix, cache=metadata) as (release, snapshot):
        catalogs = load_release_catalogs(release, snapshot, ())
        identity = hashlib.sha256((snapshot / 'release.p7m').read_bytes()).hexdigest()
        return release, catalogs, identity


def install_selection(plan, config, installer, cache, store, *, refresh_current=None, accepted_release=None):
    with directory_lock(cache.root / '.lock'):
        apply_plan(plan, config, installer, cache, store, release_digest=accepted_release)
    # Compilation may be long; it must not monopolize another target's downloads.
    binaries = [(package['root'], package['pkgname']) for package in plan['packages'] if package.get('pkgname')]
    reused = list(plan.get('compatible_reuse') or [])
    extra = _install_source_waves(plan, config, installer, cache, store, refresh_current,
                                  accepted_release, binaries) if plan['build_roots'] else None
    if extra is None:
        build_missing(config, plan['build_roots'], binaries)
    else:
        reused.extend(extra)
    if plan.get('sysroot'):
        sysroot_package.bind_compiler(config, plan['sysroot'])
    return reused


def include_sysroot(plan, release, config, cache, target):
    if target is None:
        return plan
    selected = sysroot_package.selection(release, config, cache, target)
    plan = dict(plan, sysroot=selected)
    if selected['action'] != 'installed':
        plan['unpacked_size'] += selected['artifact']['unpacked_size']
        if selected['action'] == 'download':
            plan['download_size'] += selected['artifact']['size']
    return plan


BUILD_REQUIRED = {'os_source', 'host_bfd_config'}
BUILD_OPTIONAL = {'work', 'distfiles', 'sources', 'kernel_configs'}


def build_options(config):
    """Return the checked build section of CONFIG with the Ports and SDK checkouts."""
    build, source = config.get('build'), config.get('source')
    if not isinstance(build, dict) or not BUILD_REQUIRED <= set(build) <= BUILD_REQUIRED | BUILD_OPTIONAL:
        raise SDKError('the build section needs os_source and host_bfd_config, '
                       'and accepts work, distfiles, sources and kernel_configs')
    if not isinstance(source, dict) or not isinstance(source.get('ports_root'), str):
        raise SDKError('builds need the Ports checkout in source.ports_root')
    sources = build.get('sources', {})
    if not isinstance(sources, dict):
        raise SDKError('build.sources maps EMBER_*_SOURCE to prepared trees')
    kernel_configs = build.get('kernel_configs', [])
    if not isinstance(kernel_configs, list):
        raise SDKError('build.kernel_configs lists directories of kernel configurations')
    paths = [(key, value) for key, value in build.items() if key not in ('sources', 'kernel_configs')]
    paths += [('ports_root', source['ports_root'])] + [(f'sources.{key}', value) for key, value in sources.items()]
    paths += [('kernel_configs entry', value) for value in kernel_configs]
    for key, value in paths:
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise SDKError(f'build needs an absolute {key}')
    return dict(build, ports_root=source['ports_root'], selected_sources=dict(sources),
                sdk_root=str(Path(__file__).resolve().parents[2]), host_prefix=config['host_prefix'])


def make_profile_plan(config, profile_path):
    from . import build_profile
    options = build_options(config)
    return options, build_profile.make_plan(
        build_profile.load_profile(profile_path), os_root=options['os_source'],
        ports_root=options['ports_root'], sdk_root=options['sdk_root'], host_prefix=options['host_prefix'],
        host_bfd_config=options['host_bfd_config'],
        state=options.get('work', os.path.join(config['state_dir'], 'build')),
        cache=os.path.join(config['cache_dir'], 'plan'), selected_sources=options['selected_sources'],
        kernel_configs=options.get('kernel_configs', ()))


def plan_profile(config, profile_path):
    """Print the build plan of a profile. Build, download and install nothing."""
    from . import build_profile
    _, plan = make_profile_plan(config, profile_path)
    sys.stdout.buffer.write(json_bytes(plan))
    print('Plan: ' + build_profile.summary(plan), file=sys.stderr)
    return 0


def build_profile_command(config, config_path, profile_path, *, explain=False):
    """Plan the profile, then let Ninja run the nodes that are not ready."""
    from . import build_profile, profile_build
    from .cpu_capacity import resolve_jobs
    if config_path is None:
        raise SDKError('profile-build needs an explicit --config for its Ninja edges')
    options, plan = make_profile_plan(config, profile_path)
    if not options.get('work') or not options.get('distfiles'):
        raise SDKError('profile-build needs build.work and build.distfiles')
    print('Plan: ' + build_profile.summary(plan), file=sys.stderr)
    command = [sys.executable, '-B', str(Path(__file__).resolve().parents[2] / 'scripts/packages.py'),
               '--config', str(Path(config_path).resolve()), 'profile-node']
    # The OS stages run the tools of the host prefix: keep it unchanged meanwhile.
    with prefix_lock.reading([os.path.join(config['host_prefix'], 'pkgdb')]) as readers:
        return profile_build.build(plan, work=options['work'], command=command,
                                   ninja=os.path.join(config['host_prefix'], 'bin/ninja'),
                                   jobs=resolve_jobs(None), explain=explain, pass_fds=readers)


def run_profile_node(config, node, key):
    from . import profile_build
    from .cpu_capacity import resolve_jobs
    options = build_options(config)
    if not options.get('work') or not options.get('distfiles'):
        raise SDKError('profile-node needs build.work and build.distfiles')
    return profile_build.run_node(options['work'], node, key, dict(options, jobs=resolve_jobs(None)))


def load_config(path):
    return validate_config(read_json(path))


def validate_config(value):
    if value.get('schema') != 'emberbsd.sdk-client.v1':
        raise SDKError('unsupported SDK client configuration')
    for key in ('host_prefix', 'cache_dir', 'state_dir'):
        if not isinstance(value.get(key), str) or not Path(value[key]).is_absolute():
            raise SDKError(f'configuration requires absolute {key}')
    repositories = value.get('repositories')
    if not isinstance(repositories, dict) or not repositories:
        raise SDKError('configuration requires explicit platform repositories')
    targets = 0
    for name, cfg in repositories.items():
        if bool(cfg.get('host')) == bool(cfg.get('target')):
            raise SDKError(f'{name}: select exactly one host or target platform')
        install_paths(cfg)
        if cfg.get('host'):
            host = cfg['host']
            if host['os'] != platform.system() or _arch(host['arch']) != _arch(native_cpu()):
                raise SDKError('native repository differs from this host platform')
        else:
            targets += 1
            if not isinstance(cfg.get('os_version'), str):
                raise SDKError('target installation requires pkg_tools os_version')
    if value.get('source') and targets > 1:
        raise SDKError('one Ports build configuration selects one target; use a configuration per target')
    return value


def main(argv=None, *, default_config=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=default_config is None)
    parser.add_argument('command', choices=('list', 'show', 'plan', 'source-plan', 'profile-plan', 'profile-build',
                                            'profile-node', 'catalog', 'install', 'installed', 'update',
                                            'auto-update', 'cache-clean'))
    parser.add_argument('--profile', type=Path, help='build profile for profile-plan and profile-build')
    parser.add_argument('--explain', action='store_true', help='profile-build: let Ninja explain each rebuild')
    parser.add_argument('--node', help=argparse.SUPPRESS)
    parser.add_argument('--key', help=argparse.SUPPRESS)
    parser.add_argument('--set', action='append', default=[], dest='sets')
    parser.add_argument('--package', action='append', default=[], dest='roots')
    parser.add_argument('--target', help='also acquire the application sysroot for this target')
    parser.add_argument('--installed', action='store_true', help='plan updates for installed packages')
    parser.add_argument('--interval', type=float, help='seconds between auto-update attempts (default: 3600)')
    parser.add_argument('--state-dir', type=Path, help='read-only command state directory; does not change the configuration')
    parser.add_argument('--cache-dir', type=Path, help='read-only command cache directory; does not change the configuration')
    args = parser.parse_args(argv)
    try:
        if args.installed and args.command != 'plan':
            raise SDKError('--installed requires plan')
        updating = args.command in ('update', 'auto-update') or args.installed
        if updating and (args.sets or args.target):
            raise SDKError('package updates accept only installed --package roots; use install to add sets or a sysroot')
        if args.command == 'installed' and (args.sets or args.roots or args.target):
            raise SDKError('installed lists all configured package databases without a selection')
        if args.interval is not None and args.command != 'auto-update':
            raise SDKError('--interval requires auto-update')
        if args.command == 'auto-update':
            from .package_auto_update import watch_updates
            arguments = ['update']
            if args.config is not None:
                arguments = ['--config', str(args.config), *arguments]
            for root in args.roots:
                arguments.extend(('--package', root))
            return watch_updates(lambda: main(arguments, default_config=default_config),
                                 3600 if args.interval is None else args.interval)
        profiled = args.command in ('profile-plan', 'profile-build')
        if (args.profile is not None) != profiled:
            raise SDKError(f'{args.command} does not accept --profile' if not profiled
                           else f'{args.command} requires --profile')
        if (args.explain and args.command != 'profile-build') or \
                ((args.node is not None or args.key is not None) != (args.command == 'profile-node')):
            raise SDKError('--explain belongs to profile-build; --node and --key to profile-node')
        config = load_config(args.config) if args.config else validate_config(default_config)
        if profiled or args.command == 'profile-node':
            if args.sets or args.roots or args.target or args.installed or args.state_dir or args.cache_dir:
                raise SDKError(f'{args.command} takes its selection from --profile')
            if args.command == 'profile-plan':
                return plan_profile(config, args.profile)
            if args.command == 'profile-build':
                return build_profile_command(config, args.config, args.profile, explain=args.explain)
            return run_profile_node(config, args.node, args.key)
        if args.command == 'source-plan':
            if args.target or args.state_dir or args.cache_dir:
                raise SDKError('source-plan uses configured source.state and target; no binary state/cache/sysroot options')
            return plan_source_selection(config, sets=args.sets, roots=args.roots)
        config = _read_only_directories(config, args.state_dir, args.cache_dir, args.command)
        state = Path(config['state_dir'])
        state.mkdir(parents=True, exist_ok=True)
        cache = PackageCache(config['cache_dir'])
        with directory_lock(state / 'client.lock'), ExitStack() as temporary:
            if args.command == 'cache-clean':
                with directory_lock(cache.root / '.lock'):
                    MetadataCache(cache.root / 'metadata').keep(())
                    print(f'Removed {cache.trim(0)} cached bytes; installed packages are unchanged.')
                return 0
            if args.command in ('plan', 'install') and not updating and not args.sets and not args.roots and not args.target:
                print('Nothing selected; no download, install or build requested.')
                return 0
            roles = ('pkg_info',) if args.command == 'installed' else ('openssl', 'pkg_info', 'pkg_admin', 'pkg_add', 'pkg_delete')
            crypto = ReleaseCrypto(load_host_prefix(config['host_prefix'], required_roles=roles))
            trust = Path(config.get('trust_root', Path(__file__).resolve().parents[2] / 'trust/emberbsd-release-root.pem'))
            if args.command == 'installed':
                from .package_state import installed_state
                installer = PackageInstaller(crypto, trust)
                sys.stdout.buffer.write(json_bytes(installed_state(config, installer)))
                return 0
            if 'trust_root' not in config and not trust.is_file():
                trust = temporary.enter_context(resources.as_file(
                    resources.files('emberbsd_sdk').joinpath('emberbsd-release-root.pem')))
            store = temporary.enter_context(HTTPStore(config.get('url', DEFAULT_URL),
                                                      ca_file=config.get('https_ca'), proxy_url=config.get('https_proxy')))
            installer = PackageInstaller(crypto, trust)
            archive_error = None
            # Signed release and index bytes are immutable per digest; only the
            # channel pointer is read from the network when nothing changed.
            metadata = MetadataCache(cache.root / 'metadata')
            for attempt in range(2):
                with release_snapshot(crypto, store, trust, state, prefix=config.get('url_prefix', ''),
                                      cache=metadata) as (release, snapshot):
                    identity = hashlib.sha256((snapshot / 'release.p7m').read_bytes()).hexdigest()
                    if archive_error is not None and archive_error.release_digest == identity:
                        raise archive_error
                    catalogs = load_release_catalogs(release, snapshot, ())
                    if args.command == 'catalog':
                        sys.stdout.buffer.write(json_bytes(catalog_index(
                            release, catalogs, config, installer)))
                        return 0
                    if args.command in ('list', 'show'):
                        sets = available_sets(catalogs, config)
                        if args.command == 'show':
                            if set(args.sets) - sets.keys():
                                raise SDKError('unknown package set')
                            sets = {k: v for k, v in sets.items() if not args.sets or k in args.sets}
                        sys.stdout.buffer.write(json_bytes(sets))
                        return 0
                    if updating:
                        from .package_state import update_plan
                        plan = update_plan(release, catalogs, config, installer, cache, roots=args.roots)
                    else:
                        plan = installation_plan(release, catalogs, config, installer, cache,
                                                 sets=args.sets, roots=args.roots)
                        plan = include_source_dependencies(plan, release, catalogs, config, installer, cache)
                        plan = include_sysroot(plan, release, config, cache, args.target)
                    if args.command == 'plan':
                        sys.stdout.buffer.write(json_bytes(plan))
                        return 0
                    print(f"Install: reuse {sum(p['action'] == 'installed' for p in plan['packages'])}; "
                          f"add {sum(p['action'] != 'installed' and not p['replaces'] for p in plan['packages'])}; "
                          f"replace candidates {sum(bool(p['replaces']) for p in plan['packages'])}; "
                          f"cached {sum(p['action'] == 'cache' for p in plan['packages'])}; "
                          f"{int(bool(plan.get('sysroot')) and plan['sysroot']['action'] != 'installed')} sysroots; "
                          f"download {plan['download_size']} bytes; unpack {plan['unpacked_size']} bytes; "
                          f"build {len(plan['build_roots'])} roots", file=sys.stderr)
                    for package in plan.get('retained', []):
                        print(f"Retain {package['repository']}/{package['pkgname']}: {package['reason']}", file=sys.stderr)
                    try:
                        compatible_reuse = install_selection(
                            plan, config, installer, cache, store,
                            refresh_current=lambda: _read_channel(
                                crypto, store, trust, state, config.get('url_prefix', ''), metadata),
                            accepted_release=identity)
                    except ArchiveUnavailable as exc:
                        if attempt:
                            raise
                        archive_error = exc
                        continue
                    for package in compatible_reuse:
                        print(f"Reuse compatible installed {package['pkgname']} for "
                              f"{package['root']} (catalog candidate {package['catalog_candidate']})",
                              file=sys.stderr)
                    if not updating:
                        selected = state / 'selection.json'
                        old = read_json(selected) if selected.exists() else {'roots': []}
                        write_json(selected, {'roots': sorted(set(old['roots']) | set(plan['roots']))}, exclusive=False)
                    print('Selected components installed; package databases own installed versions.')
                    return 0
    except (SDKError, OSError, ValueError, KeyError, tarfile.TarError, subprocess.TimeoutExpired) as exc:
        print(f'sdk-client: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
