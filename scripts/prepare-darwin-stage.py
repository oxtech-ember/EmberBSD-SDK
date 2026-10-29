#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Write a native replacement-stage pkgsrc configuration."""
import argparse
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('sdk-source', 'base', 'output', 'stage-root', 'prefix',
                 'object-root', 'packages', 'binding-record', 'python'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--buildlink', action='append', default=[])
    parser.add_argument('--tool', action='append', default=[], metavar='ROLE=PATH')
    parser.add_argument('--runtime-view')
    args = parser.parse_args()
    source = Path(args.sdk_source).resolve(strict=True)
    sys.path.insert(0, str(source / 'src'))
    from emberbsd_sdk.darwin_makeconf import write_native_stage_makeconf
    print(write_native_stage_makeconf(base=args.base, output=args.output,
        stage_root=args.stage_root, prefix=args.prefix, object_root=args.object_root,
        packages=args.packages, binding_record=args.binding_record, python=args.python,
        buildlinks=args.buildlink, tools=args.tool, runtime_view=args.runtime_view))


if __name__ == '__main__':
    main()
