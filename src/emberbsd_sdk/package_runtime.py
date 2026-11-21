# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Preflight pkg_tools writes into a recorded target LLVM runtime."""

from functools import lru_cache
import hashlib
import os
from pathlib import Path, PurePosixPath
import unicodedata

from . import application_toolchain, llvm_runtime, llvm_runtime_install
from .errors import SDKError
from .package_install import inspect_install_paths, install_paths, target_sysroot
from .release_signing import read_bytes
from .targets import TARGETS


def selected(config, staged=None):
    if not config.get('target') or not target_sysroot(config):
        return None
    if staged:
        root, manifest = staged
        record = manifest.get('runtime', {}).get('manifest')
        aliases = manifest['provenance'].get('base_cxx') == 'libcxx'
    else:
        root = Path(config['sysroot']).resolve()
        target = next((name for name, platform in TARGETS.items()
                       if platform['triplet'] == config['target']['triplet']), None)
        record, aliases = application_toolchain.recorded_clang_runtime(root, target=target)
    return RuntimeOverlay(config, root, record, aliases) if record is not None else None


def _fold(name):
    return unicodedata.normalize('NFD', name).casefold()


class RuntimeOverlay:
    def __init__(self, config, root, record, aliases):
        self.config = config
        self.root = root
        self.prefix = record['prefix']
        self.files = record['files']
        self.links = dict(record['symlinks'])
        if aliases:
            self.links.update(llvm_runtime_install.base_alias_plan(record))
        self.base_headers = aliases
        self.directories = llvm_runtime_install._directories(self.files.keys() | self.links.keys())
        self.resource_headers = llvm_runtime.runtime_paths('', record)['llvm-resource'] + '/include/'
        self.folded = {_fold(name): name for name in self.files.keys() | self.links.keys() | self.directories}
        # The pkgdb lock (or the private import tree) pins the destination while
        # comparing archive members. Cache shared parents only for this batch.
        self.parent = lru_cache(maxsize=4096)(self._parent)
        self.written = {}
        self.conflicts = {}
        self.parents_used = {}
        self.changed_links = {}

    def _spelling(self, name):
        """Match alternate case/normalization only when the filesystem aliases it."""
        canonical = self.folded.get(_fold(name))
        if canonical is not None and canonical != name:
            try:
                if os.path.samestat((self.root / canonical).lstat(), (self.root / name).lstat()):
                    return canonical
            except FileNotFoundError:
                pass
        return name

    def _parent(self, parent):
        actual = (self.root / parent).resolve()
        if not actual.is_relative_to(self.root):
            raise SDKError('package path escapes the selected sysroot: ' + str(parent))
        parts = actual.relative_to(self.root).parts
        # Preserve the spelling of an existing reserved ancestor when an archive
        # adds new descendants through an alternative directory spelling.
        for count in range(len(parts), 0, -1):
            directory = '/'.join(parts[:count])
            canonical = self._spelling(directory)
            if canonical != directory:
                return PurePosixPath(canonical, *parts[count:])
        return PurePosixPath(*parts)

    def _destination(self, path):
        return self._spelling((self.parent(path.parent) / path.name).as_posix())

    def visitor(self, package):
        """Collect affected members during the existing signed archive inspection."""
        written = self.written[package] = set()
        parents = self.parents_used[package] = set()
        changed_links = self.changed_links[package] = set()
        prefix = PurePosixPath(self.config['prefix'].lstrip('/'))

        def visit(member, path, archive, original_prefix):
            logical = prefix / path
            parents.add(logical.parent)
            name = self._destination(logical)
            if member.issym():
                existing = self.root / name
                if not existing.is_symlink() or os.readlink(existing) != member.linkname:
                    changed_links.add(_fold(name))
            if name in self.files:
                matches = False
                if member.isfile():
                    with archive.extractfile(member) as contents:
                        matches = hashlib.file_digest(contents, 'sha256').hexdigest() == self.files[name]
            elif name in self.links:
                matches = member.issym() and member.linkname == self.links[name]
            elif name in self.directories:
                matches = member.isdir()
            elif name.startswith(self.prefix + '/'):
                matches = member.isdir() or (member.isfile() and name.startswith(self.resource_headers))
            elif self.base_headers and name.startswith('usr/include/c++/'):
                matches = False
            else:
                return
            if not matches:
                self.conflicts.setdefault(package, name)
            if name in self.files or name in self.links:
                written.add(name)
        return visit

    def _owned(self, database, old):
        if Path(old).name != old or old in ('.', '..'):
            raise SDKError('invalid installed package name')
        directory = database / old
        if not directory.exists() and not directory.is_symlink():
            return  # A previous transaction already removed this conflict.
        lines = read_bytes(directory / '+CONTENTS', 8 * 1024 * 1024).decode().splitlines()
        prefix = PurePosixPath(self.config['prefix'].lstrip('/'))
        ignore = False
        for line in lines:
            if line == '@ignore':
                ignore = True
            elif line.startswith('@cwd '):
                prefix = PurePosixPath(line[5:].lstrip('/'))
            elif line and not line.startswith('@'):
                if not ignore:
                    name = self._destination(prefix / line)
                    if name in self.files or name in self.links:
                        yield name
                ignore = False

    def recheck(self, package, archive):
        # The private base has been published. Earlier pkg_add operations may
        # have populated a prefix directory or removed a conflicting owner.
        self.root = Path(self.config['sysroot']).resolve()
        self.parent.cache_clear()
        name = package['pkgname']
        self.conflicts.pop(name, None)
        inspect_install_paths(archive, self.config, visit=self.visitor(name))
        self.check([package])

    def check(self, packages):
        """Each operation must retain runtime files even if a later install fails."""
        _, database = install_paths(self.config)
        removed = set()
        earlier_links = set()
        for package in packages:
            if package['action'] == 'installed':
                continue
            name = package['pkgname']
            if earlier_links:
                for parent in self.parents_used[name]:
                    for directory in (parent, *parent.parents):
                        if directory.name and _fold(self._destination(directory)) in earlier_links:
                            raise SDKError(f'{name}: package writes through a directory link changed by an earlier '
                                           f'package at {directory}; install the link owner separately')
            earlier_links.update(self.changed_links[name])
            if name in self.conflicts:
                raise SDKError(f'{name}: package differs from the selected sysroot runtime at {self.conflicts[name]}')
            for key in ('removes', 'replaces'):
                for old in package.get(key, ()):
                    if old in removed:
                        continue
                    for path in self._owned(database, old):
                        if key == 'removes' or path not in self.written[name]:
                            raise SDKError(f'{old}: {name} removes the selected sysroot runtime file {path}')
                    removed.add(old)
