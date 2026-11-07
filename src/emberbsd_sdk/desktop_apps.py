# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Bind installed desktop files to signed catalog packages.

The signed catalog records PKGPATH, PKGNAME and the artifact. It has no
application name, icon or launchable. A package becomes a desktop application
only when its installed pkgdb lists a regular desktop file and that file is a
GUI application whose executable stays inside the package prefix.
"""

import os
from pathlib import Path

_MAX_DESKTOP_BYTES = 256 * 1024
_FIELD_CODES = frozenset('%f %F %u %U %d %D %n %N %i %c %k %v %m'.split())
_DESKTOP_ENVIRONMENTS = frozenset({'KDE', 'Plasma'})


def bind_installed_desktops(rows, repositories):
    """Attach verified desktop identities to catalog rows.

    ``repositories`` is the client repository map. Each row keeps its signed
    package identity. ``desktops`` is empty when no installed file proves a
    GUI application. The return value is ``installed-pkgdb`` when a configured
    database was readable, otherwise ``unavailable``.
    """
    for row in rows:
        row['desktops'] = []
    scanned = False
    for repository, cfg in repositories.items():
        prefix = Path(str(cfg.get('prefix') or ''))
        pkgdb = Path(str(cfg.get('pkgdb') or ''))
        if not prefix.is_absolute() or not pkgdb.is_absolute():
            continue
        if not prefix.is_dir() or not pkgdb.is_dir():
            continue
        scanned = True
        grouped = {}
        for row in rows:
            if row.get('repository') != repository:
                continue
            installed = row.get('installed_pkgname')
            if not _package_dir_name(installed):
                continue
            grouped.setdefault(installed, []).append(row)
        for installed, group in grouped.items():
            found = desktop_entries(prefix, pkgdb, installed)
            for row in group:
                row['desktops'] = found
                if found:
                    row['role'] = 'desktop'
    return 'installed-pkgdb' if scanned else 'unavailable'


def desktop_entries(prefix, pkgdb, pkgname):
    """Return verified GUI entries owned by one installed package."""
    if not _package_dir_name(pkgname):
        return []
    contents = Path(pkgdb) / pkgname / '+CONTENTS'
    listed = _listed_desktop_files(contents, Path(prefix), Path(pkgdb))
    entries = []
    for relative, path in listed:
        entry = _desktop_entry(path, Path(prefix), relative, pkgname)
        if entry is not None:
            entries.append(entry)
    entries.sort(key=lambda item: item['id'])
    return entries


def _package_dir_name(name):
    if not isinstance(name, str) or not name or name in {'.', '..'}:
        return False
    return '/' not in name and '\\' not in name and '\x00' not in name


def _contained_file(root, path):
    try:
        root_real = Path(root).resolve(strict=True)
        real = Path(path).resolve(strict=True)
    except OSError:
        return None
    if not real.is_file():
        return None
    try:
        real.relative_to(root_real)
    except ValueError:
        return None
    return real


def _listed_desktop_files(contents, prefix, pkgdb):
    real_contents = _contained_file(pkgdb, contents)
    if real_contents is None:
        return []
    try:
        text = real_contents.read_text(encoding='utf-8')
    except (OSError, UnicodeError):
        return []
    cwd = None
    found = []
    seen = set()
    applications = prefix / 'share' / 'applications'
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith('@cwd '):
            cwd = Path(line[5:].strip())
            continue
        if line.startswith('@') or cwd is None:
            continue
        candidate = Path(line)
        if candidate.is_absolute() or '..' in candidate.parts:
            continue
        path = cwd / candidate
        if path.parent != applications:
            continue
        filename = path.name
        if not filename.endswith('.desktop') or filename.startswith('.'):
            continue
        real = _contained_file(prefix, path)
        if real is None:
            continue
        relative = 'share/applications/' + filename
        if relative in seen:
            continue
        seen.add(relative)
        found.append((relative, real))
    return found


def _desktop_entry(path, prefix, relative, pkgname):
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size > _MAX_DESKTOP_BYTES:
        return None
    try:
        text = path.read_text(encoding='utf-8')
    except (OSError, UnicodeError):
        return None
    fields = _parse_desktop_entry(text)
    if fields is None or fields.get('Type') != 'Application':
        return None
    if _is_true(fields.get('NoDisplay')) or _is_true(fields.get('Hidden')) or _is_true(fields.get('Terminal')):
        return None
    if not _visible_in_plasma(fields):
        return None
    names = _localized(fields, 'Name')
    name = names.get('C') or ''
    if not name or '\n' in name or '\x00' in name:
        return None
    argv = _command(fields.get('Exec', ''), prefix)
    if not argv:
        return None
    try_exec = fields.get('TryExec')
    if try_exec is not None and _command(try_exec, prefix) is None:
        return None
    comments = _localized(fields, 'Comment')
    if not comments:
        comments = _localized(fields, 'GenericName')
    return dict(
        id=Path(relative).name[:-len('.desktop')],
        name=name,
        names=names,
        comment=comments.get('C', ''),
        icon=_icon(fields.get('Icon', ''), prefix),
        exec=argv,
        categories=_categories(fields.get('Categories', '')),
        file=relative,
        source='installed-pkgdb',
        pkgname=pkgname,
    )


def _parse_desktop_entry(text):
    if '\x00' in text:
        return None
    group = None
    fields = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('[') and line.endswith(']'):
            group = line[1:-1]
            continue
        if group != 'Desktop Entry' or '=' not in line:
            continue
        key, value = line.split('=', 1)
        fields[key.strip()] = _unescape(value.strip())
    return fields


def _unescape(value):
    out = []
    index = 0
    while index < len(value):
        if value[index] == '\\' and index + 1 < len(value):
            code = value[index + 1]
            out.append({'s': ' ', 'n': '\n', 't': '\t', 'r': '\r', '\\': '\\'}.get(code, code))
            index += 2
            continue
        out.append(value[index])
        index += 1
    return ''.join(out)


def _is_true(value):
    return isinstance(value, str) and value.lower() == 'true'


def _localized(fields, key):
    names = {}
    default = fields.get(key)
    if isinstance(default, str) and default:
        names['C'] = default
    prefix = key + '['
    for field, value in fields.items():
        if field.startswith(prefix) and field.endswith(']') and isinstance(value, str) and value:
            names[field[len(prefix):-1]] = value
    return names


def _visible_in_plasma(fields):
    only = _list_field(fields.get('OnlyShowIn', ''))
    if only and _DESKTOP_ENVIRONMENTS.isdisjoint(only):
        return False
    hidden = _list_field(fields.get('NotShowIn', ''))
    return _DESKTOP_ENVIRONMENTS.isdisjoint(hidden)


def _list_field(value):
    return [item.strip() for item in value.split(';') if item.strip()]


def _categories(value):
    found = []
    for item in _list_field(value):
        if item not in found and '\n' not in item and '\x00' not in item:
            found.append(item)
    return found


def _icon(value, prefix):
    if not value or '\n' in value or '\x00' in value or len(value) > 512:
        return None
    if '/' not in value and '\\' not in value and '..' not in value:
        return value
    path = Path(value)
    if not path.is_absolute():
        return None
    real = _contained_file(prefix, path)
    if real is None:
        return None
    return str(real)


def _command(value, prefix):
    if not isinstance(value, str) or not value.strip() or '\n' in value or '\x00' in value:
        return None
    tokens = _split_exec(value)
    if tokens is None:
        return None
    argv = []
    for token in tokens:
        if token == '%%':
            argv.append('%')
            continue
        if token in _FIELD_CODES:
            continue
        argv.append(token)
    if not argv:
        return None
    program = Path(argv[0])
    if not program.is_absolute():
        program = prefix / 'bin' / program
    real = _contained_file(prefix, program)
    if real is None or not os.access(real, os.X_OK):
        return None
    return [str(real), *argv[1:]]


def _split_exec(value):
    tokens = []
    buf = []
    quote = None
    index = 0
    while index < len(value):
        char = value[index]
        if quote:
            if char == quote:
                quote = None
            elif char == '\\' and index + 1 < len(value):
                buf.append(value[index + 1])
                index += 1
            else:
                buf.append(char)
        elif char in ' \t':
            if buf:
                tokens.append(''.join(buf))
                buf = []
        elif char in '"\'':
            quote = char
        elif char == '\\' and index + 1 < len(value):
            buf.append(value[index + 1])
            index += 1
        else:
            buf.append(char)
        index += 1
    if quote:
        return None
    if buf:
        tokens.append(''.join(buf))
    return tokens
