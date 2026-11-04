# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Plan scoped OS object invalidation from the OS source-owner contract."""
import json
import os
from pathlib import Path, PurePosixPath
import re

from .errors import SDKError
from . import paths

CONTRACT = 'share/mk/prepared-source-refresh.json'
RUNTIME_ARCHIVES = {'lib/libc': ('libc.a', 'libc_pic.a'),
                    'lib/libm': ('libm.a', 'libm_pic.a'),
                    'lib/libkern': ('libkern.a', None),
                    'sys/stand/efiboot/bootaa64/lib/kern': ('libkern.a', None)}


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


def plan(os_root, objects, names, archive_members=None, nested_outputs=None, *,
         extra_headers=(), extra_linked=(), allowed_source_roots=(), scan_consumers=True,
         file_outputs=None):
    root, obj = Path(os_root), Path(objects)
    try:
        root_resolved, obj_resolved = root.resolve(), obj.resolve()
        source_roots = []
        for source in allowed_source_roots:
            source = Path(source)
            if not source.is_absolute():
                raise ValueError('nonabsolute prepared source root')
            source_roots.append(source.resolve())
        source_roots = tuple(dict.fromkeys(source_roots))
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
        dirs = set()
        headers = {relative(p) for p in extra_headers}
        linked = {relative(p) for p in extra_linked}
        recursive = set()
        for name in names:
            entry = sources['EMBER_' + name.upper().replace('-', '_') + '_SOURCE']
            required_entry = {'producer_dirs', 'consumer_dirs', 'header_prefixes', 'linked_names'}
            if not required_entry <= set(entry) or set(entry) - required_entry - {'native_producer_dirs', 'recursive_producer_dirs'}:
                raise ValueError('unsupported source entry')
            declared = {relative(p) for p in entry['producer_dirs']}
            roots = entry.get('recursive_producer_dirs', [])
            if not isinstance(roots, list):
                raise ValueError('invalid recursive producers')
            roots = {relative(p) for p in roots}
            if not roots <= declared:
                raise ValueError('recursive producer is not declared')
            recursive.update(roots)
            dirs.update(relative(p) for p in entry['producer_dirs'] + entry['consumer_dirs'])
            headers.update(relative(p) for p in entry['header_prefixes'])
            linked.update(relative(p) for p in entry['linked_names'])
        nested_prune = {}
        for subdir, owner in nested.items():
            if owner not in dirs:
                continue
            source, built = root / owner, obj / owner
            nested_path = obj / subdir
            built_resolved = built.resolve()
            if (not source.resolve().is_relative_to(root_resolved) or
                    not (source / 'Makefile').is_file() or
                    not built_resolved.is_relative_to(obj_resolved) or
                    built.is_symlink() or (built.exists() and not built.is_dir()) or
                    not nested_path.resolve().is_relative_to(built_resolved) or
                    (nested_path.is_symlink() or
                     (nested_path.exists() and not nested_path.is_dir()))):
                raise ValueError('invalid nested object output: ' + subdir)
            nested_prune[nested_path] = subdir
            if nested_outputs is not None and str(nested_path) not in nested_outputs:
                nested_outputs.append(str(nested_path))
        def walk_error(error):
            raise error
        # A source with no installed headers or linked libraries affects only
        # its declared producers and consumers; no global object scan is needed.
        walk = os.walk(obj, followlinks=False, onerror=walk_error) if scan_consumers and (headers or linked) else ()
        for directory, subdirs, files in walk:
            base = Path(directory)
            subdirs[:] = [name for name in subdirs if base / name not in nested_prune]
            for name in subdirs:
                alias = base / name
                if alias.is_symlink():
                    resolved = alias.resolve()
                    if not (resolved.is_relative_to(obj_resolved) or
                            resolved.is_relative_to(root_resolved) or
                            any(resolved.is_relative_to(source) for source in source_roots)):
                        raise ValueError('unverified directory alias ' + str(alias))
            for name in files:
                path = base / name
                rel = path.relative_to(obj).as_posix()
                if path.is_symlink():
                    resolved = path.resolve()
                    in_object = resolved.is_relative_to(obj_resolved)
                    in_os_source = resolved.is_relative_to(root_resolved)
                    in_prepared_source = any(resolved.is_relative_to(source) for source in source_roots)
                    if not (in_object or in_os_source or in_prepared_source):
                        raise ValueError('unverified file alias ' + str(path))
                    if not in_object and resolved.is_file():
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
                        if file_outputs is not None and path.suffix == '.ln':
                            if path.is_symlink() or not path.is_file():
                                raise ValueError('invalid lint output ' + str(path))
                            file_outputs.append(rel)
                        else:
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
                if nested_outputs is not None and str(nested_path) not in nested_outputs:
                    nested_outputs.append(str(nested_path))
                name = owner
            if built.exists():
                if built.is_symlink() or not built.is_dir():
                    raise ValueError('invalid object directory ' + name)
                with os.scandir(built) as entries:
                    if next(entries, None) is not None:
                        result.append(name)
        if missing:
            raise ValueError('missing OS makefiles for ' + ', '.join(missing))
        result = set(result)
        return sorted(name for name in result if not any(
            parent in result and name.startswith(parent + '/') for parent in recursive))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise SDKError('cannot safely plan prepared source refresh: ' + str(exc)) from exc


