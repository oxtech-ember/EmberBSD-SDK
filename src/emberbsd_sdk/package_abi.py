# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Qualify versioned ELF implementation changes behind a retained SONAME.

Callers authenticate the archive and hold the selected pkgdb lock. A SONAME is
the maintainer's ABI contract, not proof that arbitrary code changes preserve
every C/C++ interface. Unversioned plugins and unknown formats remain subject
to the exact PROVIDES guard.
"""

from contextlib import ExitStack
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import stat
import struct
import tarfile
import tempfile

from .elf import read_elf_dynamic
from .errors import SDKError
from .package_install import _relative, inspect_install_paths, install_paths
from .paths import open_regular, verify_stable


def _identity(path):
    stream, before = open_regular(path)
    with stream:
        header = stream.read(64)
    info = read_elf_dynamic(path)
    verify_stable(path, before)
    soname = info['soname']
    if (info['type'] != 3 or not soname or soname in ('.', '..') or
            '/' in soname or any(ord(char) < 32 or ord(char) == 127 for char in soname)):
        raise SDKError('provider has no usable ELF shared-library SONAME')
    return (info['class'], info['endian'], info['machine'], info['type'],
            header[7], header[8], struct.unpack_from('<I', header, 48)[0], soname)


def _implementation(name, soname):
    return re.fullmatch(re.escape(soname) + r'(?:\.[0-9]+)+', PurePosixPath(name).name) is not None


class _Installed:
    def __init__(self, prefix):
        self.root = prefix.resolve(strict=True)
        self.bindings = {}

    def resolve(self, name):
        path = self.root / str(_relative(name))
        before = path.lstat()
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(self.root):
            raise SDKError('installed provider escapes its prefix')
        binding = (before, resolved)
        if path in self.bindings:
            self.verify()
        else:
            self.bindings[path] = binding
        return resolved.relative_to(self.root).as_posix()

    def regular(self, name):
        self.resolve(name)
        return stat.S_ISREG((self.root / name).lstat().st_mode)

    def file(self, name):
        return self.root / self.resolve(name)

    def same(self, left, right):
        return self.file(left).samefile(self.file(right))

    def verify(self):
        for path, (before, resolved) in self.bindings.items():
            verify_stable(path, before)
            if path.resolve(strict=True) != resolved:
                raise SDKError('installed provider binding changed during inspection')


class _Archive:
    def __init__(self, archive_path, config, providers, directory):
        self.members = {}
        self.files = {}

        def visit(member, path, archive):
            name = path.as_posix()
            if name in self.members:
                raise SDKError('duplicate archive path in library provider inspection')
            self.members[name] = member
            if member.isfile() and name in providers:
                # Never materialize archive names or links on the host filesystem.
                destination = directory / str(len(self.files))
                with archive.extractfile(member) as source, destination.open('xb') as output:
                    shutil.copyfileobj(source, output)
                if destination.stat().st_size != member.size:
                    raise SDKError('truncated archive library provider')
                self.files[name] = destination

        inspect_install_paths(archive_path, config, visit=visit)

    def resolve(self, name):
        seen = set()
        while True:
            path = _relative(name)
            name = path.as_posix()
            if name in seen or name not in self.members:
                raise SDKError('dangling or cyclic archive provider link')
            seen.add(name)
            for parent in path.parents:
                member = self.members.get(parent.as_posix())
                if member is not None and not member.isdir():
                    raise SDKError('archive provider traverses a non-directory member')
            member = self.members[name]
            if member.isfile():
                if name not in self.files:
                    raise SDKError('archive provider resolves to an undeclared implementation')
                return name
            if not (member.issym() or member.islnk()):
                raise SDKError('archive provider does not resolve to a regular library')
            target = PurePosixPath(member.linkname)
            if target.is_absolute():
                raise SDKError('absolute archive provider link')
            linked = path.parent / target if member.issym() else target
            name = posixpath.normpath(str(linked))

    def regular(self, name):
        member = self.members.get(name)
        return member is not None and member.isfile()

    def file(self, name):
        return self.files[self.resolve(name)]

    def same(self, left, right):
        return self.resolve(left) == self.resolve(right)

    def verify(self):
        pass  # Complete archive inspection already verified the source file.


def providers_preserved(config, archive, old_providers, new_providers, *, installed_is_old=True):
    """Retain exact providers, or prove the same versioned ELF SONAME binding.

The reverse direction also qualifies retaining a newer installed dependency.
Missing or unsupported evidence leaves the original strict guard in force.
"""
    missing = old_providers - new_providers
    if not missing:
        return True
    try:
        prefix, _ = install_paths(config)
        with ExitStack() as stack:
            directory = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='ember-package-abi-')))
            filesystem = _Installed(prefix)
            archived = _Archive(archive, config,
                                new_providers if installed_is_old else old_providers, directory)
            old, new = (filesystem, archived) if installed_is_old else (archived, filesystem)
            for name in missing:
                if not old.regular(name):
                    return False
                identity = _identity(old.file(name))
                soname = identity[-1]
                stable = (PurePosixPath(name).parent / soname).as_posix()
                if (not _implementation(name, soname) or stable not in old_providers or
                        stable not in new_providers or not old.same(stable, name)):
                    return False
                replacement = new.resolve(stable)
                if (replacement not in new_providers or not new.regular(replacement) or
                        not _implementation(replacement, soname) or
                        _identity(new.file(stable)) != identity):
                    return False
            old.verify()
            new.verify()
            return True
    except (SDKError, OSError, ValueError, RuntimeError, tarfile.TarError):
        return False
