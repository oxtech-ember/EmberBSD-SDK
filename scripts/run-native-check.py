#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Run a source-owner check using the selected native Ports tools."""

import argparse
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from emberbsd_sdk import native_checks
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.host_prefix import load_host_prefix


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host-prefix', required=True)
    parser.add_argument('--compiler-family', choices=('clang', 'gcc'), default='clang')
    parser.add_argument('--required-role', action='append', default=[])
    parser.add_argument('--tool', required=True, help='selected tool role that runs the check')
    parser.add_argument('--cwd', required=True)
    parser.add_argument('--log', required=True)
    parser.add_argument('--bindings', required=True, help='caller-owned compiler argument adapter directory')
    for name in ('cppflag', 'cflag', 'cxxflag', 'ldflag'):
        parser.add_argument('--' + name, action='append', default=[], help='one explicit native argument; repeat as needed')
    parser.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        roles = tuple(dict.fromkeys((*native_checks.REQUIRED_ROLES, *args.required_role, args.tool)))
        native = load_host_prefix(args.host_prefix, compiler_family=args.compiler_family,
                                  required_roles=roles)
        arguments = args.arguments[1:] if args.arguments[:1] == ['--'] else args.arguments
        native_checks.run_check(native, args.tool, arguments, cwd=args.cwd, log=args.log, bindings=args.bindings,
            extra_flags={name + 's':getattr(args, name) for name in ('cppflag','cflag','cxxflag','ldflag')})
        return 0
    except SDKError as error:
        print('run-native-check: ' + str(error), file=sys.stderr)
        status = getattr(error, 'returncode', 1)
        return status if status >= 0 else 128 - status


if __name__ == '__main__':
    sys.exit(main())
