# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Binary-first component installation, with source builds owned by Ports."""

import argparse
from contextlib import ExitStack, redirect_stdout
import hashlib
from importlib import resources
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile

from . import sysroot_package, package_automatic
from .binary_catalog import load_release_catalogs, select_packages
from .binary_release import _arch, _matches
from .errors import ChannelExpired, SDKError, TransportError
from .host_prefix import load_host_prefix
from .jsonio import json_bytes, read_json, write_json
from .package_cache import PackageCache, check_space, directory_lock
from .package_install import PackageInstaller, install_paths, target_sysroot
from .package_updates import reuse_compatible_dependencies
from .package_selection import _package_sets, _parse_root, _read_roots
from .release_delivery import artifact_key, release_snapshot, _key
from .release_signing import ReleaseCrypto
from .release_s3 import HTTPStore, HTTPStatusError

DEFAULT_URL = 'https://emberbsd-pkg.oxteam.me'


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
            records[artifact['id']] = dict(package, repository=repository)
            platforms.setdefault(package['root'], []).append(artifact['id'])
    missing = [root for root in requested if root not in platforms]
    for root in requested:
        role = 'host' if root.endswith('@native') else 'target'
        if not any(cfg.get(role) for cfg in config['repositories'].values()):
            raise SDKError(f'no {role} destination configured for {root}')
    selection = select_packages(release, catalogs, roots=[r for r in requested if r in platforms])
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
        packages.append(dict(record, artifact=item, action=action, replaces=replaces))
    return dict(release_id=release['release_id'], roots=requested, packages=packages,
                build_roots=missing, download_size=sum(downloads.values()), unpacked_size=unpacked)


def _source_config(config, roots):
    if not roots:
        return
    if any(c.get('target') and not target_sysroot(c) for c in config['repositories'].values()):
        raise SDKError('live target package installation requires published binaries; build with Ports on the developer host')
    source = config.get('source')
    if not source:
        raise SDKError('binary package absent; configure Ports source (ports_root, pkgsrc, makeconf, state) '
                       'to build: ' + ', '.join(roots))
    for key in ('ports_root', 'pkgsrc', 'makeconf', 'state'):
        if not isinstance(source.get(key), str) or not Path(source[key]).is_absolute():
            raise SDKError(f'Ports source requires an absolute {key}')


def run_source(config, roots, command, *, binaries=(), controller_mode=None):
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
                    status = package_selection.main(arguments, controller_stdout=sys.stderr,
                                                    controller_mode=controller_mode)
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
    result = installation_plan(release, catalogs, config, installer, cache,
                               roots=[r for r in plan['roots'] if r not in build_roots] + candidates)
    result['roots'] = plan['roots']
    result['build_roots'] = sorted(build_roots)
    result['source_packages'] = names
    return result


def apply_plan(plan, config, installer, cache, store):
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
    archives = {}
    for package in pending:
        item = package['artifact']
        path = cache.fetch(store, _key(config.get('url_prefix', ''), artifact_key(item, '')), item)
        installer.check(path, config['repositories'][package['repository']])
        archives[item['id']] = path
    sysroot_archive = None
    if new_sysroot:
        item = sysroot['artifact']
        check_space(Path(sysroot['destination']).parent, item['unpacked_size'])
        sysroot_archive = cache.fetch(store, _key(config.get('url_prefix', ''), artifact_key(item, '')), item)
        sysroot_package.check(sysroot_archive, sysroot)
    with ExitStack() as locks:
        # Same whole-build lock as cross-bulk; release before asking it to build.
        if config.get('source') and (new_sysroot or selected_repositories):
            locks.enter_context(directory_lock(Path(config['source']['state']) / 'build.lock'))
        # A target pkgdb lock can create its parent inside the sysroot. Publish
        # the verified base first, then add target packages through pkg_tools.
        if new_sysroot:
            sysroot_package.install(sysroot_archive, sysroot)
        for repository in sorted(selected_repositories):
            _, database = install_paths(config['repositories'][repository])
            locks.enter_context(directory_lock(database.parent / (database.name + '.sdk-lock')))
        # The signed graph pins artifact identities. Before applying replacements,
        # retain a newer installed dependency when selected consumers' authenticated
        # pkg_tools constraints accept it. This stays inside the pkgdb lock and never
        # suppresses an explicitly requested package root.
        if isinstance(installer, PackageInstaller):
            compatible_reuse = []
            for repository in sorted(selected_repositories):
                cfg = config['repositories'][repository]
                package_automatic.recover(installer.crypto, cfg, installer.installed(cfg, recover=False))
                repository_packages = [p for p in plan['packages'] if p['repository'] == repository]
                compatible_reuse.extend(reuse_compatible_dependencies(installer.crypto, cfg,
                    repository_packages, archives, installer.installed(cfg, recover=False), plan['roots']))
            plan['compatible_reuse'] = compatible_reuse
            pending = [p for p in plan['packages'] if p['action'] != 'installed']
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
        for package in pending:
            cfg = config['repositories'][package['repository']]
            # Recheck paths after earlier packages have populated the prefix.
            from .package_install import inspect_install_paths
            if isinstance(installer, PackageInstaller):
                inspect_install_paths(archives[package['artifact']['id']], cfg)
                automatic = automatic_states[package['repository']][package['pkgname']]
                package_automatic.prepare(cfg, package['pkgname'], automatic)
                installer.install(archives[package['artifact']['id']], cfg, checked=True,
                                  configure=False, automatic=automatic == 'yes')
                package_automatic.finish(installer.crypto, cfg, package['pkgname'])
            else:
                installer.install(archives[package['artifact']['id']], cfg, checked=True, configure=False)
        # Configure only after every selected payload has been installed and
        # relocated. Pending hooks are retried even when this selection reused
        # every exact installed package.
        for repository in sorted(selected_repositories):
            installer.configure(config['repositories'][repository])
    cache.trim(config.get('cache_limit', 512 * 1024 * 1024))


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
_RECEIPTS = ('done', 'failed', 'blocked', 'skipped')


