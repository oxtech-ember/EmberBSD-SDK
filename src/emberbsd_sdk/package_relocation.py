# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Finish a signed package's declared textual prefix relocation after pkg_add."""

from pathlib import Path

from .bootstrap_bundle import _replace_file, relative_path, relocate_text, refresh_checksums
from .errors import SDKError

MARKER = '@comment emberbsd-prefix-relocation-v1'
SOURCE = '@comment emberbsd-relocation-prefix='
TEXT = '@comment emberbsd-relocation-text='
READY = '+EMBERBSD_RELOCATION_READY'


def declaration(lines):
    if MARKER not in lines:
        return None
    prefixes = [line[len(SOURCE):] for line in lines if line.startswith(SOURCE)]
    names = [line[len(TEXT):] for line in lines if line.startswith(TEXT)]
    owned = {line for line in lines if line and not line.startswith(('@', '+'))}
    if (len(prefixes) != 1 or not Path(prefixes[0]).is_absolute() or prefixes[0] == '/' or
            len(names) != len(set(names)) or any(name not in owned for name in names)):
        raise SDKError('invalid package relocation declaration')
    for name in names:
        relative_path(name)
    return prefixes[0], names


def finish(prefix, database, name):
    """Idempotent: the source-prefix comment changes only after all file edits."""
    prefix, database = Path(prefix), Path(database)
    metadata = database / name
    contents = metadata / '+CONTENTS'
    lines = contents.read_text().splitlines()
    selected = declaration(lines)
    if selected is None or selected[0] == str(prefix):
        return
    original, names = selected
    ready = metadata / READY
    if not ready.is_file() or ready.read_text() != original + '\n':
        raise SDKError('pkg_add did not complete; repair the incomplete package before relocation: ' + metadata.name)
    # Validate the idempotent token replacement before touching any file.
    relocate_text(b'', original.encode(), str(prefix).encode())
    for name in names:
        path = prefix / relative_path(name)
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(prefix.resolve()):
            raise SDKError('package relocation text is not a contained regular file: ' + name)
        if path.stat().st_size > 8 * 1024 * 1024:
            raise SDKError('package relocation text exceeds size limit: ' + name)
        content = path.read_bytes()
        updated = relocate_text(content, original.encode(), str(prefix).encode())
        if content != updated:
            _replace_file(path, updated)
    info = metadata / '+BUILD_INFO'
    if info.is_file():
        before = info.read_bytes()
        content = b''.join(relocate_text(line, original.encode(), str(prefix).encode())
            if line.startswith((b'REQUIRES=', b'PROVIDES=')) else line
            for line in before.splitlines(keepends=True))
        if content != before:
            _replace_file(info, content)
    refresh_checksums(prefix, [metadata.name], database=database, selected=set(names))
    lines = contents.read_text().splitlines()
    lines = [SOURCE + str(prefix) if line.startswith(SOURCE) else line for line in lines]
    _replace_file(contents, ('\n'.join(lines) + '\n').encode())
    ready.unlink()


def pending(prefix, database):
    """Small pkgdb-only scan; never rescan installed compiler payloads."""
    for path in sorted(Path(database).glob('*/+CONTENTS')):
        selected = declaration(path.read_text().splitlines())
        if selected is not None and selected[0] != str(prefix):
            yield path.parent.name
