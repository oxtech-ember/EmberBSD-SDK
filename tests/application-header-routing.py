#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Exercise application bindings against a selected real Clang SDK sysroot."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk import application_toolchain, llvm_runtime

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--sysroot', required=True)
p.add_argument('--result', required=True, help='Existing SDK build result with selected compiler and runtime')
p.add_argument('--python', required=True, help='Selected native prefix Python')
a = p.parse_args()
result = json.loads(Path(a.result).read_text())
compiler = result['signature']['compiler']
record = result['signature']['runtime']['manifest']
sysroot = Path(a.sysroot).resolve(strict=True)
selected = llvm_runtime.runtime_paths(str(sysroot), record)
compiler_include = Path(compiler['resource_dir']) / 'include'
for name in ('stdint.h', 'stddef.h'):
    if not (compiler_include / name).is_file(): raise ValueError('missing selected compiler header: ' + name)

with tempfile.TemporaryDirectory(prefix='sdk-application-headers-') as tmp:
    root = Path(tmp).resolve()
    runtime = root / 'runtime'; runtime.mkdir()
    cxx_relative = Path(record['artifacts']['cxx_headers'])
    cxx_link = runtime / cxx_relative
    cxx_link.parent.mkdir(parents=True)
    cxx_link.symlink_to(sysroot / cxx_relative, target_is_directory=True)
    resource_relative = Path(selected['llvm-resource']).relative_to(sysroot)
    resource_include = runtime / resource_relative / 'include'
    source_c = root / 'probe.c'
    source_c.write_text('#include <stdint.h>\n#include <stddef.h>\n_Static_assert(sizeof(uint32_t)==4, "uint32_t");\nsize_t f(uint32_t x) { return x; }\n')
    source_cxx = root / 'probe.cc'
    source_cxx.write_text('#include <cstdint>\n#include <cstddef>\nstatic_assert(sizeof(std::uint32_t)==4, "uint32_t");\nstd::size_t f(std::uint32_t x) { return x; }\n')
    for mode in ('absent', 'present'):
        if mode == 'present':
            resource_include.mkdir(parents=True)
            for name in ('stdint.h','stddef.h'):
                (resource_include / name).write_text('#error target builtin include must not be selected\n')
        tools = application_toolchain.bind_clang_application_commands(root / ('bindings-' + mode), compiler,
            str(sysroot), record, python=a.python, runtime_root=str(runtime))
        for role, source in (('cc', source_c), ('cxx', source_cxx), ('cpp', source_c)):
            command = [tools[role], '-H', '-Werror', '-x', 'c++' if role == 'cxx' else 'c',
                       '-E' if role == 'cpp' else '-fsyntax-only', str(source)]
            run = subprocess.run(command, text=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            if run.returncode: raise ValueError(mode + '/' + role + ': ' + run.stderr[-3000:])
            trace = run.stderr
            if str(compiler_include / 'stdint.h') not in trace or str(sysroot / 'usr/include/stdint.h') not in trace:
                raise ValueError(mode + '/' + role + ': missing selected builtin or target base header')
            if str(resource_include) in trace or '/Applications/Xcode.app/' in trace or '/MacOSX.sdk/' in trace:
                raise ValueError(mode + '/' + role + ': unexpected target resource or Darwin header')
            if trace.count(str(compiler_include / 'stdint.h')) != 1:
                raise ValueError(mode + '/' + role + ': multiple compiler builtin stdint headers')
            if role == 'cxx':
                cxx_stddef = str(cxx_link / 'stddef.h')
                builtin_stddef = str(compiler_include / 'stddef.h')
                if cxx_stddef not in trace or builtin_stddef not in trace or trace.index(cxx_stddef) > trace.index(builtin_stddef):
                    raise ValueError(mode + '/cxx: libc++ stddef must precede compiler stddef')
    # Negative control: reproduce the old binding flags against the same SDK.
    current = application_toolchain.clang_application_commands
    def old_commands(*args, **kwargs):
        commands = current(*args, **kwargs)
        for role in ('cc', 'cxx', 'cpp'):
            commands[role] = [arg for arg in commands[role] if arg != '-nobuiltininc']
        return commands
    application_toolchain.clang_application_commands = old_commands
    try:
        old = application_toolchain.bind_clang_application_commands(root / 'bindings-old', compiler,
            str(sysroot), record, python=a.python, runtime_root=str(runtime))
    finally:
        application_toolchain.clang_application_commands = current
    failed = subprocess.run([old['cc'], '-Werror', '-fsyntax-only', str(source_c)],
                            text=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if failed.returncode == 0 or 'target builtin include must not be selected' not in failed.stderr:
        raise ValueError('negative control did not detect target builtin header selection')
print('PASS: actual C/C++/CPP bindings use one selected compiler header tree and target base, with target resource headers present and absent')
