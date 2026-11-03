#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Package the small Python client and public trust root, without build trees."""

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk.client_bundle import package_client


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    package_client(args.output)
    print(f'{args.output}: {args.output.stat().st_size} bytes (Python and native pkg_tools required)')


if __name__ == '__main__':
    main()
