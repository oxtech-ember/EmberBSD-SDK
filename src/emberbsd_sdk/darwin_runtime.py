# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Developed with AI assistance. The format sources are listed in docs/darwin-runtime.md.
"""Inspect explicit Darwin runtime inputs. This module does not run tools."""

from contextlib import ExitStack
import hashlib
import errno
import os
import re
import struct
import sys

from .errors import SDKError
from .inventory import sha256_file, snapshot_tree
from .native_toolset import _absolute, _darwin_slice, _json, _require, _sha
from .paths import (canonical_existing, canonical_new, inside, open_regular,
                    real_directory, reject_overlap, verify_stable)

DARWIN_RUNTIME_AUDIT_API = 1
_LOADS = {0xc, 0x80000018, 0x8000001f, 0x20, 0x80000023}
# These commands do not add a dependent image. Reject unknown commands rather
# than omit a future dependency encoding. See the pinned Apple format sources.
_OTHER_COMMANDS = {2, 4, 5, 0xb, 0xe, 0x10, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17,
                   0x1a, 0x1d, 0x1e, 0x22, 0x80000022,
                   0x26, 0x80000028, 0x29, 0x2a, 0x2b, 0x2d, 0x2e, 0x31,
                   0x80000033, 0x80000034, 0x36, 0x37, 0x38, 0x39}
_MAX_METADATA = 16 * 1024 * 1024
_MAX_LAZY_SYMBOLS = 4096
_MAX_LAZY_SYMBOL_BYTES = 1024 * 1024
_MAX_RPATH_CANDIDATES = 4096
_MAX_OUTPUTS = 64
_MAX_OBSERVED_ROOTS = 64
_MAX_TRACE_LINES = 16384
_TRACE_IMAGE = re.compile(r'dyld\[([1-9][0-9]*)\]: <([0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12})> (/.+)')


def _cstring(data, offset, end=None):
    end = len(data) if end is None else end
    _require(0 <= offset < end <= len(data), 'invalid Mach-O/cache string offset')
    stop = data.find(b'\0', offset, end)
    _require(stop >= offset and stop - offset <= 4096, 'unterminated Mach-O/cache string')
    value = data[offset:stop].decode('utf-8', 'strict')
    _require(value and not any(ord(c) < 32 or ord(c) == 127 for c in value),
             'invalid Mach-O/cache string')
    return value, stop + 1


def _uleb(data, offset, end):
    value = 0
    for shift in range(0, 64, 7):
        _require(offset < end, 'truncated cache trie integer')
        byte = data[offset]
        offset += 1
        _require(shift < 63 or byte <= 1, 'overflowing cache trie integer')
        value |= (byte & 0x7f) << shift
        if not byte & 0x80:
            return value, offset
    raise SDKError('overflowing cache trie integer')


class _File:
    def __init__(self, path, stack):
        self.path = path
        self.handle, self.before = open_regular(path)
        stack.callback(self.close)
        self.size = self.before.st_size

    def close(self):
        try:
            verify_stable(self.path, self.before)
        finally:
            self.handle.close()

    def read(self, offset, size):
        _require(type(offset) is int and type(size) is int and 0 <= size <= _MAX_METADATA
                 and 0 <= offset <= self.size and size <= self.size - offset,
                 'Mach-O/cache metadata is outside its file')
        self.handle.seek(offset)
        data = self.handle.read(size)
        _require(len(data) == size, 'truncated Mach-O/cache file')
        return data


