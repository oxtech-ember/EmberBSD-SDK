#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
# Origin: EmberBSD contributors (AI-assisted).
"""Exercise lint output invalidation and regeneration with selected SDK tools."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile

parser = argparse.ArgumentParser()
parser.add_argument('--sdk-source', type=Path, required=True)
parser.add_argument('--prefix', type=Path, required=True)
args = parser.parse_args()
sys.path.insert(0, str(args.sdk_source / 'src'))
from emberbsd_sdk import prepared_refresh

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp).resolve()
    lint, obj = root / 'unit.ln', root / 'unit.o'
    lint.write_text('old')
    obj.write_text('object')
    makefile = root / 'Makefile'
    python = args.prefix / 'bin/python3.15'
    makefile.write_text(
        f'.SHELL: name=sh path={args.prefix}/bin/mksh\n'
        f'unit.ln:\n\t{python} -B -c "from pathlib import Path; '
        "Path('unit.ln').write_text('new')\"\n")
    prepared_refresh.remove_lint_outputs(root, ['unit.ln'])
    env = dict(os.environ, PATH=f'{args.prefix}/bin:{args.prefix}/gnu/bin')
    subprocess.run([str(args.prefix / 'bin/bmake'), '-r', '-f', str(makefile), 'unit.ln'],
                   cwd=root, env=env, check=True)
    assert lint.read_text() == 'new' and obj.read_text() == 'object'
print('lint output regeneration PASS')
