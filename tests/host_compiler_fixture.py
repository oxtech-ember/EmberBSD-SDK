# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD; AI-assisted native GCC16 selector fixtures.
"""Fixture compiler drivers. They answer identity queries and compile nothing."""

from pathlib import Path


def fake_compiler(directory, name, version='16.2.0', clang=False, machine='aarch64-apple-darwin27'):
    major = version.split('.')[0]
    banner = f'Apple clang version {version} (clang-2100.0.0)' if clang else f'{name} (EmberBSD fixture) {version}'
    macros = (f'#define __clang__ 1\\n#define __GNUC__ 4\\n' if clang else f'#define __GNUC__ {major}\\n#define __GNUC_MINOR__ 2\\n')
    path = Path(directory) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'''#!/bin/sh
case "$1" in
--version) printf '%s\\n' '{banner}' ;;
-dumpfullversion) printf '%s\\n' '{version}' ;;
-dumpmachine) printf '%s\\n' '{machine}' ;;
-print-prog-name=*) printf '%s\\n' "${{1#-print-prog-name=}}" ;;
-x) printf '{macros}' ;;
*) echo "fixture compiler: unsupported $*" >&2; exit 1 ;;
esac
''')
    path.chmod(0o755)
    return str(path)


def gcc16_pair(directory):
    return {'host-cc': fake_compiler(directory, 'gcc-16'), 'host-cxx': fake_compiler(directory, 'g++-16')}