def _macho(read, host, *, cached=False, executable=False):
    header = read(0, 32)
    magic, cpu, subtype, kind, count, size, flags, reserved = struct.unpack('<8I', header)
    expected = {'arm64': 0x100000c, 'aarch64': 0x100000c,
                'x86_64': 0x1000007, 'amd64': 0x1000007}.get(host['cpu'])
    _require(magic == 0xfeedfacf and expected is not None and cpu == expected and
             kind == (2 if executable else 6), 'expected a native Darwin Mach-O image')
    _require(not cached or flags & 0x80000000, 'image is not marked as a cached dylib')
    _require(0 < count <= 8192 and 8 * count <= size <= 4 * 1024 * 1024,
             'invalid Mach-O load command extent')
    commands = read(32, size)
    result = dict(cpu=cpu, subtype=subtype, filetype=kind, uuid=None,
                  dylib_version=None, build_versions=[],
                  install_name=None, rpaths=[], segments=[], loads=[], implicit_loads=[], lazy_info=[],
                  load_commands_sha256=hashlib.sha256(header + commands).hexdigest())
    position = 0
    for _ in range(count):
        _require(position + 8 <= size, 'truncated Mach-O load command')
        command, length = struct.unpack_from('<II', commands, position)
        _require(length >= 8 and length % 8 == 0 and length <= size - position,
                 'invalid Mach-O load command size')
        data = commands[position:position + length]
        if command in _LOADS | {0xd, 0x8000001c}:
            minimum = 12 if command == 0x8000001c else 24
            _require(length >= minimum, 'truncated Mach-O dylib/path command')
            offset = struct.unpack_from('<I', data, 8)[0]
            _require(offset >= minimum, 'Mach-O string overlaps its command fields')
            name, _ = _cstring(data, offset)
            if command in _LOADS:
                _require(name.startswith(('/', '@rpath/')),
                         'relative runtime loads are not supported')
                current, compatibility = struct.unpack_from('<II', data, 16)
                attributes = {0x80000018: 1, 0x8000001f: 2, 0x80000023: 4, 0x20: 8}.get(command, 0)
                encoding = 'dylib-command'
                if offset == 28 and struct.unpack_from('<I', data, 12)[0] == 0x1a741800:
                    _require(length >= 28, 'truncated Mach-O dylib-use command')
                    attributes = struct.unpack_from('<I', data, 24)[0]
                    _require(not attributes & ~15, 'unsupported Mach-O dylib-use attributes')
                    encoding = 'dylib-use'
                result['loads'].append(dict(command=command, name=name,
                    current_version=current, compatibility_version=compatibility,
                    attributes=attributes, encoding=encoding))
            elif command == 0xd:
                _require(result['install_name'] is None, 'duplicate Mach-O install name')
                result['install_name'] = name
                current, compatibility = struct.unpack_from('<II', data, 16)
                result['dylib_version'] = dict(current=current, compatibility=compatibility)
            else:
                result['rpaths'].append(name)
        elif command == 0x1b:
            _require(length == 24 and result['uuid'] is None, 'invalid Mach-O UUID command')
            result['uuid'] = data[8:24].hex()
        elif command == 0x19:
            _require(length >= 72, 'truncated Mach-O segment command')
            name = data[8:24].split(b'\0', 1)[0].decode('ascii', 'strict')
            address, vm_size, file_offset, file_size, max_prot, init_prot, sections, segment_flags = \
                struct.unpack_from('<4Q4I', data, 24)
            _require(length == 72 + sections * 80 and file_size <= vm_size,
                     'invalid Mach-O segment extent')
            result['segments'].append(dict(name=name, address=address, vm_size=vm_size,
                file_offset=file_offset, file_size=file_size))
        elif command == 0x27:
            raise SDKError('embedded dyld environment commands are not supported')
        elif command == 0x32:
            _require(length >= 24, 'truncated Mach-O build version')
            platform, minimum, sdk, tools = struct.unpack_from('<4I', data, 8)
            _require(length == 24 + tools * 8, 'invalid Mach-O build version tools')
            result['build_versions'].append(dict(platform=platform, minimum=minimum, sdk=sdk))
        elif command == 0x24:
            _require(length == 16, 'invalid Mach-O macOS minimum version')
            minimum, sdk = struct.unpack_from('<II', data, 8)
            result['build_versions'].append(dict(platform=1, minimum=minimum, sdk=sdk))
        elif command == 0x3a:
            _require(length == 16, 'invalid lazy dylib load command')
            offset, extent = struct.unpack_from('<II', data, 8)
            result['lazy_info'].append(dict(file_offset=offset, size=extent))
        elif command not in _OTHER_COMMANDS:
            raise SDKError(f'unsupported Mach-O load command: {command:#x}')
        position += length
    _require(position == size and result['uuid'] is not None and result['segments'],
             'incomplete Mach-O identity or segments')
    _require(executable or result['install_name'] is not None, 'dylib has no install name')
    _require(any(row['platform'] == 1 for row in result['build_versions']),
             'image has no supported macOS build platform')
    # UnsafeHeader::forEachLinkedDylib synthesizes this runtime edge when no
    # regular dependency command exists. It exempts the libSystem components.
    if not any(row['command'] != 0x20 for row in result['loads']) and not (
            result['install_name'] or '').startswith('/usr/lib/system/'):
        result['implicit_loads'].append(dict(command=None, name='/usr/lib/libSystem.B.dylib',
            reason='dyld-synthesized-libSystem', current_version=0x10000,
            compatibility_version=0x10000, attributes=0))
    return result


