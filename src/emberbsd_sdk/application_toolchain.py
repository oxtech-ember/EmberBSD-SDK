# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Select a completed LLVM application runtime and explicit compiler commands."""

import os

from . import compiler_bindings, jsonio, llvm_runtime, llvm_runtime_install, paths, sysroot_transfer, target_toolchain, targets
from .errors import SDKError

APPLICATION_TOOLCHAIN_API = 2
FOUNDATION_TOOLCHAIN_API = 1
# Additive C-foundation binding support for separately installed target roots.
FOUNDATION_RUNTIME_BASE_API = 1
# Additive in API 2: the sysroot_alias keyword of the command functions.
SYSROOT_ALIAS = 1
IMPORT_RECORD = '.prebuild-sdk-import.json'


def _load_c_foundation_base(foundation_result, *, target):
    """Validate the immutable C-foundation result without selecting its root."""
    expected = targets.target_definition(target)
    result_path = paths.canonical_existing(foundation_result)
    result = jsonio.read_json(result_path)
    if (not isinstance(result, dict) or result.get('format') != 'emberbsd-llvm-c-foundation' or
            result.get('version') != 1 or result.get('target') != target):
        raise SDKError('C-only target tools require a matching LLVM C-foundation result')
    base = result.get('base')
    if (not isinstance(base, dict) or base.get('format') != 'emberbsd-llvm-os-stage' or
            base.get('phase') != 'base' or base.get('qualification') != 'built-and-inspected'):
        raise SDKError('C-foundation result has no completed base stage')
    signature = base.get('signature')
    if not isinstance(signature, dict):
        raise SDKError('C-foundation signature is malformed')
    selected_paths = base.get('paths')
    selected_runtime = signature.get('runtime')
    if not isinstance(selected_paths, dict) or not isinstance(selected_runtime, dict):
        raise SDKError('C-foundation paths or runtime selection is malformed')
    if signature.get('compiler_family') != 'clang' or signature.get('target') != expected:
        raise SDKError('C-foundation target compiler does not match selection')
    stage = selected_paths.get('llvm-runtime-stage')
    selected_base = selected_paths.get('destination')
    runtime = selected_runtime.get('manifest')
    if not isinstance(stage, str) or not isinstance(selected_base, str) or not isinstance(runtime, dict):
        raise SDKError('C-foundation runtime inputs are malformed')
    root = paths.canonical_existing(selected_base)
    runtime = llvm_runtime.validate_identity(runtime, target=target,
        required_components=('builtins', 'libunwind-static'))
    if set(runtime['components']) != {'builtins', 'libunwind-static'}:
        raise SDKError('C-foundation result must select only the C runtime components')
    recorded_compiler = signature.get('compiler')
    target_toolchain.validate_clang_identity(recorded_compiler, runtime['triplet'])
    measured = llvm_runtime.validate_stage(stage, target=target,
        required_components=('builtins', 'libunwind-static'), base=root)
    if measured != runtime:
        raise SDKError('C-foundation runtime manifest differs from its selected stage')
    return dict(root=root, stage=paths.canonical_existing(stage), runtime=measured)


def load_clang_c_foundation_runtime(foundation_result, sysroot, *, target, runtime_base=None):
    """Select the C compiler runtime from a completed C-foundation result."""
    selection = _load_c_foundation_base(foundation_result, target=target)
    root = paths.canonical_existing(sysroot)
    base = paths.canonical_existing(runtime_base) if runtime_base is not None else root
    if base != selection['root']:
        raise SDKError('C-foundation runtime inputs do not match the selected sysroot')
    paths.real_directory(root)
    return dict(root=root, base=base, stage=selection['stage'], runtime=selection['runtime'])


