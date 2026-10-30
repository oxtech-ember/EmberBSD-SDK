# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Bounded little-endian ELF32/ELF64 headers and normal GNU/BSD ar readers.

Dynamic and relocation inspection remain ELF64-only. Header-only inspection does not
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
ELF_RELOCATIONS_API = 1
MAX_RELOCATION_ENTRIES = 1_000_000

# gABI dynamic tags. GNU and Android extensions are named below.
DT_PLTRELSZ, DT_RELA, DT_RELASZ, DT_RELAENT = 2, 7, 8, 9
DT_REL, DT_RELSZ, DT_RELENT, DT_PLTREL, DT_JMPREL = 17, 18, 19, 20, 23
DT_TLSDESC_PLT, DT_TLSDESC_GOT = 0x6FFFFEF6, 0x6FFFFEF7
DT_RELACOUNT, DT_RELCOUNT = 0x6FFFFFF9, 0x6FFFFFFA
_PACKED_RELOCATION_TAGS = {
    35: 'DT_RELRSZ', 36: 'DT_RELR', 37: 'DT_RELRENT',
    0x6000000F: 'DT_ANDROID_REL', 0x60000010: 'DT_ANDROID_RELSZ',
    0x60000011: 'DT_ANDROID_RELA', 0x60000012: 'DT_ANDROID_RELASZ',
    0x6FFFE000: 'DT_ANDROID_RELR', 0x6FFFE001: 'DT_ANDROID_RELRSZ',
    0x6FFFE003: 'DT_ANDROID_RELRENT', 0x6FFFE005: 'DT_ANDROID_RELRCOUNT',
}


def parse_elf_header(data, *, label='<bytes>', machine=None, required_type=None, riscv32_soft_float=False):
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
    if riscv32_soft_float and (actual != 243 or elf_class != 1 or
            struct.unpack_from('<I', data, 36)[0] & 0xe):
        raise SDKError(f'expected RV32 soft-float target header: {label}')
    return dict({'class': 32 if elf_class == 1 else 64}, endian='little', machine=actual, type=kind)


def read_elf_header(path, machine=None, required_type=None, *, riscv32_soft_float=False):
    handle, before = open_regular(path)
    with handle:
        data = handle.read(64)
    verify_stable(path, before)
    return parse_elf_header(data, label=str(path), machine=machine, required_type=required_type,
                            riscv32_soft_float=riscv32_soft_float)


def _elf64_layout(handle, size, path, machine):
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
            dynamics.append((address, offset, filesz))
        elif kind == 0x6474e552:
            relro_ranges.append((address, memsz))
    if len(relro_ranges) > 1:
        raise SDKError(f'multiple ELF RELRO segments: {path}')
    for address, length in relro_ranges:
        if length == 0 or not any(start <= address and address - start <= size and length <= size - (address - start) for start, size in memory_loads):
            raise SDKError(f'ELF RELRO range is not inside a load segment: {path}')
    if len(dynamics) > 1:
        raise SDKError(f'multiple ELF dynamic segments: {path}')
    return result, read_at, loads, dynamics, relro_ranges, [(0, 64), (phoff, phnum * 56)]


def _elf_dynamic_tags(read_at, dynamics, path):
    tags = {}
    if dynamics:
        address, offset, length = dynamics[0]
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
        for tag, value in entries:
            tags.setdefault(tag, []).append(value)
        for tag in (5, 10, 14, 15, 29):
            if len(tags.get(tag, [])) > 1:
                raise SDKError(f'duplicate ELF dynamic singleton tag {tag}: {path}')
    return tags


def _elf_file_offset(loads, address, length, path, label):
    if address < 0 or length < 0 or address + length > 1 << 64:
        raise SDKError(f'ELF {label} address range overflows: {path}')
    offsets = [offset + address - start for start, offset, filesz in loads
               if start <= address and address - start <= filesz and
               length <= filesz - (address - start)]
    if len(offsets) != 1:
        raise SDKError(f'ELF {label} has no unique file-backed load segment: {path}')
    return offsets[0]


def read_elf_dynamic(path, *, machine=None):
    handle, before = open_regular(path)
    with handle:
        result, read_at, loads, dynamics, relro_ranges, _ = _elf64_layout(
            handle, before.st_size, path, machine)
        tags = _elf_dynamic_tags(read_at, dynamics, path)
        result.update(soname=None, needed=[], rpath=None, runpath=None, relro=bool(relro_ranges))
        if dynamics:
            strings_needed = any(tag in tags for tag in (1, 14, 15, 29))
            if strings_needed or 5 in tags or 10 in tags:
                if 5 not in tags or 10 not in tags:
                    raise SDKError(f'missing ELF dynamic string table: {path}')
                address, length = tags[5][0], tags[10][0]
                if length == 0 or length > MAX_STRTAB:
                    raise SDKError(f'invalid ELF dynamic string-table size: {path}')
                strings = read_at(_elf_file_offset(loads, address, length, path, 'string table'), length)

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


