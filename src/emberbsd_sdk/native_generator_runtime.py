# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD contributors (AI-assisted).
"""Inspect selected native ELF generator inputs through the host loader.

Linux glibc documents --list in ld.so(8); musl documents ldd-mode by argv[0]
in https://wiki.musl-libc.org/faq.html. NetBSD /usr/bin/ldd uses rtld's
resolution functions and its documented -f %p output (usr.bin/ldd/ldd.1 and
ldd_elfxx.c in the OS source). No ELF search order is implemented here.
"""
import os
import platform
from pathlib import Path
import re
import stat
import struct
import subprocess
import tempfile

from . import elf, host_prefix, paths
from .errors import SDKError
from .host_identity import native_cpu

NATIVE_GENERATOR_RUNTIME_API = 1
_MAX_TRACE = 4 * 1024 * 1024
_MAX_HEADERS = 4096
_OS_ROOTS = {'Linux': ('/lib', '/lib64', '/usr/lib', '/usr/lib64'),
             'NetBSD': ('/lib', '/libexec', '/usr/lib', '/usr/libexec')}
_MACHINES = {'x86_64': 62, 'amd64': 62, 'aarch64': 183, 'arm64': 183}


def _require(condition, message):
    if not condition:
        raise SDKError(message)


def _read(path):
    stream, before = paths.open_regular(str(path))
    with stream:
        data = stream.read()
    paths.verify_stable(str(path), before)
    return data


def elf_interpreter(path, machine):
    """Read one validated native ELF64 PT_INTERP without running the image."""
    stream, before = paths.open_regular(str(path))
    with stream:
        header = stream.read(64)
        parsed = elf.parse_elf_header(header, label=str(path), machine=machine)
        _require(parsed is not None and parsed['class'] == 64, 'native generator requires ELF64')
        offset = struct.unpack_from('<Q', header, 32)[0]
        size, count = struct.unpack_from('<HH', header, 54)
        _require(count <= _MAX_HEADERS and (not count or (size == 56 and offset >= 64)) and
                 offset <= before.st_size and count * size <= before.st_size - offset,
                 'invalid ELF program headers')
        stream.seek(offset)
        table = stream.read(count * size)
        found = []
        for index in range(count):
            kind, _, start, _, _, length, _, _ = struct.unpack_from('<IIQQQQQQ', table, index * size)
            if kind != 3:
                continue
            _require(1 < length <= 4096 and start <= before.st_size and length <= before.st_size - start,
                     'invalid ELF interpreter range')
            stream.seek(start)
            value = stream.read(length)
            _require(value.endswith(b'\0') and b'\0' not in value[:-1], 'invalid ELF interpreter')
            text = value[:-1].decode('utf-8', 'strict')
            _require(os.path.isabs(text) and os.path.normpath(text) == text, 'invalid ELF interpreter path')
            found.append(text)
        _require(len(found) <= 1, 'multiple ELF interpreters')
    paths.verify_stable(str(path), before)
    return found[0] if found else None


def _parse_linux(text):
    result = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        _require('not found' not in line, 'native generator has an unresolved library')
        if re.fullmatch(r'(linux-vdso\.so\.1|linux-gate\.so\.1) \(0x[0-9a-fA-F]+\)', line):
            continue
        match = re.fullmatch(r'(?:[^\s]+\s+=>\s+)?(/\S+)\s+\(0x[0-9a-fA-F]+\)', line)
        _require(match is not None, 'unrecognized native loader trace line: ' + line)
        result.append(match.group(1))
    return result


def _parse_netbsd(text):
    result = []
    for line in text.splitlines():
        _require(line.startswith('OBJECT|'), 'unrecognized NetBSD ldd output')
        path = line[len('OBJECT|'):]
        _require(path.startswith('/') and os.path.normpath(path) == path,
                 'native generator has an unresolved library')
        result.append(path)
    return result