def clang_c_foundation_commands(compiler, foundation_result, sysroot, *, target, runtime_base=None):
    """Bind ordinary C target commands to C headers and compiler-rt only.

    This contract deliberately has no C++ standard library. The selected
    C-foundation manifest supplies compiler-rt and static libunwind inputs.
    """
    selection = load_clang_c_foundation_runtime(foundation_result, sysroot, target=target,
        runtime_base=runtime_base)
    runtime = selection['runtime']
    target_toolchain.validate_clang_identity(compiler, runtime['triplet'])
    if compiler['version'] != runtime['llvm_version']:
        raise SDKError('selected compiler and C-foundation runtime versions differ')
    root, stage = selection['root'], selection['stage']
    selected = llvm_runtime.runtime_paths(stage, runtime)
    resource_includes = ['-isystem', root + '/usr/include', '-isystem',
                         compiler['resource_dir'] + '/include']
    common = ['--sysroot=' + root, '-resource-dir=' + selected['llvm-resource'],
              '-nostdinc']
    abi_flags = list(targets.target_compiler_flags(target))
    common += abi_flags
    commands = {role:list(words) for role, words in compiler['commands'].items()}
    for role in ('cc', 'cxx'):
        commands[role] = [word for word in commands[role]
                           if not word.startswith('--ld-path=')] + common + resource_includes
    commands['cpp'] += common + resource_includes
    commands['as'] += abi_flags
    linker = compiler['binaries']['ld']['selected_path']
    link_flags = {role:['--ld-path=' + linker, '--rtlib=compiler-rt',
        '--unwindlib=libunwind', '-L' + selected['runtime-prefix'] + '/lib',
        '-Wl,-rpath-link,' + root + '/usr/lib',
        '-Wl,-rpath-link,' + root + '/usr/pkg/lib'] for role in ('cc', 'cxx')}
    return dict(commands=commands, link_flags=link_flags, selection=selection)


def bind_clang_c_foundation_commands(destination, compiler, foundation_result, sysroot, *,
                                     target, python, include_auxiliary=False, runtime_base=None):
    """Bind C-only target drivers without selecting a C++ runtime."""
    if type(include_auxiliary) is not bool:
        raise SDKError('auxiliary command selection must be boolean')
    selected = clang_c_foundation_commands(compiler, foundation_result, sysroot,
        target=target, runtime_base=runtime_base)
    roles = ['cc', 'cxx', 'cpp']
    commands = dict(selected['commands'])
    if include_auxiliary:
        roles += ['as', 'ld']
        commands['ld'] = commands['ld'] + ['--sysroot=' + selected['selection']['root']]
    bound = compiler_bindings.bind_compilers(destination,
        commands={role:commands[role] for role in roles},
        flags={name:[] for name in ('cppflags', 'cflags', 'cxxflags', 'ldflags')},
        python=python, link_flags=selected['link_flags'])
    return dict(commands=bound, selection=selected['selection'])


def _release_dest_runtime(root, target):
    """Select only the recorded dest of a completed amd64 Clang release."""
    from . import os_release_builder
    if target != 'amd64' or os.path.basename(root) != 'dest':
        raise SDKError('select a completed or imported SDK sysroot')
    parent = os.path.dirname(root)
    if paths.entry(os.path.join(parent, sysroot_transfer.INCOMPLETE)) is not None:
        raise SDKError('cannot select an incomplete OS release')
    receipt_path = os.path.join(parent, sysroot_transfer.RECEIPT)
    profile_path = os.path.join(parent, 'build-profile.json')
    try:
        paths.regular_file(receipt_path)
        paths.regular_file(profile_path)
        receipt = jsonio.read_json(receipt_path)
        profile = jsonio.read_json(profile_path)
        if (profile['sdk_receipt'] != receipt or receipt['paths']['destination'] != parent or
                os.path.join(parent, 'dest') != root):
            raise SDKError('release destination or receipt differs from profile')
        os_release_builder.validate_profile(profile, profile['source_commit'], 'amd64')
        if ('error' in receipt or
                not all(row.get('status') == 'passed' for row in receipt['commands'])):
            raise SDKError('release contains an error or incomplete command')
        signature = receipt['signature']
        if signature.get('compiler_family') != 'clang':
            raise SDKError('application runtime requires an LLVM release')
        return (signature['runtime']['manifest'],
                llvm_runtime_install.base_cxx_enabled(signature.get('flags', [])))
    except (KeyError, TypeError, OSError) as exc:
        raise SDKError('invalid completed OS release') from exc


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
        record, base_cxx = _release_dest_runtime(root, target)
    selected = llvm_runtime.validate_install(root, record, target=target)
    if base_cxx:
        llvm_runtime_install.validate_base_aliases(root, record)
    return selected


