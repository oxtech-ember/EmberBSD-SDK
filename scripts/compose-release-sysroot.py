#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Export an application sysroot from pinned EmberBSD sets and a Ports runtime."""
import argparse
from pathlib import Path
import subprocess
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk import jsonio, sysroot_composition, target_toolchain
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.host_prefix import load_host_prefix
from emberbsd_sdk.targets import target_definition


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('release-kit', 'output', 'manifest-sha256', 'runtime-stage', 'ports-source', 'host-prefix', 'target', 'git'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    try:
        native = load_host_prefix(args.host_prefix, required_roles=('python',))
        compiler = target_toolchain.clang_compiler(args.host_prefix, target_definition(args.target)['triplet'],
            host=native.host, env={'PATH': native.path, 'LC_ALL': 'C'})
        revision = subprocess.check_output([args.git, '-C', str(Path(__file__).resolve().parents[1]),
                                            'rev-parse', 'HEAD'], text=True).strip()
        result = sysroot_composition.compose(args.release_kit, args.output, manifest_sha256=args.manifest_sha256,
            runtime_stage=args.runtime_stage, ports_source=args.ports_source, target=args.target,
            compiler=compiler, sdk_revision=revision)
        sys.stdout.buffer.write(jsonio.json_bytes(result))
        return 0
    except (SDKError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        print('compose-release-sysroot: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
