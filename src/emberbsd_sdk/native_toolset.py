# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Native host-prefix selection and shared native image and flag checks."""

import hashlib
import os
import re
import struct
from types import MappingProxyType

from .errors import SDKError
from .jsonio import MAX_JSON, loads_json
from .paths import canonical_existing, inside, open_regular, verify_stable


def load_host_prefix(prefix, *, required_roles, host=None, compiler_family='clang', abi_flags=None):
    """Load installed native Ports tools without a descriptor or receipt."""
    from .host_prefix import load_host_prefix as load
    return load(prefix, required_roles=required_roles, host=host, compiler_family=compiler_family,
                abi_flags=abi_flags)
_ABI_FLAGS = ('cppflags', 'cflags', 'cxxflags', 'ldflags')
_SHA = re.compile(r'[0-9a-f]{64}\Z')


def _require(condition, message):
    if not condition:
        raise SDKError(message)


def _text(value, label):
    _require(isinstance(value, str) and value and
             not any(ord(c) < 32 or ord(c) == 127 for c in value),
             f'invalid {label}')
    try:
        value.encode('utf-8', 'strict')
    except UnicodeError as exc:
        raise SDKError(f'invalid {label}') from exc
    return value


def _sha(value):
    _require(isinstance(value, str) and _SHA.fullmatch(value), 'invalid SHA256')
    return value


def _absolute(value):
    value = _text(value, 'absolute path')
    _require(os.path.isabs(value) and os.path.normpath(value) == value,
             f'path must be absolute and normalized: {value}')
    return value


def _bytes(reference, limit=MAX_JSON):
    _require(isinstance(reference, dict) and set(reference) == {'path', 'sha256'},
             'expected an explicit path and SHA256 reference')
    path, expected = _absolute(reference['path']), _sha(reference['sha256'])
    handle, before = open_regular(path)
    with handle:
        data = handle.read(limit + 1)
    verify_stable(path, before)
    _require(len(data) <= limit, f'evidence exceeds size limit: {path}')
    _require(hashlib.sha256(data).hexdigest() == expected, f'SHA256 mismatch: {path}')
    return data


def _json(reference):
    result = loads_json(_bytes(reference))
    _require(isinstance(result, dict), 'expected a metadata object')
    return result


def _gcc(value, host):
    _require(isinstance(value, dict) and set(value) == {'family', 'version', 'triplet'},
             'invalid native compiler identity')
    _require(value['family'] == 'gcc' and isinstance(value['version'], str) and
             re.fullmatch(r'16\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9_.-]+)?', value['version']),
             'native build tools require GCC16')
    _native_triplet(value['triplet'], host)


def _native_triplet(value, host):
    """Check the host ABI independently of the selected compiler family."""
    triplet = _text(value, 'native compiler triplet')
    cpu = {'arm64': 'aarch64', 'amd64': 'x86_64'}.get(host['cpu'], host['cpu'])
    parts = triplet.split('-')
    triplet_cpu = {'arm64': 'aarch64', 'amd64': 'x86_64'}.get(parts[0], parts[0])
    version = r'(?:[0-9]+(?:\.[0-9]+)*)?'
    os_pattern = {'Darwin': 'darwin' + version, 'Linux': r'linux(?:-(?:gnu|musl))?',
                  'NetBSD': 'netbsd' + version}.get(host['os'])
    suffix = re.fullmatch(r'(?:(?P<vendor>[a-z0-9_]*)-)?' + os_pattern,
                          '-'.join(parts[1:])) if os_pattern else None
    vendor = suffix.group('vendor') if suffix else None
    conflicting_vendor = vendor and re.fullmatch(
        r'(?:linux|darwin|netbsd|freebsd|openbsd|dragonfly|windows|mingw|cygwin)[0-9]*', vendor)
    _require(suffix is not None and triplet_cpu == cpu and not conflicting_vendor,
             'compiler triplet does not describe the actual native host')


