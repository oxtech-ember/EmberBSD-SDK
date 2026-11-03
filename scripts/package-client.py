#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Package the small Python client and public trust root, without build trees."""

import argparse
from pathlib import Path
import zipfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    with args.output.open('xb') as output:
        with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('__main__.py', 'from emberbsd_sdk.sdk_client import main\nraise SystemExit(main())\n')
            for path in sorted((source / 'src/emberbsd_sdk').rglob('*.py')):
                archive.write(path, str(path.relative_to(source / 'src')))
            archive.write(source / 'trust/emberbsd-release-root.pem', 'emberbsd_sdk/emberbsd-release-root.pem')
            archive.write(source / 'LICENSE', 'LICENSE')
    print(f'{args.output}: {args.output.stat().st_size} bytes (Python and native pkg_tools required)')


if __name__ == '__main__':
    main()
