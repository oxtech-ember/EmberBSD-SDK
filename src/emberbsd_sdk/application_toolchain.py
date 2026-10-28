# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Select a completed LLVM application runtime and explicit compiler commands."""

import os

from . import compiler_bindings, jsonio, llvm_runtime, llvm_runtime_install, paths, sysroot_transfer, target_toolchain
from .errors import SDKError

APPLICATION_TOOLCHAIN_API = 2
IMPORT_RECORD = '.prebuild-sdk-import.json'


def load_clang_runtime(sysroot, *, target):
    """Read the existing build/import record and verify its installed runtime.

    Additional package files outside the runtime prefix and regular files in
    its resource include directory are allowed. This does not verify those
    additions or establish target execution acceptance.
    """
    root = paths.canonical_existing(sysroot)
    paths.real_directory(root)
    if paths.entry(os.path.join(root, sysroot_transfer.INCOMPLETE)) is not None:
        raise SDKError('cannot select an incomplete application sysroot')
    build = os.path.join(root, sysroot_transfer.RECEIPT)
    imported = os.path.join(root, IMPORT_RECORD)
    if os.path.isfile(build) and os.path.isfile(imported):
        raise SDKError('ambiguous sysroot build and import records')
    if os.path.isfile(build):
        receipt = jsonio.read_json(build)
        sysroot_transfer._provenance(receipt, target)
        if receipt['version'] != 3:
            raise SDKError('application runtime requires an LLVM sysroot')
        record = receipt['signature']['runtime']['manifest']
        base_cxx = llvm_runtime_install.base_cxx_enabled(receipt['signature'].get('flags', []))
    elif os.path.isfile(imported):
        receipt = jsonio.read_json(imported)
        if (not isinstance(receipt, dict) or set(receipt) != {'format','version','manifest_sha256','manifest'} or
                receipt['format'] != 'emberbsd-sysroot-import' or type(receipt['version']) is not int or receipt['version'] != 1):
            raise SDKError('invalid sysroot import record')
        manifest = sysroot_transfer.validate_manifest(receipt['manifest'], target=target)
        if sysroot_transfer._hash_json(manifest) != receipt['manifest_sha256']:
            raise SDKError('sysroot import manifest digest changed')
        if manifest['version'] != 2:
            raise SDKError('application runtime requires an LLVM sysroot')
        record = manifest['runtime']['manifest']
        base_cxx = manifest['provenance'].get('base_cxx') == 'libcxx'
    else:
        raise SDKError('select a completed or imported SDK sysroot')
    selected = llvm_runtime.validate_install(root, record, target=target)
    if base_cxx:
        llvm_runtime_install.validate_base_aliases(root, record)
    return selected


def clang_application_commands(compiler, sysroot, runtime_record, *, runtime_root=None, link=False):
    """Return separate compile/link argv; callers validate selected live inputs.

    Compiler-only queries and freestanding builds use target_toolchain instead.
    A separate runtime_root supports already validated staging probes without
    requiring a completed application build receipt.
    """
    record = llvm_runtime.validate_identity(runtime_record, required_components=llvm_runtime.FULL_COMPONENTS)
    target_toolchain.validate_clang_identity(compiler, record['triplet'])
    if compiler['version'] != record['llvm_version']:
        raise SDKError('selected compiler and LLVM runtime versions differ')
    root = paths.canonical_existing(sysroot)
    runtime_root = root if runtime_root is None else paths.canonical_existing(runtime_root)
    paths.real_directory(root)
    paths.real_directory(runtime_root)
    selected = llvm_runtime.runtime_paths(runtime_root, record)
    commands = {role:list(command) for role,command in compiler['commands'].items()}
    # Host clang resource headers supply freestanding C builtins when the target
    # resource-dir has only compiler-rt. For C++ they must follow libc++ so
    # <cstddef> finds libc++'s <stddef.h> before the C resource header.
    common = ['--sysroot='+root, '-resource-dir='+selected['llvm-resource']]
    resource_includes = ['-isystem', compiler['resource_dir']+'/include']
    for role in ('cc','cxx'):
        # --ld-path is a link operation flag. Clang diagnoses it under -c -Werror.
        commands[role] = [word for word in commands[role] if not word.startswith('--ld-path=')] + common
    commands['cpp'] += common + resource_includes
    commands['cc'] += resource_includes
    commands['cxx'] += ['-nostdinc++','-isystem',
                        os.path.join(runtime_root,record['artifacts']['cxx_headers'])]
    commands['cxx'] += resource_includes
    if link:
        for role, language in (('cc','c'), ('cxx','c++')):
            commands[role] += clang_application_link_flags(compiler, root, record,
                runtime_root=runtime_root, language=language)
    return commands


def clang_application_link_flags(compiler, sysroot, runtime_record, *, runtime_root=None, language='c'):
    """Return driver-only runtime flags for the selected link language."""
    if language not in ('c', 'c++'):
        raise SDKError('application link language must be c or c++')
    record = llvm_runtime.validate_identity(runtime_record, required_components=llvm_runtime.FULL_COMPONENTS)
    target_toolchain.validate_clang_identity(compiler, record['triplet'])
    if compiler['version'] != record['llvm_version']:
        raise SDKError('selected compiler and LLVM runtime versions differ')
    root = paths.canonical_existing(sysroot)
    runtime_root = root if runtime_root is None else paths.canonical_existing(runtime_root)
    paths.real_directory(root)
    paths.real_directory(runtime_root)
    directories = tuple(dict.fromkeys(os.path.dirname(record['artifacts'][role]['shared'])
                                      for role in ('libcxx','libcxxabi','libunwind')))
    flags = ['--ld-path='+compiler['binaries']['ld']['selected_path'],
             '--rtlib=compiler-rt','--unwindlib=libunwind']
    if language == 'c++':
        flags += ['-stdlib=libc++']
    for relative in directories:
        flags += ['-L'+os.path.join(runtime_root,relative), '-Wl,-rpath,/'+relative,
                  '-Wl,-rpath-link,'+os.path.join(runtime_root,relative)]
    flags += ['-Wl,-rpath-link,'+os.path.join(root,'usr/lib'),
              '-Wl,-rpath-link,'+os.path.join(root,'usr/pkg/lib')]
    return flags


def bind_clang_application_commands(destination, compiler, sysroot, runtime_record, *, python,
                                    runtime_root=None, include_auxiliary=False):
    """Bind CC/CXX/CPP for builders that have no separate C++ link interface.

    python is the selected native-prefix interpreter. Existing compiler input
    argv are preserved, while measured runtime flags are attached only at link.
    """
    if type(include_auxiliary) is not bool:
        raise SDKError('auxiliary command selection must be boolean')
    commands = clang_application_commands(compiler, sysroot, runtime_record, runtime_root=runtime_root)
    roles = ['cc', 'cxx', 'cpp']
    if include_auxiliary:
        roles += ['as', 'ld']
        commands['ld'] += ['--sysroot=' + paths.canonical_existing(sysroot)]
    return compiler_bindings.bind_compilers(destination,
        commands={role:commands[role] for role in roles}, python=python,
        flags={name:[] for name in ('cppflags','cflags','cxxflags','ldflags')},
        link_flags={role:clang_application_link_flags(compiler, sysroot, runtime_record,
            runtime_root=runtime_root, language=language) for role,language in (('cc','c'), ('cxx','c++'))})
