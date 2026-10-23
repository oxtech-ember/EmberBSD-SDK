# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Small real archive and patch inputs for preparation and builder contracts."""
import hashlib
import io
from pathlib import Path
import tarfile

from emberbsd_sdk.libedit import RECIPE


def recipe_fixture(ports, archive, *, bad_member=None, bad_type=None):
    root = Path(ports, RECIPE)
    (root / 'patches').mkdir(parents=True)
    distname = 'libedit-20260512-3.1'
    (root / 'Makefile').write_text(f'DISTNAME=\t{distname}\nMASTER_SITES=\thttps://thrysoee.dk/editline/\n')
    with tarfile.open(archive, 'w:gz') as packed:
        for name in ['src/input' + str(n) for n in range(7)] + ['doc/editline.3']:
            info = tarfile.TarInfo(distname + '/' + name)
            data = b'original\n'; info.size = len(data); info.mode = 0o644
            packed.addfile(info, io.BytesIO(data))
        if bad_member:
            info = tarfile.TarInfo(bad_member); info.size = 1
            if bad_type is not None:
                info.type = bad_type; info.linkname = '../../outside'; info.size = 0
            packed.addfile(info, io.BytesIO(b'x'))
    data = Path(archive).read_bytes()
    rows = [f'BLAKE2s ({distname}.tar.gz) = {hashlib.blake2s(data).hexdigest()}',
            f'SHA512 ({distname}.tar.gz) = {hashlib.sha512(data).hexdigest()}',
            f'Size ({distname}.tar.gz) = {len(data)} bytes']
    for n in range(7):
        patch = f'--- src/input{n}\n+++ src/input{n}\n@@ -1 +1 @@\n-original\n+patched\n'.encode()
        name = 'patch-input' + str(n)
        (root / 'patches' / name).write_bytes(b'$NetBSD: fixture $\n\n' + patch)
        rows.append(f'SHA1 ({name}) = {hashlib.sha1(b"\n" + patch).hexdigest()}')
    (root / 'distinfo').write_text('\n'.join(rows) + '\n')


def installation_source(machine, *, arguments='a', control='c'):
    """Emit a real ELF dynamic fixture and the native installed link layout."""
    import struct
    from test_core import dynamic_elf
    library = bytearray(dynamic_elf())
    struct.pack_into('<H', library, 18, machine)
    old, new = b'libfixture.so.1', b'libedit.so.3'
    library = bytes(library).replace(old, new + b'\0' * (len(old) - len(new)))
    code = f'''prepared = Path(next(v.split('=', 1)[1] for v in {arguments} if v.startswith('EMBER_LIBEDIT_SOURCE=')))
for name in ('usr/include/readline', 'usr/share/emberbsd/ports', 'usr/lib', 'lib'):
    (dest / name).mkdir(parents=True, exist_ok=True)
(dest / 'usr/include/histedit.h').write_text('/* histedit fixture */')
(dest / 'usr/include/readline/readline.h').write_text('/* readline fixture */')
(dest / 'usr/share/emberbsd/ports/editline.distinfo').write_bytes((prepared / 'ports.distinfo').read_bytes())
(dest / 'lib/libedit.so.3.2').write_bytes({library!r})
for name, target in (('usr/include/readline/history.h', 'readline.h'), ('lib/libedit.so.3', 'libedit.so.3.2'), ('usr/lib/libedit.so', '../../lib/libedit.so.3.2')):
    path = dest / name
    if path.is_symlink(): path.unlink()
    path.symlink_to(target)
if {control}.get('missing_libedit'): (dest / 'lib/libedit.so.3.2').unlink()
if {control}.get('missing_histedit'): (dest / 'usr/include/histedit.h').unlink()
if {control}.get('missing_readline'): (dest / 'usr/include/readline/readline.h').unlink()
if {control}.get('missing_editline_marker'): (dest / 'usr/share/emberbsd/ports/editline.distinfo').unlink()
if {control}.get('wrong_editline_marker'): (dest / 'usr/share/emberbsd/ports/editline.distinfo').write_text('wrong')
if {control}.get('wrong_libedit_soname'):
    path = dest / 'lib/libedit.so.3.2'; path.write_bytes(path.read_bytes().replace(b'libedit.so.3', b'libedit.so.0'))
if {control}.get('extra_libedit_provider'):
    path = dest / 'usr/pkg/lib/libedit.so.0'; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes({library!r})
'''
    return code
