#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Maintainer-only addition of built packages to authenticated S3 current."""

import argparse
from contextlib import ExitStack
from pathlib import Path
import subprocess
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.host_prefix import load_host_prefix
from emberbsd_sdk.jsonio import json_bytes, read_json
from emberbsd_sdk.package_publish import packages_from_state, update_packages
from emberbsd_sdk.release_s3 import HTTPStore, S3Store
from emberbsd_sdk.release_signing import ReleaseCrypto, SigningIdentity


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path, help='existing package-build JSON with the producer graph and platform')
    parser.add_argument('--host-prefix', required=True)
    parser.add_argument('--ports-root', required=True, type=Path)
    parser.add_argument('--workdir', required=True, type=Path, help='private reusable state for publication and retry')
    parser.add_argument('--state', type=Path, help='Ports STATE directory: select successful packages of one repository')
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
    args = parser.parse_args(argv)
    if bool(args.state) != bool(args.packages):
        parser.error('--state and --packages must be supplied together')
    try:
        spec, checksums = read_json(args.input), None
        if args.state:
            spec, checksums = packages_from_state(spec, args.state, args.packages)
        with ExitStack() as transports:
            store = transports.enter_context(S3Store.from_environment(
                endpoint=args.endpoint, region=args.region, bucket=args.bucket, ca_file=args.tls_ca_file))
            public = transports.enter_context(HTTPStore(args.url, ca_file=args.tls_ca_file))
            crypto = ReleaseCrypto(load_host_prefix(args.host_prefix, required_roles=('openssl', 'pkg_admin', 'pkg_info')))
            identity = SigningIdentity(args.signer_directory / 'certificate.pem', args.signer_directory / 'private-key.pem')
            result = update_packages(crypto, store, public, spec, args.ports_root, args.workdir,
                                     identity, args.trust_root, prefix=args.prefix, ttl=args.ttl, checksums=checksums)
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
