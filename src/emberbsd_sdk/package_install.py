# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""pkg_tools installation into native prefixes or non-executing target roots."""

import json
import os
import platform
from pathlib import Path, PurePosixPath
import posixpath
import subprocess
import tarfile
import tempfile

from .binary_release import _arch, _package_payload
from .errors import SDKError
from .host_identity import native_cpu
from .paths import open_regular, verify_stable
from . import package_relocation
from .package_cache import directory_lock
from .release_signing import read_bytes

PORTABLE_PREFIX = package_relocation.INDEPENDENT


def install_mode(config):
    """Execution policy is distinct from the package's catalog identity."""
    mode = config.get('install_mode', 'sysroot' if config.get('target') else 'native')
    if (mode not in ('native', 'sysroot', 'native-stage') or
            (mode == 'sysroot' and not config.get('target')) or
            (mode == 'native-stage' and (not config.get('host') or config.get('target'))) or
            ('stage_root' in config and mode != 'native-stage') or
            (mode == 'native-stage' and 'sysroot' in config)):
        raise SDKError('invalid package install_mode')
    return mode


def target_sysroot(config):
    return install_mode(config) == 'sysroot'


def installation_root(config):
    """Return an explicit non-executing root, or None for a live prefix."""
    mode = install_mode(config)
    if mode == 'native':
        return None
    key = 'sysroot' if mode == 'sysroot' else 'stage_root'
    value = config.get(key)
    if not isinstance(value, str):
        raise SDKError(f'package installation requires absolute {key}')
    root = Path(value)
    if (not root.is_absolute() or '..' in root.parts or root.resolve() == Path('/')):
        raise SDKError(f'{key} must be an explicit non-root absolute directory')
    return root


def _native_target(config):
    from .targets import TARGETS
    target = config['target']
    definitions = [value for value in TARGETS.values() if _arch(value['arch']) == _arch(target['arch'])]
    release = platform.release()
    # pkg_tools os_version stays on the package ABI (11.0) while uname -r
    # can be a later kernel in the same major release (11.1). The ABI field
    # still has to be netbsd-<major>.
    if (platform.system() != 'NetBSD' or _arch(native_cpu()) != _arch(target['arch']) or
            len(definitions) != 1 or target['triplet'] != definitions[0]['triplet'] or
            str(config.get('os_version', '')).split('.', 1)[0] != release.split('.', 1)[0] or
            target['abi'] != 'netbsd-' + release.split('.', 1)[0]):
        raise SDKError('native target repository differs from this running OS/CPU/ABI')
    if 'sysroot' in config:
        raise SDKError('native target installation must not configure a sysroot')


def install_paths(config):
    prefix, database = Path(config['prefix']), Path(config['pkgdb'])
    if (not prefix.is_absolute() or not database.is_absolute() or
            '..' in prefix.parts or '..' in database.parts):
        raise SDKError('package prefix and database must be absolute normalized paths')
    root = installation_root(config)
    if root is not None:
        physical = (root / str(prefix).lstrip('/'), root / str(database).lstrip('/'))
        if any(not path.resolve().is_relative_to(root.resolve()) for path in physical):
            raise SDKError('package prefix or database escapes installation root')
        if install_mode(config) == 'native-stage':
            if prefix == Path('/'):
                raise SDKError('native SDK prefix must not be /')
            live = (prefix.resolve(), database.resolve())
            if any(path.resolve().is_relative_to(active) or active.is_relative_to(path.resolve())
                   for path in physical for active in live):
                raise SDKError('native stage must not overlap its live prefix or database')
        return physical
    if config.get('target'):
        _native_target(config)
    if prefix == Path('/'):
        raise SDKError('native SDK prefix must not be /')
    return prefix, database


def _relative(name):
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or not path.parts:
        raise SDKError(f'package path escapes prefix: {name}')
    return path


def _inside_prefix(target, original_prefix):
    """Absolute symlink target that stays inside the package's declared prefix."""
    path = PurePosixPath(target)
    return path.is_absolute() and '..' not in path.parts and path.is_relative_to(original_prefix)


