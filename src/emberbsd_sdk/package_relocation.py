# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Finish a signed package's declared textual prefix relocation after pkg_add."""

from pathlib import Path

from .bootstrap_bundle import (_replace_file, relative_path, relocate_text,
                               refresh_checksums, relocate_build_info, run_install_hooks)
from .errors import SDKError

INDEPENDENT = '@comment emberbsd-prefix-independent-v1'
MARKER = '@comment emberbsd-prefix-relocation-v1'
SOURCE = '@comment emberbsd-relocation-prefix='
TEXT = '@comment emberbsd-relocation-text='
READY = '+EMBERBSD_RELOCATION_READY'
HOOKS = '@comment emberbsd-deferred-hooks-v1'


def declaration(lines):
    if MARKER not in lines:
        if HOOKS in lines:
            raise SDKError('deferred hooks require package text relocation')
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


def installed_source_prefix(metadata):
    info = metadata / '+BUILD_INFO'
    if info.is_file():
        for line in info.read_text().splitlines():
            if line.startswith('LOCALBASE='):
                value = line[len('LOCALBASE='):]
                if not Path(value).is_absolute() or value == '/':
                    raise SDKError('invalid installed package LOCALBASE: ' + metadata.name)
                return value
    return None


def complete(prefix, metadata, lines):
    """Commit completed relocation only after payload and hooks have succeeded."""
    contents = metadata / '+CONTENTS'
    lines = [SOURCE + str(prefix) if line.startswith(SOURCE) else line for line in lines if line != HOOKS]
    _replace_file(contents, ('\n'.join(lines) + '\n').encode())
    (metadata / READY).unlink(missing_ok=True)


def complete_bootstrap(prefix, database, name):
    """Complete deferred package hooks already run by bootstrap initialization."""
    metadata = Path(database) / name
    lines = (metadata / '+CONTENTS').read_text().splitlines()
    selected = declaration(lines)
    if selected is None:
        return
    if selected[0] != str(prefix):
        raise SDKError('bootstrap package relocation prefix differs: ' + name)
    if HOOKS in lines or (metadata / READY).exists():
        complete(prefix, metadata, lines)


def finish(prefix, database, name, *, configure=True):
    """Idempotent: the source-prefix comment changes only after all file edits."""
    prefix, database = Path(prefix), Path(database)
    metadata = database / name
    contents = metadata / '+CONTENTS'
    lines = contents.read_text().splitlines()
    selected = declaration(lines)
    if selected is None or (selected[0] == str(prefix) and HOOKS not in lines):
        if selected is not None or INDEPENDENT in lines:
            original = installed_source_prefix(metadata)
            if original is not None and original != str(prefix):
                relocate_build_info(metadata / '+BUILD_INFO', original.encode(), str(prefix).encode())
        if selected is not None:
            (metadata / READY).unlink(missing_ok=True)
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
    relocate_build_info(metadata / '+BUILD_INFO', original.encode(), str(prefix).encode())
    if HOOKS in lines and configure:
        for member in ('+INSTALL', '+DEINSTALL'):
            path = metadata / member
            if path.is_symlink():
                raise SDKError('package hook is not a regular file')
            if path.exists():
                if not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
                    raise SDKError('invalid package hook')
                content = path.read_bytes()
                updated = relocate_text(content, original.encode(), str(prefix).encode())
                if content != updated:
                    _replace_file(path, updated)
        run_install_hooks(prefix, database, metadata.name)
    refresh_checksums(prefix, [metadata.name], database=database, selected=set(names))
    if HOOKS in lines and not configure:
        return
    lines = contents.read_text().splitlines()
    complete(prefix, metadata, lines)


def pending(prefix, database):
    """Small pkgdb-only scan; never rescan installed compiler payloads."""
    for path in sorted(Path(database).glob('*/+CONTENTS')):
        lines = path.read_text().splitlines()
        selected = declaration(lines)
        if selected is not None and (selected[0] != str(prefix) or HOOKS in lines or (path.parent / READY).exists()):
            yield path.parent.name
        elif selected is not None or INDEPENDENT in lines:
            original = installed_source_prefix(path.parent)
            if original is not None and original != str(prefix):
                yield path.parent.name
