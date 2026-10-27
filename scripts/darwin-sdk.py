#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Inspect explicit macOS SDK and Xcode version plists without running tools."""
import argparse
from pathlib import Path
import sys
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk.darwin_sdk import read_metadata
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.jsonio import json_bytes


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--macos-sdk', required=True)
    parser.add_argument('--xcode-version-plist', required=True)
    args = parser.parse_args(argv)
    try:
        result = read_metadata(args.macos_sdk, xcode_version_plist=args.xcode_version_plist)
    except (SDKError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1
    sys.stdout.buffer.write(json_bytes(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
