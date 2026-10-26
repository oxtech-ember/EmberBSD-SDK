#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Run an OS producer using its complete selected native Ports contract."""

import argparse
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from emberbsd_sdk import native_os
from emberbsd_sdk.errors import SDKError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host-prefix', required=True)
    parser.add_argument('--compiler-family', choices=('clang', 'gcc'), default='clang')
    for name in ('source','routing','cwd'):
        parser.add_argument('--'+name, required=True)
    output = parser.add_mutually_exclusive_group(required=True)
    output.add_argument('--log')
    output.add_argument('--capture', action='store_true')
    parser.add_argument('--append-log', action='store_true')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--build-sh', action='store_true')
    mode.add_argument('--shell', action='store_true')
    parser.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        native = native_os.select_native_os_tools(args.host_prefix, args.source,
                                                  compiler_family=args.compiler_family)
        arguments = args.arguments[1:] if args.arguments[:1] == ['--'] else args.arguments
        result = native_os.run_native_os(native, source=args.source, routing=args.routing,
            cwd=args.cwd, log=args.log, capture=args.capture, append=args.append_log,
            build_sh=args.build_sh, shell=args.shell, arguments=arguments)
        if args.capture:
            sys.stdout.buffer.write(result.stdout)
            sys.stderr.buffer.write(result.stderr)
        return 0
    except SDKError as error:
        if args.capture:
            sys.stdout.buffer.write(getattr(error, 'stdout', b''))
            sys.stderr.buffer.write(getattr(error, 'stderr', b''))
        print('run-native-os: ' + str(error), file=sys.stderr)
        status = getattr(error, 'returncode', 1)
        return status if status >= 0 else 128 - status


if __name__ == '__main__':
    sys.exit(main())