def _lazy_loads(image, read, symbol_bytes):
    for info in image['lazy_info']:
        offset, size = info['file_offset'], info['size']
        segments = [segment for segment in image['segments']
                    if segment['name'] == '__LINKEDIT' and segment['file_offset'] <= offset and
                    size <= segment['file_size'] - (offset - segment['file_offset'])]
        _require(len(segments) == 1 and size >= 24, 'lazy dylib metadata is outside __LINKEDIT')
        data = read(segments[0], offset, size)
        path_offset, flag_offset, flags, pointer_format, chain_offset, count, symbols_offset = \
            struct.unpack_from('<IIHHIII', data)
        _require(24 <= path_offset < size and
                 24 <= symbols_offset <= size and count <= (size - symbols_offset) // 4,
                 f'invalid lazy dylib metadata fields: path={path_offset}, flags={flags}, '
                 f'symbols={count}@{symbols_offset}, extent={size}')
        _require(count <= _MAX_LAZY_SYMBOLS, 'lazy dylib symbol count exceeds the supported limit')
        name, _ = _cstring(data, path_offset)
        _require(name.startswith('/'), 'relative and @rpath runtime loads are not supported')
        base = min(segment['address'] for segment in image['segments'] if segment['file_size'])
        for address, width in ((base + flag_offset, 4), (base + chain_offset, 8)):
            _require(any(segment['address'] <= address and width <=
                         segment['vm_size'] - (address - segment['address'])
                         for segment in image['segments']), 'lazy dylib pointer is outside its image')
        symbols = []
        for index in range(count):
            symbol_offset = struct.unpack_from('<I', data, symbols_offset + index * 4)[0]
            symbol, end = _cstring(data, symbol_offset)
            symbol_bytes[0] += end - symbol_offset - 1
            _require(symbol_bytes[0] <= _MAX_LAZY_SYMBOL_BYTES,
                     'lazy dylib decoded symbols exceed the audit byte budget')
            symbols.append(symbol)
        info.update(sha256=hashlib.sha256(data).hexdigest(), flag_offset=flag_offset,
                    flags=flags, pointer_format=pointer_format, chain_offset=chain_offset,
                    symbols=symbols)
        image['loads'].append(dict(command=0x3a, name=name, attributes=8 | (flags & 1),
                                  encoding='lazy-load-info'))


