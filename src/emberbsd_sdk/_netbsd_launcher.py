# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Release no producer before the controller installs kernel fork tracking."""
import os
import sys


def main():
    gate = int(sys.argv[1])
    try:
        release = os.read(gate, 2)
    finally:
        os.close(gate)
    if release != b'G':
        return 125
    try:
        os.execvpe(sys.argv[2], sys.argv[2:], os.environ)
    except OSError as exc:
        print(f'producer exec failed: {exc}', file=sys.stderr)
        return 127


if __name__ == '__main__':
    raise SystemExit(main())
