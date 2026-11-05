# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Read installed components and select their published package updates."""

from pathlib import Path
import platform

from .errors import SDKError
from .package_selection import _parse_root
from .release_signing import read_bytes


def installed_state(config, installer):
    repositories = {}
    for name, cfg in config['repositories'].items():
        release_file = Path(cfg['sysroot'] if cfg.get('target') else '/') / 'etc/release'
        header = read_bytes(release_file, 64 * 1024).decode('utf-8').splitlines() if release_file.exists() else []
        product = header[0] if header and header[0].startswith('EmberBSD ') else None
        repositories[name] = dict(platform=cfg.get('host') or cfg['target'], os_release=product,
            packages=installer.inventory(cfg))
        if cfg.get('host'):
            repositories[name]['host_kernel'] = platform.release()
    return dict(repositories=repositories)


def update_plan(release, catalogs, config, installer, cache, *, roots=()):
    """Select each pkgdb independently; never expand package sets or saved roots."""
    from .sdk_client import installation_plan
    requested = {_parse_root(root, 'update') for root in roots}
    inventory = {name: installer.inventory(cfg) for name, cfg in config['repositories'].items()}
    present = {package['root'] for packages in inventory.values() for package in packages}
    if requested - present:
        raise SDKError('update root is not installed: ' + ', '.join(sorted(requested - present)))
    result = dict(release_id=release['release_id'], roots=[], packages=[], build_roots=[],
                  retained=[], download_size=0, unpacked_size=0)
    for repository, packages in inventory.items():
        cfg = dict(config, repositories={repository: config['repositories'][repository]})
        available = {p['root']: p for c in catalogs if c['repository'] == repository for p in c['packages']}
        installed_bases = {}
        installed_roots = {}
        for package in packages:
            installed_bases.setdefault(package['root'], set()).add(package['pkgname'].rsplit('-', 1)[0])
            installed_roots[package['pkgname']] = package['root']
        selected = set()
        for package in packages:
            root = package['root']
            if requested and root not in requested:
                continue
            candidate = available.get(root)
            if candidate is None:
                result['retained'].append(dict(package, repository=repository,
                    reason='absent from current' if root else 'missing installed PKGPATH'))
                continue
            selected.add(root)
        plan = installation_plan(release, catalogs, cfg, installer, cache, roots=sorted(selected),
            installed={repository: {p['pkgname'] for p in packages}})
        for package in plan['packages']:
            for old_name in package['replaces']:
                if installed_roots[old_name] != package['root']:
                    raise SDKError(f"{old_name}: installed PKGPATH does not identify {package['root']}; "
                                   'an explicit migration is required')
            bases = installed_bases.get(package['root'], set())
            if bases and bases != {package['pkgname'].rsplit('-', 1)[0]}:
                raise SDKError(f"{package['root']}: package rename requires an explicit migration")
        result['roots'].extend(plan['roots'])
        result['packages'].extend(plan['packages'])
        result['unpacked_size'] += plan['unpacked_size']
    result['roots'] = sorted(set(result['roots']))
    downloads = {p['artifact']['sha256']: p['artifact']['size'] for p in result['packages']
                 if p['action'] == 'download'}
    result['download_size'] = sum(downloads.values())
    return result