class _Cache:
    def __init__(self, main, descriptor, stack):
        self.main = _absolute(main)
        _require(canonical_existing(self.main) == self.main, 'cache path must have real ancestors')
        self.root = os.path.dirname(self.main)
        entries = [row for row in descriptor['host_abi']['libraries'] if row['root'] == self.root]
        _require(len(entries) == 1, 'cache directory must be one explicit native ABI library root')
        self.inventory_ref = entries[0]['inventory']
        self.inventory = _json(self.inventory_ref)
        # The final inventory check verifies actual bytes. An inventory
        # reference alone does not authenticate a cache file.
        self.files, self.mappings, self.rows = {}, [], []
        root, header = self._file(os.path.basename(main), descriptor['host'], stack)
        self.uuid = header[0x58:0x68].hex()
        _require(self.uuid != '0' * 32, 'Darwin cache UUID is missing')
        mapping_offset = struct.unpack_from('<I', header, 0x10)[0]
        _require(mapping_offset > 0x1c8 and struct.unpack_from('<I', header, 0xd8)[0] == 1,
                 'unsupported Darwin cache header/platform')
        self.base = struct.unpack_from('<Q', header, 0xe0)[0]
        self.os_version = struct.unpack_from('<I', header, 0x16c)[0]
        _require(self.os_version, 'Darwin cache has no OS version')
        _require(self.rows[0]['mappings'][0][0] == self.base and
                 self.rows[0]['mappings'][0][2] == 0, 'invalid main cache base mapping')
        sub_offset, sub_count = struct.unpack_from('<II', header, 0x188)
        _require(sub_count <= 256, 'too many Darwin subcaches')
        table = root.read(sub_offset, sub_count * 56)
        uuids = {self.uuid}
        for index in range(sub_count):
            uuid, vm_offset, suffix_bytes = struct.unpack_from('<16sQ32s', table, index * 56)
            suffix, _ = _cstring(suffix_bytes, 0)
            _require(suffix.startswith('.') and '/' not in suffix and suffix not in ('.', '..'),
                     'invalid Darwin subcache suffix')
            name = os.path.basename(main) + suffix
            sub, sub_header = self._file(name, descriptor['host'], stack)
            _require(sub_header[0x58:0x68] == uuid and uuid.hex() not in uuids,
                     'Darwin subcache UUID mismatch or duplicate')
            uuids.add(uuid.hex())
            _require(self.rows[-1]['mappings'][0][0] == self.base + vm_offset,
                     'Darwin subcache VM offset mismatch')
        self.mappings.sort(key=lambda row: row[0])
        for previous, current in zip(self.mappings, self.mappings[1:]):
            _require(previous[0] + previous[1] <= current[0], 'overlapping Darwin cache mappings')
        image_offset, image_count = struct.unpack_from('<II', header, 0x1c0)
        _require(0 < image_count <= 65536, 'invalid Darwin cache image count')
        self.images = []
        table = root.read(image_offset, image_count * 32)
        for index in range(image_count):
            address, mtime, inode, path_offset, padding = struct.unpack_from('<3Q2I', table, index * 32)
            name, _ = _cstring(root.read(path_offset, min(4097, root.size - path_offset)), 0)
            _require(name.startswith('/'), 'cache image path must be absolute')
            self.images.append((address, name))
        trie_address, trie_size = struct.unpack_from('<QQ', header, 0x108)
        _require(0 < trie_size <= _MAX_METADATA, 'invalid Darwin cache dylib trie size')
        self.trie = self.read_vm(trie_address, trie_size)

    def _file(self, name, host, stack):
        _require(name not in self.files and '/' not in name, 'duplicate or invalid cache file')
        record = self.inventory.get(name)
        _require(isinstance(record, dict) and record.get('kind') == 'file',
                 'cache file is absent from the ABI inventory')
        file = _File(os.path.join(self.root, name), stack)
        header = file.read(0, 0x1cc)
        architecture = header[:16].rstrip(b'\0 ').decode('ascii', 'strict')
        allowed = ('dyld_v1  arm64e', 'dyld_v1   arm64') if host['cpu'] in ('arm64', 'aarch64') \
            else ('dyld_v1  x86_64', 'dyld_v1 x86_64h')
        _require(architecture in allowed and file.size == record['size'], 'cache architecture/size mismatch')
        offset, count = struct.unpack_from('<II', header, 0x10)
        _require(0x1cc <= offset <= 4096 and 0 < count <= 32, 'unsupported cache mappings')
        mapping_data = file.read(offset, count * 32)
        mappings = []
        for index in range(count):
            address, size, file_offset, max_prot, init_prot = struct.unpack_from('<3Q2I', mapping_data, index * 32)
            _require(size and file_offset <= file.size and size <= file.size - file_offset,
                     'cache mapping exceeds its file')
            mappings.append([address, size, file_offset, max_prot, init_prot])
            self.mappings.append((address, size, file_offset, name))
        self.files[name] = file
        self.rows.append(dict(name=name, uuid=header[0x58:0x68].hex(), size=file.size,
                              sha256=_sha(record['sha256']), mappings=mappings))
        return file, header

    def spans(self, address, size):
        result = []
        while size:
            mapping = next((row for row in self.mappings
                            if row[0] <= address < row[0] + row[1]), None)
            _require(mapping is not None, 'image bytes are outside Darwin cache mappings')
            base, length, offset, name = mapping
            count = min(size, base + length - address)
            result.append(dict(file=name, offset=offset + address - base, size=count))
            address += count
            size -= count
        return result

    def read_vm(self, address, size):
        _require(0 <= size <= _MAX_METADATA, 'cache metadata read is too large')
        return b''.join(self.files[row['file']].read(row['offset'], row['size'])
                        for row in self.spans(address, size))

    def lookup(self, name):
        rest = name.encode('utf-8')
        offset, seen = 0, set()
        while True:
            _require(offset not in seen and offset < len(self.trie), 'cyclic/outside cache dylib trie')
            seen.add(offset)
            terminal, position = _uleb(self.trie, offset, len(self.trie))
            end = position + terminal
            _require(end < len(self.trie), 'truncated cache trie node')
            if not rest and terminal:
                index, consumed = _uleb(self.trie, position, end)
                _require(consumed == end and index < len(self.images), 'invalid cache image index')
                return index
            children = self.trie[end]
            position = end + 1
            matched = None
            for _ in range(children):
                edge, position = _cstring(self.trie, position)
                child, position = _uleb(self.trie, position, len(self.trie))
                if rest.startswith(edge.encode('utf-8')):
                    _require(matched is None, 'ambiguous cache dylib trie edge')
                    matched = (child, len(edge.encode('utf-8')))
            if matched is None:
                return None
            offset, consumed = matched
            rest = rest[consumed:]


