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


def archive_hashes(data):
    """Return the archive digests used by the source preparation receipts."""
    return dict(sha256=hashlib.sha256(data).hexdigest(),
                sha512=hashlib.sha512(data).hexdigest(),
                blake2s=hashlib.blake2s(data).hexdigest(), size=len(data))


def extract_archive(archive, destination, root_name, label):
    """Extract unique regular files and directories below one archive root."""
    seen = set()
    folded = set()
    extracted_size = 0
    try:
        packed = tarfile.open(archive, 'r:*')
    except (OSError, tarfile.TarError) as exc:
        raise SDKError(f'{label} archive cannot be opened: {exc}') from exc
    with packed:
        for member in packed:
            name = member.name.rstrip('/')
            parts = name.split('/')
            if (parts[0] != root_name or
                    any(part in ('', '.', '..') for part in parts) or
                    any(ord(char) < 32 or ord(char) == 127 for char in name) or
                    unicodedata.normalize('NFC', name) != name):
                raise SDKError(f'unsafe {label} archive path')
            if name in seen or name.casefold() in folded:
                raise SDKError(f'duplicate {label} archive member')
            seen.add(name)
            folded.add(name.casefold())
            if len(seen) > MAX_ARCHIVE_MEMBERS:
                raise SDKError(f'{label} archive has too many members')
            if not (member.isdir() or member.isreg()):
                raise SDKError(f'{label} archive contains a link or special file')
            path = Path(destination).joinpath(*parts)
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
                path.chmod(0o755)
            else:
                if member.size < 0 or extracted_size + member.size > MAX_EXTRACTED_BYTES:
                    raise SDKError(f'{label} archive exceeds the extraction size limit')
                extracted_size += member.size
                path.parent.mkdir(parents=True, exist_ok=True)
                source = packed.extractfile(member)
                if source is None:
                    raise SDKError(f'{label} archive member has no data')
                with source, path.open('xb') as output:
                    shutil.copyfileobj(source, output)
                path.chmod(0o755 if member.mode & 0o111 else 0o644)


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
            parent = backup_directory.joinpath(*header.decode('ascii').split('/')[:-1])
            parent.mkdir(parents=True, exist_ok=True)
        args.extend(('-B', str(backup_directory) + os.sep))
    args.extend(('-i', str(patch_path)))
    process.capture(args, cwd=str(cwd),
                    env=process.sanitized_environment())
