# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Small archive and clean Ports recipe for the libelf source contract."""

import difflib
import hashlib
import io
import json
from pathlib import Path
import tarfile

from emberbsd_sdk.libelf import (BASE_PREFIX, BASE_REPOSITORY, BASE_TREE_IDS,
                                 PACKAGING_OVERRIDES, RECIPE,
                                 SOURCE_HEADER_OVERRIDES)

DISTNAME = 'elftoolchain-0.7.1'
URL = ('https://downloads.sourceforge.net/project/elftoolchain/'
       'Sources/elftoolchain-0.7.1/elftoolchain-0.7.1.tar.bz2')
MASTER = URL[:-len(DISTNAME + '.tar.bz2')]


def _unified(path, before, after):
    return ''.join(difflib.unified_diff(before.decode().splitlines(keepends=True),
                                        after.decode().splitlines(keepends=True),
                                        fromfile=path + '.orig', tofile=path,
                                        n=3)).encode()


def _removed(path, data):
    return (f'--- {path}\n+++ /dev/null\n'.encode() +
            f'@@ -1,{len(data.splitlines())} +0,0 @@\n'.encode() +
            b''.join(b'-' + line for line in data.splitlines(keepends=True)))


def _added(path, data):
    return (f'--- /dev/null\n+++ {path}\n'.encode() +
            f'@@ -0,0 +1,{len(data.splitlines())} @@\n'.encode() +
            b''.join(b'+' + line for line in data.splitlines(keepends=True)))