def _executable_rpath_anchor(rpath, image, path, bundle):
    """Expand only this selected executable's own, contained LC_RPATH."""
    _require(image['filetype'] == 2 and inside(path, bundle),
             'executable-relative rpath requires its selected in-bundle executable owner')
    anchor = os.path.dirname(path)
    for part in rpath[len('@executable_path'):].split('/'):
        if part in ('', '.'):
            continue
        if part == '..':
            # Do not erase a missing or aliased directory before checking it.
            real_directory(anchor)
            anchor = os.path.dirname(anchor)
        else:
            anchor = os.path.join(anchor, part)
        _require(os.path.commonpath((anchor, bundle)) == bundle and
                 canonical_new(anchor) == anchor,
                 'executable-relative rpath leaves the bundle or uses an aliased directory')
        if os.path.lexists(anchor):
            real_directory(anchor)
    return anchor


def _rpath_target(load, image, path, bundle, missing, candidates_used, output_root=None):
    """Resolve a successful own-image rpath before inherited paths are needed."""
    name = load['name']
    leaf = name[7:]
    _require(leaf not in ('', '.', '..') and '/' not in leaf and '\\' not in leaf,
             'only one @rpath runtime leaf is supported')
    _require(canonical_existing(bundle) == bundle and
             (inside(path, bundle) or output_root is not None and inside(path, output_root)) and
             load['command'] in _LOADS - {0x20},
             '@rpath runtime loads require an in-bundle file image')
    _require(0 < len(image['rpaths']) <= 64,
             'inherited or missing @rpath runtime loads are not supported')
    candidates, targets = [], set()
    for rpath in image['rpaths']:
        candidates_used[0] += 1
        _require(candidates_used[0] <= _MAX_RPATH_CANDIDATES,
                 'runtime rpath candidates exceed the audit budget')
        if rpath == '@loader_path':
            anchor = os.path.dirname(path)
        elif rpath.startswith('@loader_path/'):
            suffix = rpath[13:].split('/')
            _require(all(part not in ('', '.', '..') for part in suffix),
                     'unsupported loader-relative rpath traversal')
            anchor = os.path.join(os.path.dirname(path), *suffix)
        elif rpath == '@executable_path' or rpath.startswith('@executable_path/'):
            anchor = _executable_rpath_anchor(rpath, image, path, bundle)
        else:
            anchor = _absolute(rpath)
        output_anchor = (output_root is not None and rpath.startswith('@loader_path') and
                         os.path.commonpath((anchor, output_root)) == output_root)
        _require((os.path.commonpath((anchor, bundle)) == bundle or output_anchor) and
                 canonical_new(anchor) == anchor,
                 'runtime rpath anchor must be a real in-bundle or explicit output path')
        candidate = os.path.join(anchor, leaf)
        record = dict(rpath=rpath, path=candidate, target=None)
        if _absent(candidate):
            missing.add(candidate)
        else:
            target = canonical_existing(candidate)
            _require(target == candidate and inside(target, bundle),
                     'runtime rpath target must be a real in-bundle path')
            record['target'] = target
            targets.add(target)
        candidates.append(record)
    _require(len(targets) == 1, 'missing or ambiguous image-local @rpath runtime target')
    target = targets.pop()
    load['rpath_resolution'] = dict(owner=path, candidates=candidates)
    return target


def _reference(value):
    _require(isinstance(value, dict) and set(value) == {'path', 'sha256'},
             'expected an explicit runtime path and SHA256 reference')
    return dict(path=_absolute(value['path']), sha256=_sha(value['sha256']))