def remove_lint_outputs(objects, outputs):
    """Remove planned lint outputs under the caller's object-tree lease."""
    root = Path(os.path.abspath(objects))
    if str(root) == '/' or os.path.realpath(root) != str(root):
        raise SDKError('lint object root must not be a symlink')
    paths.real_directory(str(root))
    selected = []
    for name in outputs:
        relative(name)
        path = root / name
        if path.suffix != '.ln' or path.is_symlink() or path.resolve() != path:
            raise SDKError('invalid planned lint output: ' + str(path))
        if path.exists():
            if not path.is_file():
                raise SDKError('invalid planned lint output: ' + str(path))
            selected.append(path)
    for path in selected:
        path.unlink()


def runtime_independent_patterns(os_root):
    # Read the OS owner's existing header-independent object exceptions.
    try:
        contract = json.loads((Path(os_root) / CONTRACT).read_text())
        patterns = contract['header_independent_object_patterns']
        if contract.get('version') != 1 or not isinstance(patterns, list):
            raise ValueError('unsupported runtime refresh contract')
        return [relative(pattern, pattern=True) for pattern in patterns]
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise SDKError('cannot safely read runtime refresh contract: ' + str(exc)) from exc


def runtime_objects(objects, old_source, changed_files, independent_patterns=()):
    """List only existing build objects whose recorded dependencies changed."""
    obj = Path(objects)
    source = Path(old_source)
    if not obj.is_absolute() or not source.is_absolute() or not obj.is_dir() or not source.is_dir():
        raise SDKError('runtime refresh needs absolute existing object and source roots')
    obj_real = obj.resolve()
    paths = set()
    direct = {}
    for name in changed_files:
        relative(name)
        path = source / name
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(source.resolve()):
            raise SDKError('invalid changed runtime source: ' + name)
        if any(char.isspace() for char in str(path)):
            raise SDKError('runtime dependency paths with spaces are unsupported')
        paths.add(os.fsencode(str(path)))
        if path.suffix == '.c':
            if path.stem in direct and direct[path.stem] != os.fsencode(str(path)):
                raise SDKError('ambiguous runtime source stem: ' + path.stem)
            direct[path.stem] = os.fsencode(str(path))
    if not paths:
        raise SDKError('no changed runtime sources selected')
    result = set()
    def walk_error(error):
        raise error
    for directory, subdirs, files in os.walk(obj, followlinks=False, onerror=walk_error):
        base = Path(directory)
        subdirs[:] = [name for name in subdirs if not (base / name).is_symlink()]
        for name in files:
            file = base / name
            if file.suffix in ('.o', '.pico', '.po', '.go', '.ln') and file.stem in direct:
                dep = file.with_suffix('.d')
                if not dep.is_file() and not dep.is_symlink():
                    dep = Path(str(file) + '.d')
                if not dep.is_file() or dep.is_symlink():
                    raise SDKError('missing runtime dependency file: ' + str(file))
                data = dep.read_bytes().replace(bytes((92, 10)), b' ')
                if b':' not in data or direct[file.stem] not in data.split(b':', 1)[1].split():
                    raise SDKError('runtime dependency does not reference selected source: ' + str(file))
            if not name.endswith('.d'):
                continue
            if file.is_symlink():
                raise SDKError('symlink runtime dependency file')
            data = file.read_bytes().replace(bytes((92, 10)), b' ')
            if b':' not in data:
                if any(path in data for path in paths):
                    raise SDKError('invalid runtime dependency file: ' + str(file))
                continue
            dependencies = set(data.split(b':', 1)[1].split())
            if not paths.intersection(dependencies):
                continue
            if not file.resolve().is_relative_to(obj_real):
                raise SDKError('runtime dependency leaves object root')
            result.add(file.relative_to(obj).as_posix())
            dep_base = file.with_suffix('')
            variants = (dep_base,) if dep_base.suffix in ('.o', '.pico', '.po', '.go', '.ln') else tuple(
                file.with_suffix(extension) for extension in ('.o', '.pico', '.po', '.go', '.ln'))
            for output in variants:
                if output.is_symlink():
                    raise SDKError('symlink runtime object')
                if output.is_file():
                    if output.suffix not in ('.o', '.pico'):
                        raise SDKError('unsupported runtime object variant: ' + str(output))
                    owner = output.parent.relative_to(obj).as_posix()
                    if owner not in RUNTIME_ARCHIVES or RUNTIME_ARCHIVES[owner][output.suffix == '.pico'] is None:
                        raise SDKError('unknown runtime object archive owner: ' + str(output))
                    if not output.resolve().is_relative_to(obj_real):
                        raise SDKError('runtime object leaves object root')
                    result.add(output.relative_to(obj).as_posix())
    # A header can affect any existing object in a selected producer.  Refuse
    # unaccounted dependency files there; documented owner exceptions remain.
    affected = {str(Path(name).parent) for name in result if name.endswith('.d')}
    for directory in affected:
        aggregate = obj / directory / '.depend'
        if aggregate.is_symlink():
            raise SDKError('symlink runtime dependency aggregate: ' + str(aggregate))
        if aggregate.is_file() and any(source in aggregate.read_bytes() for source in paths):
            result.add(aggregate.relative_to(obj).as_posix())
        for file in (obj / directory).iterdir():
            if file.suffix not in ('.o', '.pico', '.po', '.go', '.ln'):
                continue
            rel = file.relative_to(obj)
            if any(len(rel.parts) == len(Path(pattern).parts) and rel.match(pattern)
                   for pattern in independent_patterns):
                continue
            dep = file.with_suffix('.d')
            long_dep = Path(str(file) + '.d')
            if file.is_symlink() or not ((dep.is_file() and not dep.is_symlink()) or
                                         (long_dep.is_file() and not long_dep.is_symlink())):
                raise SDKError('missing runtime dependency file: ' + str(file))
    return sorted(result)