def _darwin_executable_slice(read, size, host, header):
    """Select a supported native executable from a bounded universal image."""
    # mach-o/fat.h stores both fat_arch formats in big-endian order.
    wide = header[:4] == b'\xca\xfe\xba\xbf'
    _require(host['os'] == 'Darwin' and len(header) >= 8 and
             header[:4] in (b'\xca\xfe\xba\xbe', b'\xca\xfe\xba\xbf'),
             'unsupported native Mach-O fat image')
    count = struct.unpack_from('>I', header, 4)[0]
    entry_size = 32 if wide else 20
    table_end = 8 + count * entry_size
    _require(0 < count <= 128 and table_end <= size,
             'invalid native Mach-O fat table')
    table = read(8, count * entry_size)
    _require(len(table) == count * entry_size, 'truncated native Mach-O fat table')
    cpu = {'arm64': 0x100000c, 'aarch64': 0x100000c,
           'x86_64': 0x1000007, 'amd64': 0x1000007}.get(host['cpu'])
    # Use the generic x86_64/arm64 ABI, or the versioned arm64e ABI 0 used
    # by native macOS tools. Do not treat unknown feature bits as compatible.
    subtypes = (0, 0x80000002) if cpu == 0x100000c else (3,)
    selected = []
    extents = []
    identities = set()
    for index in range(count):
        fields = struct.unpack_from('>IIQQII' if wide else '>5I', table,
                                    index * entry_size)
        member_cpu, subtype, offset, extent, alignment = fields[:5]
        _require((not wide or fields[5] == 0) and alignment <= 31 and
                 table_end <= offset <= size and 0 < extent <= size - offset and
                 offset % (1 << alignment) == 0,
                 'invalid native Mach-O fat slice bounds')
        _require((member_cpu, subtype) not in identities,
                 'duplicate native Mach-O fat architecture')
        identities.add((member_cpu, subtype))
        extents.append((offset, offset + extent))
        if member_cpu == cpu and subtype in subtypes:
            _require(extent >= 32, 'truncated native Mach-O executable slice')
            image = read(offset, 32)
            _require(len(image) == 32 and struct.unpack_from('<4I', image) ==
                     (0xfeedfacf, member_cpu, subtype, 2),
                     'Mach-O fat executable slice identity mismatch')
            selected.append((subtypes.index(subtype), offset, extent, subtype, alignment))
    extents.sort()
    _require(all(left[1] <= right[0] for left, right in zip(extents, extents[1:])),
             'overlapping native Mach-O fat slices')
    _require(selected, 'no supported native Mach-O executable slice')
    _, offset, extent, subtype, alignment = min(selected)
    return offset, extent, dict(format='fat64-big-endian' if wide else 'fat32-big-endian',
                                cpu=cpu, subtype=subtype, offset=offset, size=extent,
                                alignment=alignment)


def _darwin_slice(read, size, host, *, shared_library=False, executable=False):
    """Select a native executable or the historical arm64 library wrapper."""
    header = read(0, min(size, 32))
    fat = header[:4] in (b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca',
                         b'\xca\xfe\xba\xbf', b'\xbf\xba\xfe\xca')
    if not fat:
        return 0, size, None
    if executable:
        return _darwin_executable_slice(read, size, host, header)
    _require(shared_library and host['os'] == 'Darwin' and
             host['cpu'] in ('arm64', 'aarch64') and len(header) >= 28 and
             header[:4] == b'\xca\xfe\xba\xbe', 'unsupported native Mach-O fat image')
    magic, count, cpu, subtype, offset, extent, alignment = struct.unpack('>7I', header[:28])
    _require(count == 1 and cpu == 0x100000c and subtype == 0 and
             alignment <= 31 and offset >= 28 and offset % (1 << alignment) == 0 and
             offset <= size and 32 <= extent <= size - offset,
             'unsupported or invalid native Mach-O fat slice')
    image = read(offset, 32)
    _require(len(image) == 32 and struct.unpack_from('<4I', image) ==
             (0xfeedfacf, cpu, subtype, 6), 'Mach-O fat slice identity mismatch')
    return offset, extent, dict(format='fat32-big-endian', cpu=cpu, subtype=subtype,
                                offset=offset, size=extent, alignment=alignment)


