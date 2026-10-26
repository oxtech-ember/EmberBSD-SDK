# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Check bound-recipe MAKECONF link flags; optional explicit BSD make fixture."""
import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_core  # Adds the SDK source to sys.path.
from emberbsd_sdk import bootstrap_producer as producer


FULL = 'emberbsd-native-gcc16-bootstrap-plan'
ABI = '--sysroot=/fixture/sdk'
RUNTIME = '-static-libstdc++ -static-libgcc -nodefaultrpaths'
DEPENDENCY = '-L/fixture/prefix/lib -lnbcompat'


def render(kind, profile, flags=ABI):
    plan = {'format': kind, 'metadata': {'profile': {'path': str(profile)}}}
    with patch.object(producer, 'metadata_assignments', return_value={
            'CC': '/selected/gcc', 'LDFLAGS': flags}):
        return producer.metadata_makeconf(plan, {}, {}, {})


class MakeconfScopeTests(unittest.TestCase):
    def test_metadata_bytes_remain_exact(self):
        expected = (b'# SPDX-License-Identifier: BSD-2-Clause\n'
                    b'# Source-owned SDK metadata inputs. This file is not qualification.\n'
                    b'CC= /selected/gcc\nLDFLAGS= --sysroot=/fixture/sdk\n'
                    b'.include "/fixture/profile.mk"\n')
        self.assertEqual(render('emberbsd-native-gcc16-producer-plan', '/fixture/profile.mk'), expected)

    def test_full_plan_guards_only_link_flag_initialization(self):
        data = render(FULL, '/fixture/profile.mk')
        self.assertIn(b'CC= /selected/gcc\n.if defined(BSD_PKG_MK)\nLDFLAGS= ', data)
        self.assertIn(b'\n.endif\n.include "/fixture/profile.mk"\n', data)
        self.assertEqual(data.replace(b'.if defined(BSD_PKG_MK)\n', b'').replace(b'.endif\n', b''),
                         render('emberbsd-native-gcc16-producer-plan', '/fixture/profile.mk'))

    def test_empty_selected_link_flags_still_initialize(self):
        self.assertIn(b'.if defined(BSD_PKG_MK)\nLDFLAGS= \n.endif\n',
                      render(FULL, '/fixture/profile.mk', ''))

    def test_full_plan_does_not_use_conditional_default_assignment(self):
        self.assertNotIn(b'LDFLAGS?=', render(FULL, '/fixture/profile.mk'))


