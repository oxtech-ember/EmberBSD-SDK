# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Replace an installed target base after validating its complete staged prefix."""

from contextlib import ExitStack
import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile

from . import application_toolchain, inventory, jsonio, prefix_lock, publication, sysroot_package, sysroot_transfer
from .errors import SDKError
from .release_delivery import _key, artifact_key
from .release_s3 import HTTPStatusError
from .paths import relative_inside
from .package_cache import check_space, directory_lock
from .package_install import _relative, install_paths
from .release_signing import read_bytes


def plan_update(release, catalogs, config, installer, cache, target, roots):
    from .package_state import update_plan
    selected = sysroot_package.selection(release, config, cache, target, update=True)
    repos = {name: cfg for name, cfg in config['repositories'].items()
             if cfg.get('target') == selected['artifact']['target']}
    packages = {name: installer.inventory(cfg) for name, cfg in repos.items()}
    plan = update_plan(release, catalogs, dict(config, repositories=repos), installer, cache,
                       roots=roots, inventory=packages)
    if selected['action'] == 'replace':
        selected['packages'] = packages
        plan['unpacked_size'] += selected['artifact']['unpacked_size']
        if not cache.path(selected['artifact']).exists():
            plan['download_size'] += selected['artifact']['size']
    plan['sysroot'] = selected
    return plan


def _payload(rows):
    return sysroot_transfer._host_link_modes({
        name: {key: value for key, value in row.items() if key not in ('elf', 'archive')}
        for name, row in rows.items()})


def preserve_local_files(old, stage, original, updated, exclude):
    """Apply local changes to the new base. Refuse conflicting edits before publication."""
    current = _payload(inventory.snapshot_installed_tree(str(old), exclude=exclude))
    before = _payload(original['inventory'])
    after = _payload(updated['inventory'])
    check_space(stage, sum(row.get('size', 0) for name, row in current.items()
                           if row != before.get(name) and row != after.get(name)))
    changed = []
    for name in sorted(current.keys() | before.keys()):
        local, base, new = current.get(name), before.get(name), after.get(name)
        if local == base or local == new:
            continue
        if new != base:
            raise SDKError(f'{name}: local file and new sysroot have conflicting changes')
        changed.append(name)
    # Remove children before their parents. A nonempty directory can contain
    # files introduced by the new base and must survive a conflicting removal.
    for name in reversed(changed):
        local = current.get(name)
        dest = stage / name
        if name in after and (not local or local['kind'] != after[name]['kind']):
            if after[name]['kind'] == 'directory':
                dest.rmdir()
            else:
                dest.unlink()
    hardlinks = {}
    for name in changed:
        local = current.get(name)
        if not local:
            continue
        dest, source = stage / name, old / name
        if any(parent.is_symlink() for parent in dest.parents if parent != stage and stage in parent.parents):
            raise SDKError(f'{name}: local entry has a conflicting ancestor in the new sysroot')
        dest.parent.mkdir(parents=True, exist_ok=True)
        if local['kind'] == 'directory':
            dest.mkdir(exist_ok=True)
        elif local['kind'] == 'link':
            if dest.is_symlink():
                dest.unlink()
            dest.symlink_to(os.readlink(source))
            shutil.copystat(source, dest, follow_symlinks=False)
        else:
            info = source.stat()
            identity = (info.st_dev, info.st_ino)
            if dest.exists():
                dest.unlink()
            if info.st_nlink > 1 and identity in hardlinks:
                os.link(hardlinks[identity], dest)
            else:
                shutil.copy2(source, dest)
                if info.st_nlink > 1:
                    hardlinks[identity] = dest
    for name in reversed(changed):
        if current.get(name, {}).get('kind') == 'directory':
            shutil.copystat(old / name, stage / name)
    return current


def _target_file(root, name):
    """Resolve absolute and relative target symlinks within the target root."""
    path = PurePosixPath(name)
    if not path.is_absolute() or '..' in path.parts:
        raise SDKError(f'invalid required target path: {name}')
    todo, resolved, links = list(path.parts[1:]), [], 0
    while todo:
        part = todo.pop(0)
        if part == '..':
            if resolved:
                resolved.pop()
            continue
        item = root.joinpath(*resolved, part)
        try:
            mode = item.lstat().st_mode
        except (FileNotFoundError, NotADirectoryError):
            return False
        if stat.S_ISLNK(mode):
            links += 1
            if links > 40:
                return False
            link = PurePosixPath(os.readlink(item))
            if link.is_absolute():
                resolved, todo = [], list(link.parts[1:]) + todo
            else:
                todo = list(link.parts) + todo
        else:
            if todo and not stat.S_ISDIR(mode):
                return False
            resolved.append(part)
    return root.joinpath(*resolved).is_file()


