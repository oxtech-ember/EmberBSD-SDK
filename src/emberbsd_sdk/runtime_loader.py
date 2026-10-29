# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD; AI-assisted.
"""Run one Darwin host command with an explicitly selected runtime view."""
import argparse
import json
import os
from pathlib import Path
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-view')
    parser.add_argument('--print-inputs', action='store_true')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.print_inputs:
        if args.runtime_view is not None or args.command:
            parser.error('--print-inputs does not accept a runtime view or command')
        module = Path(__file__).resolve()
        script = module.parents[2]/'scripts/run-darwin-with-runtime.py'
        print(json.dumps([str(script), str(module)]))
        return 0
    if sys.platform != 'darwin':
        parser.error('a Darwin host is required')
    if (args.runtime_view is None or not os.path.isabs(args.runtime_view) or
            ':' in args.runtime_view or
            not os.path.isdir(args.runtime_view)):
        parser.error('runtime view must be an existing absolute directory without colon')
    if len(args.command) < 2 or args.command[0] != '--':
        parser.error('select a command after --')
    command = args.command[1:]
    if not command[0]:
        parser.error('empty command')
    environment = os.environ.copy()
    environment['DYLD_LIBRARY_PATH'] = args.runtime_view
    os.execvpe(command[0], command, environment)
