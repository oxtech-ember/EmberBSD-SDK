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


def native_sources(os_root):
    """Return source names and native producers declared by the OS owner."""
    try:
        path = Path(os_root) / CONTRACT
        if not path.exists():
            return {}
        contract = json.loads(path.read_text())
        if contract.get('version') != 1 or not isinstance(contract.get('sources'), dict):
            raise ValueError('unsupported contract')
        result = {}
        for selector, entry in contract['sources'].items():
            native = entry.get('native_producer_dirs', [])
            if not isinstance(native, list):
                raise ValueError('invalid native producers')
            if native:
                match = re.fullmatch(r'EMBER_([A-Z0-9_]+)_SOURCE', selector)
                if not match:
                    raise ValueError('invalid source selector')
                result[match[1].lower().replace('_', '-')] = [relative(p) for p in native]
        return result
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise SDKError('cannot safely read native producer contract: ' + str(exc)) from exc


def plan_native(os_root, objects, names):
    root, obj = Path(os_root).resolve(), Path(objects).resolve()
    entries = native_sources(root)
    result = set()
    for name in names:
        if name not in entries:
            raise SDKError('missing native producer contract for ' + name)
        for directory in entries[name]:
            source, built = root / directory, obj / directory
            if (not source.resolve().is_relative_to(root) or
                    not built.resolve().is_relative_to(obj) or
                    not (source / 'Makefile').is_file() or
                    (built.exists() and (built.is_symlink() or not built.is_dir()))):
                raise SDKError('invalid native producer directory: ' + directory)
            result.add(directory)
    return sorted(result)


def plan(os_root, objects, names, archive_members=None, nested_outputs=None):
    root, obj = Path(os_root), Path(objects)
    try:
        contract = json.loads((root / CONTRACT).read_text())
        required = {'version', 'sources', 'header_independent_object_patterns', 'header_independent_archive_patterns'}
        if set(contract) not in (required, required | {'nested_object_owners'}) or contract['version'] != 1:
            raise ValueError('unsupported contract')
        sources = contract['sources']
        nested = contract.get('nested_object_owners', {})
        if not isinstance(nested, dict):
            raise ValueError('invalid nested object owners')
        for subdir, owner in nested.items():
            relative(subdir)
            relative(owner)
            if len(PurePosixPath(owner).parts) < 2 or not subdir.startswith(owner + '/'):
                raise ValueError('invalid nested object owner')
        patterns = [relative(p, pattern=True) for p in contract['header_independent_object_patterns']]
        archive_patterns = [relative(p, pattern=True) for p in contract['header_independent_archive_patterns']]
        dirs, headers, linked = set(), set(), set()
        for name in names:
            entry = sources['EMBER_' + name.upper().replace('-', '_') + '_SOURCE']
            if set(entry) not in ({'producer_dirs', 'consumer_dirs', 'header_prefixes', 'linked_names'},
                                   {'producer_dirs', 'consumer_dirs', 'header_prefixes', 'linked_names', 'native_producer_dirs'}):
                raise ValueError('unsupported source entry')
            dirs.update(relative(p) for p in entry['producer_dirs'] + entry['consumer_dirs'])
            headers.update(relative(p) for p in entry['header_prefixes'])
            linked.update(relative(p) for p in entry['linked_names'])
        def walk_error(error):
            raise error
        # A source with no installed headers or linked libraries affects only
        # its declared producers and consumers; no global object scan is needed.
        walk = os.walk(obj, followlinks=False, onerror=walk_error) if headers or linked else ()
        for directory, subdirs, files in walk:
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
                    if resolved.is_relative_to(root.resolve()) and resolved.is_file():
                        with resolved.open('rb') as stream:
                            magic = stream.read(8)
                        if magic.startswith(b'\x7fELF') or magic in (b'!<arch>\n', b'!<thin>\n'):
                            raise ValueError('binary alias outside object tree ' + str(path))
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
                owners = [(subdir, owner) for subdir, owner in nested.items()
                          if name == subdir or name.startswith(subdir + '/')]
                if len(owners) != 1:
                    missing.append(name)
                    continue
                subdir, owner = owners[0]
                source, built = root / owner, obj / owner
                nested_path = obj / subdir
                if (not source.resolve().is_relative_to(root.resolve()) or
                        not built.resolve().is_relative_to(obj.resolve()) or
                        not nested_path.resolve().is_relative_to(built.resolve()) or
                        not (source / 'Makefile').is_file()):
                    raise ValueError('invalid nested object owner')
                if nested_outputs is not None:
                    nested_outputs.append(str(nested_path))
                name = owner
            if built.exists():
                if built.is_symlink() or not built.is_dir():
                    raise ValueError('invalid object directory ' + name)
                result.append(name)
        if missing:
            raise ValueError('missing OS makefiles for ' + ', '.join(missing))
        return sorted(set(result))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise SDKError('cannot safely plan prepared source refresh: ' + str(exc)) from exc
