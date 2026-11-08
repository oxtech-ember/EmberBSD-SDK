# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Shared safe archive and patch operations for Ports source preparation."""

import calendar
import datetime
import hashlib
import os
import posixpath
from pathlib import Path
import re
import secrets
import shutil
import stat
import tarfile
import time
import unicodedata
import zipfile

from . import process
from .errors import SDKError
from .paths import open_regular, verify_stable

MAX_ARCHIVE_MEMBERS = 100000
MAX_EXTRACTED_BYTES = 256 * 1024 * 1024
SOURCE_PREPARATION_API = 2
# extract_archive accepts skip_directories (compatible API 2 extension).
ARCHIVE_SKIP_DIRECTORIES_API = 1
ARCHIVE_FLAT_LAYOUT_API = 1
ARCHIVE_INTERNAL_SYMLINKS_API = 1
ARCHIVE_OPTIONAL_COMPONENTS_API = 1
ZIP_ARCHIVE_API = 1


def archive_hashes(data):
    """Return the archive digests used by the source preparation receipts."""
    return dict(sha256=hashlib.sha256(data).hexdigest(),
                sha512=hashlib.sha512(data).hexdigest(),
                blake2s=hashlib.blake2s(data).hexdigest(), size=len(data))


class _ZipMember:
    """Adapt ZIP member metadata to the common archive validation loop."""
    def __init__(self, archive, info, label):
        self.archive = archive
        self.info = info
        self.label = label
        self.name = info.orig_filename
        self.size = info.file_size
        self.mode = info.external_attr >> 16 if info.create_system == 3 else 0
        kind = stat.S_IFMT(self.mode)
        if info.is_dir() and kind in (0, stat.S_IFDIR):
            self.kind = 'directory'
        elif not info.is_dir() and kind in (0, stat.S_IFREG):
            self.kind = 'file'
        elif not info.is_dir() and kind == stat.S_IFLNK:
            self.kind = 'link'
        else:
            self.kind = 'special'

    @property
    def mtime(self):
        try:
            return calendar.timegm(datetime.datetime(*self.info.date_time).timetuple())
        except (ValueError, OverflowError) as exc:
            raise SDKError(f'invalid {self.label} archive timestamp') from exc

    @property
    def linkname(self):
        if self.size > 4096:
            raise SDKError(f'unsafe {self.label} archive symlink')
        try:
            return self.archive.read(self.info).decode('utf-8', 'strict')
        except (OSError, UnicodeError, RuntimeError, zipfile.BadZipFile) as exc:
            raise SDKError(f'invalid {self.label} archive symlink') from exc

    def isdir(self):
        return self.kind == 'directory'

    def isreg(self):
        return self.kind == 'file'

    def issym(self):
        return self.kind == 'link'


class _ZipArchive:
    def __init__(self, archive, label):
        self.archive = zipfile.ZipFile(archive, 'r')
        self.label = label

    def __enter__(self):
        self.archive.__enter__()
        return self

    def __exit__(self, *args):
        return self.archive.__exit__(*args)

    def __iter__(self):
        for info in self.archive.infolist():
            yield _ZipMember(self.archive, info, self.label)

    def extractfile(self, member):
        try:
            return self.archive.open(member.info, 'r')
        except (OSError, RuntimeError, NotImplementedError, zipfile.BadZipFile) as exc:
            raise SDKError(f'{self.label} archive member cannot be opened') from exc


