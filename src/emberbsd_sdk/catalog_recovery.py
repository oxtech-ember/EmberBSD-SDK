# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Restore available selections from authenticated package withdrawal events."""
from collections import defaultdict, deque
from pathlib import Path
import time

from .binary_release import _snapshot, _withdrawn_artifacts, runtime_issues, write_catalogs
from .errors import SDKError
from .sysroot_update import _target_file


def _consumers(entries, blocked):
    reverse = defaultdict(list)
    for entry in entries.values():
        for child in entry['release_artifact']['depends']:
            reverse[child].append(entry['artifact'])
    unavailable = set(blocked)
    queue = deque(unavailable)
    while queue:
        for parent in reverse.get(queue.popleft(), ()):
            if parent not in unavailable:
                unavailable.add(parent)
                queue.append(parent)
    return unavailable


def _denials(indexes):
    return {(identity, digest) for identity, values in _withdrawn_artifacts(indexes).items()
            for digest in values}


def _event_selections(current, events):
    release, repositories, indexes = current
    inventory = {item['id']: item for item in release['artifacts']}
    denials = _denials(indexes)
    candidates = defaultdict(dict)
    memberships = defaultdict(lambda: defaultdict(list))
    for before, after in events:
        prior, following_denials = _denials(before[2]), _denials(after[2])
        new = following_denials - prior
        if not prior <= following_denials or not new or not following_denials <= denials:
            raise SDKError('withdrawal snapshots must introduce exact records retained by current')
        retained = {item['id']: item for item in after[0]['artifacts']}
        for name, old in before[1].items():
            following = after[1].get(name)
            live = repositories.get(name)
            if not following or not live:
                continue
            if any((repo['host'], repo['target']) != (old['host'], old['target'])
                   for repo in (following, live)):
                raise SDKError('withdrawal repository platform differs: ' + name)
            entries = old['entries']
            denied = {item['artifact'] for item in entries.values()
                      if (item['artifact'], item['release_artifact']['sha256']) in new}
            lost = _consumers(entries, denied)
            hidden = {}
            for root, item in entries.items():
                ident, record = item['artifact'], item['release_artifact']
                if ident not in lost or root in following['entries'] or retained.get(ident) != record:
                    continue
                hidden[root] = ident
                if (root not in live['entries'] and inventory.get(ident) == record and
                        (ident, record['sha256']) not in denials):
                    candidates[name][root] = item
            for group, members in old['catalog']['sets'].items():
                remaining = following['catalog']['sets'].get(group)
                if remaining is None or group not in live['catalog']['sets']:
                    continue
                memberships[name][group].extend((root, hidden[root]) for root in members
                                                if root in hidden and root not in remaining)
    return candidates, memberships


def _available(crypto, live, candidates, sysroot):
    if not candidates:
        return {}
    all_entries = {**live['entries'], **candidates}
    ids = {item['artifact'] for item in all_entries.values()}
    missing = {dep for item in candidates.values() for dep in item['release_artifact']['depends']
               if dep not in ids}
    # A current or historical root can have moved to another category.
    # Keep the current selection if two roots would install the same base.
    bases = defaultdict(set)
    for root, item in all_entries.items():
        bases[item['name'].rsplit('-', 1)[0]].add(root)
    missing.update(item['artifact'] for root, item in candidates.items()
                   if len(bases[item['name'].rsplit('-', 1)[0]]) > 1)
    if sysroot is not None:
        # Existing direct and transitive providers also enter a fresh sysroot.
        paths = {path for item in all_entries.values() for path in item['record'].get('REQUIRES', ())
                 if path.startswith('/') and not path.startswith('/usr/pkg/')}
        absent = {path for path in paths if not _target_file(sysroot, path)}
        missing.update(item['artifact'] for item in all_entries.values()
                       if absent.intersection(item['record'].get('REQUIRES', ())))
    blocked = _consumers(all_entries, missing)
    checks = {item['artifact']: dict(name=item['name'], record=item['record'],
                  node=dict(dependencies=item['release_artifact']['depends']))
              for item in all_entries.values() if item['artifact'] not in blocked}
    restored = {item['artifact'] for item in candidates.values()} - blocked
    if restored:
        issues = runtime_issues(crypto, checks, 'host' if live['host'] else 'target', restored, restored)
        blocked.update(_consumers(all_entries, issues))
    return {root: item for root, item in candidates.items() if item['artifact'] not in blocked}


def recover_catalogs(crypto, current, events, sysroots):
    """Keep current selections and add only recoverable members lost to these withdrawals."""
    repositories = current[1]
    for name, root in sysroots.items():
        if name not in repositories or not repositories[name]['target'] or not root.is_dir():
            raise SDKError('select an installed sysroot for an existing target repository: ' + name)
    candidates, memberships = _event_selections(current, events)
    changed = set()
    for name in candidates.keys() | memberships.keys():
        live = repositories[name]
        if live['target'] and name not in sysroots:
            continue
        recovered = _available(crypto, live, candidates.get(name, {}), sysroots.get(name))
        live['entries'].update(recovered)
        if recovered:
            changed.add(name)
            live['catalog']['packages'].extend(dict(root=root, artifact=item['artifact'], pkgname=item['name'])
                                               for root, item in recovered.items())
        for group, members in memberships.get(name, {}).items():
            selected = live['catalog']['sets'][group]
            for root, ident in members:
                if root not in selected and live['entries'].get(root, {}).get('artifact') == ident:
                    selected.append(root)
                    changed.add(name)
    return changed


def recover(crypto, current, pairs, sysroots, output, identity, trust, release_id):
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise SDKError('release output already exists')
    cache = {}
    def snapshot(path):
        key = Path(path).resolve()
        if key not in cache:
            cache[key] = _snapshot(crypto, key, trust)
        return cache[key]
    live = snapshot(current)
    events = [(snapshot(before), snapshot(after)) for before, after in pairs]
    names = recover_catalogs(crypto, live, events, sysroots)
    if not names:
        return live[0]
    spec = dict(release_id=release_id, created_at=int(time.time()), sources=live[0]['sources'])
    prepared = [(dict(id=name, host=live[1][name]['host'], target=live[1][name]['target']), {}, {})
                for name in sorted(names)]
    return write_catalogs(crypto, spec, output, identity, trust, prepared, live, current=current)