def inspect_install_paths(package, config, *, visit=None):
    """Inspect before pkg_add; visit payload with its declared logical prefix."""
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
            # pkgsrc symlinks into its own prefix, for example
            # lib/pkgconfig/Qt6Svg.pc -> /usr/pkg/qt6/lib/pkgconfig/Qt6Svg.pc.
            # pkg_add replaces such a link; it is valid only inside the target
            # root, so the host must not resolve it when the root is a sysroot.
            prefix_link = member.issym() and not portable and relocation is None and original_prefix is not None
            replaced = (prefix_link and destination.is_symlink() and
                        _inside_prefix(os.readlink(destination), original_prefix))
            existing = destination.parent if replaced else destination
            if not existing.resolve().is_relative_to(root):
                raise SDKError(f'existing package path escapes prefix: {name}')
            if any(parent in links for parent in path.parents):
                raise SDKError(f'package writes through an archive link: {name}')
            if member.issym() or member.islnk():
                target = PurePosixPath(member.linkname)
                if target.is_absolute():
                    if not prefix_link:
                        raise SDKError(f'absolute package link cannot be relocated: {name}')
                    if not _inside_prefix(str(target), original_prefix):
                        raise SDKError(f'absolute package link escapes install root: {name}')
                    parent = posixpath.join(original_prefix, str(path.parent))
                    target = PurePosixPath(posixpath.relpath(str(target), parent))
                linked = (destination.parent if member.issym() else prefix) / str(target)
                if not linked.resolve().is_relative_to(root):
                    raise SDKError(f'package link escapes prefix: {name}')
                links.add(path)
            elif not (member.isfile() or member.isdir()):
                raise SDKError(f'unsupported special file in SDK package: {name}')
            if visit is not None:
                visit(member, path, archive, original_prefix)
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
        if (recover and installation_root(config) is None and not config.get('target') and
                list(package_relocation.pending(prefix, database))):
            with directory_lock(database.parent / (database.name + '.sdk-lock')):
                for name in package_relocation.pending(prefix, database):
                    package_relocation.finish(prefix, database, name)
        result = subprocess.run([self.crypto.prefix.tool('pkg_info'), '-K', str(database), '-e', '*'],
            env={'PATH': self.crypto.prefix.path, 'LC_ALL': 'C'}, capture_output=True, timeout=60)
        if result.returncode not in (0, 1):
            raise SDKError('cannot read installed package database: ' + result.stderr.decode('utf-8', 'replace'))
        return set(result.stdout.decode('utf-8').splitlines())

    def _admin(self, config, tool, arguments):
        _, database = install_paths(config)
        if tool == 'pkg_delete':
            # Like pkg_add: unprefixed database and prefix, -P adds the sysroot.
            args = [self.crypto.prefix.tool(tool), '-K', config['pkgdb'], '-p', config['prefix']]
            root = installation_root(config)
            if root is not None:
                args += ['-P', str(root), '-D']
        else:
            args = [self.crypto.prefix.tool(tool), '-K', str(database)]
        hook_path = self.crypto.prefix.path
        if config.get('target') and not target_sysroot(config):
            hook_path += os.pathsep + os.defpath + os.pathsep + '/sbin:/usr/sbin'
        result = subprocess.run(args + arguments, env={'PATH': hook_path, 'LC_ALL': 'C'},
                                capture_output=True, timeout=600)
        if result.returncode:
            raise SDKError(f'{tool} failed: ' + result.stderr[-4096:].decode('utf-8', 'replace'))

    def remove(self, name, config):
        """Delete one installed package that a planned package conflicts with.

        The plan has proved a newer package of the same PKGBASE follows in the
        batch, so dependents keep working; ``-f`` skips their dependency check.
        """
        self._admin(config, 'pkg_delete', ['-f', name])

    def rebuild_tree(self, config):
        """Recompute +REQUIRED_BY after a conflict removal and its successor install."""
        self._admin(config, 'pkg_admin', ['rebuild-tree'])

    def configure(self, config):
        """Finish deferred native package hooks after the selected batch is installed.

        The caller holds the selected repository's SDK pkgdb lock.
        """
        if installation_root(config) is not None:
            return
        prefix, database = install_paths(config)
        for name in package_relocation.pending(prefix, database):
            package_relocation.finish(prefix, database, name)

    @staticmethod
    def _check_key(package, config):
        # A path inspected under one root/policy is not checked under another.
        return str(Path(package).absolute()), json.dumps(config, sort_keys=True)

    def check(self, package, config):
        self.crypto.verify_package(package, self.trust)
        result = inspect_install_paths(package, config)
        self._checked[self._check_key(package, config)] = result
        return result

    def check_updates(self, packages, archives, config):
        from .package_updates import check_updates
        check_updates(self.crypto, config, packages, archives, self.installed(config, recover=False))

    def install(self, package, config, *, checked=False, configure=True, automatic=False):
        if not checked:
            self.check(package, config)
        key = self._check_key(package, config)
        if key not in self._checked:
            self.check(package, config)
        selected = self._checked[key]
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
            root = installation_root(config)
            if root is not None:
                args += ['-P', str(root), '-I']
                if target_sysroot(config):
                    args += ['-m', 'NetBSD/' + _arch(config['target']['arch']) + ' ' + config['os_version']]
            elif selected is not None and selected[2]:
                args.append('-I')
            if automatic:
                args.append('-A')
            args.append(str(Path(package).absolute()))
            # Native package hooks use the running OS's base utilities. This
            # does not change explicitly selected crypto or pkg_tools paths.
            hook_path = self.crypto.prefix.path
            if config.get('target') and not target_sysroot(config):
                hook_path += os.pathsep + os.defpath + os.pathsep + '/sbin:/usr/sbin'
            environment = {'PATH': hook_path, 'LC_ALL': 'C', 'PKG_PATH': ''}
            if config.get('target') and not target_sysroot(config) and os.geteuid() == 0:
                # Packages are commonly produced by unprivileged pkgsrc builds,
                # which bake PKG_CREATE_USERGROUP=NO into their hooks. A root
                # install into the matching live target must create declared
                # service accounts; host prefixes and sysroots must not.
                environment['PKG_CREATE_USERGROUP'] = 'YES'
            result = subprocess.run(args, env=environment, capture_output=True)
            if result.returncode:
                raise SDKError('pkg_add failed: ' + result.stderr[-4096:].decode('utf-8', 'replace'))
            if selected is not None:
                name, relocation, deferred_hooks = selected
                if relocation is not None and (relocation[0] != config['prefix'] or deferred_hooks):
                    (database / name / package_relocation.READY).write_text(relocation[0] + '\n')
                package_relocation.finish(prefix, database, name,
                    configure=configure and root is None, logical_prefix=config['prefix'])
