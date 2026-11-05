#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Compose and inspect signed binary package catalogs without source-side solving."""

import argparse
from pathlib import Path
import subprocess
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk.binary_catalog import select_packages
from emberbsd_sdk.binary_release import compose, load_catalogs
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.host_prefix import load_host_prefix
from emberbsd_sdk.jsonio import json_bytes, read_json
from emberbsd_sdk.release_signing import ReleaseCrypto, SigningIdentity


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host-prefix', required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    producer = commands.add_parser('compose', help='sign packages and produce a complete new release directory')
    producer.add_argument('input', type=Path)
    producer.add_argument('--ports-root', required=True, type=Path)
    producer.add_argument('--output', required=True, type=Path)
    producer.add_argument('--signer-directory', required=True, type=Path)
    producer.add_argument('--trust-root', required=True, type=Path)
    producer.add_argument('--current', type=Path,
                          help='authenticated snapshot directory; reuse its packages by metadata and merge additions')
    for name in ('list', 'show', 'plan'):
        command = commands.add_parser(name)
        command.add_argument('--release', required=True, type=Path)
        command.add_argument('--directory', required=True, type=Path)
        command.add_argument('--trust-root', required=True, type=Path)
        command.add_argument('--repository', action='append', default=[])
        if name != 'list':
            command.add_argument('--set', action='append', default=[], dest='sets')
        if name == 'plan':
            command.add_argument('--package', action='append', default=[], dest='roots')
    args = parser.parse_args(argv)
    try:
        roles = ('openssl', 'pkg_admin', 'pkg_info') if args.command == 'compose' else ('openssl',)
        crypto = ReleaseCrypto(load_host_prefix(args.host_prefix, required_roles=roles))
        if args.command == 'compose':
            signer = SigningIdentity(args.signer_directory / 'certificate.pem', args.signer_directory / 'private-key.pem')
            value = compose(crypto, read_json(args.input), args.ports_root, args.output, signer, args.trust_root,
                            current=args.current)
        else:
            if args.command == 'plan' and not args.repository:
                raise SDKError('plan requires explicit --repository selections (host and/or targets)')
            release, catalogs = load_catalogs(crypto, args.release, args.directory, args.trust_root, args.repository)
            if args.command == 'list':
                value = {catalog['repository']: sorted(catalog['sets']) for catalog in catalogs}
            elif args.command == 'show':
                available = {name for catalog in catalogs for name in catalog['sets']}
                if set(args.sets) - available:
                    raise SDKError('unknown package set in selected repositories')
                value = {catalog['repository']: {name: roots for name, roots in catalog['sets'].items()
                         if not args.sets or name in args.sets} for catalog in catalogs}
            else:
                value = select_packages(release, catalogs, sets=args.sets, roots=args.roots)
        sys.stdout.buffer.write(json_bytes(value))
        return 0
    except (SDKError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f'binary-catalog: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
