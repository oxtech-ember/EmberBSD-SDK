#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Prepare or validate the canonical Ports elftoolchain libelf source."""
import argparse
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.libelf import prepare_source, validate_source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ports')
    parser.add_argument('--archive')
    parser.add_argument('--output')
    parser.add_argument('--patch-tool', help='absolute patch executable for preparation')
    parser.add_argument('--validate')
    args = parser.parse_args()
    if args.validate is not None:
        if args.archive is not None or args.output is not None or args.patch_tool is not None:
            parser.error('select --validate ROOT or both --archive FILE and --output NEW_ROOT')
    elif args.ports is None or args.archive is None or args.output is None:
        parser.error('preparation requires --ports, --archive FILE and --output NEW_ROOT')
    try:
        result = (validate_source(args.validate, ports=args.ports)
                  if args.validate else prepare_source(
                      args.ports, args.archive, args.output, patch_tool=args.patch_tool))
        print(json.dumps(result, indent=2))
        return 0
    except (SDKError, OSError, ValueError, UnicodeError) as exc:
        print(f'libelf source: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