def extract_archive(archive, destination, root_name, label, *, components=None,
                    max_members=MAX_ARCHIVE_MEMBERS, max_bytes=MAX_EXTRACTED_BYTES,
                    skip_directories=(), layout='rooted', internal_symlinks=False,
                    require_components=True):
    """Extract selected source components with explicit resource bounds.

    By default links and special files are unsupported. With internal_symlinks,
    relative symlinks must resolve to an existing member inside the source root.
    layout='flat' places a flat archive under root_name.
    skip_directories names exact path components below the root, such as VCS
    metadata directories, whose members are omitted before any other check.
    Repeated regular files must have the same path, data, extracted mode and timestamp.
    Each occurrence counts toward the extraction limits.
    With require_components=False, selected paths may be absent from the archive.
    This supports preimages of files that a patch will create.
    """
    def safe_name(name):
        return (isinstance(name, str) and name and not name.startswith('/') and
                '\\' not in name and all(p not in ('', '.', '..') for p in name.split('/')) and
                not any(ord(c) < 32 or ord(c) == 127 for c in name) and
                unicodedata.normalize('NFC', name) == name)
    if layout not in ('rooted', 'flat'):
        raise SDKError(f'invalid {label} archive layout')
    if type(internal_symlinks) is not bool:
        raise SDKError(f'invalid {label} symlink selection')
    if type(require_components) is not bool:
        raise SDKError(f'invalid {label} component requirement')
    if not safe_name(root_name) or '/' in root_name:
        raise SDKError(f'invalid {label} archive root')
    if (type(max_members) is not int or max_members < 1 or
            type(max_bytes) is not int or max_bytes < 1):
        raise SDKError(f'invalid {label} extraction limits')
    if components is not None:
        if isinstance(components, str):
            raise SDKError(f'invalid {label} source components')
        components = tuple(components)
        if not components or not all(safe_name(name) for name in components):
            raise SDKError(f'invalid {label} source components')
    if isinstance(skip_directories, str):
        raise SDKError(f'invalid {label} skipped directories')
    skip_directories = frozenset(skip_directories)
    if not all(safe_name(name) and '/' not in name for name in skip_directories):
        raise SDKError(f'invalid {label} skipped directories')
    destination = Path(destination)
    if not destination.is_dir() or destination.is_symlink():
        raise SDKError(f'{label} extraction destination must be a real directory')
    if internal_symlinks and os.path.lexists(destination / root_name):
        raise SDKError(f'{label} archive root must not already exist')
    seen, folded, matched = {}, {}, set()
    links = []
    extracted_size = 0

    def make_parents(path):
        current = destination
        for part in path.relative_to(destination).parts:
            current = current / part
            if current.is_symlink() or (current.exists() and not current.is_dir()):
                raise SDKError(f'{label} extraction traverses a non-directory')
            current.mkdir(exist_ok=True)

    try:
        packed = (_ZipArchive(archive, label) if zipfile.is_zipfile(archive)
                  else tarfile.open(archive, 'r:*'))
    except (OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
        raise SDKError(f'{label} archive cannot be opened: {exc}') from exc
    with packed:
        for number, member in enumerate(packed, 1):
            if number > max_members:
                raise SDKError(f'{label} archive has too many members')
            name = member.name.rstrip('/')
            parts = name.split('/')
            if not safe_name(name) or (layout == 'rooted' and parts[0] != root_name):
                raise SDKError(f'unsafe {label} archive path')
            if layout == 'flat':
                parts = [root_name, *parts]
                name = '/'.join(parts)
            relative = '/'.join(parts[1:])
            if skip_directories.intersection(parts[1:]):
                continue
            if components is not None and relative:
                chosen = [component for component in components
                          if relative == component or relative.startswith(component + '/')]
                matched.update(chosen)
                if not chosen and not any(component.startswith(relative + '/') for component in components):
                    continue
            previous = seen.get(name)
            if previous is not None:
                if (previous.name != member.name or not previous.isreg() or not member.isreg() or
                        previous.size != member.size or
                        bool(previous.mode & 0o111) != bool(member.mode & 0o111) or
                        previous.mtime != member.mtime):
                    raise SDKError(f'duplicate {label} archive member')
            seen[name] = member
            for length in range(1, len(parts) + 1):
                parent = '/'.join(parts[:length])
                if folded.get(parent.casefold(), parent) != parent:
                    raise SDKError(f'duplicate {label} archive member')
                folded[parent.casefold()] = parent
            if member.issym() and internal_symlinks:
                target = member.linkname
                if (not isinstance(target, str) or not target or target.startswith('/') or
                        '\\' in target or any(ord(c) < 32 or ord(c) == 127 for c in target) or
                        unicodedata.normalize('NFC', target) != target):
                    raise SDKError(f'unsafe {label} archive symlink')
                resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
                if resolved != root_name and not resolved.startswith(root_name + '/'):
                    raise SDKError(f'escaping {label} archive symlink')
                links.append((destination.joinpath(*parts), target))
                continue
            if not (member.isdir() or member.isreg()):
                raise SDKError(f'{label} archive contains a link or special file')
            path = destination.joinpath(*parts)
            make_parents(path if member.isdir() else path.parent)
            if member.isdir():
                path.chmod(0o755)
            else:
                if member.size < 0 or extracted_size + member.size > max_bytes:
                    raise SDKError(f'{label} archive exceeds the extraction size limit')
                extracted_size += member.size
                source = packed.extractfile(member)
                if source is None:
                    raise SDKError(f'{label} archive member has no data')
                try:
                    with source:
                        if previous is not None:
                            output, before = open_regular(path)
                            with output:
                                while chunk := source.read(64 * 1024):
                                    if output.read(len(chunk)) != chunk:
                                        raise SDKError(f'duplicate {label} archive member')
                                if output.read(1):
                                    raise SDKError(f'duplicate {label} archive member')
                            verify_stable(path, before)
                        else:
                            with path.open('xb') as output:
                                shutil.copyfileobj(source, output)
                except (OSError, RuntimeError, NotImplementedError, zipfile.BadZipFile, EOFError) as exc:
                    if not isinstance(packed, _ZipArchive):
                        raise
                    raise SDKError(f'{label} archive member cannot be extracted') from exc
                if previous is not None:
                    continue
                path.chmod(0o755 if member.mode & 0o111 else 0o644)
                try:
                    os.utime(path, (member.mtime, member.mtime))
                except (OSError, OverflowError, ValueError) as exc:
                    raise SDKError(f'invalid {label} archive timestamp') from exc
    if components is not None:
        if require_components and matched != set(components):
            raise SDKError(f'{label} archive is missing selected source components')
        if not require_components:
            make_parents(destination / root_name)
    if links:
        make_parents(destination / root_name)
        root = (destination / root_name).resolve(strict=True)
    for path, target in links:
        make_parents(path.parent)
        if os.path.lexists(path):
            raise SDKError(f'{label} archive symlink collides with a member')
        path.symlink_to(target)
    for path, _ in links:
        try:
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(root):
                raise SDKError(f'escaping {label} archive symlink')
        except (OSError, RuntimeError) as exc:
            raise SDKError(f'invalid {label} archive symlink') from exc


def apply_patch(patch_tool, patch_path, patch_bytes, cwd, label, *,
                patch_args, backup_directory=None, allow_dev_null=False):
    """Apply one verified patch with caller-owned options and safe paths."""
    headers = re.findall(rb'^(?:---|\+\+\+) ([^\t\n ]+)', patch_bytes, re.M)
    if not headers:
        raise SDKError(f'{label} patch has no unified diff headers')
    for header in headers:
        if allow_dev_null and header == b'/dev/null':
            continue
        parts = header.split(b'/')
        if (header.startswith(b'/') or
                any(part in (b'', b'.', b'..') for part in parts) or
                any(byte < 32 or byte == 127 for byte in header)):
            raise SDKError(f'unsafe {label} patch path')
        try:
            header.decode('ascii', 'strict')
        except UnicodeError as exc:
            raise SDKError(f'unsafe {label} patch path') from exc
    args = [patch_tool, *patch_args]
    if backup_directory is not None:
        # NetBSD nbpatch needs -B for new-file patches; --posix can prevent
        # those files from being created. Keep backups outside the source root.
        # -B only names backups: GNU patch makes none without -b, so pass -b
        # for every tool to keep the original bytes in backup_directory.
        backup_directory = Path(backup_directory)
        backup_directory.mkdir()
        for header in headers:
            if allow_dev_null and header == b'/dev/null':
                continue
            parts = header.decode('ascii').split('/')[:-1]
            # The caller owns patch's strip option. BSD patch uses the stripped
            # destination for backups, so allow every safe suffix of the header.
            for index in range(len(parts)):
                parent = backup_directory.joinpath(*parts[index:])
                parent.mkdir(parents=True, exist_ok=True)
        args.extend(('-b', '-B', str(backup_directory) + os.sep))
    args.extend(('-i', str(patch_path)))
    process.capture(args, cwd=str(cwd),
                    env=process.sanitized_environment())

# Package owners compute the identity of their selected recipe and inputs.
# These helpers bind that identity to a measured prepared tree; they do not
# decide which inputs constitute a package's complete source contract.
PREPARED_TREE_API = 1
PREPARED_TREE_REUSE_API = 1
PREPARED_TREE_CACHE_API = 1
PREPARED_TREE_CACHE_MAX_JSON = 64 * 1024 * 1024
PREPARED_TREE_RECEIPT = 'emberbsd-prepared-source.json'


def _prepared_tree_digest(root):
    from . import inventory, jsonio, paths
    root = paths.canonical_existing(root)
    paths.real_directory(root)
    rows = inventory.snapshot_tree(root, exclude=(PREPARED_TREE_RECEIPT,))
    return hashlib.sha256(jsonio.json_bytes(rows)).hexdigest()


def write_prepared_tree_receipt(root, *, package, identity, previous_root=None):
    """Record a prepared payload; caller owns both trees exclusively during reuse."""
    from . import inventory, jsonio, paths
    root = paths.canonical_existing(root)
    if not isinstance(package, str) or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9+_.-]*/[A-Za-z0-9][A-Za-z0-9+_.-]*', package) is None:
        raise SDKError('invalid prepared source package')
    if not isinstance(identity, dict) or not identity:
        raise SDKError('invalid prepared source identity')
    if previous_root is not None and os.path.lexists(os.path.join(root, PREPARED_TREE_RECEIPT)):
        raise SDKError('prepared source receipt already exists')
    if previous_root is None:
        digest = _prepared_tree_digest(root)
    else:
        previous_root = paths.canonical_existing(previous_root)
        paths.reject_overlap(root, previous_root)
        old_record = read_prepared_tree_receipt(previous_root, package=package)
        _, old_rows, old_stats = _validate_prepared_tree(
            previous_root, package=package, identity=old_record['identity'])
        new_stats = {}
        def measure(path, relative, info):
            new_stats[relative] = paths.stable_identity(info)
            return inventory.sha256_file(path)
        new_rows = inventory.snapshot_tree(root, exclude=(PREPARED_TREE_RECEIPT,),
                                           digest_provider=measure)
        digest = hashlib.sha256(jsonio.json_bytes(new_rows)).hexdigest()
        fresh_ns = time.time_ns()
        planned = []
        for relative, row in new_rows.items():
            if row['kind'] != 'file':
                continue
            path = os.path.join(root, relative)
            info = os.lstat(path)
            if paths.stable_identity(info) != new_stats[relative]:
                raise SDKError('prepared source changed before reuse: ' + path)
            if info.st_nlink != 1:
                raise SDKError('hardlinked prepared source cannot be retimed: ' + path)
            target_ns = fresh_ns
            old_checked = None
            if old_rows.get(relative) == row:
                old_path = os.path.join(previous_root, relative)
                old_info = os.lstat(old_path)
                if paths.stable_identity(old_info) != tuple(old_stats[relative]):
                    raise SDKError('previous prepared source changed before reuse: ' + old_path)
                target_ns = old_info.st_mtime_ns
                old_checked = (old_path, old_info)
            planned.append((path, info, target_ns, old_checked))
        # Validate the complete write set before changing the first timestamp.
        for path, info, _, old_checked in planned:
            paths.verify_stable(path, info)
            if old_checked is not None:
                paths.verify_stable(*old_checked)
            if os.lstat(path).st_nlink != 1:
                raise SDKError('hardlinked prepared source cannot be retimed: ' + path)
        for path, info, target_ns, old_checked in planned:
            if old_checked is not None:
                paths.verify_stable(*old_checked)
            if target_ns == info.st_mtime_ns:
                continue
            handle, opened = paths.open_regular(path)
            with handle:
                if paths.stable_identity(opened) != paths.stable_identity(info) or os.fstat(handle.fileno()).st_nlink != 1:
                    raise SDKError('prepared source changed during reuse: ' + path)
                os.utime(handle.fileno(), ns=(info.st_atime_ns, target_ns))
    record = dict(format='emberbsd-prepared-source', version=1,
                  package=package, identity=identity, tree_sha256=digest)
    jsonio.write_json(os.path.join(root, PREPARED_TREE_RECEIPT), record, exclusive=True)
    return record