def _output_inputs(roots, outputs, output_read_root, observed_cache_roots):
    if outputs is None and output_read_root is None and observed_cache_roots is None:
        return None
    _require(outputs is not None and output_read_root is not None and observed_cache_roots is not None,
             'output runtime audit needs outputs, read root, and observations together')
    root = _absolute(output_read_root)
    _require(canonical_existing(root) == root, 'output read root must have its real spelling')
    real_directory(root)
    for native_root in roots:
        reject_overlap(root, native_root)
    _require(isinstance(outputs, list) and 0 < len(outputs) <= _MAX_OUTPUTS,
             'output runtime audit supports one to 64 output references')
    references = [_reference(row) for row in outputs]
    _require(len({row['path'] for row in references}) == len(references), 'duplicate runtime output')
    for row in references:
        path = row['path']
        _require(canonical_existing(path) == path and inside(path, root),
                 'runtime output must be a real file inside its explicit read root')
    reference = _reference(observed_cache_roots)
    observation = _json(reference)
    _require(set(observation) == {'format', 'version', 'scope', 'cache_uuid', 'observations'} and
             observation['format'] == 'emberbsd-darwin-observed-cache-roots' and
             type(observation['version']) is int and observation['version'] == 1 and
             observation['scope'] == 'producer-output', 'unsupported output runtime observation')
    rows = observation['observations']
    _require(isinstance(rows, list) and len(rows) == len(references),
             'each output needs one explicit runtime observation')
    by_path = {row['path']: row for row in references}
    seen, trace_paths, root_count = set(), set(), 0
    for row in rows:
        _require(isinstance(row, dict) and set(row) == {'output', 'trace', 'roots'},
                 'unsupported output runtime observation fields')
        output, trace = _reference(row['output']), _reference(row['trace'])
        _require(output == by_path.get(output['path']) and output['path'] not in seen,
                 'observation output differs from the caller-pinned output')
        _require(trace['path'] not in trace_paths, 'each output needs a distinct pinned trace')
        seen.add(output['path'])
        trace_paths.add(trace['path'])
        _require(isinstance(row['roots'], list), 'observed cache roots must be a list')
        root_count += len(row['roots'])
        _require(root_count <= _MAX_OBSERVED_ROOTS, 'observed cache roots exceed the audit limit')
        names = set()
        for image in row['roots']:
            _require(isinstance(image, dict) and set(image) == {'path', 'uuid'},
                     'unsupported observed cache root fields')
            name = _absolute(image['path'])
            _require(name not in names and isinstance(image['uuid'], str) and
                     re.fullmatch('[0-9a-f]{32}', image['uuid']), 'invalid observed cache root identity')
            names.add(name)
    return dict(root=root, outputs=sorted(references, key=lambda row: row['path']),
                reference=reference, observation=observation)


def _output_traces(context, cache, stack):
    _require(context['observation']['cache_uuid'] == cache.uuid, 'observed runtime cache UUID mismatch')
    traces, roots, total_bytes, total_lines = [], set(), 0, 0
    for row in context['observation']['observations']:
        file = _File(row['trace']['path'], stack)
        total_bytes += file.size
        _require(total_bytes <= _MAX_METADATA, 'runtime traces exceed the audit byte limit')
        data = file.read(0, file.size)
        _require(hashlib.sha256(data).hexdigest() == row['trace']['sha256'], 'runtime trace SHA256 mismatch')
        lines = data.decode('utf-8', 'strict').splitlines()
        total_lines += len(lines)
        _require(total_lines <= _MAX_TRACE_LINES, 'runtime traces exceed the audit line limit')
        images, pid = {}, None
        for line in lines:
            _require(len(line) <= 8192, 'runtime trace line exceeds the supported limit')
            if not line.lstrip().startswith('dyld'):
                continue
            match = _TRACE_IMAGE.fullmatch(line)
            _require(match is not None, 'unsupported DYLD trace line')
            process, uuid, name = match.groups()
            _require(pid is None or pid == process, 'runtime trace contains multiple process IDs')
            pid = process
            name, uuid = _absolute(name), uuid.replace('-', '').lower()
            _require(name not in images, 'duplicate runtime trace image')
            images[name] = uuid
        _require(row['output']['path'] in images, 'runtime trace does not name its output')
        for root in row['roots']:
            _require(cache.lookup(root['path']) is not None and
                     images.get(root['path']) == root['uuid'],
                     'observed cache root is not in its pinned trace and cache')
            roots.add(root['path'])
        traces.append((row, images))
    return traces, roots


def _verify_output_traces(context, traces, nodes, cache):
    _require(canonical_existing(context['root']) == context['root'] and
             all(canonical_existing(row['path']) == row['path'] for row in context['outputs']),
             'runtime output read paths changed')
    cached = {node['index']: node['image']['uuid'] for node in nodes.values()
              if node['kind'] == 'cache-image'}
    for row, images in traces:
        for name, uuid in images.items():
            node = nodes.get(name)
            if node is not None and node['kind'] != 'cache-image':
                actual = node['image']['uuid']
            else:
                actual = cached.get(cache.lookup(name))
            _require(actual == uuid, 'observed DYLD image is absent or differs from the audited graph')
        for root in row['roots']:
            _require(nodes[root['path']]['image']['uuid'] == root['uuid'],
                     'observed cache root UUID differs from its actual image')
    _require(_json(context['reference']) == context['observation'], 'runtime observation changed')


def capture_darwin_runtime_audit(descriptor, *, cache_main, outputs=None,
                                output_read_root=None, observed_cache_roots=None):
    """Return byte-bound runtime metadata. This does not qualify a toolset."""
    try:
        return _capture(descriptor, cache_main, outputs, output_read_root, observed_cache_roots)
    except (OSError, KeyError, TypeError, ValueError, AttributeError, OverflowError, struct.error) as exc:
        raise SDKError(f'invalid Darwin runtime inputs: {exc}') from exc


