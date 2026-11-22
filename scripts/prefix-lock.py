#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Run a command as a reader or a writer of package prefixes.

Usage: prefix-lock.py read|write --database DB ... -- COMMAND ...

read: the command and its children read the prefixes of the databases.
write: the command changes the prefix of the database. It does not start
when another process tree reads that prefix.
"""
import argparse
import os
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from emberbsd_sdk import prefix_lock
from emberbsd_sdk.errors import SDKError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('read', 'write'))
    parser.add_argument('--database', action='append', required=True, help='absolute package database')
    argv = sys.argv[1:]
    split = argv.index('--') if '--' in argv else len(argv)
    args, command = parser.parse_args(argv[:split]), argv[split + 1:]
    if not command or not os.path.isabs(command[0]):
        parser.error('give an absolute command after --')
    if any(not os.path.isabs(database) for database in args.database):
        parser.error('give absolute package databases')
    try:
        if args.mode == 'read':
            prefix_lock.register(args.database)
        else:
            if len(args.database) != 1:
                parser.error('a writer changes one package database')
            os.set_inheritable(prefix_lock.acquire_writer(args.database[0]), True)
    except SDKError as exc:
        print(f'prefix-lock: {exc}', file=sys.stderr)
        return 75
    # The command keeps the inherited locks until it and its children exit.
    os.execv(command[0], command)


if __name__ == '__main__':
    raise SystemExit(main())
