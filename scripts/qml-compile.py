#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Compile a QML file to a native EmberBSD scene."""
import argparse
from pathlib import Path
import stat
import sys
import tempfile

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from emberbsd_sdk.qml import QmlError, compile_scene
from emberbsd_sdk.qml_image import BitmapAssets


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--name", default="app", help="suffix of the exported eqml_ symbol")
    parser.add_argument("--pngtopam", type=Path, help="absolute native Netpbm PNG decoder for Image assets")
    args = parser.parse_args()
    if args.source.resolve() == args.output.resolve():
        parser.error("source and output must be different files")
    try:
        result = compile_scene(args.source.read_text(encoding="utf-8"), args.name,
                               assets=BitmapAssets(args.source.parent, args.pngtopam))
        try:
            existing = args.output.lstat()
        except FileNotFoundError:
            existing = None
        if existing is not None and stat.S_ISREG(existing.st_mode):
            if args.output.read_bytes() == result.encode("utf-8"):
                return 0
        with tempfile.TemporaryDirectory(dir=args.output.parent) as work:
            temporary = Path(work) / "scene.c"
            temporary.write_text(result, encoding="utf-8")
            if existing is not None and stat.S_ISREG(existing.st_mode):
                temporary.chmod(existing.st_mode & 0o777)
            temporary.replace(args.output)
    except (OSError, UnicodeError, QmlError) as error:
        print(f"{args.source}:{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