def read_prepared_tree_receipt(root, *, package):
    """The receipt of a prepared tree, checked by form only.

    The receipt is the identity its owner wrote when it prepared the tree;
    the tree itself is not read."""
    from . import jsonio, paths
    root = paths.canonical_existing(root)
    paths.real_directory(root)
    record = jsonio.read_json(os.path.join(root, PREPARED_TREE_RECEIPT))
    if (not isinstance(record, dict) or
            set(record) != {'format', 'version', 'package', 'identity', 'tree_sha256'} or
            record['format'] != 'emberbsd-prepared-source' or
            type(record['version']) is not int or record['version'] != 1 or
            record['package'] != package or
            not isinstance(record['identity'], dict) or not record['identity'] or
            not isinstance(record['tree_sha256'], str) or
            re.fullmatch('[0-9a-f]{64}', record['tree_sha256']) is None):
        raise SDKError('prepared source receipt is not for ' + package)
    return record


def validate_prepared_tree(root, *, package, identity, cache_path=None):
    """Refuse changed inputs or payload; optional external cache avoids old-file rehashes.

    The cache is a local acceleration hint only. It never replaces the complete
    namespace and metadata walk or the aggregate comparison with the receipt.
    Set EMBER_PREPARED_SOURCE_CACHE to a private cache directory to opt in;
    an explicit cache_path takes precedence. No environment setting preserves
    the original full-hash behavior.
    """
    return _validate_prepared_tree(root, package=package, identity=identity, cache_path=cache_path)[0]