def read_elf_relocations(path, *, machine=None):
    """Read bounded ELF64 dynamic REL/RELA/PLT tables, without target policy."""
    handle, before = open_regular(path)
    with handle:
        result, read_at, loads, dynamics, _, protected = _elf64_layout(
            handle, before.st_size, path, machine)
        if result['type'] not in (2, 3):
            raise SDKError(f'expected executable or shared ELF for dynamic relocations: {path}')
        tags = _elf_dynamic_tags(read_at, dynamics, path)
        singleton_tags = (DT_REL, DT_RELSZ, DT_RELENT, DT_RELA, DT_RELASZ, DT_RELAENT,
                          DT_JMPREL, DT_PLTRELSZ, DT_PLTREL, DT_RELCOUNT, DT_RELACOUNT,
                          DT_TLSDESC_PLT, DT_TLSDESC_GOT)
        for tag in singleton_tags:
            if len(tags.get(tag, [])) > 1:
                raise SDKError(f'duplicate ELF relocation singleton tag {tag}: {path}')
        for tag, name in _PACKED_RELOCATION_TAGS.items():
            if tag in tags:
                raise SDKError(f'unsupported packed ELF relocation encoding {name}: {path}')
        for address, offset, length in dynamics:
            if (address % 8 or offset % 8 or
                    _elf_file_offset(loads, address, length, path, 'dynamic table') != offset):
                raise SDKError(f'invalid file-backed ELF dynamic table: {path}')
            protected.append((offset, length))

        def value(tag):
            return tags[tag][0]

        def counts_list(counts):
            return [dict(type=kind, count=counts[kind]) for kind in sorted(counts)]

        tables, counts, total = [], {}, 0
        used_entries = set()

        def add_table(kind, encoding, address, length):
            nonlocal total
            entry_size = 16 if encoding == 'REL' else 24
            if address % 8 or length % entry_size:
                raise SDKError(f'invalid ELF {kind} relocation table size/alignment: {path}')
            count = length // entry_size
            if count > MAX_RELOCATION_ENTRIES - total:
                raise SDKError(f'ELF relocation count exceeds limit: {path}')
            offset = _elf_file_offset(loads, address, length, path, kind + ' relocation table')
            for start, size in protected:
                if length and size and offset < start + size and start < offset + length:
                    raise SDKError(f'overlapping ELF relocation/metadata tables: {path}')
            protected.append((offset, length))
            current = {}
            packing = '<QQ' if encoding == 'REL' else '<QQq'
            for entry in struct.iter_unpack(packing, read_at(offset, length)):
                relocation_type = entry[1] & 0xFFFFFFFF
                current[relocation_type] = current.get(relocation_type, 0) + 1
                counts[relocation_type] = counts.get(relocation_type, 0) + 1
            total += count
            tables.append(dict(kind=kind, encoding=encoding, address=address, file_offset=offset,
                               size=length, entry_size=entry_size, count=count,
                               types=sorted(current), type_counts=counts_list(current)))

        for name, pointer, size, entry, expected in (
            ('REL', DT_REL, DT_RELSZ, DT_RELENT, 16),
            ('RELA', DT_RELA, DT_RELASZ, DT_RELAENT, 24),
        ):
            if entry in tags and value(entry) != expected:
                raise SDKError(f'wrong ELF {name} relocation entry size: {path}')
            if pointer in tags or size in tags:
                if not all(tag in tags for tag in (pointer, size, entry)):
                    raise SDKError(f'missing paired ELF {name} relocation tags: {path}')
                add_table(name, name, value(pointer), value(size))
                used_entries.add(entry)
        if any(tag in tags for tag in (DT_JMPREL, DT_PLTRELSZ, DT_PLTREL)):
            if not all(tag in tags for tag in (DT_JMPREL, DT_PLTRELSZ, DT_PLTREL)):
                raise SDKError(f'missing paired ELF PLT relocation tags: {path}')
            encoding = {DT_REL: 'REL', DT_RELA: 'RELA'}.get(value(DT_PLTREL))
            if encoding is None:
                raise SDKError(f'unsupported ELF PLT relocation encoding: {path}')
            add_table('JMPREL', encoding, value(DT_JMPREL), value(DT_PLTRELSZ))
            used_entries.add(DT_RELENT if encoding == 'REL' else DT_RELAENT)
        if any(tag in tags and tag not in used_entries for tag in (DT_RELENT, DT_RELAENT)):
            raise SDKError(f'orphan ELF relocation entry-size tag: {path}')
        for tag, name in ((DT_RELCOUNT, 'REL'), (DT_RELACOUNT, 'RELA')):
            if tag in tags:
                main = next((table for table in tables if table['kind'] == name), None)
                if main is None or value(tag) > main['count']:
                    raise SDKError(f'invalid ELF {name} relative relocation count: {path}')
        result.update(count=total, types=sorted(counts), type_counts=counts_list(counts), tables=tables,
                      tlsdesc=dict(plt=value(DT_TLSDESC_PLT) if DT_TLSDESC_PLT in tags else None,
                                   got=value(DT_TLSDESC_GOT) if DT_TLSDESC_GOT in tags else None))
    verify_stable(path, before)
    return result


def read_archive(path, machine, *, riscv32_soft_float=False):
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
                parsed = parse_elf_header(data, label=label, machine=machine, required_type=1,
                                          riscv32_soft_float=riscv32_soft_float)
                if parsed is None:
                    raise SDKError(f'non-ELF archive member: {label}')
                count += 1
            handle.seek(finish)
            if size % 2 and handle.read(1) != b'\n':
                raise SDKError(f'missing archive padding: {path}')
    verify_stable(path, before)
    return dict(elf_members=count, machine=machine)
