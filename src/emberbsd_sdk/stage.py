# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Select a complete package partition in an exclusively owned staging tree."""

import os
from collections.abc import Mapping

from .errors import SDKError
from .inventory import snapshot_tree
from .paths import canonical_existing, path_string, real_directory, verify_stable

STAGE_PARTITION_API = 1


def _names(values):
    if isinstance(values, (str, bytes)):
        raise SDKError('stage members require a collection, not a scalar string')
    result = set()
    for value in values:
        if (not isinstance(value, str) or not value
                or any(ord(c) < 32 or ord(c) == 127 for c in value)
                or any(c in ('', '.', '..') for c in value.split('/'))):
            raise SDKError(f'invalid relative stage member: {value!r}')
        try:
            value.encode('utf-8', 'strict')
        except UnicodeError as exc:
            raise SDKError(f'invalid UTF-8 stage member: {value!r}') from exc
        if value in result:
            raise SDKError(f'duplicate stage member: {value}')
        result.add(value)
    return result


def partition_stage(stage, full, partitions, selected, *, dependencies=None, keep_empty=()):
    """Prune an audited complete stage to one declared partition.

    Callers own the stage exclusively throughout this operation. ``full`` and
    each partition enumerate files and links, not directories. Their disjoint
    union must equal the actual complete stage. A partition may link into
    another partition only through declared dependencies (including transitive
    dependencies). Links must resolve to listed regular files. ``keep_empty``
    declares existing directories that the selected package must retain.

    All validation, link ownership checks and a second stable inventory finish
    before the first deletion. An I/O error during pruning is a failed build;
    callers must not package that partial stage. This is not a transaction or
    a sandbox for a stage that another process can mutate concurrently.
    """
    stage = path_string(stage)
    if stage == os.path.sep or canonical_existing(stage) != stage:
        raise SDKError('stage requires a normalized non-root path with real ancestors')
    root_info = real_directory(stage)
    full = _names(full)
    if not isinstance(partitions, Mapping):
        raise SDKError('stage partitions require a mapping')
    if not full or not partitions or selected not in partitions:
        raise SDKError('nonempty full inventory and a known partition are required')
    owners = {}
    parts = {}
    for part, members in partitions.items():
        if not isinstance(part, str) or not part:
            raise SDKError('partition names must be nonempty strings')
        parts[part] = _names(members)
        for name in parts[part]:
            if name in owners:
                raise SDKError(f'overlapping stage partitions: {name}')
            owners[name] = part
    if set(owners) != full:
        raise SDKError('stage partitions do not exhaust the complete inventory')

    dependencies = {} if dependencies is None else dependencies
    if not isinstance(dependencies, Mapping):
        raise SDKError('partition dependencies require a mapping')
    if set(dependencies) - set(parts):
        raise SDKError('unknown dependency source partition')
    edges = {}
    for part in parts:
        values = dependencies.get(part, ())
        if isinstance(values, (str, bytes)):
            raise SDKError('partition dependency names require a collection')
        edges[part] = set(values)
        if edges[part] - set(parts):
            raise SDKError(f'unknown dependency destination for partition: {part}')
    allowed = {}
    for part in parts:
        reached, todo = set(), [part]
        while todo:
            current = todo.pop()
            if current not in reached:
                reached.add(current)
                todo.extend(edges[current] - reached)
        allowed[part] = reached

    before = snapshot_tree(stage)
    actual = {name for name, row in before.items() if row['kind'] != 'directory'}
    if actual != full:
        raise SDKError('actual complete stage differs from the declared inventory')
    kept_dirs = _names(keep_empty)
    for name in kept_dirs:
        if name not in before or before[name]['kind'] != 'directory':
            raise SDKError(f'retained empty directory is absent or not a directory: {name}')
    # Keep ancestors of explicit empty directories as well.
    for name in list(kept_dirs):
        while '/' in name:
            name = name.rsplit('/', 1)[0]
            kept_dirs.add(name)

    for name, row in before.items():
        if row['kind'] != 'link':
            continue
        todo, resolved, hops = name.split('/'), [], 0
        while todo:
            component = todo.pop(0)
            if component in ('', '.'):
                continue
            if component == '..':
                if not resolved:
                    raise SDKError(f'escaping stage link: {name}')
                resolved.pop()
                continue
            current = '/'.join(resolved + [component])
            target = before.get(current)
            if target is None:
                raise SDKError(f'absent stage link component: {name} -> {current}')
            if target['kind'] == 'link':
                hops += 1
                if hops > 64:
                    raise SDKError(f'cyclic or excessive stage link: {name}')
                if owners[current] not in allowed[owners[name]]:
                    raise SDKError(f'link uses an undeclared partition: {name} -> {current}')
                todo[:0] = target['target'].split('/')
                continue
            if todo and target['kind'] != 'directory':
                raise SDKError(f'non-directory stage link component: {name} -> {current}')
            resolved.append(component)
        terminal = '/'.join(resolved)
        if terminal not in actual or before[terminal]['kind'] != 'file':
            raise SDKError(f'stage link must resolve to a listed regular file: {name}')
        if owners[terminal] not in allowed[owners[name]]:
            raise SDKError(f'link resolves to an undeclared partition: {name} -> {terminal}')

    if snapshot_tree(stage) != before:
        raise SDKError('stage changed during partition validation')
    verify_stable(stage, root_info)
    removed = sorted(actual - parts[selected])
    for name in removed:
        os.unlink(os.path.join(stage, name))
    for name in sorted(before, key=lambda p: (p.count('/'), p), reverse=True):
        if before[name]['kind'] == 'directory' and name not in kept_dirs:
            path = os.path.join(stage, name)
            if not os.listdir(path):
                os.rmdir(path)
    return {'partition': selected, 'complete_files': len(actual),
            'kept_files': sorted(parts[selected]), 'removed_files': removed}
