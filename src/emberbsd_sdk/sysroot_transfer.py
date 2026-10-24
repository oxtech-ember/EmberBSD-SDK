# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD; AI-assisted sysroot transfer contracts.
"""Transfer a completed sysroot through a byte store, without live object reuse.

The caller owns the source and output parents throughout export/import. A share
is a transport for regular files. It need not preserve POSIX payload metadata.
SHA256 pins provide integrity, not publisher identity or runtime qualification.
"""

import gzip
import hashlib
import os
import re
import shutil
import stat
import tarfile
import tempfile

from . import inventory, jsonio, host_compiler, paths, publication, tarstream
from .errors import SDKError
from .targets import target_definition

SYSROOT_TRANSFER_API = 1
FORMAT = 'emberbsd-sysroot-transfer'
RECEIPT = '.prebuild-sdk-build.json'
INCOMPLETE = '.prebuild-sdk-build-incomplete'
EXCLUDED = (RECEIPT, INCOMPLETE, '.prebuild-sdk-build.log')
MAX_MANIFEST = 32 * 1024 * 1024
MAX_ENTRIES = 200_000
MAX_BYTES = 64 * 1024 ** 3
MAX_FILE = 8 ** 11 - 1  # The fixed POSIX ustar size field.
COMPILER_BINARIES = ('gcc', 'g++', 'cpp', 'ar', 'as', 'ld', 'nm', 'objcopy',
                    'objdump', 'ranlib', 'readelf', 'strip', 'resolved-cc1',
                    'resolved-cc1plus', 'resolved-collect2', 'resolved-lto1',
                    'resolved-as', 'resolved-ld')


def _digest(value, size=64):
    if not isinstance(value, str) or re.fullmatch('[0-9a-f]{' + str(size) + '}', value) is None:
        raise SDKError('invalid digest or source commit')
    return value


def _hash_json(value):
    return hashlib.sha256(jsonio.json_bytes(value)).hexdigest()