def _validate_prepared_tree(root, *, package, identity, cache_path=None):
    from . import inventory, jsonio, paths
    root = paths.canonical_existing(root)
    record = jsonio.read_json(os.path.join(root, PREPARED_TREE_RECEIPT))
    if (not isinstance(record, dict) or
            set(record) != {'format', 'version', 'package', 'identity', 'tree_sha256'} or
            record['format'] != 'emberbsd-prepared-source' or
            type(record['version']) is not int or record['version'] != 1 or
            record['package'] != package or record['identity'] != identity or
            not isinstance(identity, dict) or not identity or
            not isinstance(record['tree_sha256'], str) or
            re.fullmatch('[0-9a-f]{64}', record['tree_sha256']) is None):
        raise SDKError('prepared source identity or payload changed')

    if cache_path is None:
        cache_directory = os.environ.get('EMBER_PREPARED_SOURCE_CACHE')
        if cache_directory is not None:
            root_info = paths.real_directory(root)
            cache_path = _prepared_tree_cache_from_environment(
                cache_directory, root, package, record['tree_sha256'],
                root_info, jsonio, paths)
    if cache_path is None:
        observed = {}
        def measure(path, relative, info):
            observed[relative] = list(paths.stable_identity(info))
            return inventory.sha256_file(path)
        rows = inventory.snapshot_tree(root, exclude=(PREPARED_TREE_RECEIPT,), digest_provider=measure)
        if record['tree_sha256'] != hashlib.sha256(jsonio.json_bytes(rows)).hexdigest():
            raise SDKError('prepared source identity or payload changed')
        return record, rows, observed

    cache_path = _prepared_tree_cache_path(cache_path, root, paths)
    started_at_ns = time.time_ns()
    root_info = paths.real_directory(root)
    previous = _read_prepared_tree_cache(cache_path, root, package, identity,
                                         record['tree_sha256'], root_info,
                                         jsonio, paths)
    cached_files = previous['files'] if previous is not None else {}
    previous_started = previous['started_at_ns'] if previous is not None else 0
    observed = {}
    cache_hits = 0
    force_hash = False

    def digest_provider(path, relative, info):
        nonlocal cache_hits
        stable = paths.stable_identity(info)
        cached = cached_files.get(relative)
        if (not force_hash and cached is not None and list(stable) == cached['stable'] and
                previous_started > 0 and started_at_ns > previous_started and
                info.st_mtime_ns < previous_started - 1_000_000_000 and
                info.st_ctime_ns < previous_started - 1_000_000_000):
            digest = cached['sha256']
            cache_hits += 1
        else:
            digest = inventory.sha256_file(path)
        observed[relative] = {'stable': list(stable), 'sha256': digest}
        return digest

    rows = inventory.snapshot_tree(root, exclude=(PREPARED_TREE_RECEIPT,),
                                   digest_provider=digest_provider)
    aggregate = hashlib.sha256(jsonio.json_bytes(rows)).hexdigest()
    if record['tree_sha256'] != aggregate and cache_hits:
        # A local cache hint may be damaged while remaining well-formed. Retry
        # once from bytes before treating the prepared payload as changed.
        observed.clear()
        force_hash = True
        rows = inventory.snapshot_tree(root, exclude=(PREPARED_TREE_RECEIPT,),
                                       digest_provider=digest_provider)
        aggregate = hashlib.sha256(jsonio.json_bytes(rows)).hexdigest()
    if record['tree_sha256'] != aggregate:
        raise SDKError('prepared source identity or payload changed')
    _write_prepared_tree_cache(cache_path, dict(
        format='emberbsd-prepared-tree-cache', version=1,
        root={'path': root, 'dev': root_info.st_dev, 'ino': root_info.st_ino},
        package=package, identity=identity, tree_sha256=record['tree_sha256'],
        started_at_ns=started_at_ns, files=observed), jsonio)
    return record, rows, {name: value['stable'] for name, value in observed.items()}


