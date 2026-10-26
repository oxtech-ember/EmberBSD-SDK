# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Complete native set lists derived from the verified LLVM runtime rows."""

import os
import posixpath
from pathlib import Path
import stat

from . import llvm_runtime_install, paths
from .errors import SDKError


def runtime_set_plan(record):
    """Map installed paths to base/comp without a second runtime inventory."""
    if not isinstance(record, dict) or not isinstance(record.get('inventory'), dict):
        raise SDKError('runtime sets require the verified runtime record')
    manifest = record.get('manifest')
    aliases = llvm_runtime_install.base_alias_plan(manifest)
    prefix = manifest['prefix']
    rows = {}
    for relative, row in record['inventory'].items():
        if (not isinstance(relative, str) or relative.startswith('/') or
                any(part in ('', '.', '..') for part in relative.split('/')) or
                not isinstance(row, dict) or row.get('kind') not in ('file', 'link', 'directory')):
            raise SDKError('invalid runtime set inventory path or row')
        rows[prefix + '/' + relative] = row
    if ({name: row.get('sha256') for name, row in rows.items() if row['kind'] == 'file'} != manifest['files'] or
            {name: row.get('target') for name, row in rows.items() if row['kind'] == 'link'} != manifest['symlinks']):
        raise SDKError('runtime set inventory differs from the verified manifest')
    for name in rows:
        if any(char.isspace() or char in '\\#' or ord(char) < 32 or ord(char) == 127 for char in name):
            raise SDKError('native set lists cannot represent this runtime path: ' + name)
    selected = {}

    def classify(name, trail=()):
        if name in selected:
            return selected[name]
        if name in trail or name not in rows:
            raise SDKError('runtime set link has a cyclic or missing target')
        row = rows[name]
        if row['kind'] == 'link':
            target = posixpath.normpath(posixpath.join(posixpath.dirname(name), row['target']))
            value = classify(target, (*trail, name))
        elif row.get('elf', {}).get('type') == 3 or name.startswith(prefix + '/share/licenses/'):
            value = 'base'
        else:
            value = 'comp'
        selected[name] = value
        return value

    for name, row in rows.items():
        if row['kind'] != 'directory':
            classify(name)
    for name, target in aliases.items():
        selected[name] = classify(posixpath.normpath(posixpath.join(posixpath.dirname(name), target)))
    # Include observed empty directories. Shared parents belong to base; a
    # header-only subtree remains in comp. Existing OS directories may also
    # appear in the source lists, which native makeflist already deduplicates.
    for name, row in rows.items():
        if row['kind'] == 'directory':
            selected.setdefault(name, 'comp')
    for name, kind in list(selected.items()):
        parent = posixpath.dirname(name)
        while parent:
            if kind == 'base' or parent not in selected:
                selected[parent] = kind
            parent = posixpath.dirname(parent)
    return dict(sorted(selected.items()))


def prepare_sets_overlay(source_lists, destination, record):
    """Create complete lists beside the producer output, preserving OS lists.

    ``source_lists`` is the OS distrib/sets root. Only the base/comp mi files
    gain generated rows; all other source files are referenced unchanged.
    Call this after importing the runtime and enable it only for final checks
    and packaging, never for the preceding clean distribution operation.
    """
    plan = runtime_set_plan(record)
    source = Path(paths.path_string(source_lists))
    destination = Path(paths.path_string(destination))
    paths.real_directory(str(source / 'lists'))
    paths.real_directory(str(destination.parent))
    if (paths.canonical_existing(str(source)) != str(source) or
            paths.canonical_existing(str(destination.parent)) != str(destination.parent)):
        raise SDKError('runtime sets require real source and destination ancestors')
    if os.path.commonpath((str(source), str(destination))) == str(source):
        raise SDKError('runtime set output must not modify the source lists')
    if paths.entry(str(destination)) is not None:
        raise SDKError('runtime set overlay already exists')
    files, directories = [], []
    for root, subdirs, names in os.walk(source / 'lists', followlinks=False):
        for name in subdirs:
            path = Path(root) / name
            if not stat.S_ISDIR(path.lstat().st_mode):
                raise SDKError('runtime set source has a linked directory')
            directories.append(path.relative_to(source))
        for name in names:
            path = Path(root) / name
            if not stat.S_ISREG(path.lstat().st_mode):
                raise SDKError('runtime set source requires regular list files')
            files.append(path.relative_to(source))
    for selected in ('base', 'comp'):
        if Path('lists', selected, 'mi') not in files:
            raise SDKError('runtime set source is missing a required mi list')
    destination.mkdir()
    (destination / 'lists').mkdir()
    for relative in sorted(directories, key=lambda p: (len(p.parts), str(p))):
        (destination / relative).mkdir()
    for relative in sorted(files):
        output = destination / relative
        selected = relative.parts[1]
        if relative == Path('lists', selected, 'mi') and selected in ('base', 'comp'):
            text = (source / relative).read_text()
            with output.open('x') as handle:
                handle.write(text)
                if text and not text.endswith('\n'):
                    handle.write('\n')
                handle.write('# Common LLVM payload and base aliases, derived from the verified runtime.\n')
                for name, kind in plan.items():
                    if kind == selected:
                        handle.write('./' + name + '\t' + selected + '-sys-root\n')
        else:
            output.symlink_to(source / relative)
    return str(destination)