def _trace(argv, *, executable=None):
    # Loader and ldd are native OS services; no PATH or inherited LD_* values.
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        try:
            result = subprocess.run(argv, executable=executable, env={'LC_ALL': 'C'},
                                    stdout=output, stderr=errors, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SDKError('native loader trace failed') from error
        _require(result.returncode == 0, 'native loader trace returned failure')
        _require(output.tell() <= _MAX_TRACE and errors.tell() <= _MAX_TRACE,
                 'native loader trace exceeded output limit')
        output.seek(0)
        return output.read().decode('utf-8', 'strict')


def runtime_inputs(prefix, executable, host, package_dirs, *, search_path):
    """Trace selected ELF tool; return actual runtime and owning pkgdb files.

    `package_dirs` are already-selected native pkgdb package directories.
    No package versions, library paths or search order are selected here.
    Host libc/loader paths may come only from documented OS library roots.
    """
    system, cpu = host.get('os'), host.get('cpu')
    _require(system in _OS_ROOTS and cpu in _MACHINES, 'unsupported native ELF host')
    actual_cpu = native_cpu()
    _require(system == platform.system() and actual_cpu in _MACHINES and
             _MACHINES[cpu] == _MACHINES[actual_cpu], 'selected host differs from actual host')
    prefix = Path(prefix).resolve(strict=True)
    executable = Path(host_prefix.validate_host_executable(str(prefix), str(executable), host,
                                                             search_path=search_path))
    real_exe = executable.resolve(strict=True)
    before = os.stat(real_exe)
    _require(not before.st_mode & (stat.S_ISUID | stat.S_ISGID), 'privileged generator is unsupported')
    machine = _MACHINES[cpu]
    dynamic = elf.read_elf_dynamic(str(real_exe), machine=machine)
    interpreter = elf_interpreter(real_exe, machine)
    owners = {}
    metadata = {}
    for package in package_dirs:
        package = Path(package).resolve(strict=True)
        _require(package.parent == prefix / 'pkgdb' and package.is_dir(), 'pkgdb selection outside prefix')
        contents = package / '+CONTENTS'
        version = package / '+BUILD_VERSION'
        build_info = package / '+BUILD_INFO'
        rows = _read(contents).decode('utf-8', 'strict').splitlines()
        _read(version)
        _read(build_info)
        metadata[str(package)] = (str(contents), str(version), str(build_info))
        for row in rows:
            if row and not row.startswith('@') and not row.startswith('/') and '..' not in Path(row).parts:
                owners.setdefault(row, []).append(str(package))
    _require(interpreter is not None or not dynamic['needed'], 'dynamic generator has no interpreter')
    roots = [Path(p) for p in _OS_ROOTS[system] if Path(p).is_dir()]
    if interpreter:
        loader = Path(interpreter)
        actual_loader = loader.resolve(strict=True)
        _require(loader.is_absolute() and actual_loader.is_file(), 'invalid native interpreter')
        in_prefix = loader.is_relative_to(prefix) and actual_loader.is_relative_to(prefix)
        in_os = (any(loader.is_relative_to(root) for root in roots) and
                 any(actual_loader.is_relative_to(root.resolve()) for root in roots))
        _require(in_prefix or in_os, 'native interpreter outside selected prefix and OS roots')
        if in_prefix:
            for member in {loader, actual_loader}:
                _require(len(owners.get(str(member.relative_to(prefix)), [])) == 1,
                         'native interpreter has no unique selected package owner')
    records = []
    if interpreter:
        if system == 'NetBSD':
            trace = _trace(['/usr/bin/ldd', '-f', 'OBJECT|%p\\n', '-f', 'OBJECT|%p\\n', str(executable)])
            records = _parse_netbsd(trace)
        else:
            name = Path(interpreter).name
            if name.startswith('ld-musl-'):
                trace = _trace(['ldd', str(executable)], executable=interpreter)
            elif name.startswith(('ld-linux', 'ld64.so', 'ld.so')):
                trace = _trace([interpreter, '--list', str(executable)])
            else:
                raise SDKError('unsupported Linux native loader: ' + name)
            records = _parse_linux(trace)
        records.append(interpreter)
    inputs = {str(executable), str(real_exe)}
    for item in {executable, real_exe}:
        _require(item.is_relative_to(prefix), 'native generator escapes prefix')
        packages = owners.get(str(item.relative_to(prefix)), [])
        _require(len(packages) == 1, 'generator has no unique selected package owner: ' + str(item))
        inputs.update(metadata[packages[0]])
    found_names = set()
    for value in records:
        item = Path(value)
        _require(item.is_absolute() and os.path.normpath(value) == value, 'non-absolute loader path')
        before_link = os.lstat(item)
        actual = item.resolve(strict=True)
        _require(actual.is_file(), 'loader path is not a file')
        if item.is_relative_to(prefix) and actual.is_relative_to(prefix):
            for member in {item, actual}:
                relative = str(member.relative_to(prefix))
                packages = owners.get(relative, [])
                _require(len(packages) == 1, 'runtime input has no unique selected package owner: ' + str(member))
                inputs.update(metadata[packages[0]])
        else:
            _require(any(item.is_relative_to(root) for root in roots) and
                     any(actual.is_relative_to(root.resolve()) for root in roots),
                     'runtime input outside native OS roots: ' + value)
        inputs.update((str(item), str(actual)))
        found_names.add(item.name)
        image = elf.read_elf_dynamic(str(actual), machine=machine)
        if image['soname']:
            found_names.add(image['soname'])
        _require(paths.stable_identity(os.lstat(item)) == paths.stable_identity(before_link) and
                 item.resolve(strict=True) == actual, 'runtime link changed during inspection')
    _require(all(name in found_names for name in dynamic['needed']), 'native loader trace omitted a direct dependency')
    _require(paths.stable_identity(os.stat(real_exe)) == paths.stable_identity(before), 'generator changed during trace')
    return sorted(inputs)
