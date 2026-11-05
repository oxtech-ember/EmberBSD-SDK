# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""pkg_tools installation into native prefixes or non-executing target roots."""

import os
from pathlib import Path, PurePosixPath
import subprocess
import tarfile
import tempfile

from .binary_release import _arch, _package_payload
from .errors import SDKError
from .paths import open_regular, verify_stable
from . import package_relocation
from .package_cache import directory_lock
from .release_signing import read_bytes

PORTABLE_PREFIX = package_relocation.INDEPENDENT


def install_paths(config):
    prefix, database = Path(config['prefix']), Path(config['pkgdb'])
    if not prefix.is_absolute() or not database.is_absolute():
        raise SDKError('package prefix and database must be absolute')
    if config.get('target'):
        root = Path(config['sysroot'])
        if not root.is_absolute() or root == Path('/'):
            raise SDKError('target sysroot must be an explicit non-root absolute directory')
        return root / str(prefix).lstrip('/'), root / str(database).lstrip('/')
    if prefix == Path('/'):
        raise SDKError('native SDK prefix must not be /')
    return prefix, database


def _relative(name):
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or not path.parts:
        raise SDKError(f'package path escapes prefix: {name}')
    return path


def inspect_install_paths(package, config):
    """Inspect signed archives before pkg_add; never unpack target files ourselves."""
    prefix, _ = install_paths(config)
    root = prefix.resolve()
    stream, before = open_regular(package)
    with stream, tarfile.open(fileobj=_package_payload(stream), mode='r|*') as archive:
        original_prefix = None
        relocation = None
        package_name = None
        portable = False
        hooks = False
        deferred_hooks = False
        links = set()
        for member in archive:
            name = member.name.removeprefix('./')
            path = _relative(name)
            if name.startswith('+'):
                if name == package_relocation.READY:
                    raise SDKError('package contains client-owned relocation state')
                if name == '+CONTENTS':
                    if member.size > 8 * 1024 * 1024:
                        raise SDKError('oversized package packing list')
                    lines = archive.extractfile(member).read().decode('utf-8').splitlines()
                    relocation = package_relocation.declaration(lines)
                    deferred_hooks = package_relocation.HOOKS in lines
                    portable = (PORTABLE_PREFIX in lines or relocation is not None) and not config.get('target')
                    if portable:
                        names = [line[6:] for line in lines if line.startswith('@name ')]
                        if len(names) != 1 or len(_relative(names[0]).parts) != 1:
                            raise SDKError('portable package requires one package name')
                        package_name = names[0]
                    if relocation is not None:
                        if config.get('target') or PORTABLE_PREFIX in lines:
                            raise SDKError('text relocation is a distinct native package contract')
                        from .bootstrap_bundle import relocate_text
                        relocate_text(b'', relocation[0].encode(), config['prefix'].encode())
                    if portable and any(line.startswith(('@exec ', '@unexec ')) for line in lines):
                        raise SDKError('prefix-independent package contains installation commands')
                    for line in lines:
                        if line.startswith('@cwd '):
                            value = line[5:]
                            if original_prefix is None:
                                original_prefix = value
                                if relocation is not None and relocation[0] != value:
                                    raise SDKError('package relocation source differs from its prefix')
                                if value != config['prefix'] and not portable:
                                    raise SDKError('package prefix differs; this package has no qualified relocation')
                            elif value != original_prefix:
                                raise SDKError('package changes directory outside its selected prefix')
                        elif line and not line.startswith('@'):
                            _relative(line)
                        elif line.startswith(('@pkgdir ', '@dirrm ')):
                            _relative(line.split(' ', 1)[1])
                elif name in ('+INSTALL', '+DEINSTALL'):
                    if not member.isfile() or member.size > 8 * 1024 * 1024:
                        raise SDKError('invalid package installation script')
                    hooks = True
                continue
            destination = prefix / str(path)
            if not destination.resolve().is_relative_to(root):
                raise SDKError(f'existing package path escapes prefix: {name}')
            if any(parent in links for parent in path.parents):
                raise SDKError(f'package writes through an archive link: {name}')
            if member.issym() or member.islnk():
                target = PurePosixPath(member.linkname)
                if target.is_absolute():
                    raise SDKError(f'absolute package link escapes install root: {name}')
                linked = (destination.parent if member.issym() else prefix) / str(target)
                if not linked.resolve().is_relative_to(root):
                    raise SDKError(f'package link escapes prefix: {name}')
                links.add(path)
            elif not (member.isfile() or member.isdir()):
                raise SDKError(f'unsupported special file in SDK package: {name}')
        if portable and hooks and not deferred_hooks:
            raise SDKError('prefix-independent package contains installation scripts')
        if relocation is not None and original_prefix is None:
            raise SDKError('relocatable package has no original prefix')
    verify_stable(package, before)
    return (package_name, relocation, deferred_hooks) if portable else None


