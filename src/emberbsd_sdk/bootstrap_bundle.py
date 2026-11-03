# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Pack complete Ports packages into one movable native SDK prefix."""

from contextlib import ExitStack
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import struct
import subprocess
import tarfile
import tempfile

from .binary_release import _package_payload
from .darwin_runtime import read_macho_runtime
from .errors import SDKError
from .jsonio import read_json, write_json
from .package_cache import directory_lock

MANIFEST = 'share/emberbsd/bootstrap.json'
_SYSTEM = ('/usr/lib/', '/System/Library/')
_MACHO = (b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf', b'\xca\xfe\xba\xbe', b'\xca\xfe\xba\xbf')


def relative_path(value):
    path = PurePosixPath(value)
    if not value or path.is_absolute() or '..' in path.parts or '\\' in value:
        raise SDKError('unsafe bootstrap member: ' + value)
    return Path(*path.parts)


def _destination(root, name):
    path = root / relative_path(name)
    if not path.parent.resolve().is_relative_to(root.resolve()):
        raise SDKError('bootstrap member crosses an external link: ' + name)
    return path


def unpack_package(archive_path, root):
    """Read local producer packages. Distribution authentication is separate."""
    with ExitStack() as stack:
        stream = stack.enter_context(open(archive_path, 'rb'))
        archive = stack.enter_context(tarfile.open(fileobj=_package_payload(stream), mode='r|*'))
        metadata, package, prefix, owned = {}, None, None, set()
        for member in archive:
            name = member.name.removeprefix('./')
            relative_path(name)
            if name.startswith('+'):
                if '/' in name or not member.isfile() or member.size > 8 * 1024 * 1024:
                    raise SDKError('invalid package metadata member: ' + name)
                content = archive.extractfile(member).read()
                metadata[name] = (content, member.mode & 0o777)
                if name == '+CONTENTS':
                    for line in content.decode().splitlines():
                        if line.startswith('@cwd '):
                            if prefix is not None and prefix != line[5:]:
                                raise SDKError('bootstrap package has multiple prefixes')
                            prefix = line[5:]
                        elif line.startswith('@name '):
                            package = line[6:]
                continue
            if not package or not prefix:
                raise SDKError('package payload precedes its identity')
            destination = _destination(root, name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if os.path.lexists(destination):
                if member.isdir() and destination.is_dir() and not destination.is_symlink():
                    continue
                inode = destination.lstat().st_ino
                if inode in owned and member.issym() and destination.is_symlink() and \
                        os.readlink(destination) == member.linkname:
                    continue
                if inode in owned and not destination.is_symlink():
                    # pkgsrc has case aliases, for example OpenSSL manual pages.
                    # Accept identical entries in this package on Darwin's usual FS.
                    if member.isfile() and destination.read_bytes() == archive.extractfile(member).read():
                        continue
                    if member.islnk() and os.path.samefile(destination, _destination(root, member.linkname)):
                        continue
                raise SDKError('bootstrap packages overlap: ' + name)
            if member.isdir():
                destination.mkdir(mode=member.mode & 0o777)
            elif member.isfile():
                with destination.open('xb') as output:
                    shutil.copyfileobj(archive.extractfile(member), output)
                destination.chmod(member.mode & 0o777)
            elif member.issym():
                target = Path(member.linkname)
                if target.is_absolute():
                    try:
                        target = root / target.relative_to(prefix)
                    except ValueError as exc:
                        raise SDKError('external bootstrap link: ' + name) from exc
                else:
                    target = destination.parent / target
                if not target.resolve().is_relative_to(root.resolve()):
                    raise SDKError('escaping bootstrap link: ' + name)
                destination.symlink_to(os.path.relpath(target, destination.parent))
            elif member.islnk():
                target = _destination(root, member.linkname)
                if target.is_symlink() or not target.is_file():
                    raise SDKError('bootstrap hardlink target must precede its member')
                # Mutable installed files must not share an inode.
                shutil.copy2(target, destination)
            else:
                raise SDKError('unsupported bootstrap file: ' + name)
            if member.isfile() or member.islnk():
                # Python timestamp caches and build inputs retain package times.
                os.utime(destination, (member.mtime, member.mtime))
            owned.add(destination.lstat().st_ino)
        if not package or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9+_.-]*', package):
            raise SDKError('invalid bootstrap package name')
        if not prefix or not Path(prefix).is_absolute() or prefix == '/':
            raise SDKError('invalid bootstrap source prefix')
        database = root / 'pkgdb' / package
        database.mkdir(parents=True, exist_ok=False)
        for name, (content, mode) in metadata.items():
            if name not in ('+REQUIRED_BY', '+INSTALLED_INFO'):
                (database / name).write_bytes(content)
                (database / name).chmod(mode)
        return package, prefix


def _local_reference(name, relative, prefix, image):
    original = Path(prefix) / relative
    if name.startswith('@loader_path/'):
        return original.parent / name[len('@loader_path/'):]
    if name.startswith('@executable_path/') and image['filetype'] == 2:
        return original.parent / name[len('@executable_path/'):]
    if name.startswith('/'):
        return Path(name)
    raise SDKError('unsupported native runtime reference: ' + name)


def make_relative(root, prefix, host, install_name_tool, codesign, *, dependency_root=None,
                  dependency_files=None):
    """Rewrite native load commands before release signing, never on a client."""
    changed = []
    dependency_root = dependency_root or root
    for path in sorted(root.rglob('*')):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open('rb') as stream:
            header = stream.read(16)
            magic = header[:4]
        if magic not in _MACHO:
            continue
        if magic == b'\xcf\xfa\xed\xfe' and len(header) == 16 and struct.unpack_from('<I', header, 12)[0] == 1:
            continue  # Installed development objects have no runtime load commands.
        relative = path.relative_to(root)
        image = read_macho_runtime(path, host)
        options = []
        for load in image['loads']:
            name = load['name']
            if name.startswith(_SYSTEM):
                continue
            if name.startswith('@rpath/'):
                candidates = [_local_reference(r, relative, prefix, image) / name[len('@rpath/'):]
                              for r in image['rpaths']]
                candidates = [p for p in candidates if p.is_relative_to(prefix) and
                              ((root / p.relative_to(prefix)).is_file() or
                               (dependency_root / p.relative_to(prefix)).is_file())]
                if len(set(candidates)) != 1:
                    raise SDKError('ambiguous or missing runtime RPATH: ' + str(relative))
                dependency = candidates[0]
            else:
                dependency = _local_reference(name, relative, prefix, image)
            dependency = Path(os.path.normpath(dependency))
            if not dependency.is_relative_to(prefix):
                raise SDKError('external native library: ' + name)
            target = root / dependency.relative_to(prefix)
            provider = target if target.is_file() else dependency_root / dependency.relative_to(prefix)
            if provider != target and dependency_files is not None and dependency.relative_to(prefix).as_posix() not in dependency_files:
                raise SDKError('native library lacks a declared package dependency: ' + name)
            if not provider.is_file() or not provider.resolve().is_relative_to(
                    root.resolve() if provider == target else dependency_root.resolve()):
                raise SDKError('bootstrap lacks native library: ' + name)
            new = '@loader_path/' + os.path.relpath(target, path.parent)
            if new != name:
                options += ['-change', name, new]
        if image['install_name']:
            options += ['-id', '@loader_path/' + path.name]
        for rpath in image['rpaths']:
            if rpath.startswith(prefix + '/') or rpath == prefix:
                options += ['-rpath', rpath, '@loader_path/' + os.path.relpath(
                    root / Path(rpath).relative_to(prefix), path.parent)]
            elif not rpath.startswith(('@loader_path/', '@executable_path/', *_SYSTEM)):
                raise SDKError('external native RPATH: ' + rpath)
        if options:
            subprocess.run([str(install_name_tool), *options, str(path)], check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            # macOS requires a valid code directory after load-command edits.
            # This ad-hoc signature is separate from the EmberBSD release seal.
            subprocess.run([str(codesign), '--force', '--sign', '-', str(path)], check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            changed.append(relative.as_posix())
        result = read_macho_runtime(path, host)
        if any(not load['name'].startswith(('@loader_path/', *_SYSTEM)) for load in result['loads']):
            raise SDKError('native library remains outside the runtime: ' + str(relative))
    return changed


def refresh_checksums(root, packages):
    """Keep pkg_tools ownership and installed MD5 records after relocation."""
    for name in packages:
        path = root / 'pkgdb' / name / '+CONTENTS'
        lines, previous, ignored = [], None, False
        for line in path.read_text().splitlines():
            if line == '@ignore':
                ignored = True
            elif line and not line.startswith('@'):
                previous = None if ignored else _destination(root, line)
                ignored = False
            elif line.startswith('@comment MD5:') and previous is not None:
                if previous.is_symlink():
                    # pkg_create uses the literal link value for SYMLINK records.
                    raise SDKError('unexpected MD5 record for symlink: ' + str(previous))
                with previous.open('rb') as stream:
                    line = '@comment MD5:' + hashlib.file_digest(stream, 'md5').hexdigest()
            elif line.startswith('@comment Symlink:') and previous is not None:
                line = '@comment Symlink:' + os.readlink(previous)
            lines.append(line)
        _replace_file(path, ('\n'.join(lines) + '\n').encode())


def _replace_file(path, content):
    mode = stat.S_IMODE(path.stat().st_mode)
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def relocate_text(content, old, new):
    """Replace path tokens. Overlapping prefixes cannot be retried unambiguously."""
    if b'\0' in content:
        raise SDKError('bootstrap text contains a NUL byte')
    if new.startswith(old + b'/'):
        raise SDKError('SDK destination must not be inside its producer prefix')
    content = re.sub(re.escape(old) + rb'(?=$|[^A-Za-z0-9_.-])', lambda _: new, content)
    return content


def package_paths(root, packages):
    """Return exact owned payload paths, excluding ignored packing-list entries."""
    paths = {}
    for package in packages:
        ignored = False
        for line in (root / 'pkgdb' / package / '+CONTENTS').read_text().splitlines():
            if line == '@ignore':
                ignored = True
            elif line and not line.startswith('@'):
                if ignored:
                    ignored = False
                    continue
                path = _destination(root, line)
                if not os.path.lexists(path):
                    raise SDKError('bootstrap package member is missing: ' + line)
                paths[path.relative_to(root).as_posix()] = path
    return [paths[name] for name in sorted(paths)]


def bundle_paths(root, packages):
    """Include every exact package spelling, even on a case-insensitive stage."""
    paths = {path.relative_to(root).as_posix(): path for path in root.rglob('*')}
    for path in package_paths(root, packages):
        while path != root:
            paths[path.relative_to(root).as_posix()] = path
            path = path.parent
    return [paths[name] for name in sorted(paths)]


def write_archive(root, output, packages):
    """Serialize package names rather than the staging filesystem's case folding."""
    paths = [root, *bundle_paths(root, packages)]
    with Path(output).open('xb') as stream:
        with tarfile.open(fileobj=stream, mode='w:gz', compresslevel=6,
                          dereference=False) as archive:
            for path in paths:
                relative = path.relative_to(root)
                name = 'ember-sdk' if path == root else 'ember-sdk/' + relative.as_posix()
                member = archive.gettarinfo(str(path), arcname=name)
                member.uid = member.gid = 0
                member.uname = member.gname = ''
                if member.isreg() or member.islnk():
                    # Case aliases must not become self-hardlinks on the consumer FS.
                    member.type, member.linkname = tarfile.REGTYPE, ''
                    member.size = path.stat().st_size
                    with path.open('rb') as payload:
                        archive.addfile(member, payload)
                else:
                    archive.addfile(member)


def text_references(root, prefix, packages):
    result = []
    for path in bundle_paths(root, packages):
        if (not path.is_file() or path.is_symlink() or path.stat().st_size > 8 * 1024 * 1024 or
                path.name in ('+BUILD_INFO', '+BUILD_VERSION') or path == root / MANIFEST):
            continue
        content = path.read_bytes()
        if prefix.encode() not in content or b'\0' in content:
            continue
        try:
            content.decode('utf-8')
        except UnicodeError:
            continue
        result.append(path.relative_to(root).as_posix())
    return result


def refresh_bytecode(root, packages, changed_sources):
    """Refresh owned Python caches after extraction and actual text relocation."""
    import importlib.util
    import py_compile

    for path in package_paths(root, packages):
        if path.suffix != '.pyc' or path.is_symlink():
            continue
        try:
            source = Path(importlib.util.source_from_cache(str(path)))
        except ValueError as exc:
            raise SDKError('unsupported bootstrap bytecode path: ' + str(path)) from exc
        _destination(root, source.relative_to(root).as_posix())
        if not source.is_file() or source.is_symlink():
            raise SDKError('bootstrap bytecode lacks its source: ' + str(path))
        optimization = path.stem.rsplit('.opt-', 1)[1] if '.opt-' in path.stem else ''
        if optimization not in ('', '1', '2') or Path(importlib.util.cache_from_source(
                str(source), optimization=optimization)) != path:
            raise SDKError('bootstrap bytecode targets a different Python: ' + str(path))
        with path.open('rb') as cache:
            header = cache.read(16)
        flags = int.from_bytes(header[4:8], 'little')
        mode = py_compile.PycInvalidationMode.TIMESTAMP
        valid = len(header) == 16 and header[:4] == importlib.util.MAGIC_NUMBER
        if flags in (1, 3):
            mode = (py_compile.PycInvalidationMode.CHECKED_HASH if flags == 3 else
                    py_compile.PycInvalidationMode.UNCHECKED_HASH)
            valid = valid and header[8:16] == importlib.util.source_hash(source.read_bytes())
        elif flags == 0:
            info = source.stat()
            valid = valid and header[8:16] == struct.pack('<II',
                int(info.st_mtime) & 0xffffffff, info.st_size & 0xffffffff)
        else:
            valid = False
        if source in changed_sources or not valid:
            py_compile.compile(str(source), cfile=str(path), doraise=True,
                               optimize=int(optimization or '0'), invalidation_mode=mode)


def initialize(root):
    root = root.resolve()
    if not re.fullmatch(r'[A-Za-z0-9_./-]+', str(root)):
        raise SDKError('SDK path must contain only letters, digits, _, ., / and - (Ports path contract)')
    state = root / 'var/sdk'
    state.mkdir(parents=True, exist_ok=True)
    with directory_lock(state / 'bootstrap.lock'):
        metadata = read_json(root / MANIFEST)
        if metadata['schema'] != 'emberbsd.bootstrap.v1':
            raise SDKError('unsupported SDK bootstrap')
        if metadata['prefix'] == str(root) and metadata.get('initialized'):
            return metadata
        if metadata.get('initialized'):
            raise SDKError('an installed SDK cannot be moved; unpack a fresh bootstrap at the new path')
        old = metadata['prefix'].encode()
        if str(root).startswith(metadata['prefix'] + '/'):
            raise SDKError('SDK destination must not be inside its producer prefix')
        changed_sources = set()
        for name in metadata['text_files']:
            path = _destination(root, name)
            if path.is_symlink() or not path.is_file():
                raise SDKError('bootstrap text file changed type: ' + name)
            content = path.read_bytes()
            updated = relocate_text(content, old, str(root).encode())
            if updated != content:
                _replace_file(path, updated)
                if path.suffix == '.py':
                    changed_sources.add(path)
        for name in metadata['packages']:
            path = root / 'pkgdb' / name / '+BUILD_INFO'
            if path.exists():
                lines = path.read_bytes().splitlines(keepends=True)
                # These two fields are active pkg_tools metadata. Other fields
                # describe the original build and must retain its provenance.
                content = b''.join(relocate_text(line, old, str(root).encode())
                    if line.startswith((b'REQUIRES=', b'PROVIDES=')) else line for line in lines)
                if content != path.read_bytes():
                    _replace_file(path, content)
        refresh_bytecode(root, metadata['packages'], changed_sources)
        refresh_checksums(root, metadata['packages'])
        # Run the package owner's normal configuration actions. The host's
        # POSIX utilities are installation services, not a compiler fallback.
        environment = {'PATH': str(root / 'bin') + ':' + str(root / 'sbin') + ':/usr/bin:/bin:/usr/sbin:/sbin',
                       'LC_ALL': 'C', 'PKG_PREFIX': str(root), 'PKG_DBDIR': str(root / 'pkgdb'),
                       'PKG_REFCOUNT_DBDIR': str(root / 'pkgdb.refcount'), 'PKG_REGISTER_SHELLS': 'NO'}
        for name in metadata['packages']:
            database = root / 'pkgdb' / name
            script = database / '+INSTALL'
            if not script.exists():
                continue
            for stage in ('PRE-INSTALL', 'POST-INSTALL'):
                result = subprocess.run([str(script), name, stage], cwd=database,
                    env=dict(environment, PKG_METADATA_DIR=str(database)), capture_output=True)
                if result.returncode:
                    raise SDKError('bootstrap package configuration failed: ' + name + ': ' +
                                   result.stderr.decode('utf-8', 'replace'))
        command = [str(root / 'sbin/pkg_admin'), '-K', str(root / 'pkgdb')]
        for action in ('rebuild', 'rebuild-tree'):
            result = subprocess.run(command + [action], env={'PATH': str(root / 'bin') + ':' + str(root / 'sbin'),
                                    'LC_ALL': 'C'}, capture_output=True)
            if result.returncode:
                raise SDKError('cannot register bootstrap packages: ' + result.stderr.decode('utf-8', 'replace'))
        metadata['prefix'] = str(root)
        metadata['initialized'] = True
        write_json(root / MANIFEST, metadata, exclusive=False)
        return metadata


def client_main(argv=None):
    """The runtime Python selects its own prefix; no checkout or hidden config."""
    import sys
    from .sdk_client import main
    try:
        root = Path(sys.executable).resolve().parents[1]
        metadata = initialize(root)
        config = {'schema': 'emberbsd.sdk-client.v1', 'host_prefix': str(root),
                  'state_dir': str(root / 'var/sdk'), 'cache_dir': str(root / 'var/cache/packages'),
                  'https_ca': str(root / metadata['https_ca']),
                  'repositories': {metadata['repository']: {'host': metadata['host'],
                      'prefix': str(root), 'pkgdb': str(root / 'pkgdb')}}}
        return main(argv, default_config=config)
    except (SDKError, OSError, ValueError) as exc:
        print('ember-sdk: ' + str(exc), file=sys.stderr)
        return 1


def prepare(root, archives, crypto, *, host, repository, install_name_tool, codesign, https_ca):
    """Assemble a fresh bundle; keep whole packages and one package database."""
    from .binary_release import _matches
    from .client_bundle import package_client
    root.mkdir(parents=True, exist_ok=False)
    packages, prefixes = [], set()
    for archive in archives:
        package, prefix = unpack_package(archive, root)
        packages.append(package)
        prefixes.add(prefix)
    if len(prefixes) != 1:
        raise SDKError('bootstrap packages must use one producer prefix')
    prefix = prefixes.pop()
    for name in packages:
        for line in (root / 'pkgdb' / name / '+CONTENTS').read_text().splitlines():
            if line.startswith('@pkgdep ') and not any(_matches(crypto, line[8:], item) for item in packages):
                raise SDKError('bootstrap lacks package dependency: ' + line[8:])
    for member in ('bin/python3.15', 'bin/openssl', 'sbin/pkg_add', 'sbin/pkg_admin', 'sbin/pkg_info'):
        if not (root / member).is_file():
            raise SDKError('bootstrap lacks ' + member)
    make_relative(root, prefix, {'os': host['os'], 'cpu': host['arch']}, install_name_tool, codesign)
    refresh_checksums(root, packages)
    text_files = text_references(root, prefix, packages)
    (root / 'share/emberbsd').mkdir(parents=True, exist_ok=True)
    package_client(root / 'share/emberbsd/ember-sdk.pyz', bootstrap=True)
    launcher = root / 'bin/ember-sdk'
    launcher.write_text('#!/bin/sh\nset -eu\nroot=$(CDPATH= cd -- "${0%/*}/.." && pwd -P)\n'
                        'exec "$root/bin/python3.15" -I -B "$root/share/emberbsd/ember-sdk.pyz" "$@"\n')
    launcher.chmod(0o755)
    metadata = dict(schema='emberbsd.bootstrap.v1', prefix=prefix, initialized=False, host=host, repository=repository,
                    https_ca=https_ca, packages=packages, text_files=text_files)
    write_json(root / MANIFEST, metadata)
    return metadata