def verify_packages(installer, config):
    root = Path(config['sysroot'])
    _, db = install_paths(config)
    installed = installer.inventory(config)
    for row in installed:
        name = row['pkgname']
        info = db / name / '+BUILD_INFO'
        for line in info.read_text().splitlines():
            if line.startswith('REQUIRES=') and not _target_file(root, line[9:]):
                raise SDKError(f'{name}: new sysroot lacks required file {line[9:]}')
    return installed


def _package_files(config, packages):
    """Read pkg_tools' recorded payload names and original file checksums."""
    prefix, db = install_paths(config)
    root = Path(config['sysroot'])
    for package in packages:
        name = package['pkgname']
        if Path(name).name != name or name in ('.', '..'):
            raise SDKError('invalid installed package name')
        directory = db / name
        if directory.resolve() != directory:
            raise SDKError(f'{name}: unexpected package database link')
        cwd, ignored, member = prefix.relative_to(root), False, None
        lines = read_bytes(directory / '+CONTENTS', 8 * 1024 * 1024).decode().splitlines()
        for line in lines:
            if line.startswith('@cwd '):
                path = PurePosixPath(line[5:])
                if not path.is_absolute() or '..' in path.parts:
                    raise SDKError(f'{name}: invalid package directory')
                cwd = Path(*path.parts[1:])
                member = None
            elif line == '@ignore':
                ignored, member = True, None
            elif line and not line.startswith('@'):
                member = None if ignored else (cwd / _relative(line)).as_posix()
                ignored = False
                if member:
                    yield member, name, None
            elif member and line.startswith(('@comment MD5:', '@comment Symlink:')):
                yield member, name, line[9:]


def check_local_package_changes(old, captured, final, original, repositories, packages, updated, databases):
    """Allow pkg_add to change its original payload, while protecting local edits."""
    owned = {}
    for repo, cfg in repositories.items():
        owned.update((path, (name, recorded)) for path, name, recorded in _package_files(cfg, packages[repo]))
    dbdirs = [db.relative_to(old) for db in databases]
    base = _payload(original['inventory'])
    for path in captured.keys() | owned.keys() | base.keys():
        local = captured.get(path)
        if local == final.get(path) or any(Path(path).is_relative_to(db) for db in dbdirs):
            continue
        owner, recorded = owned.get(path, (None, None))
        if owner is None and local == base.get(path):
            continue  # Upstream changed an unmodified base file.
        if owner in updated and recorded and local:
            if (local['kind'] == 'link' and recorded == 'Symlink:' + local['target']):
                continue
            if local['kind'] == 'file' and recorded.startswith('MD5:'):
                with (old / path).open('rb') as contents:
                    if hashlib.file_digest(contents, 'md5').hexdigest() == recorded[4:]:
                        continue
        raise SDKError(f'{path}: package update conflicts with an installed file or a local change')


def _check_bound_compiler(config, selected, runtime):
    """Check the existing command selection against the new base before switching."""
    binding = sysroot_package.compiler_binding(config, selected)
    if binding is None:
        return
    destination, links, triplet = binding
    if not destination.exists():
        if any(os.path.lexists(link) for link in links.values()):
            raise SDKError('installed target compiler commands have no binding')
        return
    # Commands point to the stable installation path even during staging.
    with tempfile.TemporaryDirectory(dir=Path(selected['destination']).parent) as work:
        prepared = Path(work) / 'commands'
        sysroot_package.prepare_compiler_binding(config, selected, prepared, triplet, runtime)
        if jsonio.read_json(destination / 'commands.json') != jsonio.read_json(prepared / 'commands.json'):
            raise SDKError('installed compiler bindings differ from the selected target runtime')