class PackageInstaller:
    def __init__(self, crypto, trust):
        self.crypto, self.trust = crypto, Path(trust)
        self._checked = {}

    def inventory(self, config):
        """Read installed identities in one query, without running pending hooks."""
        from .binary_catalog import parse_summary, scalar
        from .package_selection import _parse_root
        _, database = install_paths(config)
        if not database.exists():
            return []
        result = subprocess.run([self.crypto.prefix.tool('pkg_info'), '-K', str(database), '-X', '-a'],
            env={'PATH': self.crypto.prefix.path, 'LC_ALL': 'C'}, capture_output=True, timeout=60)
        if result.returncode and not (result.returncode == 1 and not result.stdout and not result.stderr):
            raise SDKError('cannot read installed package inventory: ' + result.stderr.decode('utf-8', 'replace'))
        packages = []
        for record in parse_summary(result.stdout):
            root = scalar(record, 'PKGPATH') if 'PKGPATH' in record else None
            if root is not None:
                root = _parse_root(root + ('@native' if config.get('host') is not None else ''),
                                   'installed PKGPATH')
            packages.append(dict(pkgname=scalar(record, 'PKGNAME'), root=root))
        return sorted(packages, key=lambda package: package['pkgname'])

    def installed(self, config, *, recover=True):
        prefix, database = install_paths(config)
        if not database.exists():
            return set()
        if recover and not config.get('target') and list(package_relocation.pending(prefix, database)):
            with directory_lock(database.parent / (database.name + '.sdk-lock')):
                for name in package_relocation.pending(prefix, database):
                    package_relocation.finish(prefix, database, name)
        result = subprocess.run([self.crypto.prefix.tool('pkg_info'), '-K', str(database), '-e', '*'],
            env={'PATH': self.crypto.prefix.path, 'LC_ALL': 'C'}, capture_output=True, timeout=60)
        if result.returncode not in (0, 1):
            raise SDKError('cannot read installed package database: ' + result.stderr.decode('utf-8', 'replace'))
        return set(result.stdout.decode('utf-8').splitlines())

    def configure(self, config):
        """Finish deferred native package hooks after the selected batch is installed.

        The caller holds the selected repository's SDK pkgdb lock.
        """
        if config.get('target'):
            return
        prefix, database = install_paths(config)
        for name in package_relocation.pending(prefix, database):
            package_relocation.finish(prefix, database, name)

    def check(self, package, config):
        self.crypto.verify_package(package, self.trust)
        result = inspect_install_paths(package, config)
        self._checked[str(Path(package).absolute())] = result
        return result

    def check_updates(self, packages, archives, config):
        from .package_updates import check_updates
        _, database = install_paths(config)
        check_updates(self.crypto, database, packages, archives, self.installed(config, recover=False))

    def install(self, package, config, *, checked=False, configure=True):
        if not checked:
            self.check(package, config)
        key = str(Path(package).absolute())
        selected = (self._checked[key] if key in self._checked else inspect_install_paths(package, config))
        prefix, database = install_paths(config)
        database.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='ember-install-') as directory:
            anchor = Path(directory) / 'root.pem'
            anchor.write_bytes(read_bytes(self.trust, 128 * 1024))
            conf = Path(directory) / 'pkg_install.conf'
            conf.write_text(f'CERTIFICATE_ANCHOR_PKGS={anchor}\nVERIFIED_INSTALLATION=always\n'
                            f'GPG_KEYRING_VERIFY={os.devnull}\nPKG_PATH=\n')
            args = [self.crypto.prefix.tool('pkg_add'), '-C', str(conf), '-U',
                    '-K', config['pkgdb'], '-p', config['prefix']]
            if config.get('target'):
                args += ['-P', config['sysroot'], '-I', '-m',
                         'NetBSD/' + _arch(config['target']['arch']) + ' ' + config['os_version']]
            elif selected is not None and selected[2]:
                args.append('-I')
            args.append(str(Path(package).absolute()))
            result = subprocess.run(args, env={'PATH': self.crypto.prefix.path, 'LC_ALL': 'C',
                                              'PKG_PATH': ''}, capture_output=True)
            if result.returncode:
                raise SDKError('pkg_add failed: ' + result.stderr[-4096:].decode('utf-8', 'replace'))
            if selected is not None:
                name, relocation, deferred_hooks = selected
                if relocation is not None and (relocation[0] != str(prefix) or deferred_hooks):
                    (database / name / package_relocation.READY).write_text(relocation[0] + '\n')
                package_relocation.finish(prefix, database, name, configure=configure)
