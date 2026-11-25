#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Package a Darwin bootstrap from complete local Ports producer archives."""

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk.bootstrap_bundle import prepare, write_archive
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.release_signing import ReleaseCrypto


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host-prefix', required=True)
    parser.add_argument('--trust-root', required=True, type=Path,
                        help='trusted root for every qualified package')
    parser.add_argument('--stage', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--package', type=Path, action='append', required=True)
    parser.add_argument('--repository', required=True,
                        help='native repository ID from the signed release (for example, darwin-arm64)')
    parser.add_argument('--abi', required=True)
    parser.add_argument('--https-ca', default='etc/openssl/certs/ca-certificates.crt')
    parser.add_argument('--client-pyz', type=Path,
                        help='reuse a verified SDK client zipapp instead of building one')
    parser.add_argument('--client-sha256',
                        help='required SHA256 when --client-pyz is selected')
    parser.add_argument('--forbid-prefix', action='append', default=[],
                        help='reject this producer path from the complete bundle; may be repeated')
    parser.add_argument('--bootstrap-install-name-tool', type=Path, required=True,
                        help='explicit initial Mach-O editor; never selected from PATH')
    parser.add_argument('--codesign', type=Path, required=True,
                        help='explicit macOS code-signing service (ad-hoc signatures)')
    args = parser.parse_args()
    try:
        if not args.stage.is_absolute() or not args.output.is_absolute() or args.output.exists():
            raise SDKError('select a new absolute stage and archive')
        for tool in (args.bootstrap_install_name_tool, args.codesign):
            if not tool.is_absolute() or not tool.is_file():
                raise SDKError('select explicit absolute packaging tools')
        crypto = ReleaseCrypto.from_prefix(args.host_prefix, packages=True)
        import platform
        if platform.system() != 'Darwin':
            raise SDKError('bootstrap packaging currently supports Darwin hosts')
        metadata = prepare(args.stage, args.package, crypto, trust=args.trust_root,
            host=dict(os='Darwin', arch=platform.machine(), abi=args.abi, runtime='libSystem'),
            repository=args.repository, install_name_tool=args.bootstrap_install_name_tool,
            codesign=args.codesign, https_ca=args.https_ca,
            client_pyz=args.client_pyz, client_sha256=args.client_sha256)
        write_archive(args.stage, args.output, metadata['packages'],
                      forbidden_tokens=metadata['_producer_tokens'] +
                      [str(Path(args.host_prefix).resolve()),
                       str(Path(__file__).resolve().parents[1]), *args.forbid_prefix])
        print(f'{len(metadata["packages"])} complete packages; {args.output.stat().st_size} archive bytes')
        return 0
    except (SDKError, OSError, ValueError) as exc:
        print('package-bootstrap: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
