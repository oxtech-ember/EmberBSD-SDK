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
    parser = argparse.ArgumentParser(description='Build an EmberBSD OS release with a native Ports prefix.')
    for name in ('sdk', 'os-source', 'ports', 'compiler', 'runtime-work', 'jobs', 'sdk-source', 'libedit-source', 'libarchive-source', 'libevent-source', 'host-prefix', 'host-binutils'):
        parser.add_argument('--' + name, required=True, action=UniqueOption, help={'sdk-source': 'selected SDK source checkout', 'host-prefix': 'native Ports tool prefix'}.get(name))
    parser.add_argument('--host-compiler-family', choices=('clang', 'gcc'), action=UniqueOption,
                        help='Clang by default; select GCC explicitly for a platform exception')
    parser.add_argument('--compiler-family', choices=('clang', 'gcc'), action=UniqueOption,
                        help='target compiler family, independent of host; defaults to Clang')
    parser.add_argument('--gcc-archive', action=UniqueOption, help='required only for an explicit GCC runtime')
    parser.add_argument('--pkgsrc', action=UniqueOption, help='exported catalog for the explicit GCC runtime')
    parser.add_argument('--llvm-source', action=UniqueOption, help='common Ports prepared LLVM source')
    parser.add_argument('--llvm-runtime-stage', action=UniqueOption,
                        help='reuse a completed common LLVM runtime; otherwise build its prerequisites')
    parser.add_argument('--llvm-bootstrap-stage', action=UniqueOption,
                        help='verified bootstrap runtime consumed by the full runtime')
    parser.add_argument('--base-cxx', action=argparse.BooleanOptionalAction, default=argparse.SUPPRESS,
                        help='install the selected LLVM libc++ headers and linker aliases into the OS base paths (default for Clang)')
    parser.add_argument('--profile', required=True, choices=('amd64', 'aarch64-sysroot'))
    for name in ('destination', 'objects', 'tools'):
        parser.add_argument('--' + name, action=UniqueOption, required=name == 'destination')
    for name in ('host-cc', 'host-cxx', 'python'):
        parser.add_argument('--' + name, action=UniqueOption, help='optional checked alias inside --host-prefix')
    parser.add_argument('--plan', action='store_true', help='inspect inputs and emit commands without creating outputs')
    arguments = parser.parse_args()
    try:
        source = Path(arguments.sdk_source).resolve(strict=True)
        sys.path.insert(0, str(source / 'src'))
        from emberbsd_sdk import API_VERSION
        from emberbsd_sdk.errors import SDKError
        from emberbsd_sdk.os_release_builder import OSReleaseBuilder
    except (OSError, ImportError, AttributeError, ValueError) as exc:
        print(f'OS release build: cannot load selected SDK source: {exc}', file=sys.stderr)
        return 1
    if type(API_VERSION) is not int or API_VERSION != 1:
        print('OS release build: selected SDK API must be 1', file=sys.stderr)
        return 1
    try:
        options = {key.replace('_', '-'): value for key, value in vars(arguments).items() if value is not None}
        options['os'] = options.pop('os-source')
        options['target'] = 'amd64' if options.pop('profile') == 'amd64' else 'aarch64'
        print(json.dumps(OSReleaseBuilder(options).run(), ensure_ascii=False, indent=2))
        return 0
    except KeyboardInterrupt as exc:
        print(f'OS release build interrupted: {exc}', file=sys.stderr)
        return 130
    except (SDKError, OSError, UnicodeError) as exc:
        print(f'OS release build: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