def cleanup_runtime_quarantine(objects, names, quarantine):
    # Delete only the verified inventory created by this refresh.
    root, backup = Path(objects), Path(quarantine)
    if not root.is_absolute() or backup.parent != root or backup.is_symlink() or not backup.is_dir():
        raise SDKError('invalid runtime quarantine location')
    expected = set()
    expected_dirs = {'.'}
    for name in names:
        relative(name)
        if name in expected:
            raise SDKError('duplicate runtime refresh object')
        expected.add(name)
        parent = Path(name).parent
        while str(parent) != '.':
            expected_dirs.add(parent.as_posix())
            parent = parent.parent
    found, directories = set(), set()
    def walk_error(error):
        raise error
    for directory, subdirs, files in os.walk(backup, followlinks=False, onerror=walk_error):
        base = Path(directory)
        directories.add(base.relative_to(backup).as_posix())
        if any((base / name).is_symlink() for name in subdirs + files):
            raise SDKError('quarantine inventory changed')
        for name in files:
            path = base / name
            if not path.is_file():
                raise SDKError('quarantine inventory changed')
            found.add(path.relative_to(backup).as_posix())
    if found != expected or directories != expected_dirs:
        raise SDKError('quarantine inventory changed')
    for name in sorted(expected):
        (backup / name).unlink()
    for directory in sorted(expected_dirs - {'.'}, key=lambda d: len(Path(d).parts), reverse=True):
        (backup / directory).rmdir()
    backup.rmdir()