def apply(plan, config, installer, cache, store, release_digest):
    from .sdk_client import ArchiveUnavailable, apply_plan
    selected = plan['sysroot']
    old = Path(selected['destination'])
    repositories = {key: cfg for key, cfg in config['repositories'].items()
                    if cfg.get('target') == selected['artifact']['target']}
    if any(Path(cfg['sysroot']).resolve() != old for cfg in repositories.values()):
        raise SDKError('target repository uses another sysroot')
    external = [config[key] for key in ('host_prefix', 'state_dir', 'cache_dir') if key in config]
    if config.get('source'):
        state = config['source'].get('state')
        if not isinstance(state, str) or not Path(state).is_absolute():
            raise SDKError('Ports source requires an absolute state directory')
        external.append(state)
    if any(relative_inside(str(Path(path).resolve()), str(old)) is not None for path in external):
        raise SDKError('the host prefix, cache, SDK state and Ports state must be outside the target sysroot')
    default = dict(target=selected['artifact']['target'], sysroot=str(old),
                   prefix='/usr/pkg', pkgdb='/usr/pkg/pkgdb')
    databases = {install_paths(cfg)[1].resolve() for cfg in repositories.values() or [default]}
    publication.require_directory_exchange()
    item = selected['artifact']
    check_space(old.parent, plan['unpacked_size'])
    try:
        archive = cache.fetch(store, _key(config.get('url_prefix', ''), artifact_key(item, '')), item)
    except HTTPStatusError as exc:
        if exc.status not in (403, 404):
            raise
        raise ArchiveUnavailable(release_digest, exc) from exc
    with ExitStack() as locks:
        locks.enter_context(directory_lock(old.parent / f'.{old.name}.update.lock'))
        if config.get('source'):
            locks.enter_context(directory_lock(Path(config['source']['state']) / 'build.lock'))
        for db in sorted(databases):
            readers, _ = prefix_lock._files(db)
            if readers.is_symlink():
                raise SDKError('package reader directory must not be a symlink')
            locks.enter_context(prefix_lock.writing(db, allow_own_readers=False))
        previous = jsonio.read_json(old / sysroot_package.RECEIPT)
        if previous != selected['previous']:
            raise SDKError('installed sysroot changed after planning; repeat the update')
        for repo, cfg in repositories.items():
            if installer.inventory(cfg) != selected['packages'][repo]:
                raise SDKError('installed target packages changed after planning; repeat the update')
        if not repositories and any(db.is_dir() and any(db.glob('*/+CONTENTS')) for db in databases):
            raise SDKError('configure the installed target package repository before updating its base')
        original = sysroot_transfer.read_import_manifest(old, selected['target'])
        stage, manifest = locks.enter_context(sysroot_package.staged_install(archive, selected))
        excluded = {sysroot_package.RECEIPT, application_toolchain.IMPORT_RECORD}
        for db in databases:
            readers, writer = prefix_lock._files(db)
            excluded.update((readers.relative_to(old).as_posix(), writer.relative_to(old).as_posix()))
        original_mode = stat.S_IMODE(old.stat().st_mode)
        if original_mode != original['root_mode']:
            if manifest['root_mode'] not in (original_mode, original['root_mode']):
                raise SDKError('local root mode conflicts with the new sysroot')
            stage.chmod(original_mode)
        captured = preserve_local_files(old, stage, original, manifest, excluded)
        staged_repos = {name: dict(cfg, sysroot=str(stage)) for name, cfg in repositories.items()}
        private = dict(config, repositories=staged_repos)
        private.pop('source', None)
        packages = dict(plan)
        packages.pop('sysroot')
        apply_plan(packages, private, installer, cache, store, source_locked=True, release_digest=release_digest)
        for repo, cfg in staged_repos.items():
            final = verify_packages(installer, cfg)
            expected = {row['pkgname'] for row in selected['packages'][repo]}
            for package in packages['packages']:
                if package['repository'] == repo and package['action'] != 'installed':
                    expected.difference_update(package['replaces'])
                    expected.add(package['pkgname'])
            if {row['pkgname'] for row in final} != expected:
                raise SDKError('updated sysroot package inventory differs from the selection')
        changed_packages = {old_name for package in packages['packages'] if package['action'] != 'installed'
                            for old_name in package['replaces']}
        staged_files = _payload(inventory.snapshot_installed_tree(str(stage), exclude=excluded))
        check_local_package_changes(old, captured, staged_files, original, repositories,
                                    selected['packages'], changed_packages, databases)
        runtime = application_toolchain.load_clang_runtime(str(stage), target=selected['target'])
        _check_bound_compiler(config, selected, runtime)
        if (stat.S_IMODE(old.stat().st_mode) != original_mode or
                _payload(inventory.snapshot_installed_tree(str(old), exclude=excluded)) != captured):
            raise SDKError('installed file changed during sysroot update; repeat the update')
        # Waiting old and new clients must acquire the same writer inode.
        for db in databases:
            readers, writer = prefix_lock._files(db)
            target_db = stage / db.relative_to(old)
            if target_db.resolve() != target_db:
                raise SDKError('new sysroot changes the path to the package database')
            new_readers = stage / readers.relative_to(old)
            if new_readers.is_symlink():
                raise SDKError('new sysroot supplies an invalid package reader directory')
            new_readers.mkdir(parents=True, exist_ok=True)
            live = stage / writer.relative_to(old)
            live.unlink(missing_ok=True)
            os.link(writer, live)
        publication.exchange_directories(str(stage), str(old))
        plan['compatible_reuse'] = packages.get('compatible_reuse', [])
