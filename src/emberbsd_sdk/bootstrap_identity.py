# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Independent native identity observations. Never run id or parse its output."""
import os

from .native_toolset import _require

IDENTITY_OUTPUTS = frozenset(('uid', 'ruid', 'uname', 'runame', 'gid', 'rgid',
    'gname', 'rgname', 'groups', 'group-names', 'summary', 'root-uid',
    'root-uname', 'root-gid', 'root-gname', 'root-groups', 'root-group-names',
    'root-summary', 'root-pretty', 'numeric-root-summary'))


def identity_output(mode):
    """Use OS credentials and pwd/grp services; no environment identity values."""
    _require(isinstance(mode, str) and mode in IDENTITY_OUTPUTS, 'unsupported identity oracle')
    import pwd
    import grp

    def number(value):
        return str(value).encode('ascii')

    def user(uid):
        try:
            return pwd.getpwuid(uid)
        except KeyError:
            return None

    def group(gid):
        try:
            return grp.getgrgid(gid)
        except KeyError:
            return None

    def user_name(uid):
        entry = user(uid)
        return os.fsencode(entry.pw_name) if entry else number(uid)

    def group_name(gid):
        entry = group(gid)
        return os.fsencode(entry.gr_name) if entry else number(gid)

    def distinct(values, *, primary=False):
        result = []
        for value in values:
            if result and (value == result[-1] or primary and value == values[0]):
                continue
            result.append(value)
        return result

    def group_line(values, names):
        return b' '.join(group_name(gid) if names else number(gid)
                         for gid in distinct(values, primary=True)) + b'\n'

    def annotation(value, entry, field):
        return number(value) + (b'(' + os.fsencode(getattr(entry, field)) + b')' if entry else b'')

    def summary(uid, gid, values, *, euid=None, egid=None):
        result = b'uid=' + annotation(uid, user(uid), 'pw_name')
        result += b' gid=' + annotation(gid, group(gid), 'gr_name')
        if euid is not None and euid != uid:
            result += b' euid=' + annotation(euid, user(euid), 'pw_name')
        if egid is not None and egid != gid:
            result += b' egid=' + annotation(egid, group(egid), 'gr_name')
        if values:
            result += b' groups=' + b','.join(annotation(g, group(g), 'gr_name') for g in distinct(values))
        return result + b'\n'

    if mode.startswith('root-') or mode == 'numeric-root-summary':
        try:
            if mode == 'numeric-root-summary':
                try:
                    entry = pwd.getpwnam('0')
                except KeyError:
                    entry = pwd.getpwuid(0)
                operation = 'summary'
            else:
                entry = pwd.getpwnam('root')
                operation = mode.removeprefix('root-')
        except KeyError:
            _require(False, 'fixed root identity subject is unavailable')
        if operation in ('uid', 'uname', 'gid', 'gname'):
            return {'uid': number(entry.pw_uid), 'uname': os.fsencode(entry.pw_name),
                    'gid': number(entry.pw_gid), 'gname': group_name(entry.pw_gid)}[operation] + b'\n'
        values = os.getgrouplist(entry.pw_name, entry.pw_gid)
        if operation == 'summary':
            return summary(entry.pw_uid, entry.pw_gid, values)
        if operation == 'pretty':
            return b'uid\t' + os.fsencode(entry.pw_name) + b'\ngroups\t' + group_line(values, True)
        return group_line(values, operation == 'group-names')
    if mode in ('uid', 'ruid', 'uname', 'runame'):
        value = os.getuid() if mode in ('ruid', 'runame') else os.geteuid()
        return (user_name(value) if mode in ('uname', 'runame') else number(value)) + b'\n'
    if mode in ('gid', 'rgid', 'gname', 'rgname'):
        value = os.getgid() if mode in ('rgid', 'rgname') else os.getegid()
        return (group_name(value) if mode in ('gname', 'rgname') else number(value)) + b'\n'
    values = os.getgroups()
    if mode == 'summary':
        return summary(os.getuid(), os.getgid(), values, euid=os.geteuid(), egid=os.getegid())
    return group_line([os.getgid(), *values], mode == 'group-names')
