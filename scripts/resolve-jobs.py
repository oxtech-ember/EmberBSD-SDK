#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Print the selected SDK CPU job count for shell-owned build entrypoints."""
import argparse
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from emberbsd_sdk.cpu_capacity import resolve_jobs
from emberbsd_sdk.errors import SDKError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--jobs', help='explicit positive CPU job count')
    args = parser.parse_args()
    try:
        print(resolve_jobs(args.jobs))
    except SDKError as exc:
        print(f'resolve-jobs: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
