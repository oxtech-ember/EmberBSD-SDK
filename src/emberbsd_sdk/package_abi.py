# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Qualify versioned library implementations behind a retained loader identity.

Callers authenticate the archive and hold the selected pkgdb lock. A loader
identity is the maintainer's ABI contract, not proof that arbitrary code changes
preserve every C/C++ interface. Unversioned plugins and unknown formats remain subject
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
from .darwin_runtime import read_macho_runtime
from .errors import SDKError
from .package_install import _relative, inspect_install_paths, install_paths
from .paths import open_regular, verify_stable
from .release_signing import read_bytes


def _identity(path, config, prefix, name):
    stream, before = open_regular(path)
    with stream:
        header = stream.read(64)
    if header[:4] == b'\xcf\xfa\xed\xfe':
        host = config.get('host', {})
        if host.get('os') != 'Darwin':
            raise SDKError('Mach-O provider requires a Darwin host repository')
        image = read_macho_runtime(path, {'cpu': host.get('arch')})
        verify_stable(path, before)
        install_name, versions = image['install_name'], image['dylib_version']
        if image['filetype'] != 6 or not install_name or not versions:
            raise SDKError('provider is not a versioned Mach-O dylib')
        if install_name.startswith('@rpath/'):
            basename = install_name.removeprefix('@rpath/')
            if '/' in basename or basename in ('.', '..'):
                raise SDKError('provider install ID is not a local basename')
            stable = PurePosixPath(name).parent / basename
        else:
            identifier = PurePosixPath(install_name)
            if (not isinstance(prefix, str) or not PurePosixPath(prefix).is_absolute() or
                    not identifier.is_absolute() or '..' in identifier.parts or
                    not identifier.is_relative_to(prefix)):
                raise SDKError('provider install ID escapes its declared prefix')
            stable = identifier.relative_to(prefix)
            if stable.parent != PurePosixPath(name).parent:
                raise SDKError('provider install ID differs from its library directory')
        return dict(kind='macho', stable=stable.as_posix(), current=versions['current'],
                    abi=(image['cpu'], image['subtype'], image['filetype'],
                         versions['compatibility'],
                         tuple(sorted((row['platform'], row['minimum'])
                                      for row in image['build_versions']))))
    info = read_elf_dynamic(path)
    verify_stable(path, before)
    soname = info['soname']
    if (info['type'] != 3 or not soname or soname in ('.', '..') or
            '/' in soname or any(ord(char) < 32 or ord(char) == 127 for char in soname)):
        raise SDKError('provider has no usable ELF shared-library SONAME')
    return dict(kind='elf', stable=(PurePosixPath(name).parent / soname).as_posix(),
                abi=(info['class'], info['endian'], info['machine'], info['type'],
                     header[7], header[8], struct.unpack_from('<I', header, 48)[0]))


def _implementation(name, identity):
    stable = PurePosixPath(identity['stable']).name
    if identity['kind'] == 'macho':
        if not stable.endswith('.dylib'):
            return False
        pattern = re.escape(stable[:-6]) + r'(?:\.[0-9]+)+\.dylib'
    else:
        pattern = re.escape(stable) + r'(?:\.[0-9]+)+'
    return re.fullmatch(pattern, PurePosixPath(name).name) is not None


def _same_abi(old, new):
    # Package policy, not a claim about dyld's version enforcement. The
    # declared ABI and minimum platform stay fixed; current versions may grow.
    return (old['kind'] == new['kind'] and old['stable'] == new['stable'] and
            old['abi'] == new['abi'] and
            (old['kind'] != 'macho' or new['current'] >= old['current']))


