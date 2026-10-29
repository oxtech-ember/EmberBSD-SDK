# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD; AI-assisted.
"""Run one Darwin host command with an explicitly selected runtime view."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

from .errors import SDKError
from . import paths



def bind_command(output, *, python, view, command):
    """Publish a single executable that runs one command with a scoped view."""
    if os.path.lexists(output):
        paths.regular_file(output)
    output = paths.canonical_new(output)
    paths.real_directory(os.path.dirname(output))
    for value in (python, view, command):
        if not os.path.isabs(value) or os.path.normpath(value) != value:
            raise SDKError('runtime command inputs must be absolute normalized paths')
    if any(ch.isspace() for ch in python):
        raise SDKError('runtime command Python cannot contain whitespace in the shebang')
    if ':' in view or not os.path.isdir(view):
        raise SDKError('runtime view must be an existing single directory')
    if not all(os.path.isfile(p) and os.access(p, os.X_OK) for p in (python, command)):
        raise SDKError('runtime command requires selected executable inputs')
    launcher = str(Path(__file__).resolve().parents[2]/'scripts/run-darwin-with-runtime.py')
    paths.regular_file(launcher)
    data = (f'#!{python}\nimport os,sys\n'
            f'os.execv({python!r}, [{python!r}, "-B", {launcher!r}, "--runtime-view", '
            f'{view!r}, "--", {command!r}, *sys.argv[1:]])\n').encode()
    if os.path.lexists(output):
        paths.regular_file(output)
        if Path(output).read_bytes() != data or not os.access(output, os.X_OK):
            raise SDKError('runtime command binding differs from selected inputs')
        return output
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix='.runtime-command-', dir=os.path.dirname(output),
                                         delete=False) as stream:
            temporary = stream.name
            stream.write(data)
        os.chmod(temporary, 0o555)
        os.link(temporary, output)
    finally:
        if temporary and os.path.lexists(temporary):
            os.unlink(temporary)
    return output

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