def rollback_failed_runtime(objects, quarantine, evidence, names, aggregates):
    """Preserve failed outputs and restore only the original quarantined files."""
    root, backup, saved = Path(objects), Path(quarantine), Path(evidence)
    if (not root.is_absolute() or not root.is_dir() or backup.parent != root or
            backup.is_symlink() or not backup.is_dir() or not saved.is_absolute() or
            os.path.lexists(saved) or not saved.parent.is_dir() or
            saved.parent.stat().st_dev != root.stat().st_dev):
        raise SDKError('invalid failed runtime rollback paths')
    original = [relative(name) for name in names]
    dependencies = [relative(name) for name in aggregates]
    if len(set(original)) != len(original) or len(set(dependencies)) != len(dependencies) or set(original) & set(dependencies):
        raise SDKError('duplicate failed runtime rollback path')
    expected = set(original)
    expected_dirs = {'.'}
    for name in original:
        parent = Path(name).parent
        while str(parent) != '.':
            expected_dirs.add(parent.as_posix())
            parent = parent.parent
    actual, actual_dirs = set(), set()
    def walk_error(error):
        raise error
    for directory, subdirs, files in os.walk(backup, followlinks=False, onerror=walk_error):
        parent = Path(directory)
        actual_dirs.add(parent.relative_to(backup).as_posix())
        if any((parent / name).is_symlink() for name in subdirs + files):
            raise SDKError('invalid runtime quarantine inventory')
        for name in files:
            path = parent / name
            if not path.is_file():
                raise SDKError('invalid runtime quarantine inventory')
            actual.add(path.relative_to(backup).as_posix())
    if actual != expected or actual_dirs != expected_dirs:
        raise SDKError('runtime quarantine inventory changed')
    selected = []
    for name in original + dependencies:
        path = root / name
        if path.is_symlink() or (os.path.lexists(path) and not path.is_file()) or not path.resolve().is_relative_to(root.resolve()):
            raise SDKError('invalid failed runtime output: ' + name)
        if path.is_file():
            selected.append(name)
    saved.mkdir(mode=0o700)
    moved = []
    try:
        for name in selected:
            destination = saved / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(root / name, destination)
            moved.append((root / name, destination))
        for name in original:
            destination = root / name
            if os.path.lexists(destination):
                raise SDKError('runtime rollback destination appeared: ' + name)
            os.replace(backup / name, destination)
            moved.append((backup / name, destination))
    except BaseException:
        for source, destination in reversed(moved):
            if not os.path.lexists(source) and os.path.lexists(destination):
                source.parent.mkdir(parents=True, exist_ok=True)
                os.replace(destination, source)
        raise
    for directory, subdirs, files in os.walk(backup, topdown=False, followlinks=False, onerror=walk_error):
        if files:
            raise SDKError('runtime quarantine retained unexpected files')
        os.rmdir(directory)
    return selected


def runtime_shared_identity(objects, names):
    root = Path(objects)
    result = {}
    for name in names:
        relative(name)
        path = root / name
        if path.suffix != '.pico':
            continue
        parent = path.parent.relative_to(root).as_posix()
        if parent not in ('lib/libc', 'lib/libm'):
            raise SDKError('unknown runtime shared library owner: ' + parent)
        shared = path.parent / ('libc.so' if parent == 'lib/libc' else 'libm.so')
        resolved = shared.resolve()
        if not resolved.is_relative_to(path.parent.resolve()) or not resolved.is_file():
            raise SDKError('invalid runtime shared library: ' + parent)
        st = resolved.stat()
        result[parent] = (str(resolved), st.st_dev, st.st_ino, st.st_ctime_ns, st.st_mtime_ns, st.st_size)
    return result


