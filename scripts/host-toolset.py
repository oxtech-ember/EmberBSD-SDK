#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Read historical GCC16 native-toolset evidence.

For current Clang or explicit GCC native-prefix selection, use the
load_host_prefix API documented in docs/native-toolset.md.
"""

import argparse
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.jsonio import json_bytes
from emberbsd_sdk.native_toolset import load_native_toolset


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    check = commands.add_parser('check', help='check historical pinned GCC16 producer evidence without executing tools',
                                description='Read historical GCC16 evidence; current builds use the native prefix API in docs/native-toolset.md.')
    check.add_argument('--descriptor', required=True)
    check.add_argument('--descriptor-sha256', required=True)
    check.add_argument('--producer-receipt', required=True)
    check.add_argument('--producer-receipt-sha256', required=True)
    check.add_argument('--required-role', action='append', required=True)
    args = parser.parse_args(argv)
    try:
        selected = load_native_toolset(
            args.descriptor, descriptor_sha256=args.descriptor_sha256,
            accepted_producer_receipt=dict(path=args.producer_receipt, sha256=args.producer_receipt_sha256),
            required_roles=args.required_role)
        sys.stdout.write(json_bytes(selected.as_dict()).decode('utf-8'))
        return 0
    except SDKError as exc:
        print(f'host-toolset: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
