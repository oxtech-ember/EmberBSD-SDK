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
    parser = argparse.ArgumentParser(description='Plan a complete GCC16 EmberBSD OS release. Actual builds are refused until OS nested native-tool routing is qualified.')
    for name in ('sdk', 'os-source', 'ports', 'compiler', 'runtime-work', 'gcc-archive', 'pkgsrc', 'jobs', 'python', 'sdk-source', 'libedit-source', 'host-cc', 'host-cxx'):
        parser.add_argument('--' + name, required=True, action=UniqueOption, help='explicit host Python 3.15+ interpreter' if name == 'python' else 'selected SDK source checkout' if name == 'sdk-source' else 'absolute SDK native GCC 16 driver (no PATH or Clang fallback)' if name.startswith('host-') else None)
    parser.add_argument('--profile', required=True, choices=('amd64', 'aarch64-sysroot'))
    for name in ('destination', 'objects', 'tools'):
        parser.add_argument('--' + name, action=UniqueOption, required=name == 'destination')
    for name in ('native-toolset', 'native-toolset-sha256', 'native-producer-receipt', 'native-producer-receipt-sha256'):
        parser.add_argument('--' + name, required=True, action=UniqueOption,
                            help='explicit qualified native toolset evidence; see docs/native-toolset.md')
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
