#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Install signed source tools in a disposable bootstrap and exercise them.

Run with that bootstrap's Python. The caller can deny all producer/checkout
reads to prove the delivery boundary; this does not qualify a compiler.
"""
import argparse
import hashlib
from importlib import resources
import os
from pathlib import Path
import subprocess
import sys
import tempfile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--prefix', required=True, type=Path)
parser.add_argument('--packages', required=True, type=Path)
args = parser.parse_args()
root = args.prefix.resolve()
sys.path.insert(0, str(root / 'share/emberbsd/ember-sdk.pyz'))
from emberbsd_sdk.host_prefix import load_host_prefix
from emberbsd_sdk.package_install import PackageInstaller
from emberbsd_sdk.release_signing import ReleaseCrypto

crypto = ReleaseCrypto(load_host_prefix(str(root), required_roles=('openssl', 'pkg_admin', 'pkg_add', 'pkg_info')))
config = dict(prefix=str(root), pkgdb=str(root / 'pkgdb'),
              host=dict(os='Darwin', arch='arm64', abi='macos-27.0', runtime='libSystem'))
with resources.as_file(resources.files('emberbsd_sdk').joinpath('emberbsd-release-root.pem')) as trust:
    installer = PackageInstaller(crypto, trust)
    installed = installer.installed(config)
    for package in sorted(args.packages.glob('*.tgz')):
        if package.name.removesuffix('.tgz') not in installed:
            installer.install(package, config)
    installed = installer.installed(config)
    for name in ('digest-', 'nawk-', 'nbsed-', 'nbpatch-', 'mktools-', 'cwrappers-'):
        assert any(item.startswith(name) for item in installed), name

environment = dict(PATH=str(root / 'bin') + ':' + str(root / 'sbin') + ':/usr/bin:/bin', LC_ALL='C')
with tempfile.TemporaryDirectory(prefix='source-tools-', dir=root.parent) as directory:
    work = Path(directory)
    def run(tool, *arguments, data=None, extra=None):
        result = subprocess.run([str(root / tool), *map(str, arguments)], input=data,
                              env=dict(environment, **(extra or {})), cwd=work,
                              capture_output=True)
        if result.returncode:
            raise RuntimeError(tool + ': ' + result.stderr.decode('utf-8', 'replace'))
        return result.stdout
    source = work / 'source.txt'
    source.write_bytes(b'Ember 7\nBSD 5\n')
    assert run('bin/digest', 'sha256', source).decode().split()[-1] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert run('bin/nawk', '{s += $2} END {print s}', source) == b'12\n'
    assert run('bin/nbsed', 's/Ember/Creator/', source) == b'Creator 7\nBSD 5\n'
    run('bin/nbpatch', '-p0', data=b'--- source.txt\n+++ source.txt\n@@ -1,2 +1,2 @@\n-Ember 7\n+Creator 7\n BSD 5\n')
    assert source.read_bytes() == b'Creator 7\nBSD 5\n'
    link = work / 'links/source.txt'
    run('libexec/mktools/mk-buildlink-symlinks', data=f'{link} -> {source}\n'.encode())
    assert link.is_symlink() and link.read_bytes() == source.read_bytes()
    wrappers = work / 'wrappers'
    wrappers.mkdir()
    (wrappers / 'cc').write_text('exec=/not-executed/compiler\nexec_path=/not-executed/compiler\nworklog=' +
                               str(work / 'wrapper.log') + '\n')
    argv = run('libexec/cwrappers/cc-wrapper', '-c', 'source.c', '-o', 'source.o',
               extra=dict(CWRAPPERS_CONFIG_DIR=str(wrappers), CWRAPPERS_EMIT_ARGV='1')).split(b'\0')
    assert argv == [b'/not-executed/compiler', b'-c', b'source.c', b'-o', b'source.o', b''], argv
    for name in ('nawk-', 'nbsed-'):
        database = next((root / 'pkgdb').glob(name + '*'))
        assert (database / '+PRESERVE').is_file()
    notices = (root / 'share/doc/nbpatch/COPYING').read_text()
    assert all(name in notices for name in ('Larry Wall', 'Free Software Foundation', 'Regents'))
    print(run('sbin/pkg_admin', '-K', root / 'pkgdb', 'check').decode(), end='')
print('PASS: signed source tools, runtime operations, notices and package ownership')
