# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Create the client zipapp from the SDK source."""

from pathlib import Path
import zipfile


def package_client(output, *, bootstrap=False):
    source = Path(__file__).resolve().parents[2]
    entry = 'bootstrap_bundle import client_main as main' if bootstrap else 'sdk_client import main'
    with Path(output).open('xb') as stream:
        with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('__main__.py', 'from emberbsd_sdk.' + entry + '\nraise SystemExit(main())\n')
            for path in sorted((source / 'src/emberbsd_sdk').rglob('*.py')):
                archive.write(path, str(path.relative_to(source / 'src')))
            archive.write(source / 'trust/emberbsd-release-root.pem', 'emberbsd_sdk/emberbsd-release-root.pem')
            archive.write(source / 'LICENSE', 'LICENSE')
