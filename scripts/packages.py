#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Select explicit package roots and delegate to the shared Ports graph."""

from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from emberbsd_sdk.package_selection import main


if __name__ == "__main__":
    if '--config' in sys.argv or any(arg.startswith('--config=') for arg in sys.argv):
        from emberbsd_sdk.sdk_client import main
    raise SystemExit(main())