def _prepared_tree_cache_from_environment(value, root, package, tree_sha256,
                                           root_info, jsonio, paths):
    """Create/select an owner-private external cache directory for opt-in use."""
    if not hasattr(os, 'geteuid'):
        raise SDKError('prepared source cache requires POSIX ownership checks')
    requested = os.path.normpath(paths.path_string(value))
    original = paths.entry(requested)
    if original is not None and stat.S_ISLNK(original.st_mode):
        raise SDKError('prepared source cache directory must not be a symlink')
    canonical = paths.canonical_new(requested)
    if canonical == root or canonical.startswith(root + os.sep):
        raise SDKError('prepared source cache must be outside the payload')
    requested = canonical
    current = paths.entry(requested)
    created = False
    if current is None:
        try:
            os.mkdir(requested, 0o700)
            created = True
        except FileExistsError:
            pass
    info = paths.entry(requested)
    if info is None or not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise SDKError('prepared source cache directory must be a real directory')
    if paths.canonical_existing(requested) != requested:
        raise SDKError('prepared source cache directory has aliased ancestors')
    flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(requested, flags)
    try:
        opened = os.fstat(fd)
        if paths.stable_identity(opened) != paths.stable_identity(info):
            raise SDKError('prepared source cache directory changed during inspection')
        if created:
            os.fchmod(fd, 0o700)
            opened = os.fstat(fd)
    finally:
        os.close(fd)
    info = opened
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise SDKError('prepared source cache directory must be owned mode 0700')
    key = hashlib.sha256(jsonio.json_bytes(dict(
        root=root, dev=root_info.st_dev, ino=root_info.st_ino,
        package=package, tree_sha256=tree_sha256))).hexdigest()
    return os.path.join(requested, key + '.json')