class _Installed:
    def __init__(self, prefix, logical_prefix):
        self.prefix = logical_prefix
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
        self.prefix = None

        def visit(member, path, archive, original_prefix):
            self.prefix = original_prefix
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
    """Retain exact providers, or prove a versioned ELF/Mach-O loader binding.

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
            filesystem = _Installed(prefix, config['prefix'])
            archived = _Archive(archive, config,
                                new_providers if installed_is_old else old_providers, directory)
            old, new = (filesystem, archived) if installed_is_old else (archived, filesystem)
            for name in missing:
                if not old.regular(name):
                    return False
                identity = _identity(old.file(name), config, old.prefix, name)
                stable = identity['stable']
                if (not _implementation(name, identity) or stable not in old_providers or
                        stable not in new_providers or not old.same(stable, name)):
                    return False
                replacement = new.resolve(stable)
                if (replacement not in new_providers or not new.regular(replacement) or
                        not _implementation(replacement, identity) or
                        not _same_abi(identity, _identity(new.file(stable), config,
                                                        new.prefix, stable))):
                    return False
            old.verify()
            new.verify()
            return True
    except (SDKError, OSError, ValueError, RuntimeError, tarfile.TarError):
        return False


_ELF_MAGIC = b'\x7fELF'
_MACHO_MAGIC = b'\xcf\xfa\xed\xfe'
# Other Mach-O forms (32-bit, byte-swapped, universal) are not read here.
_OTHER_MACHO = (b'\xfe\xed\xfa\xce', b'\xce\xfa\xed\xfe', b'\xfe\xed\xfa\xcf',
                b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca')


def _loads(path, config):
    """Return the library names that the ELF or Mach-O file PATH loads, or None."""
    stream, before = open_regular(path)
    with stream:
        magic = stream.read(4)
    verify_stable(path, before)
    if magic == _ELF_MAGIC:
        return read_elf_dynamic(path).get('needed') or []
    if magic == _MACHO_MAGIC:
        host = config.get('host', {})
        if host.get('os') != 'Darwin':
            raise SDKError('Mach-O consumer requires a Darwin host repository')
        image = read_macho_runtime(path, {'cpu': host.get('arch')})
        return [item['name'] for item in image['loads'] + image['implicit_loads']]
    if magic in _OTHER_MACHO:
        raise SDKError('unsupported Mach-O consumer format')
    return None


def consumers_unbound(config, database, consumers, removed):
    """Prove that no file of the installed CONSUMERS loads a REMOVED provider.

    The consumers stay installed while their dependency changes, also between
    the individual package updates and after a partial failure. Each regular
    ELF and Mach-O file of each consumer is read. A load of a library with the
    basename of a removed provider binds the consumer to it. dlopen users are
    not seen, as for REQUIRES. A consumer without a readable image, a missing
    listed file and unreadable evidence keep the strict guard.
    """
    names = {PurePosixPath(name).name for name in removed}
    if not names:
        return True
    try:
        prefix, _ = install_paths(config)
        root, logical = prefix.resolve(strict=True), PurePosixPath(config['prefix'])
        for consumer in consumers:
            if Path(consumer).name != consumer or consumer in ('.', '..'):
                return False
            contents = read_bytes(Path(database) / consumer / '+CONTENTS', 8 * 1024 * 1024)
            directory, images, ignore = None, 0, False
            for line in contents.decode('utf-8').splitlines():
                if line.startswith('@cwd '):
                    directory = PurePosixPath(line[5:].rstrip('/') or '/')
                    continue
                if line == '@ignore':
                    ignore = True  # The next entry is package metadata, not a file.
                    continue
                if not line or line.startswith('@'):
                    continue
                if ignore:
                    ignore = False
                    continue
                # Installed packages record the logical prefix of this host.
                if directory is None or not directory.is_relative_to(logical):
                    return False
                relative = _relative((directory / line).relative_to(logical).as_posix())
                path = root / str(relative)
                if path.is_symlink():
                    continue
                if not path.is_file():
                    return False
                loads = _loads(path, config)
                if loads is None:
                    continue
                if names & {PurePosixPath(name).name for name in loads}:
                    return False
                images += 1
            if not images:
                return False
        return True
    except (SDKError, OSError, ValueError, RuntimeError, UnicodeDecodeError, KeyError):
        return False
