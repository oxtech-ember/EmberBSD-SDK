#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Prepare the signed set inputs for one transition from the former FFS updater."""

import argparse
import io
import sys
import tarfile
import tempfile
from hashlib import sha256
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.inventory import sha256_file
from emberbsd_sdk.jsonio import json_bytes, read_json
from emberbsd_sdk.os_update import MAX_GENERATION
from emberbsd_sdk.release_catalog import _SLUG

STUB = b'#!/bin/sh\nexit 0\n'
RC = './etc/rc.d/ember_os_update'
OLD_PKG_RC = './etc/rc.d/ember_pkg_update'
PROGRAM = './usr/libexec/ember-os-update'
SHARE = './usr/share/ember-update'
HANDOFF = SHARE + '/handoff'


def mtree(name, data):
    attrs = 'type=dir uname=root gname=wheel mode=0755'
    if data is not None:
        mode = '0555' if name in (RC, PROGRAM) else '0444'
        attrs = f'type=file uname=root gname=wheel mode={mode} size={len(data)} sha256={sha256(data).hexdigest()}'
    return f'{name} {attrs}\n'.encode()


def add_member(archive, name, data, stamp):
    entry = tarfile.TarInfo(name)
    entry.uname, entry.gname, entry.mtime = 'root', 'wheel', stamp
    if data is None:
        entry.type, entry.mode = tarfile.DIRTYPE, 0o755
    else:
        entry.mode = 0o555 if name in (RC, PROGRAM) else 0o444
        entry.size = len(data)
    archive.addfile(entry, io.BytesIO(data) if data is not None else None)


def check_manifest_records(text):
    # maketars omits content measurements for its generated manifests.
    names = {b'./etc/mtree/set.base', b'./etc/mtree/set.etc'}
    metadata = {b'type', b'mode', b'uname', b'gname', b'uid', b'gid', b'time', b'optional'}
    for line in text.splitlines():
        fields = line.split()
        if fields and fields[0] in names:
            if any(field.split(b'=', 1)[0] not in metadata for field in fields[1:]):
                raise SDKError(f'cannot retain measured generated manifest: {fields[0].decode()}')


def compose(source, role, additions, output):
    manifest = f'./etc/mtree/set.{role}'
    obsolete = f'./var/db/obsolete/{role}'
    required = {manifest, obsolete, './etc/rc.d/ember_update'} if role == 'etc' else {
        manifest, obsolete, './usr/sbin/ember-update', './usr/libexec/ember-os-slot',
        './usr/libexec/ember-install-slot', './usr/libexec/ember-stage-ffs'}
    retired = {RC, OLD_PKG_RC} if role == 'etc' else {PROGRAM}
    listing, seen = [], set()
    stamp = None
    with tarfile.open(source, 'r|xz') as old, tarfile.open(output / f'{role}.tar.xz', 'w|xz') as new:
        for member in old:
            name = member.name
            if name in seen or name in retired or (name in additions and
                    (additions[name] is not None or not member.isdir())):
                raise SDKError(f'unexpected or duplicate stock {role} member: {name}')
            seen.add(name)
            listing.append(name)
            body = old.extractfile(member) if member.isfile() else None
            if name in required and body is None:
                raise SDKError(f'expected a regular file: {name}')
            if name == obsolete:
                data = body.read()
                if not {n.encode() for n in retired} <= set(data.splitlines()):
                    raise SDKError(f'{name} omits a former updater')
                body = io.BytesIO(data)
            if name == manifest:
                stamp = member.mtime
                content = body.read()
                check_manifest_records(content)
                if not content.endswith(b'\n'):
                    raise SDKError(f'incomplete stock manifest: {name}')
                lines = content.splitlines(keepends=True)
                paths = {line.split()[0].decode('ascii'): line for line in lines if line.strip()}
                if retired.intersection(paths):
                    raise SDKError(f'{name} still selects a former updater')
                for path, data in additions.items():
                    if path not in paths:
                        lines.append(mtree(path, data))
                    elif data is not None or b'type=dir' not in paths[path].split():
                        raise SDKError(f'conflicting manifest entry: {path}')
                updated = b''.join(lines)
                member.size = len(updated)
                body = io.BytesIO(updated)
            new.addfile(member, body)
        missing = required - seen
        if missing:
            raise SDKError(f'missing stock {role} members: {sorted(missing)}')
        for name, data in additions.items():
            if name not in seen:
                add_member(new, name, data, stamp)
                listing.append(name)
    (output / f'{role}.list').write_text('\n'.join(sorted(listing)) + '\n', encoding='utf-8')


def prepare(args):
    slots = read_json(args.description)['slots']
    profile = slots['profile']
    if (not isinstance(profile, str) or not _SLUG.fullmatch(profile) or
            slots.get('adapter') != 'uboot-ffs' or slots.get('storage') != 'ffs' or
            slots.get('roots') != {'A': 'netbsd-root', 'B': 'ember-root-b'} or slots.get('esp') != 'EFI' or
            not 0 < args.generation <= MAX_GENERATION):
        raise SDKError('expected a legacy-compatible U-Boot FFS profile and a positive generation')
    if args.output.exists():
        raise SDKError(f'output already exists: {args.output}')
    with tempfile.TemporaryDirectory(dir=args.output.parent) as temporary:
        output = Path(temporary) / 'sets'
        output.mkdir()
        compose(args.etc, 'etc', {RC: STUB}, output)
        seal = json_bytes(dict(profile=slots, generation=args.generation, etc_sha256=sha256_file(output / 'etc.tar.xz')))
        additions = {SHARE: None, HANDOFF: None, HANDOFF + '/' + profile + '.json': seal, PROGRAM: STUB}
        compose(args.base, 'base', additions, output)
        metalog = args.metalog.read_bytes()
        check_manifest_records(metalog)
        paths = {line.split()[0]: line for line in metalog.splitlines() if line.strip()}
        if not metalog.endswith(b'\n'):
            raise SDKError('incomplete source METALOG')
        with (output / 'METALOG').open('wb') as f:
            f.write(metalog)
            for name, data in {RC: STUB, **additions}.items():
                line = paths.get(name.encode())
                if line is None:
                    f.write(mtree(name, data))
                elif data is not None or b'type=dir' not in line.split():
                    raise SDKError(f'conflicting source METALOG entry: {name}')
        output.rename(args.output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('base', 'etc', 'metalog', 'description', 'output'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--generation', required=True, type=int)
    try:
        prepare(parser.parse_args())
    except (OSError, SDKError, tarfile.TarError, KeyError) as exc:
        parser.exit(1, f'{parser.prog}: {exc}\n')


if __name__ == '__main__':
    main()