def real_make_fixture(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('work', type=Path)
    parser.add_argument('--make-arg', action='append', required=True)
    parser.add_argument('--shell', required=True)
    args = parser.parse_args(argv)
    if not args.work.is_absolute() or args.work.exists() or args.work.is_symlink():
        raise AssertionError('Use a new absolute source-test work directory')
    if not all(os.path.isabs(value) and not any(c.isspace() for c in value)
               for value in (args.make_arg[0], args.shell)):
        raise AssertionError('Use explicit selected make and shell paths')
    if sum(value.startswith('MAKECONF=') for value in args.make_arg) != 1:
        raise AssertionError('Pass the complete selected make tuple with one MAKECONF')
    args.work.mkdir()
    empty = args.work / 'empty-bin'
    empty.mkdir()
    env = dict(PATH=str(empty), LC_ALL='C', TZ='UTC', SHELL=args.shell, TMPDIR=str(args.work))
    profile = args.work / 'profile.mk'
    profile.write_text('LDFLAGS+= ' + RUNTIME + '\n')
    new = render(FULL, profile)
    old = new.replace(b'.if defined(BSD_PKG_MK)\n', b'').replace(b'.endif\n', b'')
    question = old.replace(b'LDFLAGS= ', b'LDFLAGS?= ')
    observations = []

    def query(mode, context, config, inherited=None, command_line=None):
        path = args.work / (mode + '-' + context + '.mk')
        shell = f'.SHELL: name=sh path={args.shell} hasErrCtl=no check="set -e" ignore="set +e" echoFlag=v errFlag=e\n'
        if context.startswith('outer'):
            # A defined empty make default must not suppress selected ABI flags.
            body = 'BSD_PKG_MK=1\nLDFLAGS=\n.include "${MAKECONF}"\nLDFLAGS+= ' + DEPENDENCY + '\n'
        else:
            # This is the real common BSD include which reads MAKECONF again.
            body = '.include <bsd.own.mk>\n'
        path.write_text(shell + body)
        command = [value if not value.startswith('MAKECONF=') else 'MAKECONF=' + str(config)
                   for value in args.make_arg]
        command += ['-r', '-f', str(path), '-V', 'LDFLAGS=${LDFLAGS}']
        if command_line is not None:
            command += ['LDFLAGS=' + command_line]
        child_env = dict(env)
        if inherited is not None:
            child_env['LDFLAGS'] = inherited
        result = subprocess.run(command, cwd=args.work, env=child_env, capture_output=True)
        stdout, stderr = path.with_suffix('.stdout'), path.with_suffix('.stderr')
        stdout.write_bytes(result.stdout)
        stderr.write_bytes(result.stderr)
        row = dict(mode=mode, context=context, argv=command, exit=result.returncode,
                   stdout=str(stdout), stderr=str(stderr), inherited_LDFLAGS=inherited,
                   command_line_LDFLAGS=command_line)
        observations.append(row)
        path.with_suffix('.result.json').write_text(json.dumps(row, indent=2, sort_keys=True) + '\n')
        if result.returncode or result.stderr or not result.stdout.startswith(b'LDFLAGS='):
            raise AssertionError('The explicit BSD make query failed; see ' + str(stderr))
        return result.stdout.decode().removeprefix('LDFLAGS=').rstrip('\n')

    values = {}
    for mode, data in [('old', old), ('new', new), ('question', question)]:
        config = args.work / (mode + '.conf')
        config.write_bytes(data)
        outer = query(mode, 'outer', config)
        inner = query(mode, 'inner', config, outer)
        values[mode] = dict(outer=outer, inner=inner)
    words = lambda value: shlex.split(value)
    if words(values['old']['inner']) != words(ABI + ' ' + RUNTIME):
        raise AssertionError('The old assignment did not reproduce the inherited flag loss')
    expected_outer = words(ABI + ' ' + RUNTIME + ' ' + DEPENDENCY)
    if words(values['new']['outer']) != expected_outer:
        raise AssertionError('The pkgsrc context lost selected ABI or dependency flags')
    if words(values['new']['inner']) != expected_outer + words(RUNTIME):
        raise AssertionError('The nested BSD include replaced inherited link flags')
    if ABI in words(values['question']['outer']):
        raise AssertionError('The defined-empty conditional-default control lost its causal failure')
    config = args.work / 'new.conf'
    undefined = query('new', 'inner-undefined', config)
    empty_value = query('new', 'inner-empty', config, '')
    command_line = '--sysroot=/command/sdk -L/command/lib'
    override = query('new', 'inner-command-line', config, values['new']['outer'], command_line)
    if words(undefined) != words(RUNTIME) or words(empty_value) != words(RUNTIME):
        raise AssertionError('A non-pkgsrc context invented selected ABI flags instead of preserving input')
    if words(override) != words(command_line):
        raise AssertionError('The configuration replaced a make command-line override')
    report = dict(scope='Explicit BSD make source semantics only; no compiler or tool qualification',
                  observations=observations, values=values,
                  extra_controls=dict(undefined_inherited=undefined, empty_inherited=empty_value,
                                      command_line_override=override), status='PASS')
    (args.work / 'result.json').write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
    print(json.dumps(dict(status='PASS', result=str(args.work / 'result.json'))))


if __name__ == '__main__':
    if any(value.startswith('--make-arg') for value in sys.argv[1:]):
        real_make_fixture(sys.argv[1:])
    else:
        unittest.main()
