# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Bounded source-owned filesystem fixtures and observations. No executable hooks."""

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import stat

from .bootstrap_program_data import data_size, decode_data
from .native_toolset import _json, _require
from .jsonio import json_bytes

PROGRAMS = ('chmod', 'mkdir', 'ln', 'rmdir', 'rm')
MAX_NODES = 64
MAX_BYTES = 1024 * 1024
_TIMES = ('atime_ns', 'mtime_ns')
_DIRECTORY = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)


def step_key(step):
    return step['id'].removeprefix('case-')


def step_case(step, manifest):
    if step['id'] == 'runtime-observation':
        return manifest['runtime_cases'][0]
    if step['phase'] == 'program-case':
        return next(case for case in manifest['runtime_cases'] if 'case-' + case['name'] == step['id'])
    return None


def _path(value):
    _require(isinstance(value, str) and len(value) <= 512 and
             1 <= len(value.split('/')) <= 8 and
             all(re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]*', part) and
                 part not in ('.', '..') for part in value.split('/')),
             'filesystem case needs a bounded relative path')
    return value


def _mode(value):
    _require(isinstance(value, str) and re.fullmatch(r'0[0-7]{3}', value),
             'filesystem case needs a fixed permission mode')
    return int(value, 8)


def validate(value):
    """Validate the closed fixture and post-state union before any setup."""
    _require(isinstance(value, dict) and set(value) == {'umask', 'fixtures', 'expect'},
             'unsupported filesystem case fields')
    _mode(value['umask'])
    fixtures, expected = value['fixtures'], value['expect']
    _require(isinstance(fixtures, dict) and isinstance(expected, dict) and
             set(fixtures) <= set(expected) and len(expected) <= MAX_NODES,
             'every bounded fixture needs an explicit final expectation')
    names = set(fixtures) | set(expected)
    for name in names:
        _path(name)
    _require(len({name.casefold() for name in names}) == len(names),
             'filesystem paths collide on a case-insensitive filesystem')
    for mapping, initial in ((fixtures, True), (expected, False)):
        total = 0
        for name, row in mapping.items():
            _require(isinstance(row, dict), 'filesystem entry must be an object')
            kind = row.get('type')
            fields = {'file': {'type', 'mode', 'data'}, 'directory': {'type', 'mode'},
                      'symlink': {'type', 'target'}}
            if initial:
                fields['hardlink'] = {'type', 'target'}
            else:
                fields['absent'] = {'type'}
            allowed = fields.get(kind, set())
            optional = ({'same_inode_as'} if not initial and kind in ('file', 'symlink') else set())
            if kind in ('file', 'directory', 'symlink'):
                optional.update(_TIMES)
            if not initial and kind == 'symlink':
                optional.add('mode')
            _require(bool(allowed) and allowed <= set(row) <= allowed | optional,
                     'unsupported filesystem entry fields or type')
            if 'mode' in row:
                _mode(row['mode'])
            for field in _TIMES:
                if field in row:
                    _require(type(row[field]) is int and -(2**63) <= row[field] < 2**63,
                             'filesystem timestamp needs signed 64-bit integer nanoseconds')
            if kind == 'file':
                total += data_size(row['data'])
            if 'target' in row:
                target = _path(row['target'])
                if kind == 'symlink':
                    # Symlink targets are relative to the link's parent.
                    _path('/'.join([*name.split('/')[:-1], target]))
                # Hardlink targets use the fixture root, as in prepare().
            if 'same_inode_as' in row:
                peer = _path(row['same_inode_as'])
                _require(peer != name and peer in expected and
                         isinstance(expected[peer], dict) and expected[peer].get('type') == kind,
                         'filesystem identity needs another declared same-type peer')
            parts = name.split('/')
            for n in range(1, len(parts)):
                parent = '/'.join(parts[:n])
                _require(parent in mapping and (mapping[parent].get('type') == 'directory' or
                         not initial and kind == 'absent' and mapping[parent].get('type') == 'absent'),
                         'filesystem ancestors must be explicit directories, never links')
        _require(total <= MAX_BYTES, 'filesystem fixture data exceeds its bound')
    for name, row in fixtures.items():
        seen = {name}
        while row['type'] == 'hardlink':
            target = row['target']
            _require(target in fixtures and target not in seen,
                     'filesystem hardlink target is missing or cyclic')
            seen.add(target)
            row = fixtures[target]
        _require(fixtures[name]['type'] != 'hardlink' or row['type'] == 'file',
                 'fixture hardlinks must terminate at a regular file')
    return value


def case_directory(work, key):
    _require(isinstance(key, str) and re.fullmatch(r'[a-z][a-z0-9-]*', key),
             'invalid filesystem case name')
    return Path(work) / 'cases' / key