def _selected_roots(sysroot, runtime_root, sysroot_alias):
    """Validate the real sysroot and runtime root; return their command spellings.

    A sysroot_alias is an absolute path, normally a symbolic link owned by
    the caller, that must resolve to the selected sysroot now. Commands then
    name the alias instead of the stage directory, so outputs that embed
    them do not depend on the stage directory name. A runtime root inside
    the sysroot is spelled through the alias too.
    """
    root = paths.canonical_existing(sysroot)
    runtime_root = root if runtime_root is None else paths.canonical_existing(runtime_root)
    paths.real_directory(root)
    paths.real_directory(runtime_root)
    if sysroot_alias is None:
        return root, runtime_root
    alias = os.path.normpath(paths._raw_absolute(sysroot_alias))
    if not os.path.isabs(sysroot_alias) or paths.canonical_existing(alias) != root:
        raise SDKError('sysroot alias does not resolve to the selected sysroot')
    if os.path.commonpath((root, runtime_root)) == root:
        runtime_root = os.path.normpath(os.path.join(alias, os.path.relpath(runtime_root, root)))
    return alias, runtime_root


def clang_application_commands(compiler, sysroot, runtime_record, *, runtime_root=None, link=False,
                               sysroot_alias=None):
    """Return separate compile/link argv; callers validate selected live inputs.

    Compiler-only queries and freestanding builds use target_toolchain instead.
    A separate runtime_root supports already validated staging probes without
    requiring a completed application build receipt. See _selected_roots for
    sysroot_alias.
    """
    record = llvm_runtime.validate_identity(runtime_record, required_components=llvm_runtime.FULL_COMPONENTS)
    target_toolchain.validate_clang_identity(compiler, record['triplet'])
    if compiler['version'] != record['llvm_version']:
        raise SDKError('selected compiler and LLVM runtime versions differ')
    root, runtime_root = _selected_roots(sysroot, runtime_root, sysroot_alias)
    selected = llvm_runtime.runtime_paths(runtime_root, record)
    commands = {role:list(command) for role,command in compiler['commands'].items()}
    # Host clang resource headers supply freestanding C builtins when the target
    # resource-dir has only compiler-rt. For C++ they must follow libc++ so
    # <cstddef> finds libc++'s <stddef.h> before the C resource header.
    common = ['--sysroot='+root, '-resource-dir='+selected['llvm-resource'], '-nobuiltininc']
    abi_flags = list(targets.target_compiler_flags(record['target']))
    common += abi_flags
    resource_includes = ['-isystem', compiler['resource_dir']+'/include']
    for role in ('cc','cxx'):
        # --ld-path is a link operation flag. Clang diagnoses it under -c -Werror.
        commands[role] = [word for word in commands[role] if not word.startswith('--ld-path=')] + common
    commands['cpp'] += common + resource_includes
    commands['as'] += abi_flags
    commands['cc'] += resource_includes
    commands['cxx'] += ['-nostdinc++','-isystem',
                        os.path.join(runtime_root,record['artifacts']['cxx_headers'])]
    commands['cxx'] += resource_includes
    if link:
        for role, language in (('cc','c'), ('cxx','c++')):
            commands[role] += clang_application_link_flags(compiler, sysroot, record,
                runtime_root=runtime_root, language=language, sysroot_alias=sysroot_alias)
    return commands