def _open_transport(path):
    """Open regular transport bytes without trusting SMB timestamp semantics."""
    before = paths.regular_file(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    try:
        current = os.fstat(fd)
        if (not stat.S_ISREG(current.st_mode)
                or (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino)):
            raise SDKError('transport file identity changed during open')
        return os.fdopen(fd, 'rb')
    except BaseException:
        os.close(fd)
        raise


def _read(path, limit, *, transport=False):
    if transport:
        handle, before = _open_transport(path), None
    else:
        handle, before = paths.open_regular(path)
    with handle:
        data = handle.read(limit + 1)
    if before is not None:
        paths.verify_stable(path, before)
    if len(data) > limit:
        raise SDKError('transfer metadata exceeds size limit')
    return data


def _directory(path):
    path = paths.path_string(path)
    paths.real_directory(path)
    if paths.canonical_existing(path) != path:
        raise SDKError('transfer paths must have real ancestors')
    return path


def _new_path(path):
    path = paths.path_string(path)
    parent, name = os.path.split(path)
    _directory(parent)
    if name in ('', '.', '..') or paths.entry(path) is not None:
        raise SDKError('transfer destination must be a new named entry')
    return path


def _mode(value):
    if type(value) is not int or not 0 <= value <= 0o777:
        raise SDKError('transfer mode must contain ordinary permission bits only')
    return value


def _name(value):
    if (not isinstance(value, str) or not value or len(value.encode('utf-8')) > 4096
            or '\\' in value or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or any(p in ('', '.', '..') for p in value.split('/'))):
        raise SDKError('invalid relative inventory path')
    return value


def _validate_rows(rows, max_bytes):
    if type(max_bytes) is not int or max_bytes < 1:
        raise SDKError('max-bytes must be a positive integer')
    if not isinstance(rows, dict) or not rows or len(rows) > MAX_ENTRIES:
        raise SDKError('invalid transfer inventory size')
    total = 0
    for name, row in rows.items():
        _name(name)
        if name.split('/')[0].startswith('.prebuild-sdk-'):
            raise SDKError('reserved SDK metadata in payload')
        if not isinstance(row, dict) or row.get('kind') not in ('file', 'directory', 'link'):
            raise SDKError('invalid inventory entry kind')
        _mode(row.get('mode'))
        required = {'kind', 'mode'}
        if row['kind'] == 'file':
            required |= {'size', 'sha256'}
            if type(row.get('size')) is not int or not 0 <= row['size'] <= MAX_FILE:
                raise SDKError('invalid file size for ustar transfer')
            _digest(row.get('sha256'))
            total += row['size']
            if set(row) - required - {'elf', 'archive'}:
                raise SDKError('unknown inventory file fields')
        elif row['kind'] == 'link':
            required.add('target')
            target = row.get('target')
            if (not isinstance(target, str) or not target or target.startswith('/')
                    or '\\' in target or len(target.encode('utf-8')) > 4096
                    or any(ord(c) < 32 or ord(c) == 127 for c in target)):
                raise SDKError('invalid relative link target')
        if not required <= set(row) or (row['kind'] != 'file' and set(row) != required):
            raise SDKError('invalid inventory fields')
        parts = name.split('/')
        for index in range(1, len(parts)):
            parent = rows.get('/'.join(parts[:index]))
            if not isinstance(parent, dict) or parent.get('kind') != 'directory':
                raise SDKError('inventory ancestor must be a listed directory')
    if total > max_bytes:
        raise SDKError('sysroot exceeds max-bytes')
    # Resolve links in metadata before creating any filesystem entries.
    for name, row in rows.items():
        if row['kind'] != 'link':
            continue
        todo, resolved, hops = name.split('/'), [], 0
        while todo:
            part = todo.pop(0)
            if part in ('', '.'):
                continue
            if part == '..':
                if not resolved:
                    raise SDKError('link escapes the sysroot')
                resolved.pop()
                continue
            key = '/'.join(resolved + [part])
            item = rows.get(key)
            if item is None:
                raise SDKError('dangling sysroot link')
            if item['kind'] == 'link':
                hops += 1
                if hops > 64:
                    raise SDKError('cyclic or excessive sysroot links')
                todo[:0] = item['target'].split('/')
            else:
                if todo and item['kind'] != 'directory':
                    raise SDKError('link traverses a non-directory')
                resolved.append(part)
        if not resolved:
            raise SDKError('link to sysroot root is unsupported')
    return total


def _provenance(receipt, target):
    definition = target_definition(target)
    try:
        if (receipt['format'] != 'emberbsd-sysroot-build' or type(receipt['version']) is not int
                or receipt['version'] not in (1, 2) or receipt['qualification'] != 'built-and-inspected'
                or receipt['workspace']['layout_version'] != 2
                or receipt['workspace']['target'] != target
                or receipt['workspace']['triplet'] != definition['triplet']
                or receipt['signature']['target'] != definition):
            raise SDKError('export requires a completed matching SDK sysroot build')
        signature = receipt['signature']
        if receipt['version'] == 2:
            host_compiler.validate_binding(signature, receipt['paths'])
        compiler = signature['compiler']
        if (compiler['version'] != '16.2.0' or compiler['triplet'] != definition['triplet']
                or compiler['threads'] != 'posix'):
            raise SDKError('export requires the common GCC16 compiler receipt')
        binaries = {name: _digest(compiler['binaries'][name]['sha256'])
                    for name in COMPILER_BINARIES}
        sources = {name: _digest(receipt['sources'][name]['commit'], 40)
                   for name in ('os', 'ports', 'sdk')}
        host = signature['host']
        if not isinstance(host, str) or re.fullmatch('[A-Za-z0-9_.+-]{1,160}', host) is None:
            raise SDKError('invalid build host identity')
        return dict(sources=sources, build_host=host,
                    compiler=dict(version='16.2.0', triplet=definition['triplet'],
                                  threads='posix', binaries=binaries),
                    runtime_sha256=_hash_json(signature['runtime']),
                    build_context_sha256=_hash_json(signature),
                    build_receipt_sha256=_hash_json(receipt))
    except (KeyError, TypeError) as exc:
        raise SDKError('missing sysroot build provenance') from exc


def _write(path, data):
    with open(path, 'xb') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


class _HashedReader:
    def __init__(self, handle):
        self.handle = handle
        self.digest = hashlib.sha256()

    def read(self, size):
        data = self.handle.read(size)
        self.digest.update(data)
        return data


class _HashedWriter:
    def __init__(self, handle):
        self.handle = handle
        self.digest = hashlib.sha256()
        self.size = 0

    def write(self, data):
        count = self.handle.write(data)
        if count != len(data):
            raise SDKError('short archive write')
        self.digest.update(data)
        self.size += count
        return count

    def flush(self):
        self.handle.flush()


def _check_payload(path, metadata):
    digest, count = hashlib.sha256(), 0
    with _open_transport(path) as handle:
        while block := handle.read(1024 * 1024):
            count += len(block)
            if count > metadata['size']:
                raise SDKError('payload exceeds recorded size')
            digest.update(block)
    if count != metadata['size'] or digest.hexdigest() != metadata['sha256']:
        raise SDKError('payload size or SHA256 mismatch')


def export_sysroot(source, output, *, target):
    """Export a frozen SDK-built tree. Publish READY only after verification."""
    definition = target_definition(target)
    source, output = _directory(source), _new_path(output)
    paths.reject_overlap(source, output)
    if paths.entry(os.path.join(source, INCOMPLETE)) is not None:
        raise SDKError('cannot export an incomplete sysroot build')
    receipt_path = os.path.join(source, RECEIPT)
    receipt = jsonio.loads_json(_read(receipt_path, MAX_MANIFEST), max_bytes=MAX_MANIFEST)
    provenance = _provenance(receipt, target)
    rows = inventory.sysroot(source, definition['elf'], exclude=EXCLUDED)
    if receipt.get('outputs') != rows:
        raise SDKError('sysroot differs from the completed build receipt')
    total = _validate_rows(rows, MAX_BYTES)
    root_mode = _mode(stat.S_IMODE(os.stat(source).st_mode))
    os.mkdir(output)
    payload = os.path.join(output, 'payload.tar.gz')
    # Only regular byte payloads enter tar. Names, links and modes live in JSON.
    # The reader never runs tar extraction or interprets archive paths.
    with open(payload, 'xb') as raw:
        written = _HashedWriter(raw)
        with gzip.GzipFile(fileobj=written, mode='wb', filename='', mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w|', format=tarfile.USTAR_FORMAT) as archive:
                index = 0
                for name, row in sorted(rows.items()):
                    if row['kind'] != 'file':
                        continue
                    member = tarfile.TarInfo(f'files/{index:08d}')
                    member.size, member.mode = row['size'], 0o600
                    handle, before = paths.open_regular(os.path.join(source, name))
                    with handle:
                        reader = _HashedReader(handle)
                        archive.addfile(member, reader)
                        if reader.digest.hexdigest() != row['sha256']:
                            raise SDKError('file changed during export')
                    paths.verify_stable(os.path.join(source, name), before)
                    index += 1
        raw.flush()
        os.fsync(raw.fileno())
    if (inventory.sysroot(source, definition['elf'], exclude=EXCLUDED) != rows
            or jsonio.loads_json(_read(receipt_path, MAX_MANIFEST), max_bytes=MAX_MANIFEST) != receipt
            or paths.entry(os.path.join(source, INCOMPLETE)) is not None
            or stat.S_IMODE(os.stat(source).st_mode) != root_mode):
        raise SDKError('sysroot changed during export')
    manifest = dict(format=FORMAT, version=1, scope='sysroot-transfer', target=target,
                    triplet=definition['triplet'], provenance=provenance,
                    qualification='built-and-inspected', relocation='not-assessed',
                    execution_acceptance='not-assessed', root_mode=root_mode,
                    inventory=rows, unpacked_bytes=total,
                    payload=dict(name='payload.tar.gz', format='gzip-ustar-flat-v1',
                                 size=written.size, sha256=written.digest.hexdigest()))
    data = jsonio.json_bytes(manifest)
    if len(data) > MAX_MANIFEST:
        raise SDKError('transfer manifest exceeds size limit')
    _write(os.path.join(output, 'manifest.json'), data)
    digest = hashlib.sha256(data).hexdigest()
    # Read back both files before publishing readiness on a byte-only share.
    if _read(os.path.join(output, 'manifest.json'), MAX_MANIFEST, transport=True) != data:
        raise SDKError('export read-back verification failed')
    _check_payload(payload, manifest['payload'])
    _write(os.path.join(output, 'READY'), (digest + '\n').encode('ascii'))
    return dict(bundle=output, manifest_sha256=digest, target=target,
                qualification='transferred', relocation='not-assessed')


def _manifest(bundle, target, digest, max_bytes):
    definition = target_definition(target)
    _digest(digest)
    data = _read(os.path.join(bundle, 'manifest.json'), MAX_MANIFEST, transport=True)
    if (_read(os.path.join(bundle, 'READY'), 65, transport=True) != (digest + '\n').encode('ascii')
            or hashlib.sha256(data).hexdigest() != digest):
        raise SDKError('manifest pin or READY mismatch')
    m = jsonio.loads_json(data, max_bytes=MAX_MANIFEST)
    keys = {'format', 'version', 'scope', 'target', 'triplet', 'provenance', 'qualification',
            'relocation', 'execution_acceptance', 'root_mode', 'inventory', 'unpacked_bytes', 'payload'}
    if (not isinstance(m, dict) or set(m) != keys or m['format'] != FORMAT
            or type(m['version']) is not int or m['version'] != 1
            or m['scope'] != 'sysroot-transfer' or m['target'] != target
            or m['triplet'] != definition['triplet'] or m['qualification'] != 'built-and-inspected'
            or m['relocation'] != 'not-assessed' or m['execution_acceptance'] != 'not-assessed'):
        raise SDKError('unsupported transfer manifest or target mismatch')
    _mode(m['root_mode'])
    total = _validate_rows(m['inventory'], max_bytes)
    if type(m['unpacked_bytes']) is not int or m['unpacked_bytes'] != total:
        raise SDKError('unpacked byte count mismatch')
    p = m['payload']
    if (not isinstance(p, dict) or set(p) != {'name', 'format', 'size', 'sha256'}
            or p['name'] != 'payload.tar.gz' or p['format'] != 'gzip-ustar-flat-v1'
            or type(p['size']) is not int or not 0 < p['size'] <= max_bytes + MAX_MANIFEST):
        raise SDKError('invalid transfer payload metadata')
    _digest(p['sha256'])
    provenance = m['provenance']
    if not isinstance(provenance, dict) or set(provenance) != {
            'sources', 'build_host', 'compiler', 'runtime_sha256', 'build_context_sha256', 'build_receipt_sha256'}:
        raise SDKError('invalid transfer provenance')
    try:
        host = provenance['build_host']
        if not isinstance(host, str) or re.fullmatch('[A-Za-z0-9_.+-]{1,160}', host) is None:
            raise SDKError('invalid transfer build host')
        if set(provenance['sources']) != {'os', 'ports', 'sdk'}:
            raise SDKError('missing source pins')
        for commit in provenance['sources'].values():
            _digest(commit, 40)
        for key in ('runtime_sha256', 'build_context_sha256', 'build_receipt_sha256'):
            _digest(provenance[key])
        compiler = provenance['compiler']
        if (set(compiler) != {'version', 'triplet', 'threads', 'binaries'}
                or compiler['version'] != '16.2.0' or compiler['triplet'] != definition['triplet']
                or compiler['threads'] != 'posix' or not isinstance(compiler['binaries'], dict)
                or set(compiler['binaries']) != set(COMPILER_BINARIES)):
            raise SDKError('invalid transfer compiler')
        for value in compiler['binaries'].values():
            _digest(value)
    except (KeyError, TypeError, AttributeError) as exc:
        raise SDKError('invalid transfer provenance') from exc
    return m


def _copy_payload(source, destination, metadata):
    handle = _open_transport(source)
    digest, count = hashlib.sha256(), 0
    with handle, open(destination, 'xb') as output:
        while block := handle.read(1024 * 1024):
            count += len(block)
            if count > metadata['size']:
                raise SDKError('payload exceeds recorded size')
            digest.update(block)
            output.write(block)
        output.flush()
        os.fsync(output.fileno())
    if count != metadata['size'] or digest.hexdigest() != metadata['sha256']:
        raise SDKError('payload size or SHA256 mismatch')


def _restore(payload, root, rows):
    for name, row in sorted(rows.items()):
        if row['kind'] == 'directory':
            os.mkdir(os.path.join(root, name), 0o700)
    files = [(name, row) for name, row in sorted(rows.items()) if row['kind'] == 'file']
    with gzip.open(payload, 'rb') as stream:
        records = iter(tarstream.tar_records(stream))
        for index, (name, row) in enumerate(files):
            record = next(records, None)
            if (record is None or record.name != f'files/{index:08d}'.encode('ascii')
                    or record.typeflag != b'0' or record.linkname or record.size != row['size']):
                raise SDKError('unexpected, missing or non-regular tar payload record')
            path = os.path.join(root, name)
            digest = hashlib.sha256()
            with open(path, 'xb') as output:
                while block := record.read(1024 * 1024):
                    digest.update(block)
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())
            if digest.hexdigest() != row['sha256']:
                raise SDKError('restored file SHA256 mismatch')
            os.chmod(path, row['mode'])
        if next(records, None) is not None:
            raise SDKError('extra tar payload record')
        # The writer adds at most one tar record block of zero padding. Bound
        # decompression even if an attacker appends a second gzip stream.
        trailer = stream.read(10241)
        if len(trailer) > 10240 or any(trailer):
            raise SDKError('invalid or excessive tar trailer')
    for name, row in sorted(rows.items()):
        path = os.path.join(root, name)
        if row['kind'] == 'link':
            os.symlink(row['target'], path)
            if stat.S_IMODE(os.lstat(path).st_mode) != row['mode']:
                if not hasattr(os, 'lchmod'):
                    raise SDKError('host cannot preserve symbolic-link mode')
                os.lchmod(path, row['mode'])
    for name, row in sorted(rows.items(), reverse=True):
        if row['kind'] == 'directory':
            os.chmod(os.path.join(root, name), row['mode'])


def _remove_owned_stage(stage):
    # Restore directory access before scandir, including restrictive manifest
    # modes. Never follow payload symlinks while preparing our own cleanup.
    pending = [stage]
    while pending:
        directory = pending.pop()
        os.chmod(directory, 0o700)
        with os.scandir(directory) as children:
            pending.extend(child.path for child in children if child.is_dir(follow_symlinks=False))
    shutil.rmtree(stage)


def import_sysroot(bundle, destination, *, target, manifest_sha256, max_bytes=MAX_BYTES):
    """Verify a pinned bundle, then publish a new locally restored sysroot."""
    bundle, destination = _directory(bundle), _new_path(destination)
    paths.reject_overlap(bundle, destination)
    manifest = _manifest(bundle, target, manifest_sha256, max_bytes)
    publication.require_directory_publication()
    parent = os.path.dirname(destination)
    stage = tempfile.mkdtemp(prefix='.sysroot-import-', dir=parent)
    try:
        payload, tree = os.path.join(stage, 'payload.tar.gz'), os.path.join(stage, 'tree')
        _copy_payload(os.path.join(bundle, 'payload.tar.gz'), payload, manifest['payload'])
        os.mkdir(tree, 0o700)
        _restore(payload, tree, manifest['inventory'])
        if inventory.sysroot(tree, target_definition(target)['elf']) != manifest['inventory']:
            raise SDKError('restored inventory or filesystem metadata mismatch')
        # Preserve portable provenance, not the builder's host paths or logs.
        jsonio.write_json(os.path.join(tree, '.prebuild-sdk-import.json'),
                          dict(format='emberbsd-sysroot-import', version=1,
                               manifest_sha256=manifest_sha256, manifest=manifest))
        os.chmod(tree, manifest['root_mode'])
        if stat.S_IMODE(os.stat(tree).st_mode) != manifest['root_mode']:
            raise SDKError('destination filesystem cannot preserve root mode')
        publication.publish_directory_noreplace(tree, destination)
    finally:
        _remove_owned_stage(stage)
    return dict(destination=destination, target=target, manifest_sha256=manifest_sha256,
                qualification='transferred-and-inspected', relocation='not-assessed',
                execution_acceptance='not-assessed')
