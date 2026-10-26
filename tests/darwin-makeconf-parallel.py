#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Run the real pkgsrc barrier with rendered policy; no package or compiler runs."""
import argparse
import os
from pathlib import Path
import shlex
import subprocess
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from test_darwin_makeconf import DarwinMakeconfTests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--make', type=Path, required=True)
    parser.add_argument('--shell', type=Path, required=True)
    parser.add_argument('--pkgsrc', type=Path, required=True)
    args = parser.parse_args()
    make, shell, pkgsrc = (path.resolve(strict=True) for path in (args.make, args.shell, args.pkgsrc))
    fixture = DarwinMakeconfTests()
    fixture.setUp()
    try:
        fixture.tools.update(make=str(make), sh=str(shell))
        Path(fixture.options['makefiles'], 'sys.mk').write_text('.SHELL: name=sh path=' + str(shell) + '\n')
        fixture.options.update(pkgsrc=str(pkgsrc), tool_phase='initial',
            tool_roots=(*fixture.options['tool_roots'], str(make.parent), str(shell.parent),
                        str(Path(sys.executable).parent)))
        recipe = fixture.root / 'Makefile'
        result = fixture.root / 'leaf'
        conf = Path(fixture.options['output'])
        command = shlex.join([str(make), '-r', '-f', str(recipe)])
        recipe.write_text('EMBERBSD_DARWIN_BOOTSTRAP=yes\n'
            'EMBERBSD_DARWIN_METADATA=yes\n'
            '.include "' + str(conf) + '"\n'
            # A dependency boundary cannot rely on the outer command overrides.
            '.MAKEOVERRIDES=\nMAKEFLAGS=\n'
            '.SHELL: name=sh path=' + str(shell) + '\n'
            'RECURSIVE_MAKE= ' + command + '\n'
            'WRKDIR= ' + str(fixture.root / 'barrier') + '\n'
            'PKGNAME= parallel-policy-fixture\nRUN= @\nTEST= test\n'
            'MKDIR= mkdir -p\nECHO= echo\n'
            'checksum-phase makedirs depends error-check:\n'
            '.include "' + str(pkgsrc / 'mk/bsd.pkg.barrier.mk') + '"\n'
            '.if !defined(_PKGSRC_BARRIER)\nbuild: barrier\n.else\n'
            'build:\n\t@printf "%s\\n" "${MAKE_JOBS}" "${EMBERBSD_DARWIN_CPU_BUDGET}" '
            "'${EMBERBSD_DARWIN_CMAKE_LINKER_LAUNCHER}' \"${.MAKE.LEVEL}\" > " + str(result) + '\n.endif\n')
        env = {key: value for key, value in os.environ.items() if key not in ('MAKEFLAGS', 'MFLAGS', 'MAKELEVEL')}
        env['PATH'] = '/usr/bin:/bin'  # Explicit test shell utilities; no build commands.
        for jobs in (1, 18):
            selected = dict(jobs=jobs, cpu_budget=18)
            if jobs > 1:
                selected.update(link_python=sys.executable, lock_root=fixture.root / 'work',
                                heavy_link_context=fixture.root / 'work/heavy-link')
            fixture.render(**selected)
            child = subprocess.run([str(make), '-r', '-f', str(recipe), 'MAKE_JOBS=18', 'build'],
                                   cwd=fixture.root, env=env, text=True, capture_output=True, timeout=30)
            assert child.returncode == 0, child.stdout + child.stderr
            leaf = result.read_text().splitlines()
            assert leaf[0:2] == [str(jobs), '18'], leaf
            assert int(leaf[3]) >= 1, leaf
            if jobs == 18:
                assert '/scripts/link-launcher.py;--lock-root;' in leaf[2], leaf
            else:
                assert leaf[2] == '', leaf
        print('PASS: outer jobs=18 becomes 1 across the real barrier with old config; persisted jobs/budget/launcher survive')
    finally:
        fixture.doCleanups()


if __name__ == '__main__':
    main()