def _prepared_tree_cache_path(value, root, paths):
    value = paths.path_string(value)
    parent = paths.canonical_existing(os.path.dirname(value))
    paths.real_directory(parent)
    name = os.path.basename(value)
    if name in ('', '.', '..'):
        raise SDKError('invalid prepared source cache path')
    path = os.path.join(parent, name)
    if path == root or path.startswith(root + os.sep):
        raise SDKError('prepared source cache must be outside the payload')
    return path


def _read_prepared_tree_cache(path, root, package, identity, tree_sha256,
                              root_info, jsonio, paths):
    """Return a well-formed trusted local cache, or treat it as a cold miss."""
    if not hasattr(os, 'geteuid'):
        return None
    try:
        before = paths.regular_file(path)
        if stat.S_IMODE(before.st_mode) != 0o600 or before.st_uid != os.geteuid():
            return None
        handle, opened = paths.open_regular(path)
        with handle:
            if paths.stable_identity(opened) != paths.stable_identity(before):
                return None
            data = handle.read(PREPARED_TREE_CACHE_MAX_JSON + 1)
        paths.verify_stable(path, opened)
        cache = jsonio.loads_json(data, max_bytes=PREPARED_TREE_CACHE_MAX_JSON)
        if (not isinstance(cache, dict) or
                set(cache) != {'format', 'version', 'root', 'package', 'identity',
                               'tree_sha256', 'started_at_ns', 'files'} or
                cache['format'] != 'emberbsd-prepared-tree-cache' or
                type(cache['version']) is not int or cache['version'] != 1):
            return None
    except (OSError, SDKError, ValueError, TypeError):
        return None

    cache_root = cache['root']
    if (not isinstance(cache_root, dict) or
            set(cache_root) != {'path', 'dev', 'ino'} or
            cache_root['path'] != root or
            type(cache_root['dev']) is not int or cache_root['dev'] != root_info.st_dev or
            type(cache_root['ino']) is not int or cache_root['ino'] != root_info.st_ino or
            cache['package'] != package or cache['identity'] != identity or
            cache['tree_sha256'] != tree_sha256 or
            type(cache['started_at_ns']) is not int or cache['started_at_ns'] <= 0 or
            not isinstance(cache['files'], dict)):
        return None
    for relative, row in cache['files'].items():
        if (not isinstance(relative, str) or not isinstance(row, dict) or
                set(row) != {'stable', 'sha256'} or
                not isinstance(row['stable'], list) or len(row['stable']) != 6 or
                any(type(item) is not int for item in row['stable']) or
                not isinstance(row['sha256'], str) or
                re.fullmatch('[0-9a-f]{64}', row['sha256']) is None):
            return None
    return cache


def _write_prepared_tree_cache(path, cache, jsonio):
    """Best-effort atomic mode-0600 cache publication after successful validation."""
    parent = os.path.dirname(path)
    temporary = None
    try:
        data = jsonio.json_bytes(cache)
        if len(data) > PREPARED_TREE_CACHE_MAX_JSON:
            return
        for _ in range(128):
            candidate = os.path.join(parent, '.' + os.path.basename(path) + '.' + secrets.token_hex(16))
            try:
                fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                             getattr(os, 'O_NOFOLLOW', 0), 0o600)
                temporary = candidate
                break
            except FileExistsError:
                continue
        if temporary is None:
            return
        with os.fdopen(fd, 'wb') as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        if os.name == 'posix':
            directory = os.open(parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except (OSError, SDKError):
        # Cache write failures do not turn a verified payload into a failure.
        pass
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
