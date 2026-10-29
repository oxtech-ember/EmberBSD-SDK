#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD; AI-assisted native OS build orchestration.
import argparse
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True


class UniqueOption(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        if getattr(namespace, self.dest, None) is not None:
            parser.error(f'duplicate {option_string}')
        setattr(namespace, self.dest, values)


def main():
    parser = argparse.ArgumentParser(description='Build an EmberBSD OS sysroot with a native Ports prefix.')
    for name in ('sdk', 'os', 'ports', 'compiler', 'target', 'jobs', 'sdk-source', 'libedit-source', 'host-prefix', 'host-binutils'):
        parser.add_argument('--' + name, required=True, action=UniqueOption, help={'sdk-source': 'selected SDK source checkout', 'host-prefix': 'native Ports tool prefix'}.get(name))
    parser.add_argument('--runtime-work', action=UniqueOption,
                        help='required for GCC or to build an LLVM runtime; not needed with a selected LLVM stage')
    for name in ('libarchive-source', 'libevent-source', 'byacc-source', 'texinfo-source', 'rcs-source', 'diffutils-source', 'grep-source', 'ekermit-source', 'nvi-source', 'tdb-source', 'ppp-source'):
        parser.add_argument('--' + name, action=UniqueOption, help='required for the OS base build')
    parser.add_argument('--source-patch', action=UniqueOption, help='selected patch executable inside the native prefix')
    for name in ('libevent-archive', 'byacc-archive', 'texinfo-archive', 'rcs-archive', 'diffutils-archive', 'grep-archive', 'ekermit-archive', 'nvi-archive', 'tdb-archive', 'ppp-archive'):
        parser.add_argument('--' + name, action=UniqueOption, help='archive for comparing changed source inputs')
    parser.add_argument('--host-compiler-family', choices=('clang', 'gcc'), action=UniqueOption,
                        help='Clang by default; select GCC explicitly for a platform exception')
    parser.add_argument('--compiler-family', choices=('clang', 'gcc'), action=UniqueOption,
                        help='target compiler family, independent of host; defaults to Clang')
    parser.add_argument('--gcc-archive', action=UniqueOption, help='required only for an explicit GCC runtime')
    parser.add_argument('--pkgsrc', action=UniqueOption, help='exported catalog for the explicit GCC runtime')
    parser.add_argument('--llvm-source', action=UniqueOption, help='common Ports prepared LLVM source')
    parser.add_argument('--llvm-runtime-original-source', action=UniqueOption,
                        help='prepared LLVM source used by a reused runtime')
    parser.add_argument('--llvm-runtime-stage', action=UniqueOption,
                        help='reuse a completed common LLVM runtime; otherwise build its prerequisites')
    parser.add_argument('--llvm-bootstrap-stage', action=UniqueOption,
                        help='verified bootstrap runtime consumed by the full runtime')
    parser.add_argument('--llvm-objects', action=UniqueOption,
                        help='LLVM coordinator object directory inside SDK/obj')
    parser.add_argument('--llvm-stages', action=UniqueOption,
                        help='LLVM coordinator stage directory inside SDK/stages')
    parser.add_argument('--base-cxx', action=argparse.BooleanOptionalAction, default=argparse.SUPPRESS,
                        help='install the selected LLVM libc++ headers and linker aliases into the OS base paths (default for Clang)')
    for name in ('destination', 'objects', 'tools'):
        parser.add_argument('--' + name, action=UniqueOption)
    for name in ('host-cc', 'host-cxx', 'python'):
        parser.add_argument('--' + name, action=UniqueOption, help='optional checked alias inside --host-prefix')
    parser.add_argument('--plan', action='store_true', help='inspect inputs and emit commands without creating outputs')
    arguments = parser.parse_args()
    try:
        source = Path(arguments.sdk_source).resolve(strict=True)
        sys.path.insert(0, str(source / 'src'))
        from emberbsd_sdk import API_VERSION
        from emberbsd_sdk.errors import SDKError
        from emberbsd_sdk.sysroot_builder import SysrootBuilder
    except (OSError, ImportError, AttributeError, ValueError) as exc:
        print(f'sysroot build: cannot load selected SDK source: {exc}', file=sys.stderr)
        return 1
    if type(API_VERSION) is not int or API_VERSION != 1:
        print('sysroot build: selected SDK API must be 1', file=sys.stderr)
        return 1
    try:
        options = {key.replace('_', '-'): value for key, value in vars(arguments).items() if value is not None}
        print(json.dumps(SysrootBuilder(options).run(), ensure_ascii=False, indent=2))
        return 0
    except KeyboardInterrupt as exc:
        print(f'sysroot build interrupted: {exc}', file=sys.stderr)
        return 130
    except (SDKError, OSError, UnicodeError) as exc:
        print(f'sysroot build: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
