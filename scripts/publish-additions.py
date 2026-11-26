#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Maintainer-only addition of built packages to authenticated S3 current."""

import argparse
import json
from contextlib import ExitStack
from pathlib import Path
import subprocess
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.host_prefix import load_host_prefix
from emberbsd_sdk.jsonio import json_bytes, read_json
from emberbsd_sdk.package_publish import follow_completions, packages_from_state, update_packages
from emberbsd_sdk.release_s3 import HTTPStore, S3Store
from emberbsd_sdk.release_signing import ReleaseCrypto, SigningIdentity


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path, help='existing package-build JSON with the producer graph and platform')
    parser.add_argument('--host-prefix', required=True)
    parser.add_argument('--ports-root', required=True, type=Path)
    parser.add_argument('--workdir', required=True, type=Path, help='private reusable state for publication and retry')
    parser.add_argument('--state', type=Path, help='Ports STATE directory: select successful packages of one repository')
    parser.add_argument('--state-root', action='append', default=[], metavar='PKGPATH',
                        help='publish this completed state root using the existing graph; repeat for a batch')
    parser.add_argument('--follow', action='store_true',
                        help='publish batches of completed roots until this Ports build releases its build.lock')
    parser.add_argument('--interval', type=int, help='seconds between receipt checks with --follow (default: 30)')
    parser.add_argument('--hold-root', action='append', default=[], metavar='PKGPATH',
                        help='hold a known bad root and its consumers with --follow')
    parser.add_argument('--packages', type=Path, help='archive directory for that role, e.g. target PACKAGES/All')
    parser.add_argument('--url', required=True, help='public HTTPS base URL')
    parser.add_argument('--prefix', default='')
    parser.add_argument('--trust-root', required=True, type=Path)
    parser.add_argument('--signer-directory', required=True, type=Path)
    parser.add_argument('--endpoint', help='defaults to EMBERBSD_S3_ENDPOINT')
    parser.add_argument('--region', help='defaults to EMBERBSD_S3_REGION')
    parser.add_argument('--bucket', help='defaults to EMBERBSD_S3_BUCKET')
    parser.add_argument('--tls-ca-file', type=Path)
    parser.add_argument('--ttl', type=int, default=7 * 86400)
    parser.add_argument('--forbid-prefix', action='append', default=[], metavar='DIRECTORY',
                        help='reject new archives that contain this absolute producer path')
    args = parser.parse_args(argv)
    if bool(args.state) != bool(args.packages):
        parser.error('--state and --packages must be supplied together')
    if args.state_root and not args.state:
        parser.error('--state-root requires --state and --packages')
    if args.follow and (not args.state or args.state_root):
        parser.error('--follow requires --state and --packages and selects its own completed roots')
    if not args.follow and (args.hold_root or args.interval is not None):
        parser.error('--hold-root and --interval require --follow')
    try:
        spec, checksums = read_json(args.input), None
        if args.state and not args.follow:
            spec, checksums = packages_from_state(spec, args.state, args.packages, selected_roots=args.state_root)
        with ExitStack() as transports:
            store = transports.enter_context(S3Store.from_environment(
                endpoint=args.endpoint, region=args.region, bucket=args.bucket, ca_file=args.tls_ca_file))
            public = transports.enter_context(HTTPStore(args.url, ca_file=args.tls_ca_file))
            crypto = ReleaseCrypto(load_host_prefix(args.host_prefix, required_roles=('openssl', 'pkg_admin', 'pkg_info')))
            identity = SigningIdentity(args.signer_directory / 'certificate.pem', args.signer_directory / 'private-key.pem')
            if args.follow:
                for event in follow_completions(crypto, store, public, spec, args.ports_root,
                        args.workdir, identity, args.trust_root, args.state, args.packages,
                        held=args.hold_root, prefix=args.prefix, ttl=args.ttl,
                        forbidden_prefixes=args.forbid_prefix, interval=30 if args.interval is None else args.interval):
                    print(json.dumps(event, sort_keys=True), flush=True)
                return 0
            result = update_packages(crypto, store, public, spec, args.ports_root, args.workdir,
                                     identity, args.trust_root, prefix=args.prefix, ttl=args.ttl,
                                     checksums=checksums, forbidden_prefixes=args.forbid_prefix)
        counts = result['statistics']
        print('Packages: {added} added, {replaced} replaced, {reused} reused; '
              '{uploaded_packages} upload candidates ({upload_bytes} bytes); {built} built.'.format(**counts), file=sys.stderr)
        sys.stdout.buffer.write(json_bytes(result))
        return 0
    except (SDKError, OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        print(f'publish-additions: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
