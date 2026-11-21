#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Compile a QML file to a native EmberBSD scene."""
import argparse
from contextlib import ExitStack
from pathlib import Path
import stat
import sys
import tempfile

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from emberbsd_sdk.qml import QmlError, compile_scene, compile_scene_with_header
from emberbsd_sdk.qml_image import BitmapAssets


def check_paths(paths):
    resolved = set()
    existing = set()
    for path in paths:
        real = path.resolve()
        identity = None
        try:
            info = path.stat()
            identity = (info.st_dev, info.st_ino)
        except FileNotFoundError:
            pass
        if real in resolved or (identity is not None and identity in existing):
            raise QmlError("source and output files must be different")
        resolved.add(real)
        if identity is not None:
            existing.add(identity)


def write_outputs(outputs):
    # Stage every changed output before atomically replacing each destination.
    with ExitStack() as stack:
        directories = {}
        pending = []
        for path, data in outputs:
            content = data.encode("utf-8")
            try:
                mode = path.lstat().st_mode
            except FileNotFoundError:
                mode = None
            if mode is not None and stat.S_ISDIR(mode):
                raise IsADirectoryError(str(path))
            regular = mode is not None and stat.S_ISREG(mode)
            if regular and path.read_bytes() == content:
                continue
            parent = path.parent.stat()
            identity = (parent.st_dev, parent.st_ino)
            if identity not in directories:
                directories[identity] = Path(stack.enter_context(tempfile.TemporaryDirectory(dir=path.parent)))
            temp = directories[identity] / path.name
            with temp.open("xb") as target:
                target.write(content)
            if regular:
                temp.chmod(stat.S_IMODE(mode) & 0o777)
            pending.append((temp, path))
        for temp, path in pending:
            temp.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--header", type=Path, help="also emit a C header with scene and application bindings")
    parser.add_argument("--name", default="app", help="suffix of the exported eqml_ symbol")
    parser.add_argument("--pngtopam", type=Path, help="absolute native Netpbm PNG decoder for Image assets")
    args = parser.parse_args()
    try:
        check_paths([args.source, args.output] + ([args.header] if args.header else []))
        source = args.source.read_text(encoding="utf-8")
        assets = BitmapAssets(args.source.parent, args.pngtopam)
        if args.header is None:
            outputs = [(args.output, compile_scene(source, args.name, assets=assets))]
        else:
            scene, header = compile_scene_with_header(source, args.name, assets=assets)
            outputs = [(args.output, scene), (args.header, header)]
        write_outputs(outputs)
    except (OSError, UnicodeError, QmlError) as error:
        print(f"{args.source}:{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
