#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD; AI-assisted workspace layout CLI.
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.workspace import create


def main():
    args = sys.argv[1:]
    if not ((len(args) == 1 or (len(args) == 3 and args[1] == '--adopt')) and not args[0].startswith('-')):
        print('Usage: create-prebuild-sdk.py ROOT [--adopt EXISTING]', file=sys.stderr)
        return 2
    try:
        print(json.dumps(create(args[0], adopt=args[2] if len(args) == 3 else None), ensure_ascii=False, indent=2))
        return 0
    except (SDKError, OSError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
