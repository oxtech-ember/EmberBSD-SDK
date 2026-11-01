# SPDX-License-Identifier: BSD-2-Clause
# Origin: EmberBSD; AI-assisted incremental build support.
"""Describe C++ runtime updates that can use the existing dependency planner."""


def cpp_update(before, after):
    """Return header/link inputs to refresh, or None for incompatible changes.

    Compiler and other build options are compared by the caller. The current
    runtime has already passed its owner verifier. Compare its recorded files,
    without reading or hashing either installed tree again.
    """
    try:
        if before.get('compiler_family') != 'clang' or after.get('compiler_family') != 'clang':
            return None
        old, new = before['runtime']['manifest'], after['runtime']['manifest']
        identity = ('format', 'version', 'compiler_family', 'llvm_version', 'components',
                    'target', 'triplet', 'machine_arch', 'elf_machine', 'prefix',
                    'source', 'artifacts', 'symlinks')
        if any(old[key] != new[key] for key in identity):
            return None
        if (old['format'] != 'emberbsd-llvm-runtime' or old['version'] != 1 or
                old['compiler_family'] != 'clang'):
            return None
        # An unrelated OS commit does not change the runtime's target C ABI.
        if ({k: v for k, v in old['base'].items() if k != 'os_revision'} !=
                {k: v for k, v in new['base'].items() if k != 'os_revision'}):
            return None
        headers = new['artifacts']['cxx_headers'] + '/'
        if headers != new['prefix'] + '/include/c++/v1/':
            return None
        shared = {new['artifacts'][name]['shared'] for name in ('libcxx', 'libcxxabi')}
        changed = {name for name in old['files'].keys() | new['files'].keys()
                   if old['files'].get(name) != new['files'].get(name)}
        if any(name not in shared and not name.startswith(headers) for name in changed):
            # Builtins, unwinder, static archives and other runtime inputs stay
            # unchanged; this is not a compiler or general runtime ABI switch.
            return None
        if changed or before['runtime_prefix'] != after['runtime_prefix']:
            # A moved prefix also needs new dependency files: otherwise a later
            # header update could remain hidden behind old absolute .d paths.
            # The optional base C++ installation copies these headers to
            # usr/include/c++; dependency files may use either public route.
            return dict(headers=(headers, 'usr/include/c++/'),
                        linked=('libc++.so', 'libc++abi.so'))
        return dict(headers=(), linked=())
    except (KeyError, TypeError, AttributeError):
        return None
