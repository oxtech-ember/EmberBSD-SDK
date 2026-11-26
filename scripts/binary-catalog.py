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
from emberbsd_sdk.catalog_recovery import recover
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.host_prefix import load_host_prefix
from emberbsd_sdk.jsonio import json_bytes, read_json
from emberbsd_sdk.package_publish import publication_candidates
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
    producer.add_argument('--forbid-prefix', action='append', default=[], metavar='DIRECTORY',
                          help='reject new archives that contain this absolute producer path')
    producer.add_argument('--current', type=Path,
                          help='authenticated snapshot directory; reuse its packages by metadata and merge additions')
    candidates = commands.add_parser('publication-candidates', help='list completed Ports roots missing or changed in a signed catalog')
    candidates.add_argument('input', type=Path, help='package-build JSON for one producer graph and repository')
    candidates.add_argument('--ready', action='store_true',
                            help='order completed roots with available runtime providers')
    candidates.add_argument('--hold-root', action='append', default=[], metavar='PKGPATH',
                            help='with --ready, omit this candidate from the batch; repeat for known failures')
    candidates.add_argument('--state', required=True, type=Path)
    candidates.add_argument('--packages', required=True, type=Path)
    candidates.add_argument('--current', required=True, type=Path)
    candidates.add_argument('--trust-root', required=True, type=Path)
    recovery = commands.add_parser('recover-selections', help='restore available catalogs after a withdrawal')
    recovery.add_argument('--current', required=True, type=Path)
    recovery.add_argument('--withdrawal', action='append', nargs=2, type=Path, required=True,
                          metavar=('BEFORE', 'AFTER'), help='signed snapshots on both sides of the withdrawal')
    recovery.add_argument('--target-sysroot', action='append', nargs=2, default=[],
                          metavar=('REPOSITORY', 'DIRECTORY'), help='check exact target base library paths')
    recovery.add_argument('--release-id', required=True)
    recovery.add_argument('--output', required=True, type=Path)
    recovery.add_argument('--signer-directory', required=True, type=Path)
    recovery.add_argument('--trust-root', required=True, type=Path)
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
        roles = {'compose': ('openssl', 'pkg_admin', 'pkg_info'),
                 'recover-selections': ('openssl', 'pkg_admin')}.get(args.command, ('openssl',))
        crypto = ReleaseCrypto(load_host_prefix(args.host_prefix, required_roles=roles))
        if args.command in ('compose', 'recover-selections'):
            signer = SigningIdentity(args.signer_directory / 'certificate.pem', args.signer_directory / 'private-key.pem')
        if args.command == 'publication-candidates':
            if args.hold_root and not args.ready:
                parser.error('--hold-root requires --ready')
            value = publication_candidates(crypto, read_json(args.input), args.state,
                                           args.packages, args.current, args.trust_root, ready=args.ready, held=args.hold_root)
        elif args.command == 'recover-selections':
            sysroots = {name: Path(root) for name, root in args.target_sysroot}
            if len(sysroots) != len(args.target_sysroot):
                raise SDKError('select each target sysroot once')
            value = recover(crypto, args.current, args.withdrawal, sysroots, args.output, signer,
                            args.trust_root, args.release_id)
        elif args.command == 'compose':
            value = compose(crypto, read_json(args.input), args.ports_root, args.output, signer, args.trust_root,
                            current=args.current, forbidden_prefixes=args.forbid_prefix)
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
