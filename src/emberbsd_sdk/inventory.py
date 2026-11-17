# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Stable streaming hashes and deterministic lstat payload inventory."""

import hashlib
import os
import re
import stat
from .errors import SDKError
from .paths import canonical_existing, inside, open_regular, path_string, real_directory, regular_file, stable_identity, verify_stable
from .elf import read_elf_header, read_archive


def hash_file(path, algorithm='sha256'):
    """Hash a stable file. SHA1 is only for legacy upstream recipe identity."""
    if algorithm not in ('sha256', 'sha512', 'sha1'):
        raise SDKError(f'unsupported file hash algorithm: {algorithm}')
    digest = hashlib.new(algorithm)
    handle, before = open_regular(path)
    with handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
        if stable_identity(os.fstat(handle.fileno())) != stable_identity(before):
            raise SDKError(f'file changed during inspection: {path}')
    verify_stable(path, before)
    return digest.hexdigest()


def sha256_file(path):
    return hash_file(path, 'sha256')


INSTALLED_TREE_INVENTORY_API = 1


def snapshot_tree(root, machine=None, exclude=(), digests=None, digest_provider=None):
    """Inspect a strict payload. Links must resolve within the payload root.

    digests maps relative file names to SHA-256 values already recorded by
    an input key, such as a reused stage manifest; those files are not read
    again. digest_provider(path, relative, lstat_result) may provide a digest
    for a stable regular file. It is mutually exclusive with digests. The
    rows have the same shape either way."""
    exclude = tuple(exclude)
    if digests is not None and digest_provider is not None:
        raise SDKError('digests and digest_provider are mutually exclusive')
    return _snapshot_tree(root, machine, exclude, installed_links=False,
                          digests=digests, digest_provider=digest_provider)


def snapshot_installed_tree(root, exclude=()):
    """Inspect existing files. Preserve link text without reading its destination."""
    return _snapshot_tree(root, None, exclude, installed_links=True)


def _snapshot_tree(root, machine, exclude, *, installed_links, digests=None,
                   digest_provider=None):
    root = path_string(root)
    real_directory(root)
    if canonical_existing(root) != root:
        raise SDKError(f'payload root must have real ancestors: {root}')
    rows = {}
    excluded = set(exclude)

    def visit(directory, prefix=''):
        before = real_directory(directory)
        with os.scandir(directory) as scan:
            children = sorted(scan, key=lambda item: item.name)
        for child in children:
            relative = prefix + child.name
            if relative in excluded:
                continue
            if any(ord(char) < 32 or ord(char) == 127 for char in relative):
                raise SDKError(f'control character in payload path: {relative}')
            try:
                relative.encode('utf-8', 'strict')
            except UnicodeError as exc:
                raise SDKError(f'invalid UTF-8 payload path: {relative!r}') from exc
            path = os.path.join(directory, child.name)
            info = os.lstat(path)
            row = dict(mode=stat.S_IMODE(info.st_mode))
            if stat.S_ISLNK(info.st_mode):
                target = os.readlink(path)
                if installed_links:
                    if any(ord(char) < 32 or ord(char) == 127 for char in target):
                        raise SDKError(f'control character in installed link target: {path}')
                    try:
                        target.encode('utf-8', 'strict')
                    except UnicodeError as exc:
                        raise SDKError(f'invalid UTF-8 installed link target: {path}') from exc
                elif os.path.isabs(target) or any(ord(char) < 32 or ord(char) == 127 for char in target) or not inside(canonical_existing(path), root):
                    raise SDKError(f'absolute or escaped payload link: {path}')
                row.update(kind='link', target=target)
            elif stat.S_ISDIR(info.st_mode):
                row['kind'] = 'directory'
                visit(path, relative + '/')
            elif stat.S_ISREG(info.st_mode):
                known = digests.get(relative) if digests is not None else None
                if known is None and digest_provider is not None:
                    known = digest_provider(path, relative, info)
                    if known is not None and (not isinstance(known, str) or
                                              re.fullmatch('[0-9a-f]{64}', known) is None):
                        raise SDKError(f'invalid SHA-256 from digest provider: {relative}')
                row.update(kind='file', size=info.st_size,
                           sha256=known if known is not None else sha256_file(path))
                if machine is not None:
                    header = read_elf_header(path, machine)
                    if header:
                        row['elf'] = header
                    if relative.endswith('.a'):
                        row['archive'] = read_archive(path, machine)
            else:
                raise SDKError(f'special file in payload: {path}')
            verify_stable(path, info)
            rows[relative] = row
        verify_stable(directory, before)

    visit(root)
    return dict(sorted(rows.items()))


def required_file(root, relative):
    path = canonical_existing(os.path.join(root, relative))
    if not inside(path, root):
        raise SDKError(f'required path escapes payload: {relative}')
    info = regular_file(path)
    if info.st_size == 0:
        raise SDKError(f'empty required file: {relative}')
    return path


def required_elf(root, relative, machine, kind):
    path = required_file(root, relative)
    if read_elf_header(path, machine, kind) is None:
        raise SDKError(f'required ELF missing: {relative}')


def runtime(root, triplet, machine):
    rows = snapshot_tree(root, machine=machine)
    for relative in ('include/c++/vector', f'include/c++/{triplet}/bits/c++config.h', 'include/c++/backward/backward_warning.h'):
        required_file(root, relative)
    for name in ('crtbegin.o', 'crtbeginS.o', 'crtend.o', 'crtendS.o'):
        required_elf(root, f'lib/gcc/{triplet}/16.2.0/{name}', machine, 1)
    for relative in (f'lib/gcc/{triplet}/16.2.0/libgcc.a', f'lib/gcc/{triplet}/16.2.0/libgcc_eh.a', 'lib/libstdc++.a', 'lib/libsupc++.a'):
        if read_archive(required_file(root, relative), machine)['elf_members'] == 0:
            raise SDKError(f'empty developer archive: {relative}')
    for relative in ('lib/libgcc_s.so.1', 'lib/libstdc++.so.7', 'lib/libstdc++.so'):
        required_elf(root, relative, machine, 3)
    return rows


def sysroot(root, machine, exclude=()):
    rows = snapshot_tree(root, machine=machine, exclude=exclude)
    for relative in ('usr/include/stdio.h', 'usr/include/sys/types.h', 'usr/include/math.h', 'usr/include/pthread.h'):
        required_file(root, relative)
    for name in ('crt0.o', 'crti.o', 'crtn.o'):
        required_elf(root, 'usr/lib/' + name, machine, 1)
    for name in ('libc.so', 'libm.so', 'libpthread.so'):
        required_elf(root, 'usr/lib/' + name, machine, 3)
    required_elf(root, 'libexec/ld.elf_so', machine, 3)
    return rows
