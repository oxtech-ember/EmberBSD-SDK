#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Private child for the SDK-controlled current Ports source exporter."""

import sys

sys.dont_write_bytecode = True


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 3:
        print('usage: bootstrap-pkgsrc-export-worker.py SOURCE SOURCE_SHA256 PLAN', file=sys.stderr)
        return 2
    source_path, source_sha256, plan_path = argv
    # Derive imports from this fixed worker file, never from mutable plan data.
    import os
    sdk_root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    sys.path.insert(0, sdk_root + '/src')
    from emberbsd_sdk.bootstrap_pkgsrc_export import worker_main
    return worker_main(source_path, source_sha256, plan_path)


if __name__ == '__main__':
    raise SystemExit(main())
