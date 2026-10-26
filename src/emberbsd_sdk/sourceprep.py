# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Shared safe archive and patch operations for Ports source preparation."""

import hashlib
import os
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


def archive_hashes(data):
    """Return the archive digests used by the source preparation receipts."""
    return dict(sha256=hashlib.sha256(data).hexdigest(),
                sha512=hashlib.sha512(data).hexdigest(),
                blake2s=hashlib.blake2s(data).hexdigest(), size=len(data))


def extract_archive(archive, destination, root_name, label, *, components=None,
                    max_members=MAX_ARCHIVE_MEMBERS, max_bytes=MAX_EXTRACTED_BYTES):
    """Extract selected source components with explicit resource bounds.

    Defaults preserve the original regular-file-only contract. Links and special
    files remain unsupported, including in explicitly selected components.
    """
    def safe_name(name):
        return (isinstance(name, str) and name and not name.startswith('/') and
                '\\' not in name and all(p not in ('', '.', '..') for p in name.split('/')) and
                not any(ord(c) < 32 or ord(c) == 127 for c in name) and
                unicodedata.normalize('NFC', name) == name)
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
    destination = Path(destination)
    if not destination.is_dir() or destination.is_symlink():
        raise SDKError(f'{label} extraction destination must be a real directory')
    seen, folded, matched = set(), {}, set()
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
            if parts[0] != root_name or not safe_name(name):
                raise SDKError(f'unsafe {label} archive path')
            relative = '/'.join(parts[1:])
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
    if components is not None and matched != set(components):
        raise SDKError(f'{label} archive is missing selected source components')


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
        args.extend(('-B', str(backup_directory) + os.sep))
    args.extend(('-i', str(patch_path)))
    process.capture(args, cwd=str(cwd),
                    env=process.sanitized_environment())