@contextmanager
def _parent(root_fd, relative):
    """Walk every ancestor with directory descriptors and no-follow opens."""
    parts = _path(relative).split('/')
    fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            child = os.open(part, _DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd, parts[-1]
    finally:
        os.close(fd)


def prepare(work, case, *, key=None):
    """Create only a fresh owned case directory. Never clean or reuse old paths."""
    value = validate(case['filesystem'])
    _require(hasattr(os, 'O_DIRECTORY') and hasattr(os, 'O_NOFOLLOW'),
             'filesystem cases require native no-follow directory descriptors')
    key = key or case['name']
    root = case_directory(work, key)
    work_fd = os.open(work, _DIRECTORY)
    try:
        try:
            os.mkdir('cases', 0o700, dir_fd=work_fd)
        except FileExistsError:
            pass
        cases_fd = os.open('cases', _DIRECTORY, dir_fd=work_fd)
        try:
            os.mkdir(key, 0o700, dir_fd=cases_fd)
            root_fd = os.open(key, _DIRECTORY, dir_fd=cases_fd)
        finally:
            os.close(cases_fd)
    finally:
        os.close(work_fd)
    try:
        pending = dict(value['fixtures'])
        created = set()
        while pending:
            progress = False
            for name, row in sorted(list(pending.items()), key=lambda item: (item[0].count('/'), item[0])):
                if row['type'] == 'hardlink' and row['target'] not in created:
                    continue
                with _parent(root_fd, name) as (fd, leaf):
                    if row['type'] == 'directory':
                        os.mkdir(leaf, 0o700, dir_fd=fd)
                    elif row['type'] == 'file':
                        file_fd = os.open(leaf, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                          0o600, dir_fd=fd)
                        with os.fdopen(file_fd, 'wb') as stream:
                            stream.write(decode_data(row['data']))
                            os.fchmod(stream.fileno(), _mode(row['mode']))
                    elif row['type'] == 'symlink':
                        os.symlink(row['target'], leaf, dir_fd=fd)
                    else:
                        with _parent(root_fd, row['target']) as (source_fd, source):
                            _require(stat.S_ISREG(os.stat(source, dir_fd=source_fd,
                                                        follow_symlinks=False).st_mode),
                                     'hardlink fixture source changed type')
                            os.link(source, leaf, src_dir_fd=source_fd, dst_dir_fd=fd,
                                    follow_symlinks=False)
                created.add(name)
                del pending[name]
                progress = True
            _require(progress, 'filesystem fixture graph cannot be prepared')
        for name, row in sorted(value['fixtures'].items(), reverse=True):
            if row['type'] == 'directory':
                with _parent(root_fd, name) as (fd, leaf):
                    child = os.open(leaf, _DIRECTORY, dir_fd=fd)
                    try:
                        os.fchmod(child, _mode(row['mode']))
                    finally:
                        os.close(child)
        # Complete child creation and mode changes before assigning directory
        # times. Hardlink fixtures inherit the regular target's times.
        for name, row in sorted(value['fixtures'].items(), reverse=True):
            if not any(field in row for field in _TIMES):
                continue
            with _parent(root_fd, name) as (fd, leaf):
                before = os.stat(leaf, dir_fd=fd, follow_symlinks=False)
                check_type = {'file': stat.S_ISREG, 'directory': stat.S_ISDIR,
                              'symlink': stat.S_ISLNK}[row['type']]
                _require(check_type(before.st_mode), 'filesystem timestamp target changed type')
                times = tuple(row.get(field, getattr(before, 'st_' + field)) for field in _TIMES)
                os.utime(leaf, ns=times, dir_fd=fd, follow_symlinks=False)
                after = os.stat(leaf, dir_fd=fd, follow_symlinks=False)
                _require((before.st_dev, before.st_ino, before.st_mode) ==
                         (after.st_dev, after.st_ino, after.st_mode) and
                         (after.st_atime_ns, after.st_mtime_ns) == times,
                         'filesystem timestamp did not roundtrip exactly')
    finally:
        os.close(root_fd)
    return root


def _expected(value):
    result = {}
    for name, row in value['expect'].items():
        out = {k: v for k, v in row.items() if k != 'data'}
        if row['type'] == 'file':
            data = decode_data(row['data'])
            out.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
        result[name] = out
    return result


def observe(work, case, *, key=None):
    """Observe only after child drainage. Reject all undeclared side effects."""
    value = validate(case['filesystem'])
    _require(hasattr(os, 'O_DIRECTORY') and hasattr(os, 'O_NOFOLLOW'),
             'filesystem cases require native no-follow directory descriptors')
    key = key or case['name']
    root = case_directory(work, key)
    work_fd = os.open(work, _DIRECTORY)
    entries, identities, snapshots = {}, {}, {}
    total = 0

    def walk(fd, prefix=''):
        nonlocal total
        names = []
        remaining = MAX_NODES - len(entries)
        with os.scandir(fd) as iterator:
            for item in iterator:
                _require(len(names) < remaining,
                         'filesystem directory exceeds the remaining node budget')
                names.append(item.name)
        for leaf in sorted(names):
            name = prefix + leaf
            _path(name)
            _require(name in value['expect'] and len(entries) < MAX_NODES,
                     'filesystem child created an undeclared entry')
            st = os.stat(leaf, dir_fd=fd, follow_symlinks=False)
            snapshots[name] = st
            identities[name] = (st.st_dev, st.st_ino)
            row = dict(mode=f'{stat.S_IMODE(st.st_mode):04o}')
            row.update({field: getattr(st, 'st_' + field) for field in _TIMES
                        if field in value['expect'][name]})
            if stat.S_ISDIR(st.st_mode):
                row['type'] = 'directory'
                entries[name] = row
                child = os.open(leaf, _DIRECTORY, dir_fd=fd)
                try:
                    opened = os.fstat(child)
                    _require((opened.st_dev, opened.st_ino) == identities[name],
                             'filesystem directory changed during observation')
                    walk(child, name + '/')
                finally:
                    os.close(child)
            elif stat.S_ISREG(st.st_mode):
                _require(st.st_size <= MAX_BYTES, 'filesystem output exceeds its bound')
                row['type'] = 'file'
                entries[name] = row
            elif stat.S_ISLNK(st.st_mode):
                row['type'] = 'symlink'
                if 'mode' not in value['expect'][name]:
                    del row['mode']
                entries[name] = row
            else:
                _require(False, 'filesystem case created a special file')

    try:
        with _parent(work_fd, 'cases/' + key) as (fd, leaf):
            case_fd = os.open(leaf, _DIRECTORY, dir_fd=fd)
            try:
                walk(case_fd)
                # Capture every alias before reading file data or link text.
                # Those reads can update atime, including a peer hardlink.
                for name, row in entries.items():
                    if row['type'] == 'directory':
                        continue
                    st = snapshots[name]
                    with _parent(case_fd, name) as (parent_fd, member):
                        if row['type'] == 'symlink':
                            row['target'] = os.readlink(member, dir_fd=parent_fd)
                            after = os.stat(member, dir_fd=parent_fd, follow_symlinks=False)
                            _require((st.st_dev, st.st_ino, st.st_mode, st.st_mtime_ns, st.st_ctime_ns) ==
                                     (after.st_dev, after.st_ino, after.st_mode, after.st_mtime_ns, after.st_ctime_ns),
                                     'filesystem link changed during observation')
                            continue
                        file_fd = os.open(member, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
                        with os.fdopen(file_fd, 'rb') as stream:
                            before = os.fstat(stream.fileno())
                            _require(stat.S_ISREG(before.st_mode) and
                                     (before.st_dev, before.st_ino) == identities[name],
                                     'filesystem file changed type or identity')
                            raw = stream.read(MAX_BYTES + 1)
                            after = os.fstat(stream.fileno())
                        total += len(raw)
                        _require(total <= MAX_BYTES and len(raw) == st.st_size and
                                 (st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns) ==
                                 (before.st_mode, before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
                                 (after.st_mode, after.st_size, after.st_mtime_ns, after.st_ctime_ns),
                                 'filesystem data changed during observation or exceeds its bound')
                        row.update(size=len(raw), sha256=hashlib.sha256(raw).hexdigest())
            finally:
                os.close(case_fd)
        for name, wanted in value['expect'].items():
            if name not in entries:
                entries[name] = dict(type='absent')
            if 'same_inode_as' in wanted:
                peer = wanted['same_inode_as']
                _require(entries[name]['type'] == wanted['type'] and
                         entries.get(peer, {}).get('type') == wanted['type'] and
                         identities.get(name) is not None and identities[name] == identities.get(peer),
                         'filesystem inode relation differs without following links')
                entries[name]['same_inode_as'] = peer
        _require(entries == _expected(value), 'filesystem actual state differs from its source contract')
        report = dict(format='emberbsd-bootstrap-filesystem-observation', version=1,
                      case=key, contract_sha256=hashlib.sha256(json_bytes(value)).hexdigest(), entries=entries)
        raw = json_bytes(report)
        name = key + '.filesystem.json'
        report_fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=work_fd)
        with os.fdopen(report_fd, 'wb') as stream:
            stream.write(raw)
    finally:
        os.close(work_fd)
    return dict(path=str(Path(work) / name), sha256=hashlib.sha256(raw).hexdigest())


def verify_report(reference, case, *, key=None):
    """Check retained facts with the same source-owned expectations, without hooks."""
    value = validate(case['filesystem'])
    expected = dict(format='emberbsd-bootstrap-filesystem-observation', version=1,
                    case=key or case['name'], contract_sha256=hashlib.sha256(json_bytes(value)).hexdigest(),
                    entries=_expected(value))
    _require(_json(reference) == expected, 'historical filesystem observation differs from its source contract')