def _package_path(root):
    return root[:-len('@native')] if root.endswith('@native') else root


def _ready_wave(order, deps, pending):
    ready = [root for root in order if root in pending and
             not any(dep in pending for dep in deps.get(root, ()))]
    selected = set(ready)
    by_path = {}
    for root in pending:
        by_path.setdefault(_package_path(root), root)
    changed = True
    while changed:
        changed = False
        for producer, consumer in _SHARED_STAGE_PAIRS:
            producer_root = by_path.get(producer)
            consumer_root = by_path.get(consumer)
            if (producer_root not in selected or consumer_root is None or consumer_root in selected):
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


def _replacement_is_upgrade(installer, graph_name, installed):
    """True only when pkg_tools shows every installed sibling is older than the catalog package."""
    crypto = getattr(installer, 'crypto', None)
    if crypto is None or '-' not in graph_name:
        return False
    base, pattern = _version_pattern(graph_name)
    siblings = [name for name in installed if name.rsplit('-', 1)[0] == base and name != graph_name]
    return bool(siblings) and all(not _matches(crypto, pattern, name) for name in siblings)


def _adopt_published(config, installer, cache, store, release, catalogs, names, order, pending, binaries):
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
            apply_plan(merged, config, installer, cache, store)
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


def _receipt_text(path):
    if not path.exists() or path.is_dir():
        return ''
    return path.read_text()


def _link_receipt(real_state, private, name):
    source = real_state / name
    if name == 'logs':
        source.mkdir(exist_ok=True)
    elif not source.exists():
        source.write_text('')
    (private / name).symlink_to(source)


def _new_receipt_roots(before, current):
    previous = set(before.splitlines())
    return [line.split()[0] for line in current.splitlines() if line.strip() and line not in previous]


def _persist_detached_receipts(real_state, private):
    """Ports renames a symlink when it rotates failed/blocked; keep those new lines."""
    for name in ('failed', 'blocked', 'skipped', 'done'):
        path = private / name
        if path.is_symlink() or not path.is_file():
            continue
        text = path.read_text()
        if not text:
            continue
        with (real_state / name).open('a') as stream:
            if text and not text.endswith('\n'):
                text += '\n'
            stream.write(text)


def _build_saved_wave(config, ready, binaries):
    """Build one ready wave without rewriting the shared Ports order.

    cross-bulk `build` schedules `$state/order` and reads target-fed tools from
    `$state/edges`. The shared order stays in place. This wave's schedule is a
    private file, and the real edges and receipt files are linked into that
    directory. The real `build.lock` is held so another client or `packages.py`
    cannot build the same state; the controller locks only the private directory,
    because the Ports lock is not reentrant.
    """
    real_state = Path(config['source']['state'])
    with directory_lock(real_state / 'build.lock'):
        private = Path(tempfile.mkdtemp(prefix='ember-wave-'))
        try:
            before = {name: _receipt_text(real_state / name) for name in _RECEIPTS}
            for name in ('edges', 'packages', 'progress', 'logs', *_RECEIPTS):
                _link_receipt(real_state, private, name)
            (private / 'order').write_text(''.join(root + '\n' for root in ready))
            wave = dict(config, source=dict(config['source'], state=str(private)))
            try:
                run_source(wave, ready, 'build', binaries=binaries, controller_mode='build')
            finally:
                _persist_detached_receipts(real_state, private)
            receipts = {name: _new_receipt_roots(before[name], _receipt_text(private / name))
                        for name in _RECEIPTS}
        finally:
            shutil.rmtree(private)
    return receipts