def recipe_fixture(ports, archive, *, bad_member=None, bad_type=None,
                   source_header_overrides=True):
    recipe = Path(ports, RECIPE)
    (recipe / 'patches').mkdir(parents=True)
    (recipe / 'files').mkdir()
    makefile = f'''DISTNAME=\t{DISTNAME}
PKGREVISION=\t1
MASTER_SITES=\t{MASTER}
EXTRACT_SUFX=\t.tar.bz2

.include "../../mk/bsd.prefs.mk"
.if (${'{'}OPSYS{'}'} == "NetBSD" && (${ '{' }EMBERBSD_PORTS:Uno:tl{'}'} == "yes" || \\
    exists(${ '{' }.PARSEDIR{'}'}/../../EMBERBSD-PKGSRC-REVISION))) || \\
    exists(${ '{' }TOOLS_CROSS_DESTDIR{'}'}/usr/share/emberbsd/ports/elftoolchain.distinfo)
PKG_FAIL_REASON+= "EmberBSD base provides libelf. Rebuild the base from this Ports source recipe."
.endif
.include "../../mk/bsd.pkg.mk"
'''.replace('${ {', '${').replace('{ }', '}')
    (recipe / 'Makefile').write_text(makefile)

    original = {
        'common/Makefile': b'common upstream\n',
        'common/elf.h': b'elf header upstream\n',
        'common/sys/elfconstants.m4': b'elf constants upstream\n',
        'common/sys/elfdefinitions.m4': b'elf definitions upstream\n',
        'libelf/Makefile': b'libelf make upstream\n',
        'libelf/Version.map': b'version map upstream\n',
        'libelf/libelf.c': b'libelf source upstream\n',
        'common/native-elf-format': b'legacy format\n',
        'common/os.Linux.mk': b'linux source\n',
    }
    output = {
        'common/Makefile': b'common patched\n',
        'common/elf.h': b'elf header patched\n',
        'common/sys/elfconstants.m4': b"define(`STN_UNDEF', 0)\n",
        'common/sys/elfdefinitions.m4': b'elf definitions patched\n',
        'common/sys/generated.m4': b'new generated source\n',
        'libelf/Makefile': b'libelf ABI2 Makefile\n',
        'libelf/Version.map': b'ELF_1.0 { global: elf_getversion; };\n',
        'libelf/libelf.c': b'libelf source patched\n',
    }
    with tarfile.open(archive, 'w:bz2') as packed:
        for name, data in original.items():
            info = tarfile.TarInfo(DISTNAME + '/' + name)
            info.size = len(data); info.mode = 0o644
            packed.addfile(info, io.BytesIO(data))
        if bad_member is not None:
            info = tarfile.TarInfo(bad_member)
            if bad_type is None:
                extra = b'bad member\n'
                info.size = len(extra)
                packed.addfile(info, io.BytesIO(extra))
            else:
                info.type = bad_type
                info.linkname = '../../outside'
                info.size = 0
                packed.addfile(info)

    patches = {
        'patch-00-bsd-libelf-common': (
            _unified('common/Makefile', original['common/Makefile'], output['common/Makefile']) +
            _unified('common/elf.h', original['common/elf.h'], output['common/elf.h']) +
            _unified('common/sys/elfconstants.m4', original['common/sys/elfconstants.m4'], output['common/sys/elfconstants.m4']) +
            _unified('common/sys/elfdefinitions.m4', original['common/sys/elfdefinitions.m4'], output['common/sys/elfdefinitions.m4']) +
            _removed('common/native-elf-format', original['common/native-elf-format']) +
            _removed('common/os.Linux.mk', original['common/os.Linux.mk']) +
            _added('common/sys/generated.m4', output['common/sys/generated.m4'])),
        'patch-01-libelf-version-map': _unified('libelf/Version.map', original['libelf/Version.map'], output['libelf/Version.map']),
        'patch-libelf_Makefile': _unified('libelf/Makefile', original['libelf/Makefile'], output['libelf/Makefile']),
        'patch-libelf_source': _unified('libelf/libelf.c', original['libelf/libelf.c'], output['libelf/libelf.c']),
    }
    archive_bytes = Path(archive).read_bytes()
    overridden = set(PACKAGING_OVERRIDES)
    if source_header_overrides:
        overridden.update(SOURCE_HEADER_OVERRIDES)
    files = {}
    for name, data in output.items():
        if name in overridden:
            base = original[name]
            files[name] = dict(mode=0o644, size=len(base), sha256=hashlib.sha256(base).hexdigest())
        else:
            files[name] = dict(mode=0o644, size=len(data), sha256=hashlib.sha256(data).hexdigest())
    overrides = {}
    for name in sorted(overridden):
        data = output[name]
        overrides[name] = dict(mode=0o644, size=len(data), sha256=hashlib.sha256(data).hexdigest(),
                               base_sha256=files[name]['sha256'], reason='portable native ABI contract')
    manifest = {
        'format': 'emberbsd-elftoolchain-base-source', 'version': 1,
        'upstream': {'distname': DISTNAME, 'url': URL, 'svn_revision': 3417,
                     'archive': {'size': len(archive_bytes),
                                 'sha256': hashlib.sha256(archive_bytes).hexdigest(),
                                 'sha512': hashlib.sha512(archive_bytes).hexdigest(),
                                 'blake2s': hashlib.blake2s(archive_bytes).hexdigest()}},
        'base': {'repository': BASE_REPOSITORY, 'commit': 'c5d396f5698b68e956b29ee5b256bc7877e6c517',
                 'prefix': BASE_PREFIX, 'trees': BASE_TREE_IDS},
        'files': files, 'packaging_overrides': overrides,
    }
    (recipe / 'files/base-source.json').write_text(json.dumps(manifest, indent=2) + '\n')
    distinfo = [f'BLAKE2s ({DISTNAME}.tar.bz2) = {manifest["upstream"]["archive"]["blake2s"]}',
                f'SHA512 ({DISTNAME}.tar.bz2) = {manifest["upstream"]["archive"]["sha512"]}',
                f'Size ({DISTNAME}.tar.bz2) = {len(archive_bytes)} bytes']
    for name, data in sorted(patches.items()):
        path = recipe / 'patches' / name
        path.write_bytes(b'$NetBSD: fixture $\n' + data)
        filtered = b''.join(line for line in path.read_bytes().splitlines(keepends=True)
                            if b'$NetBSD' not in line)
        distinfo.append(f'SHA1 ({name}) = {hashlib.sha1(filtered).hexdigest()}')
    (recipe / 'distinfo').write_text('$NetBSD: distinfo fixture $\n' + '\n'.join(distinfo) + '\n')
