#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Publish signed releases to S3; read pinned release metadata through HTTPS."""

import argparse
from contextlib import ExitStack
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.jsonio import json_bytes
from emberbsd_sdk.release_delivery import fetch_artifact, fetch_release, publish_release
from emberbsd_sdk.release_s3 import HTTPStore, S3Store
from emberbsd_sdk.release_signing import ReleaseCrypto, SigningIdentity


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host-prefix', required=True)
    parser.add_argument('--tls-ca-file', type=Path, help='HTTPS trust bundle; separate from the release root')
    parser.add_argument('--https-proxy', help='explicit HTTP CONNECT proxy URL for HTTPS')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('publish', 'fetch', 'fetch-artifact'):
        command = commands.add_parser(name)
        command.add_argument('--url', required=True, help='public HTTPS base URL, without S3 credentials')
        command.add_argument('--prefix', default='', help='optional repository key prefix, shared by S3 and HTTPS')
        command.add_argument('--trust-root', required=True, type=Path)
        if name == 'publish':
            command.add_argument('release', type=Path, help='local release.p7m beside its payloads')
            command.add_argument('--signer-directory', required=True, type=Path)
            command.add_argument('--endpoint', help='defaults to EMBERBSD_S3_ENDPOINT')
            command.add_argument('--region', help='defaults to EMBERBSD_S3_REGION')
            command.add_argument('--bucket', help='defaults to EMBERBSD_S3_BUCKET')
            command.add_argument('--ttl', type=int, default=7 * 86400, help='channel lifetime in seconds')
            command.add_argument('--refresh', action='store_true', help='refresh expiry even for the same release')
            command.add_argument('--expected-current',
                                 help='refuse a changed current before uploading; SHA256 of current.p7m, or absent for an empty repository')
            command.add_argument('--reuse-current', action='store_true',
                                 help='reuse exact missing records from authenticated current (requires --expected-current)')
            command.add_argument('--previous-channel-time', type=int,
                                 help='publisher recovery only: authenticate the old channel at this past Unix time')
        elif name == 'fetch':
            command.add_argument('--output', required=True, type=Path, help='new local metadata snapshot directory')
            command.add_argument('--repository', action='append', default=[])
        else:
            command.add_argument('--output', required=True, type=Path, help='local artifact file to create or replace')
            command.add_argument('--artifact', required=True, help='exact signed artifact ID')
    args = parser.parse_args(argv)
    try:
        with ExitStack() as transports:
            crypto = ReleaseCrypto.from_prefix(args.host_prefix, packages=args.command == 'publish')
            public = transports.enter_context(HTTPStore(args.url, ca_file=args.tls_ca_file, proxy_url=args.https_proxy))
            if args.command == 'publish':
                store = transports.enter_context(S3Store.from_environment(
                    endpoint=args.endpoint, region=args.region, bucket=args.bucket, ca_file=args.tls_ca_file, proxy_url=args.https_proxy))
                identity = SigningIdentity(args.signer_directory / 'certificate.pem',
                                           args.signer_directory / 'private-key.pem')
                value = publish_release(crypto, store, public, args.release, identity, args.trust_root,
                                        prefix=args.prefix, ttl=args.ttl, refresh=args.refresh,
                                        previous_channel_time=args.previous_channel_time,
                                        expected_current=args.expected_current,
                                        reuse_current=args.reuse_current)
            elif args.command == 'fetch':
                value = fetch_release(crypto, public, args.trust_root, args.output,
                                      prefix=args.prefix, repositories=args.repository)
            else:
                value = fetch_artifact(crypto, public, args.trust_root, args.output,
                                       args.artifact, prefix=args.prefix)
        sys.stdout.buffer.write(json_bytes(value))
        return 0
    except (SDKError, OSError, ValueError) as exc:
        print(f's3-release: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