def _accept_built_wave(config, installer, names, ready, pending, receipts):
    """Honor this wave's done, skipped and failed receipts.

    A native package Ports reused through `pkg_info -e BASE>=version` is not
    written to `state/done`. That reuse is accepted only from the host package
    database. A skipped package still makes `build` exit 0; its dependents must
    not run in a later wave. A target package is accepted from the target
    database or a done receipt, never because the native database has the same name.
    """
    skipped = set(receipts['skipped'])
    failed = set(receipts['failed']) | set(receipts['blocked'])
    done = set(receipts['done'])
    blocked = [root for root in ready if root in skipped or root in failed]
    if blocked:
        raise SDKError('Ports did not build ' + ', '.join(blocked))
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


def _install_source_waves(plan, config, installer, cache, store, refresh_current, accepted_channel, binaries):
    """Re-read current between waves. Return reuse rows, or None to keep one selected build."""
    source = config.get('source')
    names = plan.get('source_packages')
    if refresh_current is None or not names or not source:
        return None
    graph = _read_source_graph(Path(source['state']))
    if graph is None:
        return None
    order, deps = graph
    if any(root not in order for root in plan['build_roots']):
        return None
    handled = {package['root'] for package in plan['packages']}
    pending = {root for root in order if root not in handled}
    reused, seen = [], accepted_channel
    while pending:
        snapshot = _refresh_channel(refresh_current)
        if snapshot is not None:
            release, catalogs, identity = snapshot
            if seen != identity:
                if seen is not None:
                    print('Current changed; checking for published packages.', file=sys.stderr)
                seen = identity
            binaries, extra = _adopt_published(config, installer, cache, store, release, catalogs,
                                               names, order, pending, binaries)
            reused.extend(extra)
        if not pending:
            break
        ready = _ready_wave(order, deps, pending)
        if not ready:
            raise SDKError('source graph has no ready package')
        receipts = _build_saved_wave(config, ready, binaries)
        _accept_built_wave(config, installer, names, ready, pending, receipts)
    return reused


def _read_channel(crypto, store, trust, state, prefix):
    """Load one authenticated current into memory. The snapshot directory does not outlive this call."""
    with release_snapshot(crypto, store, trust, state, prefix=prefix) as (release, snapshot):
        catalogs = load_release_catalogs(release, snapshot, ())
        identity = hashlib.sha256((snapshot / 'current.p7m').read_bytes()).hexdigest()
        return release, catalogs, identity


def install_selection(plan, config, installer, cache, store, *, refresh_current=None, accepted_channel=None):
    with directory_lock(cache.root / '.lock'):
        apply_plan(plan, config, installer, cache, store)
    # Compilation may be long; it must not monopolize another target's downloads.
    binaries = [(package['root'], package['pkgname']) for package in plan['packages'] if package.get('pkgname')]
    reused = list(plan.get('compatible_reuse') or [])
    extra = _install_source_waves(plan, config, installer, cache, store, refresh_current,
                                  accepted_channel, binaries) if plan['build_roots'] else None
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
            if host['os'] != platform.system() or _arch(host['arch']) != _arch(platform.machine()):
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
    parser.add_argument('command', choices=('list', 'show', 'plan', 'install', 'installed', 'update', 'auto-update', 'cache-clean'))
    parser.add_argument('--set', action='append', default=[], dest='sets')
    parser.add_argument('--package', action='append', default=[], dest='roots')
    parser.add_argument('--target', help='also acquire the application sysroot for this target')
    parser.add_argument('--installed', action='store_true', help='plan updates for installed packages')
    parser.add_argument('--interval', type=float, help='seconds between auto-update attempts (default: 3600)')
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
        config = load_config(args.config) if args.config else validate_config(default_config)
        state = Path(config['state_dir'])
        state.mkdir(parents=True, exist_ok=True)
        cache = PackageCache(config['cache_dir'])
        with directory_lock(state / 'client.lock'), ExitStack() as temporary:
            if args.command == 'cache-clean':
                with directory_lock(cache.root / '.lock'):
                    print(f'Removed {cache.trim(0)} cached bytes; installed packages are unchanged.')
                return 0
            if args.command in ('plan', 'install') and not updating and not args.sets and not args.roots and not args.target:
                print('Nothing selected; no download, install or build requested.')
                return 0
            roles = ('pkg_info',) if args.command == 'installed' else ('openssl', 'pkg_info', 'pkg_admin', 'pkg_add')
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
            previous = None
            for attempt in range(2):
                with release_snapshot(crypto, store, trust, state,
                                      prefix=config.get('url_prefix', '')) as (release, snapshot):
                    identity = hashlib.sha256((snapshot / 'release.p7m').read_bytes()).hexdigest()
                    channel = hashlib.sha256((snapshot / 'current.p7m').read_bytes()).hexdigest()
                    if previous == identity:
                        raise SDKError('published package is missing from unchanged current; retry after publisher repair')
                    catalogs = load_release_catalogs(release, snapshot, ())
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
                                crypto, store, trust, state, config.get('url_prefix', '')),
                            accepted_channel=channel)
                    except HTTPStatusError as exc:
                        if exc.status != 404 or attempt:
                            raise
                        previous = identity
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
