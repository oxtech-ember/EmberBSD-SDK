# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Bounded little-endian ELF32/ELF64 headers and normal GNU/BSD ar readers.

Dynamic ELF inspection remains ELF64-only. Header-only inspection does not
qualify dynamic linkage. Package policy belongs in the caller. This module
never invokes objdump, readelf, or an extractor.
"""

import os
import re
import struct
from .errors import SDKError
from .paths import open_regular, verify_stable

MAX_HEADERS = 4096
MAX_DYNAMIC = 4 * 1024 * 1024
MAX_STRTAB = 16 * 1024 * 1024


def parse_elf_header(data, *, label='<bytes>', machine=None, required_type=None):
    if not data.startswith(b'\x7fELF'):
        return None

    if len(data) < 16:
        raise SDKError(f'expected ELF64 little-endian or ELF32 little-endian target header: {label}')
    elf_class = data[4]
    if elf_class == 1:
        expected_size, size_offset = 52, 40
        description = 'ELF32 little-endian'
    elif elf_class == 2:
        expected_size, size_offset = 64, 52
        description = 'ELF64 little-endian'
    else:
        raise SDKError(f'expected ELF64 little-endian or ELF32 little-endian target header: {label}')
    if (len(data) < expected_size or data[5:7] != b'\x01\x01' or
            struct.unpack_from('<I', data, 20)[0] != 1 or
            struct.unpack_from('<H', data, size_offset)[0] != expected_size):
        raise SDKError(f'expected {description} target header: {label}')
    kind, actual = struct.unpack_from('<HH', data, 16)
    if machine is not None and actual != machine:
        raise SDKError(f'expected {description} target header: {label}')
    if required_type is not None and kind != required_type:
        raise SDKError(f'wrong ELF type: {label}')
    return dict({'class': 32 if elf_class == 1 else 64}, endian='little', machine=actual, type=kind)


def read_elf_header(path, machine=None, required_type=None):
    handle, before = open_regular(path)
    with handle:
        data = handle.read(64)
    verify_stable(path, before)
    return parse_elf_header(data, label=str(path), machine=machine, required_type=required_type)


def read_elf_dynamic(path, *, machine=None):
    handle, before = open_regular(path)
    with handle:
        size = before.st_size

        def read_at(offset, length):
            if offset < 0 or length < 0 or offset > size or length > size - offset:
                raise SDKError(f'ELF data outside file bounds: {path}')
            handle.seek(offset)
            data = handle.read(length)
            if len(data) != length:
                raise SDKError(f'truncated ELF data: {path}')
            return data

        header = read_at(0, min(64, size))
        result = parse_elf_header(header, label=str(path), machine=machine)
        if result is None or result['class'] != 64:
            raise SDKError(f'expected ELF64 little-endian target header: {path}')
        phoff = struct.unpack_from('<Q', header, 32)[0]
        phentsize, phnum = struct.unpack_from('<HH', header, 54)
        if phnum > MAX_HEADERS or (phnum and phentsize != 56):
            raise SDKError(f'unsupported ELF program-header table: {path}')
        if phnum and phoff < 64:
            raise SDKError(f'invalid ELF program-header offset: {path}')
        table = read_at(phoff, phnum * phentsize) if phnum else b''
        loads, memory_loads, dynamics, relro_ranges = [], [], [], []
        for index in range(phnum):
            kind, flags, offset, address, physical, filesz, memsz, align = struct.unpack_from('<IIQQQQQQ', table, index * 56)
            if filesz > memsz or address + memsz > 1 << 64 or offset > size or filesz > size - offset:
                raise SDKError(f'ELF segment outside file bounds: {path}')
            if kind == 1:
                loads.append((address, offset, filesz))
                memory_loads.append((address, memsz))
            elif kind == 2:
                dynamics.append((offset, filesz))
            elif kind == 0x6474e552:
                relro_ranges.append((address, memsz))
        if len(relro_ranges) > 1:
            raise SDKError(f'multiple ELF RELRO segments: {path}')
        for address, length in relro_ranges:
            if length == 0 or not any(start <= address and address - start <= size and length <= size - (address - start) for start, size in memory_loads):
                raise SDKError(f'ELF RELRO range is not inside a load segment: {path}')
        if len(dynamics) > 1:
            raise SDKError(f'multiple ELF dynamic segments: {path}')
        result.update(soname=None, needed=[], rpath=None, runpath=None, relro=bool(relro_ranges))
        if dynamics:
            offset, length = dynamics[0]
            if length == 0 or length > MAX_DYNAMIC or length % 16:
                raise SDKError(f'invalid ELF dynamic segment size: {path}')
            entries = []
            terminated = False
            for tag, value in struct.iter_unpack('<qQ', read_at(offset, length)):
                if tag == 0:
                    terminated = True
                    break
                entries.append((tag, value))
            if not terminated:
                raise SDKError(f'unterminated ELF dynamic segment: {path}')
            tags = {}
            for tag, value in entries:
                tags.setdefault(tag, []).append(value)
            for tag in (5, 10, 14, 15, 29):
                if len(tags.get(tag, [])) > 1:
                    raise SDKError(f'duplicate ELF dynamic singleton tag {tag}: {path}')
            strings_needed = any(tag in tags for tag in (1, 14, 15, 29))
            if strings_needed or 5 in tags or 10 in tags:
                if 5 not in tags or 10 not in tags:
                    raise SDKError(f'missing ELF dynamic string table: {path}')
                address, length = tags[5][0], tags[10][0]
                if length == 0 or length > MAX_STRTAB:
                    raise SDKError(f'invalid ELF dynamic string-table size: {path}')
                offsets = [offset + address - start for start, offset, filesz in loads if start <= address and address - start <= filesz and length <= filesz - (address - start)]
                if len(offsets) != 1:
                    raise SDKError(f'ELF string table has no unique file-backed load segment: {path}')
                strings = read_at(offsets[0], length)

                def string_at(index):
                    if index >= len(strings):
                        raise SDKError(f'ELF string index outside table: {path}')
                    end = strings.find(b'\0', index)
                    if end == -1:
                        raise SDKError(f'unterminated ELF dynamic string: {path}')
                    try:
                        return strings[index:end].decode('utf-8', 'strict')
                    except UnicodeError as exc:
                        raise SDKError(f'invalid ELF dynamic string: {path}') from exc

                result['needed'] = [string_at(index) for index in tags.get(1, [])]
                for tag, key in ((14, 'soname'), (15, 'rpath'), (29, 'runpath')):
                    if tag in tags:
                        result[key] = string_at(tags[tag][0])
    verify_stable(path, before)
    return result


def read_archive(path, machine):
    count = 0
    handle, before = open_regular(path)
    with handle:
        if handle.read(8) != b'!<arch>\n':
            raise SDKError(f'expected a normal static archive: {path}')
        names = None
        while handle.tell() < before.st_size:
            header = handle.read(60)
            if len(header) != 60 or header[58:60] != b'`\n' or re.fullmatch(rb'[0-9]+ *', header[48:58]) is None:
                raise SDKError(f'invalid archive header: {path}')
            size = int(header[48:58])
            finish = handle.tell() + size
            if finish > before.st_size:
                raise SDKError(f'truncated archive member: {path}')
            name = header[:16].strip()
            if name.startswith(b'#1/'):
                if re.fullmatch(rb'#1/[0-9]+', name) is None:
                    raise SDKError(f'invalid BSD archive name: {path}')
                length = int(name[3:])
                if length > size or length > 4096:
                    raise SDKError(f'invalid BSD archive name size: {path}')
                name = handle.read(length).rstrip(b'\0')
                if b'\0' in name:
                    raise SDKError(f'invalid BSD archive name: {path}')
            if name == b'//':
                if size > MAX_STRTAB:
                    raise SDKError(f'archive name table exceeds size limit: {path}')
                names = handle.read(size)
            elif name not in (b'/', b'/SYM64/') and name.rstrip(b'/') not in (b'__.SYMDEF', b'__.SYMDEF SORTED', b'__.SYMDEF_64', b'__.SYMDEF_64 SORTED'):
                if name.startswith(b'/'):
                    if re.fullmatch(rb'/[0-9]+', name) is None or names is None:
                        raise SDKError(f'invalid GNU archive name: {path}')
                    index = int(name[1:])
                    if index >= len(names) or (index and names[index - 1:index] != b'\n'):
                        raise SDKError(f'invalid GNU archive name offset: {path}')
                    end = names.find(b'/\n', index)
                    if end == -1:
                        raise SDKError(f'unterminated GNU archive name: {path}')
                    name = names[index:end]
                data = handle.read(min(finish - handle.tell(), 64))
                label = f'{path}:{name!r}'
                if parse_elf_header(data, label=label, machine=machine, required_type=1) is None:
                    raise SDKError(f'non-ELF archive member: {label}')
                count += 1
            handle.seek(finish)
            if size % 2 and handle.read(1) != b'\n':
                raise SDKError(f'missing archive padding: {path}')
    verify_stable(path, before)
    return dict(elf_members=count, machine=machine)
