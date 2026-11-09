#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD contributors (AI-assisted).
"""Causal selected generator/loader trace checks, no target programs run."""
from pathlib import Path
import struct
import tempfile
from unittest.mock import patch
from emberbsd_sdk import native_generator_runtime as mod
from emberbsd_sdk.errors import SDKError


def fails(call):
    try:
        call()
    except (SDKError, FileNotFoundError):
        return
    raise AssertionError('invalid runtime input accepted')


with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary).resolve()
    for machine in (62, 183):
        binary = root / ('elf-' + str(machine))
        name = b'/lib/ld-linux-test.so.2\0'
        header = bytearray(64)
        header[:7] = b'\x7fELF\x02\x01\x01'
        struct.pack_into('<HHI', header, 16, 2, machine, 1)
        struct.pack_into('<Q', header, 32, 64)
        struct.pack_into('<H', header, 52, 64)
        struct.pack_into('<HH', header, 54, 56, 1)
        segment = struct.pack('<IIQQQQQQ', 3, 0, 120, 0, 0, len(name), len(name), 1)
        binary.write_bytes(header + segment + name)
        assert mod.elf_interpreter(binary, machine) == name[:-1].decode()
        fails(lambda: mod.elf_interpreter(binary, 183 if machine == 62 else 62))
        bad_header = bytearray(header)
        struct.pack_into('<Q', bad_header, 32, 10**9)
        binary.write_bytes(bad_header + segment + name)
        fails(lambda: mod.elf_interpreter(binary, machine))
        bad_segment = struct.pack('<IIQQQQQQ', 3, 0, 120, 0, 0, 10**9, 0, 1)
        binary.write_bytes(header + bad_segment + name)
        fails(lambda: mod.elf_interpreter(binary, machine))
        binary.write_bytes(header + segment + name[:-1] + b'X')
        fails(lambda: mod.elf_interpreter(binary, machine))
    assert mod._parse_linux('linux-vdso.so.1 (0x123)\nliba.so => /opt/p/lib/liba.so (0x123)\n') == ['/opt/p/lib/liba.so']
    assert mod._parse_linux('/lib/ld-musl-x86_64.so.1 (0x123)\nlibc.so => /lib/ld-musl-x86_64.so.1 (0x123)\n') == ['/lib/ld-musl-x86_64.so.1'] * 2
    assert mod._parse_netbsd('OBJECT|/usr/lib/libc.so.12\n') == ['/usr/lib/libc.so.12']
    fails(lambda: mod._parse_linux('libbad.so => not found\n'))
    fails(lambda: mod._parse_netbsd('OBJECT|not found\n'))
    prefix = root / 'prefix'
    for name in ('bin', 'lib', 'pkgdb/tool', 'pkgdb/lib'):
        (prefix / name).mkdir(parents=True)
    tool = prefix / 'bin/tool'
    library = prefix / 'lib/liba.so'
    tool.write_bytes(b'tool')
    library.write_bytes(b'library')
    tool.chmod(0o755)
    (prefix/'pkgdb/tool/+CONTENTS').write_text('bin/tool\n')
    (prefix/'pkgdb/lib/+CONTENTS').write_text('lib/liba.so\n')
    for name in ('tool', 'lib'):
        (prefix/'pkgdb'/name/'+BUILD_VERSION').write_text('owner: hash\n')
        (prefix/'pkgdb'/name/'+BUILD_INFO').write_text('PKGPATH=example/' + name + '\n')
    oslib = root / 'oslib'
    oslib.mkdir()
    interp = oslib / 'ld-linux-test.so.2'
    interp.write_bytes(b'loader')
    fake = lambda p, machine=None: {'needed': ['liba.so'] if Path(p) == tool else [],
                                     'soname': 'liba.so' if Path(p) == library else None}
    with patch.object(mod.platform, 'system', return_value='Linux'), \
         patch.object(mod, 'native_cpu', return_value='x86_64'), \
         patch.object(mod.host_prefix, 'validate_host_executable', return_value=str(tool)), \
         patch.object(mod, 'elf_interpreter', return_value=str(interp)), \
         patch.object(mod.elf, 'read_elf_dynamic', side_effect=fake), \
         patch.dict(mod._OS_ROOTS, {'Linux': (str(oslib),)}), \
         patch.object(mod, '_trace', return_value=f'liba.so => {library} (0x123)\n'):
        selected = [prefix/'pkgdb/tool', prefix/'pkgdb/lib']
        inputs = mod.runtime_inputs(prefix, tool, {'os': 'Linux', 'cpu': 'x86_64'}, selected, search_path=str(prefix/'bin'))
        assert str(library) in inputs and str(prefix/'pkgdb/lib/+CONTENTS') in inputs
        assert str(prefix/'pkgdb/lib/+BUILD_INFO') in inputs
        assert str(prefix/'pkgdb/tool/+BUILD_INFO') in inputs
        fails(lambda: mod.runtime_inputs(prefix, tool, {'os': 'Linux', 'cpu': 'x86_64'}, selected[:1], search_path=str(prefix/'bin')))
        # Loader selection follows platform ABI; it never resolves libraries itself.
        with patch.object(mod, '_trace', return_value=f'liba.so => {library} (0x123)\n') as trace:
            mod.runtime_inputs(prefix, tool, {'os': 'Linux', 'cpu': 'x86_64'}, selected, search_path=str(prefix/'bin'))
            assert trace.call_args.args[0] == [str(interp), '--list', str(tool)]
        musl = oslib/'ld-musl-x86_64.so.1'
        musl.write_bytes(b'loader')
        with patch.object(mod, 'elf_interpreter', return_value=str(musl)), \
             patch.object(mod, '_trace', return_value=f'liba.so => {library} (0x123)\n') as trace:
            mod.runtime_inputs(prefix, tool, {'os': 'Linux', 'cpu': 'x86_64'}, selected, search_path=str(prefix/'bin'))
            assert trace.call_args.args[0] == ['ldd', str(tool)]
            assert trace.call_args.kwargs['executable'] == str(musl)
        with patch.object(mod.platform, 'system', return_value='NetBSD'), \
             patch.dict(mod._OS_ROOTS, {'NetBSD': (str(oslib),)}), \
             patch.object(mod, '_trace', return_value=f'OBJECT|{library}\n') as trace:
            mod.runtime_inputs(prefix, tool, {'os': 'NetBSD', 'cpu': 'x86_64'}, selected, search_path=str(prefix/'bin'))
            assert trace.call_args.args[0][:2] == ['/usr/bin/ldd', '-f']
            with patch.object(mod.platform, 'machine', return_value='evbarm'), \
                 patch.object(mod, 'native_cpu', return_value='aarch64'):
                inputs = mod.runtime_inputs(prefix, tool, {'os': 'NetBSD', 'cpu': 'aarch64'},
                                            selected, search_path=str(prefix/'bin'))
                assert str(library) in inputs
        outside_loader = root/'ld-linux-foreign.so.2'
        outside_loader.write_bytes(b'foreign loader')
        with patch.object(mod, 'elf_interpreter', return_value=str(outside_loader)), \
             patch.object(mod, '_trace', side_effect=AssertionError('foreign loader executed')):
            fails(lambda: mod.runtime_inputs(prefix, tool, {'os': 'Linux', 'cpu': 'x86_64'}, selected, search_path=str(prefix/'bin')))
        # Cross-root OS alias (/usr/libexec -> /libexec) matches NetBSD layout.
        os_root = root / 'native-os'
        (os_root/'libexec').mkdir(parents=True)
        (os_root/'usr/libexec').mkdir(parents=True)
        net_loader = os_root/'libexec/ld.elf_so'
        net_loader.write_bytes(b'loader')
        net_alias = os_root/'usr/libexec/ld.elf_so'
        net_alias.symlink_to(net_loader)
        with patch.object(mod.platform, 'system', return_value='NetBSD'), \
             patch.dict(mod._OS_ROOTS, {'NetBSD': (str(os_root/'libexec'), str(os_root/'usr/libexec'))}), \
             patch.object(mod, 'elf_interpreter', return_value=str(net_alias)), \
             patch.object(mod, '_trace', return_value=f'OBJECT|{library}\n'):
            result = mod.runtime_inputs(prefix, tool, {'os': 'NetBSD', 'cpu': 'x86_64'}, selected, search_path=str(prefix/'bin'))
            assert str(net_alias) in result and str(net_loader) in result
        assert '/libexec' in mod._OS_ROOTS['NetBSD']
        foreign = root/'foreign.so'
        foreign.write_bytes(b'foreign')
        with patch.object(mod, '_trace', return_value=f'liba.so => {foreign} (0x123)\n'):
            fails(lambda: mod.runtime_inputs(prefix, tool, {'os': 'Linux', 'cpu': 'x86_64'}, selected, search_path=str(prefix/'bin')))
print('PASS: ELF64 CPU bounds, glibc/musl/NetBSD trace formats, ownership and foreign refusal')
