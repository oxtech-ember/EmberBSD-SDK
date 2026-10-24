#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Check an independently accepted seed and one exact native GCC16 plan."""

import argparse
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from emberbsd_sdk.bootstrap_seed import load_bootstrap_seed
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.jsonio import json_bytes


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    check = commands.add_parser('check', help='check current inputs; do not execute the plan')
    for name in ('descriptor', 'accepted-seed-receipt', 'bootstrap-plan'):
        check.add_argument('--' + name, required=True)
        check.add_argument('--' + name + '-sha256', required=True)
    check.add_argument('--required-role', action='append', required=True)
    args = parser.parse_args(argv)
    try:
        selected = load_bootstrap_seed(
            args.descriptor, descriptor_sha256=args.descriptor_sha256,
            accepted_seed_receipt=dict(path=args.accepted_seed_receipt,
                                       sha256=args.accepted_seed_receipt_sha256),
            bootstrap_plan=dict(path=args.bootstrap_plan, sha256=args.bootstrap_plan_sha256),
            required_roles=args.required_role)
        sys.stdout.write(json_bytes(selected.as_dict()).decode('utf-8'))
        return 0
    except SDKError as exc:
        print(f'bootstrap-seed: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