def _capture(descriptor, cache_main, outputs, output_read_root, observed_cache_roots):
    host = descriptor['host']
    _require(host['os'] == 'Darwin', 'Darwin cache inputs require a Darwin host')
    roots = [descriptor['bundle_root']] + [row['root'] for row in descriptor['host_abi']['libraries']]
    output_context = _output_inputs(roots, outputs, output_read_root, observed_cache_roots)
    output_refs = {} if output_context is None else {row['path']: row for row in output_context['outputs']}
    selected_tools = [row['path'] for row in descriptor['tools'].values() if row['kind'] == 'native']
    tools = {canonical_existing(path) for path in selected_tools}
    aliased_images = {canonical_existing(path) for path in selected_tools
                      if canonical_existing(path) != path}
    _require(tools, 'Darwin runtime graph needs native tool roots')
    with ExitStack() as stack:
        cache = _Cache(cache_main, descriptor, stack)
        traces, observed_roots = ([], set()) if output_context is None else _output_traces(
            output_context, cache, stack)
        executables = tools | set(output_refs)
        nodes, pending, absent = {}, sorted(executables | observed_roots), set()
        symbol_bytes = [0]
        missing_rpaths, rpath_candidates = set(), [0]
        while pending:
            path = pending.pop()
            if path in nodes:
                continue
            _require(len(nodes) < 8192, 'Darwin runtime graph is too large')
            index = None if path in executables else cache.lookup(path)
            if index is not None:
                address, catalog_name = cache.images[index]
                image = _macho(lambda offset, size: cache.read_vm(address + offset, size), host, cached=True)
                _require(image['install_name'] == catalog_name and cache.lookup(catalog_name) == index,
                         'cache trie/catalog/Mach-O install name mismatch')
                node = dict(kind='cache-image', index=index, catalog_name=catalog_name, image=image)
                for segment in image['segments']:
                    segment['spans'] = cache.spans(segment['address'], segment['file_size'])
                _lazy_loads(image, lambda segment, offset, size: cache.read_vm(
                    segment['address'] + offset - segment['file_offset'], size), symbol_bytes)
            else:
                _require(path.startswith('/') and canonical_existing(path) == path and
                         (path in output_refs or any(inside(path, root) for root in roots)),
                         'runtime dependency is outside declared native inputs')
                file = _File(path, stack)
                slice_offset, slice_size, wrapper = _darwin_slice(
                    file.read, file.size, host, shared_library=path not in executables)
                def read_image(offset, size):
                    _require(0 <= offset <= slice_size and 0 <= size <= slice_size - offset,
                             'Mach-O metadata is outside its selected slice')
                    return file.read(slice_offset + offset, size)
                image = _macho(read_image, host, executable=path in executables)
                node = dict(kind='output-executable' if path in output_refs else 'file',
                            sha256=sha256_file(path), image=image)
                if path in output_refs:
                    _require(node['sha256'] == output_refs[path]['sha256'], 'runtime output SHA256 mismatch')
                if wrapper is not None:
                    node['macho_slice'] = wrapper
                for segment in image['segments']:
                    _require(segment['file_offset'] <= slice_size and
                             segment['file_size'] <= slice_size - segment['file_offset'],
                             'Mach-O segment exceeds its file')
                _lazy_loads(image, lambda segment, offset, size: read_image(offset, size), symbol_bytes)
            nodes[path] = node
            _require(all(row['minimum'] <= cache.os_version for row in image['build_versions']
                         if row['platform'] == 1), 'image needs a newer macOS runtime')
            for load in image['loads'] + image['implicit_loads']:
                name = load['name']
                if name.startswith('@rpath/'):
                    _require(node['kind'] != 'cache-image', '@rpath runtime loads in cached images are not supported')
                    target = _rpath_target(load, image, path, descriptor['bundle_root'],
                                           missing_rpaths, rpath_candidates,
                                           output_context['root'] if path in output_refs else None)
                elif cache.lookup(name) is not None:
                    target = name
                elif _absent(name):
                    _require(load['attributes'] & 1 and not load['attributes'] & 2,
                             'required Darwin runtime dependency is absent')
                    # Keep the original load command and its explicit absence.
                    # Only an effective weak dylib load can have a null target.
                    load['target'] = None
                    absent.add(name)
                    continue
                else:
                    # Current dyld breaks regular cached-image weak edges that
                    # leave the cache, even when an on-disk file exists. This
                    # API supports a null edge only after verified absence.
                    _require(not (node['kind'] == 'cache-image' and
                             load['command'] in _LOADS - {0x20} and
                             load['attributes'] & 1 and not load['attributes'] & 2),
                             'cached weak dependency exists outside the cache')
                    target = canonical_existing(name)
                    if target != name:
                        aliased_images.add(target)
                _require(target not in executables, 'dylib edge selects a native executable')
                load['target'] = target
                pending.append(target)
        _require(not any(path in aliased_images and any(load['name'].startswith('@rpath/')
                     for load in node['image']['loads']) for path, node in nodes.items()),
                 '@rpath owner cannot have an aliased load path')
        # Readers stay open from metadata parsing through this final content
        # check. Their stable identities are checked when ExitStack drains.
        _require(snapshot_tree(cache.root) == cache.inventory, 'Darwin cache inventory changed')
        _require(all(_absent(path) for path in absent), 'missing weak Darwin dependency appeared')
        _require(all(_absent(path) for path in missing_rpaths), 'missing runtime rpath candidate appeared')
        if output_context is not None:
            _verify_output_traces(output_context, traces, nodes, cache)
        result = dict(format='emberbsd-darwin-runtime-audit', version=1,
                    api=DARWIN_RUNTIME_AUDIT_API, host=host,
                    cache=dict(main=cache.main, root=cache.root, inventory=cache.inventory_ref,
                               uuid=cache.uuid, os_version=cache.os_version, files=cache.rows),
                    tools=sorted(tools), nodes=dict(sorted(nodes.items())))
        if output_context is not None:
            result.update(format='emberbsd-darwin-output-runtime-audit', scope='producer-output',
                          output_runtime=dict(read_root=output_context['root'],
                              outputs=output_context['outputs'], observed_cache_roots=output_context['reference']))
        return result


