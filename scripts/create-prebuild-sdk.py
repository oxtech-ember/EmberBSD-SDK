#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD; AI-assisted workspace layout CLI.
import argparse
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.workspace import create
from emberbsd_sdk.targets import BUILD_TARGETS


def main():
    parser = argparse.ArgumentParser(
        description='Create a target workspace, or a read-only alias. Legacy alias adoption can omit --target.')
    parser.add_argument('root')
    parser.add_argument('--target', choices=BUILD_TARGETS)
    parser.add_argument('--adopt')
    args = parser.parse_args()
    if args.adopt is None and args.target is None:
        parser.error('--target is required for a new workspace')
    try:
        print(json.dumps(create(args.root, adopt=args.adopt, target=args.target), ensure_ascii=False, indent=2))
        return 0
    except (SDKError, OSError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
