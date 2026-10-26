# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted). Shared bounded archive data parser.
from .native_toolset import _require


def members(data):
    _require(data.startswith(b'!<arch>\n'), 'component output is not a static archive')
    offset, members = 8, []
    while offset < len(data):
        header = data[offset:offset + 60]
        _require(len(header) == 60 and header[58:] == b'`\n',
                 'component archive header is invalid')
        try:
            size = int(header[48:58])
            name = header[:16].decode('ascii').strip()
        except (ValueError, UnicodeError):
            _require(False, 'component archive member is invalid')
        _require(size >= 0 and offset + 60 + size <= len(data), 'component archive member is truncated')
        body = data[offset + 60:offset + 60 + size]
        if name.startswith('#1/'):
            try:
                count = int(name[3:])
            except ValueError:
                _require(False, 'component archive extended name is invalid')
            _require(0 < count <= len(body), 'component archive extended name is invalid')
            try:
                name = body[:count].rstrip(b'\0').decode('ascii')
            except UnicodeError:
                _require(False, 'component archive extended name is invalid')
            body = body[count:]
        if name not in ('/', '/SYM64/', '__.SYMDEF', '__.SYMDEF SORTED',
                         '__.SYMDEF_64', '__.SYMDEF_64 SORTED'):
            members.append((name.rstrip('/'), body))
        offset += 60 + size + size % 2
    _require(offset == len(data), 'component archive padding or size differs')
    return members
