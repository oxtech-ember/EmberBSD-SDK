#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD; AI-assisted read-only workspace inspection CLI.
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.workspace import inspect_layout


def main():
    if len(sys.argv) != 2 or sys.argv[1].startswith('-'):
        print('Usage: inspect-prebuild-sdk.py ROOT', file=sys.stderr)
        return 2
    try:
        print(json.dumps(inspect_layout(sys.argv[1]), ensure_ascii=False, indent=2))
        return 0
    except (SDKError, OSError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