_RUNTIME_LIBRARIES = (('libcxx', 'libc++'), ('libcxxabi', 'libc++abi'), ('libunwind', 'libunwind'))


def _runtime_directories(record):
    return tuple(dict.fromkeys(os.path.dirname(record['artifacts'][role]['shared'])
                               for role, _ in _RUNTIME_LIBRARIES))


def clang_application_runtime_rpath(runtime_record):
    """Return the target search path of the runtime libraries and their names.

    The path belongs only in outputs that need one of these libraries.
    """
    record = llvm_runtime.validate_identity(runtime_record, required_components=llvm_runtime.FULL_COMPONENTS)
    return dict(flags=['-Wl,-rpath,/'+relative for relative in _runtime_directories(record)],
                libraries=[name for _, name in _RUNTIME_LIBRARIES])


def clang_application_link_flags(compiler, sysroot, runtime_record, *, runtime_root=None, language='c',
                                 runtime_rpath=True, sysroot_alias=None):
    """Return driver-only runtime flags for the selected link language.

    runtime_rpath=False omits the target search path of the runtime
    libraries; see clang_application_runtime_rpath.
    """
    if language not in ('c', 'c++'):
        raise SDKError('application link language must be c or c++')
    record = llvm_runtime.validate_identity(runtime_record, required_components=llvm_runtime.FULL_COMPONENTS)
    target_toolchain.validate_clang_identity(compiler, record['triplet'])
    if compiler['version'] != record['llvm_version']:
        raise SDKError('selected compiler and LLVM runtime versions differ')
    root, runtime_root = _selected_roots(sysroot, runtime_root, sysroot_alias)
    directories = _runtime_directories(record)
    flags = ['--ld-path='+compiler['binaries']['ld']['selected_path'],
             '--rtlib=compiler-rt','--unwindlib=libunwind']
    if language == 'c++':
        flags += ['-stdlib=libc++']
    for relative in directories:
        flags += ['-L'+os.path.join(runtime_root,relative)]
        if runtime_rpath:
            flags += ['-Wl,-rpath,/'+relative]
        flags += ['-Wl,-rpath-link,'+os.path.join(runtime_root,relative)]
    flags += ['-Wl,-rpath-link,'+os.path.join(root,'usr/lib'),
              '-Wl,-rpath-link,'+os.path.join(root,'usr/pkg/lib')]
    return flags


def bind_clang_application_commands(destination, compiler, sysroot, runtime_record, *, python,
                                    runtime_root=None, include_auxiliary=False, sysroot_alias=None):
    """Bind CC/CXX/CPP for builders that have no separate C++ link interface.

    python is the selected native-prefix interpreter. Existing compiler input
    argv are preserved, while measured runtime flags are attached only at link.
    The runtime library search path stays only in outputs that need one of
    the runtime libraries.
    """
    if type(include_auxiliary) is not bool:
        raise SDKError('auxiliary command selection must be boolean')
    commands = clang_application_commands(compiler, sysroot, runtime_record, runtime_root=runtime_root,
                                          sysroot_alias=sysroot_alias)
    roles = ['cc', 'cxx', 'cpp']
    if include_auxiliary:
        roles += ['as', 'ld']
        commands['ld'] += ['--sysroot=' + _selected_roots(sysroot, runtime_root, sysroot_alias)[0]]
    return compiler_bindings.bind_compilers(destination,
        commands={role:commands[role] for role in roles}, python=python,
        flags={name:[] for name in ('cppflags','cflags','cxxflags','ldflags')},
        link_flags={role:clang_application_link_flags(compiler, sysroot, runtime_record,
            runtime_root=runtime_root, language=language, runtime_rpath=False,
            sysroot_alias=sysroot_alias)
            for role,language in (('cc','c'), ('cxx','c++'))},
        runtime_rpath=clang_application_runtime_rpath(runtime_record))
