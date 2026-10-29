# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Shared safe archive and patch operations for Ports source preparation."""

import hashlib
import os
import posixpath
from pathlib import Path
import re
import shutil
import tarfile
import unicodedata

from . import process
from .errors import SDKError

MAX_ARCHIVE_MEMBERS = 100000
MAX_EXTRACTED_BYTES = 256 * 1024 * 1024
SOURCE_PREPARATION_API = 2
# extract_archive accepts skip_directories (compatible API 2 extension).
ARCHIVE_SKIP_DIRECTORIES_API = 1
ARCHIVE_FLAT_LAYOUT_API = 1
ARCHIVE_INTERNAL_SYMLINKS_API = 1


def archive_hashes(data):
    """Return the archive digests used by the source preparation receipts."""
    return dict(sha256=hashlib.sha256(data).hexdigest(),
                sha512=hashlib.sha512(data).hexdigest(),
                blake2s=hashlib.blake2s(data).hexdigest(), size=len(data))


def extract_archive(archive, destination, root_name, label, *, components=None,
                    max_members=MAX_ARCHIVE_MEMBERS, max_bytes=MAX_EXTRACTED_BYTES,
                    skip_directories=(), layout='rooted', internal_symlinks=False):
    """Extract selected source components with explicit resource bounds.

    By default links and special files are unsupported. With internal_symlinks,
    relative symlinks must resolve to an existing member inside the source root.
    layout='flat' places a flat archive under root_name.
    skip_directories names exact path components below the root, such as VCS
    metadata directories, whose members are omitted before any other check.
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
    seen, folded, matched = set(), {}, set()
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
        packed = tarfile.open(archive, 'r:*')
    except (OSError, tarfile.TarError) as exc:
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
            if name in seen:
                raise SDKError(f'duplicate {label} archive member')
            seen.add(name)
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
                with source, path.open('xb') as output:
                    shutil.copyfileobj(source, output)
                path.chmod(0o755 if member.mode & 0o111 else 0o644)
                try:
                    os.utime(path, (member.mtime, member.mtime))
                except (OSError, OverflowError, ValueError) as exc:
                    raise SDKError(f'invalid {label} archive timestamp') from exc
    if components is not None and matched != set(components):
        raise SDKError(f'{label} archive is missing selected source components')
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
PREPARED_TREE_RECEIPT = 'emberbsd-prepared-source.json'


def _prepared_tree_digest(root):
    from . import inventory, jsonio, paths
    root = paths.canonical_existing(root)
    paths.real_directory(root)
    rows = inventory.snapshot_tree(root, exclude=(PREPARED_TREE_RECEIPT,))
    return hashlib.sha256(jsonio.json_bytes(rows)).hexdigest()


def write_prepared_tree_receipt(root, *, package, identity):
    """Record an owner-selected identity and the exact prepared payload."""
    from . import jsonio, paths
    root = paths.canonical_existing(root)
    if not isinstance(package, str) or re.fullmatch(r'[a-z0-9][a-z0-9+_.-]*/[a-z0-9][a-z0-9+_.-]*', package) is None:
        raise SDKError('invalid prepared source package')
    if not isinstance(identity, dict) or not identity:
        raise SDKError('invalid prepared source identity')
    record = dict(format='emberbsd-prepared-source', version=1,
                  package=package, identity=identity,
                  tree_sha256=_prepared_tree_digest(root))
    jsonio.write_json(os.path.join(root, PREPARED_TREE_RECEIPT), record, exclusive=True)
    return record


def validate_prepared_tree(root, *, package, identity):
    """Refuse changed package inputs, receipt or prepared payload."""
    from . import jsonio, paths
    root = paths.canonical_existing(root)
    record = jsonio.read_json(os.path.join(root, PREPARED_TREE_RECEIPT))
    if (not isinstance(record, dict) or
            set(record) != {'format', 'version', 'package', 'identity', 'tree_sha256'} or
            record['format'] != 'emberbsd-prepared-source' or
            type(record['version']) is not int or record['version'] != 1 or
            record['package'] != package or record['identity'] != identity or
            not isinstance(identity, dict) or not identity or
            not isinstance(record['tree_sha256'], str) or
            re.fullmatch('[0-9a-f]{64}', record['tree_sha256']) is None or
            record['tree_sha256'] != _prepared_tree_digest(root)):
        raise SDKError('prepared source identity or payload changed')
    return record
