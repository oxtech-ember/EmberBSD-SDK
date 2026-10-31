# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Plan scoped OS object invalidation from the OS source-owner contract."""
import json
import os
from pathlib import Path, PurePosixPath
import re

from .errors import SDKError

CONTRACT = 'share/mk/prepared-source-refresh.json'


def relative(value, *, pattern=False):
    if (not isinstance(value, str) or not value or value.startswith('/') or
            any(part in ('', '.', '..') for part in value.rstrip('/').split('/')) or
            not re.fullmatch(r'[A-Za-z0-9_./+*\-]+', value) or
            ('*' in value and not pattern)):
        raise SDKError('invalid prepared source refresh path')
    return value


def plan(os_root, objects, names, archive_members=None):
    root, obj = Path(os_root), Path(objects)
    try:
        contract = json.loads((root / CONTRACT).read_text())
        if set(contract) != {'version', 'sources', 'header_independent_object_patterns', 'header_independent_archive_patterns'} or contract['version'] != 1:
            raise ValueError('unsupported contract')
        sources = contract['sources']
        patterns = [relative(p, pattern=True) for p in contract['header_independent_object_patterns']]
        archive_patterns = [relative(p, pattern=True) for p in contract['header_independent_archive_patterns']]
        dirs, headers, linked = set(), set(), set()
        for name in names:
            entry = sources['EMBER_' + name.upper().replace('-', '_') + '_SOURCE']
            if set(entry) != {'producer_dirs', 'consumer_dirs', 'header_prefixes', 'linked_names'}:
                raise ValueError('unsupported source entry')
            dirs.update(relative(p) for p in entry['producer_dirs'] + entry['consumer_dirs'])
            headers.update(relative(p) for p in entry['header_prefixes'])
            linked.update(relative(p) for p in entry['linked_names'])
        def walk_error(error):
            raise error
        for directory, subdirs, files in os.walk(obj, followlinks=False, onerror=walk_error):
            base = Path(directory)
            for name in subdirs:
                alias = base / name
                if alias.is_symlink():
                    resolved = alias.resolve()
                    if not (resolved.is_relative_to(obj.resolve()) or resolved.is_relative_to(root.resolve())):
                        raise ValueError('unverified directory alias ' + str(alias))
            for name in files:
                path = base / name
                rel = path.relative_to(obj).as_posix()
                if path.is_symlink():
                    resolved = path.resolve()
                    if not (resolved.is_relative_to(obj.resolve()) or resolved.is_relative_to(root.resolve())):
                        raise ValueError('unverified file alias ' + str(path))
                if name.endswith('.a') and not path.is_symlink():
                    relpath = PurePosixPath(rel)
                    independent = any(len(relpath.parts) == len(PurePosixPath(p).parts) and relpath.match(p) for p in archive_patterns)
                    if not independent:
                        if archive_members is None:
                            raise ValueError('archive inspection unavailable')
                        members = archive_members(path)
                        if any('/' in member or not (base / member).is_file() for member in members):
                            dirs.add(base.relative_to(obj).as_posix())
                elif not path.is_symlink() and path.is_file():
                    with path.open('rb') as stream:
                        magic = stream.read(4)
                        if magic == b'\x7fELF':
                            data = magic + stream.read()
                            if any(name.encode() in data for name in linked):
                                dirs.add(base.relative_to(obj).as_posix())
                if name.endswith('.d'):

                    if path.is_symlink():
                        raise ValueError('symlink dependency file')
                    data = path.read_bytes()
                    if b':' not in data or any(('/' + prefix).encode() in data for prefix in headers):
                        dirs.add(base.relative_to(obj).as_posix())
                elif path.suffix in ('.o', '.pico', '.po', '.go', '.ln') and not path.with_suffix('.d').exists() and not Path(str(path) + '.d').exists():
                    relpath = PurePosixPath(rel)
                    independent = any(len(relpath.parts) == len(PurePosixPath(p).parts) and relpath.match(p) for p in patterns)
                    if not independent:
                        dirs.add(base.relative_to(obj).as_posix())
        result = []
        missing = []
        for name in sorted(dirs):
            relative(name)
            source, built = root / name, obj / name
            if not source.resolve().is_relative_to(root.resolve()) or not built.resolve().is_relative_to(obj.resolve()):
                raise ValueError('refresh path escapes its root')
            if not (source / 'Makefile').is_file():
                missing.append(name)
                continue
            if built.exists():
                if built.is_symlink() or not built.is_dir():
                    raise ValueError('invalid object directory ' + name)
                result.append(name)
        if missing:
            raise ValueError('missing OS makefiles for ' + ', '.join(missing))
        return result
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise SDKError('cannot safely plan prepared source refresh: ' + str(exc)) from exc