def verify_runtime_outputs(objects, names, new_source, changed_files, archive_members, archive_content, before_shared=None):
    # A rebuilt object must have fresh dependency data, and every relevant
    # installed archive member must contain the newly built bytes.
    root, source = Path(objects), Path(new_source)
    if not root.is_absolute() or not source.is_absolute():
        raise SDKError('invalid runtime output verification roots')
    dependencies = {os.fsencode(str(source / relative(name))) for name in changed_files}
    for name in names:
        relative(name)
        path = root / name
        if path.name == '.depend':
            affected_objects = any(Path(item).parent == Path(name).parent and
                                   Path(item).suffix in ('.o', '.pico') for item in names)
            if affected_objects and (path.is_symlink() or not path.is_file() or
                    not any(dep in path.read_bytes() for dep in dependencies)):
                raise SDKError('runtime dependency aggregate was not rebuilt: ' + name)
            continue
        if path.suffix == '.d':
            continue
        if path.suffix not in ('.o', '.pico'):
            raise SDKError('unsupported runtime object variant: ' + name)
        if (path.is_symlink() or not path.is_file() or
                not path.resolve().is_relative_to(root.resolve())):
            raise SDKError('runtime object was not rebuilt: ' + name)
        dep = path.with_suffix('.d')
        if not dep.is_file() and not dep.is_symlink():
            dep = Path(str(path) + '.d')
        if dep.is_symlink() or not dep.is_file():
            raise SDKError('runtime dependency was not rebuilt: ' + name)
        data = dep.read_bytes().replace(bytes((92, 10)), b' ')
        if b':' not in data or not dependencies.intersection(data.split(b':', 1)[1].split()):
            raise SDKError('runtime dependency did not select new source: ' + name)
        parent = path.parent.relative_to(root).as_posix()
        if parent not in RUNTIME_ARCHIVES:
            raise SDKError('unknown runtime object archive owner: ' + parent)
        archive_name = RUNTIME_ARCHIVES[parent][path.suffix == '.pico']
        if archive_name is None:
            raise SDKError('unknown runtime object archive variant: ' + name)
        archive = path.parent / archive_name
        if archive.is_symlink() or not archive.is_file() or path.name not in archive_members(archive):
            raise SDKError('runtime object missing from archive: ' + name)
        if archive_content(archive, path.name) != path.read_bytes():
            raise SDKError('runtime archive member is stale: ' + name)
        if path.suffix == '.pico':
            shared = path.parent / ('libc.so' if parent == 'lib/libc' else 'libm.so')
            resolved = shared.resolve()
            if (not resolved.is_relative_to(path.parent.resolve()) or not resolved.is_file() or
                    resolved.stat().st_mtime_ns < path.stat().st_mtime_ns):
                raise SDKError('runtime shared library is stale: ' + name)
            st = resolved.stat()
            identity = (str(resolved), st.st_dev, st.st_ino, st.st_ctime_ns, st.st_mtime_ns, st.st_size)
            if (before_shared is None or parent not in before_shared or
                    identity[0] != before_shared[parent][0] or identity == before_shared[parent]):
                raise SDKError('runtime shared library was not relinked: ' + name)


def quarantine_runtime_objects(objects, names, quarantine):
    """Move only the selected old objects aside, with rollback on failure."""
    obj = Path(objects)
    backup = Path(quarantine)
    if not obj.is_absolute() or not obj.is_dir() or backup.parent != obj:
        raise SDKError('invalid runtime quarantine location')
    if os.path.lexists(backup):
        raise SDKError('runtime quarantine already exists')
    selected = []
    for name in names:
        relative(name)
        source = obj / name
        if source.is_symlink() or not source.is_file() or not source.resolve().is_relative_to(obj.resolve()):
            raise SDKError('invalid runtime refresh object: ' + name)
        selected.append((source, backup / name))
    if len({source for source, _ in selected}) != len(selected):
        raise SDKError('duplicate runtime refresh object')
    backup.mkdir(mode=0o700)
    moved = []
    try:
        for source, destination in selected:
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, destination)
            moved.append((source, destination))
    except BaseException:
        for source, destination in reversed(moved):
            if not os.path.lexists(source):
                os.replace(destination, source)
        for directory, subdirs, files in os.walk(backup, topdown=False):
            if not files:
                os.rmdir(directory)
        raise
    return [source.relative_to(obj).as_posix() for source, _ in moved]
