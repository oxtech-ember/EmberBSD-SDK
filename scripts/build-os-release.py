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
    parser = argparse.ArgumentParser(description='Build or plan a complete GCC16 EmberBSD OS release with a Darwin or Linux process backend.')
    input_help = {
        'python': 'explicit host Python 3.15+ interpreter',
        'sdk-source': 'selected SDK source checkout',
        'host-toolchain': 'absolute SDK native GCC16 provider root',
        'host-cc': 'absolute native GCC16 C compiler path from the provider manifest',
        'host-cxx': 'absolute native GCC16 C++ compiler path from the provider manifest',
        'host-toolchain-manifest': 'absolute provider input manifest path',
    }
    for name in ('sdk', 'os-source', 'ports', 'compiler', 'host-toolchain', 'host-cc', 'host-cxx', 'host-toolchain-manifest', 'runtime-work', 'gcc-archive', 'pkgsrc', 'jobs', 'python', 'sdk-source', 'libedit-source'):
        parser.add_argument('--' + name, required=True, action=UniqueOption, help=input_help.get(name))
    parser.add_argument('--profile', required=True, choices=('amd64', 'aarch64-sysroot'))
    for name in ('destination', 'objects', 'tools'):
        parser.add_argument('--' + name, action=UniqueOption, required=name == 'destination')
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
