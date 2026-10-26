#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Write the ordinary Ports MAKECONF for an initial Darwin arm64 prefix."""

import argparse
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True


class UniqueOption(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        if getattr(namespace, self.dest, None) is not None:
            parser.error('duplicate ' + option_string)
        setattr(namespace, self.dest, values)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('sdk-source', 'pkgsrc', 'output', 'prefix', 'work-root', 'distfiles',
                 'packages', 'pkgdb', 'utility-bin', 'makefiles', 'macos-sdk', 'deployment'):
        parser.add_argument('--' + name, required=True, action=UniqueOption)
    parser.add_argument('--tool-root', action='append', required=True,
                        help='explicit initial tool root; repeat for separate initial installations')
    parser.add_argument('--tool', action='append', required=True, metavar='ROLE=ABSOLUTE_PATH')
    for role in ('cc', 'cxx', 'cpp'):
        parser.add_argument('--' + role + '-arg', action='append', default=[], metavar='WORD',
                            help='append one compiler argument; use --' + role + '-arg=-OPTION')
    parser.add_argument('--ldflag', action='append', default=[])
    parser.add_argument('--source-tool', action='append', default=[])
    parser.add_argument('--jobs', type=int, action=UniqueOption, help='serial initial build; default 1')
    parser.add_argument('--tool-phase', choices=('initial', 'full'), action=UniqueOption)
    args = parser.parse_args(argv)
    try:
        source = Path(args.sdk_source).resolve(strict=True)
        sys.path.insert(0, str(source / 'src'))
        from emberbsd_sdk import API_VERSION
        from emberbsd_sdk.errors import SDKError
        from emberbsd_sdk.darwin_makeconf import write_makeconf
        if type(API_VERSION) is not int or API_VERSION != 1:
            raise SDKError('selected SDK API must be 1')
        tools = {}
        for entry in args.tool:
            role, separator, path = entry.partition('=')
            if not separator or role in tools:
                raise SDKError('supply each tool once as ROLE=ABSOLUTE_PATH')
            tools[role] = path
        options = {name: getattr(args, name) for name in (
            'pkgsrc', 'output', 'prefix', 'work_root', 'distfiles', 'packages', 'pkgdb',
            'utility_bin', 'makefiles', 'macos_sdk', 'deployment')}
        result = write_makeconf(**options, tools=tools, tool_roots=args.tool_root,
            compiler_args={role: getattr(args, role + '_arg') for role in ('cc', 'cxx', 'cpp')},
            ldflags=args.ldflag, source_tools=args.source_tool,
            jobs=1 if args.jobs is None else args.jobs, tool_phase=args.tool_phase or 'full')
        print(json.dumps(result, indent=2))
        return 0
    except Exception as error:
        print('Darwin bootstrap configuration: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