def darwin_runtime_closure(audit, audit_reference):
    """Bind a captured graph to an external, caller-pinned audit JSON file."""
    result = {}
    for path, node in audit['nodes'].items():
        dependencies = list(dict.fromkeys(load['target'] for load in
                                         node['image']['loads'] + node['image']['implicit_loads']
                                         if load['target'] is not None))
        if node['kind'] == 'cache-image':
            result[path] = dict(cache_image=dict(audit_reference), dependencies=dependencies)
        elif node['kind'] == 'output-executable':
            result[path] = dict(output_executable=dict(audit_reference), dependencies=dependencies)
        else:
            result[path] = dict(sha256=node['sha256'], dependencies=dependencies)
    return result


def validate_darwin_runtime_closure(value, descriptor, *, outputs=None,
                                    output_read_root=None, observed_cache_roots=None):
    """Verify one explicit cache audit and all file/cache graph nodes."""
    try:
        _require(isinstance(value, dict) and value, 'missing Darwin runtime closure')
        references = [row[key] for row in value.values() if isinstance(row, dict)
                      for key in ('cache_image', 'output_executable') if key in row]
        _require(references and all(row == references[0] for row in references),
                 'Darwin runtime closure needs one common cache audit')
        reference = references[0]
        audit = _json(reference)
        _require(audit['cache']['uuid'] == _runtime_cache_uuid(),
                 'declared Darwin cache is not the current process runtime cache')
        actual = capture_darwin_runtime_audit(descriptor, cache_main=audit['cache']['main'],
            outputs=outputs, output_read_root=output_read_root, observed_cache_roots=observed_cache_roots)
        _require(audit == actual, 'Darwin runtime audit differs from current bytes or edges')
        _require(value == darwin_runtime_closure(actual, reference),
                 'Darwin runtime closure differs from the complete audited graph')
    except (OSError, KeyError, TypeError, ValueError, AttributeError, OverflowError, struct.error) as exc:
        raise SDKError(f'invalid Darwin runtime closure: {exc}') from exc


def _absent(path):
    try:
        os.lstat(path)
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ENOTDIR):
            return True
        raise
    return False


def _runtime_cache_uuid():
    """Use the loaded OS dyld service. Do not search for a library or tool."""
    _require(sys.platform == 'darwin', 'Darwin runtime validation needs a Darwin host')
    try:
        import ctypes
        query = ctypes.CDLL(None)._dyld_get_shared_cache_uuid
        query.argtypes = (ctypes.POINTER(ctypes.c_ubyte),)
        query.restype = ctypes.c_bool
        value = (ctypes.c_ubyte * 16)()
        _require(query(value), 'the current process has no dyld shared cache')
        return bytes(value).hex()
    except (ImportError, AttributeError, OSError, TypeError, ValueError) as exc:
        raise SDKError(f'Darwin runtime cache service is unavailable: {exc}') from exc
