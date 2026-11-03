#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Qualify a simple Darwin package for installation into any SDK prefix.

Whole packages only. Packages with hooks or remaining embedded paths require
their owner's relocation support and are deliberately rejected here.
"""
import argparse
from pathlib import Path
import platform
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk.bootstrap_bundle import unpack_package, make_relative, _replace_file
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.host_prefix import load_host_prefix
from emberbsd_sdk.package_install import PORTABLE_PREFIX
from emberbsd_sdk.release_signing import ReleaseCrypto
from emberbsd_sdk.binary_release import _matches


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('package', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--host-prefix', required=True)
    parser.add_argument('--dependency-prefix', type=Path, required=True)
    parser.add_argument('--bootstrap-install-name-tool', type=Path, required=True)
    parser.add_argument('--codesign', type=Path, required=True)
    args = parser.parse_args()
    try:
        if platform.system() != 'Darwin' or args.output.exists():
            raise SDKError('select a new output on a Darwin producer')
        for tool in (args.bootstrap_install_name_tool, args.codesign):
            if not tool.is_absolute() or not tool.is_file():
                raise SDKError('select explicit absolute packaging tools')
        crypto = ReleaseCrypto(load_host_prefix(args.host_prefix, required_roles=('pkg_create', 'pkg_admin')))
        with tempfile.TemporaryDirectory(prefix='ember-portable-') as directory:
            root = Path(directory) / 'payload'
            root.mkdir()
            name, prefix = unpack_package(args.package, root)
            database = root / 'pkgdb' / name
            if any((database / script).exists() for script in ('+INSTALL', '+DEINSTALL')):
                raise SDKError('package has installation scripts')
            lines = (database / '+CONTENTS').read_text().splitlines()
            if any(line.startswith(('@exec ', '@unexec ')) for line in lines):
                raise SDKError('package has installation commands')
            # pkg_add's REQUIRES check uses absolute paths, even with -p.
            # Replace qualified prefix-local library requirements with the
            # existing pkgdep contracts; keep all system requirements intact.
            dependencies = [line[8:] for line in lines if line.startswith('@pkgdep ')]
            providers = set()
            for contents in (args.dependency_prefix / 'pkgdb').glob('*/+CONTENTS'):
                if any(_matches(crypto, pattern, contents.parent.name) for pattern in dependencies):
                    providers.update(line for line in contents.read_text().splitlines()
                                     if line and not line.startswith(('@', '+')))
            make_relative(root, prefix, dict(os='Darwin', cpu=platform.machine()),
                          args.bootstrap_install_name_tool, args.codesign,
                          dependency_root=args.dependency_prefix, dependency_files=providers)
            info = database / '+BUILD_INFO'
            updated = []
            for line in info.read_text().splitlines():
                if line.startswith('PROVIDES=') and prefix + '/' in line:
                    raise SDKError('portable library providers require package-owner relocation support')
                if line.startswith('REQUIRES='):
                    required = []
                    for value in line[9:].split():
                        if value.startswith(prefix + '/'):
                            relative = value[len(prefix) + 1:]
                            supplied = root / relative
                            owner = root
                            if not supplied.is_file() and relative in providers:
                                supplied = args.dependency_prefix / relative
                                owner = args.dependency_prefix
                            if not supplied.is_file() or not supplied.resolve().is_relative_to(owner.resolve()):
                                raise SDKError('local library has no declared package dependency: ' + relative)
                        elif value.startswith(('/usr/lib/', '/System/Library/')):
                            required.append(value)
                        else:
                            raise SDKError('external package requirement: ' + value)
                    if not required:
                        continue
                    line = 'REQUIRES=' + ' '.join(required)
                updated.append(line)
            _replace_file(info, ('\n'.join(updated) + '\n').encode())
            for path in root.rglob('*'):
                if path.is_file() and not path.is_symlink() and not path.is_relative_to(root / 'pkgdb'):
                    # Only a producer check; clients trust the signed qualification.
                    with path.open('rb') as source:
                        tail = b''
                        while chunk := source.read(1024 * 1024):
                            chunk = tail + chunk
                            if prefix.encode() in chunk:
                                raise SDKError('payload still embeds its producer prefix: ' + str(path.relative_to(root)))
                            tail = chunk[-len(prefix):]
            packing, ignored = [PORTABLE_PREFIX], False
            for line in lines:
                if ignored:
                    ignored = False
                elif line == '@ignore':
                    ignored = True
                elif not line.startswith(('@cwd ', '@comment MD5:', '@comment Symlink:')):
                    packing.append(line)
            plist = Path(directory) / 'PLIST'
            plist.write_text('\n'.join(packing) + '\n')
            command = ['-F', 'gzip', '-f', plist, '-p', root, '-I', prefix]
            for flag, member in (('-c', '+COMMENT'), ('-d', '+DESC'), ('-B', '+BUILD_INFO'),
                                 ('-b', '+BUILD_VERSION'), ('-s', '+SIZE_PKG'), ('-S', '+SIZE_ALL'),
                                 ('-n', '+PRESERVE'), ('-D', '+DISPLAY')):
                if (database / member).exists():
                    command += [flag, database / member]
            crypto.run_package('pkg_create', [*command, args.output.absolute()])
        print(args.output)
        return 0
    except (SDKError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        print('package-portable: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
