#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Wrap a verified sysroot transfer for the signed binary channel."""

import argparse
from pathlib import Path
import sys
import tarfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk import jsonio, sysroot_package
from emberbsd_sdk.errors import SDKError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bundle', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--target', required=True)
    parser.add_argument('--manifest-sha256', required=True)
    args = parser.parse_args()
    try:
        result = sysroot_package.package(args.bundle, args.output, target=args.target,
                                         manifest_sha256=args.manifest_sha256)
        sys.stdout.buffer.write(jsonio.json_bytes(result))
        return 0
    except (SDKError, OSError, ValueError, tarfile.TarError) as exc:
        print('package-sysroot: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
