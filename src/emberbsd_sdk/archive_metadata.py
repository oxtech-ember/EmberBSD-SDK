# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD contributors (AI-assisted).
"""Read selected pkgsrc source archive metadata without resolving packages."""
import hashlib
from pathlib import Path
import re

from . import paths
from .errors import SDKError

ARCHIVE_METADATA_API = 1


def _read(path):
    stream, before = paths.open_regular(path)
    with stream:
        data = stream.read()
    paths.verify_stable(path, before)
    return data


def read_recipe(recipe_root, archive_name, archive_root):
    """Return validated archive metadata and the complete patch inventory."""
    for value in (archive_name, archive_root):
        if (not isinstance(value, str) or not value or value in ('.', '..') or
                '/' in value or '\\' in value or
                any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise SDKError('archive name and root must be single safe components')
    root = Path(recipe_root)
    info = _read(root / 'distinfo').decode('utf-8', 'strict')
    metadata = {'archive': archive_name, 'archive_root': archive_root,
                'distname': archive_root}
    for label, key, pattern in (('SHA512', 'sha512', r'[0-9a-f]{128}'),
                                ('BLAKE2s', 'blake2s', r'[0-9a-f]{64}'),
                                ('Size', 'size', r'[0-9]+ bytes')):
        prefix = label + ' (' + archive_name + ')'
        lines = [line for line in info.splitlines() if line.startswith(prefix)]
        if len(lines) != 1:
            raise SDKError('missing or duplicate archive ' + label)
        match = re.fullmatch(re.escape(prefix) + r' = (' + pattern + r')', lines[0])
        if not match:
            raise SDKError('invalid archive ' + label)
        value = match.group(1)
        metadata[key] = int(value[:-6]) if key == 'size' else value
    declarations = [line for line in info.splitlines() if line.startswith('SHA1 (patch-')]
    declared = []
    for line in declarations:
        match = re.fullmatch(r'SHA1 \((patch-[A-Za-z0-9_.+-]+)\) = ([0-9a-f]{40})', line)
        if not match:
            raise SDKError('invalid patch checksum declaration')
        declared.append(match.groups())
    directory = root / 'patches'
    patches = sorted(directory.glob('patch-*')) if directory.exists() else []
    if [p.name for p in patches] != sorted(name for name, _ in declared):
        raise SDKError('patch inventory differs from distinfo')
    for name, expected in declared:
        raw = _read(directory / name)
        filtered = b''.join(line for line in raw.splitlines(keepends=True)
                            if b'$NetBSD' not in line)
        if hashlib.sha1(filtered).hexdigest() != expected:
            raise SDKError('patch checksum differs: ' + name)
    files = [root / 'Makefile', root / 'distinfo', *patches]
    metadata['recipe'] = {
        str(p.relative_to(root)): hashlib.sha256(_read(p)).hexdigest() for p in files
    }
    return metadata, patches