def _native_image(path, host, *, object_file=False, shared_library=False):
    """Reject scripts, target images and renamed wrappers before selection."""
    handle, before = open_regular(path)
    with handle:
        def read(offset, size):
            handle.seek(offset)
            return handle.read(size)
        if host['os'] == 'Darwin':
            offset, extent, wrapper = _darwin_slice(read, before.st_size, host,
                                                  shared_library=shared_library and not object_file,
                                                  executable=not (shared_library or object_file))
            header = read(offset, min(extent, 64))
        else:
            header = handle.read(64)
    verify_stable(path, before)
    cpu = {'arm64': 'aarch64', 'amd64': 'x86_64'}.get(host['cpu'], host['cpu'])
    if host['os'] == 'Darwin':
        expected = {'x86_64': 0x01000007, 'aarch64': 0x0100000c}.get(cpu)
        valid = len(header) >= 32 and header[:4] == b'\xcf\xfa\xed\xfe'
        valid = valid and struct.unpack_from('<I', header, 4)[0] == expected
        valid = valid and struct.unpack_from('<I', header, 12)[0] == (1 if object_file else 6 if shared_library else 2)
    else:
        expected = {'x86_64': 62, 'aarch64': 183}.get(cpu)
        valid = len(header) >= 64 and header[:7] == b'\x7fELF\x02\x01\x01'
        valid = valid and struct.unpack_from('<H', header, 18)[0] == expected
        valid = valid and struct.unpack_from('<H', header, 16)[0] in ((1,) if object_file else (3,) if shared_library else (2, 3))
    kind = 'object' if object_file else 'shared library' if shared_library else 'executable, not a wrapper'
    _require(expected is not None and valid, f'expected a native {kind}: {path}')


def _flags(value, allowed_roots=None, *, host=None):
    _require(isinstance(value, dict) and set(value) == set(_ABI_FLAGS),
             'expected separate native ABI flag arrays')
    result = {}
    # Keep this API deliberately small. Response files, driver overrides and
    # implicit search directories need a new qualified contract, not a bypass.
    plain = re.compile(r'(?:-O[0-3sg]|-g[0-3]?|-fPIC|-fPIE|-fno-common|-pthread|'
                       r'-std=[A-Za-z0-9+_.-]+|-D[A-Za-z_][A-Za-z0-9_]*(?:=.*)?|'
                       r'-U[A-Za-z_][A-Za-z0-9_]*|-W(?:all|extra|error|no-[A-Za-z0-9_-]+))\Z')
    for name, words in value.items():
        _require(isinstance(words, list), f'expected an array: {name}')
        normalized = []
        index = 0
        while index < len(words):
            word = _text(words[index], 'ABI flag')
            path = None
            if word in ('-I', '-L', '-isystem', '-isysroot', '--sysroot'):
                index += 1
                _require(index < len(words), 'missing ABI search path')
                path = _absolute(words[index])
                normalized.append(word)
                word = path
            elif word.startswith(('-I', '-L')) and len(word) > 2:
                path = _absolute(word[2:])
            elif word.startswith('--sysroot='):
                path = _absolute(word.split('=', 1)[1])
            elif word.startswith('-mmacosx-version-min='):
                # This deployment option selects no tools or search directories.
                component = r'(?:0|[1-9][0-9]{0,2})'
                version = r'[1-9][0-9]{0,2}(?:\.' + component + r'){0,2}'
                _require(isinstance(host, dict) and host.get('os') == 'Darwin' and
                         re.fullmatch(r'-mmacosx-version-min=' + version, word),
                         f'unsupported native Darwin deployment flag: {word}')
            else:
                _require(plain.fullmatch(word), f'unsupported native ABI flag: {word}')
            if path is not None and allowed_roots is not None:
                resolved = canonical_existing(path)
                _require(any(resolved == root or inside(resolved, root) for root in allowed_roots),
                         f'native ABI search escapes declared inputs: {path}')
            normalized.append(word)
            index += 1
        result[name] = tuple(normalized)
    return MappingProxyType(result)


def validate_selection(value, *, required_roles):
    """Check saved host-prefix selection consistency without reading files."""
    _require(isinstance(value, dict) and value.get('format') == 'emberbsd-host-prefix',
             'invalid saved native selection')
    from .host_prefix import validate_host_prefix
    validate_host_prefix(value, required_roles=required_roles)


