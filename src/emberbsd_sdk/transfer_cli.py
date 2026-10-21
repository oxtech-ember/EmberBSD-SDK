# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD; AI-assisted transfer command interface.
"""Command policy for the sysroot transfer API."""

import argparse
import json
import sys
import tarfile
import zlib

from .errors import SDKError
from .sysroot_transfer import MAX_BYTES, export_sysroot, import_sysroot
from .targets import TARGETS


class UniqueOption(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        if getattr(namespace, self.dest, None) is not None:
            parser.error(f'duplicate {option_string}')
        setattr(namespace, self.dest, values)


def main(operation):
    parser = argparse.ArgumentParser(description=f'{operation.capitalize()} a verified sysroot transfer bundle.')
    names = ('sysroot', 'output') if operation == 'export' else ('bundle', 'destination', 'manifest-sha256')
    for name in names:
        parser.add_argument('--' + name, required=True, action=UniqueOption)
    parser.add_argument('--target', choices=TARGETS, required=True, action=UniqueOption)
    if operation == 'import':
        parser.add_argument('--max-bytes', type=int, action=UniqueOption,
                            help=f'maximum unpacked regular-file bytes (default: {MAX_BYTES})')
    arguments = parser.parse_args()
    try:
        if sys.implementation.name != 'cpython' or sys.version_info[:2] != (3, 15):
            raise SDKError('sysroot transfer requires the CPython 3.15 host profile')
        if operation == 'export':
            result = export_sysroot(arguments.sysroot, arguments.output, target=arguments.target)
        else:
            result = import_sysroot(arguments.bundle, arguments.destination, target=arguments.target,
                                    manifest_sha256=arguments.manifest_sha256,
                                    max_bytes=MAX_BYTES if arguments.max_bytes is None else arguments.max_bytes)
        print(json.dumps(result, indent=2))
        return 0
    except KeyboardInterrupt:
        print('sysroot transfer interrupted', file=sys.stderr)
        return 130
    except (SDKError, OSError, UnicodeError, EOFError, tarfile.TarError, zlib.error) as exc:
        print(f'sysroot transfer: {exc}', file=sys.stderr)
        return 1
