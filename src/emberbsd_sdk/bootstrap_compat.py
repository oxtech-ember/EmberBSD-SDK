# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Finite OS-owned efun probes. The existing pilot owns every compiler child."""

import os
import re
from pathlib import Path

from .inventory import sha256_file
from .native_toolset import _json, _require
from .paths import entry

EFUN_PROGRAMS = ('cut', 'sort', 'hexdump')
EFUN_PROGRAM_STAGES = tuple(name + '-bootstrap' for name in EFUN_PROGRAMS)

MANIFEST_RELATIVE = 'tools/compat/bootstrap-efun.json'
MANIFEST_SHA256 = '3aa76b9c12fbb0581205ac1477b5d1f11b79cf980acffbc74e2c9972410ac7d1'
SOURCES = ('lib/libutil/efun.c', 'lib/libc/stdlib/reallocarr.c',
           'common/lib/libc/stdlib/strtoi.c', 'common/lib/libc/stdlib/strtou.c')
HEADERS = ('tools/compat/compat_efun.h', 'tools/compat/namespace.h',
           'common/lib/libc/stdlib/_strtoi.h')
PROBE_SOURCES = tuple('tools/compat/probes/' + name + '.c' for name in
    ('decl-reallocarr', 'decl-strtoi', 'decl-strtou', 'func-esetfunc', 'func-reallocarr'))
_MACROS = {'HAVE_DECL_REALLOCARR', 'HAVE_DECL_STRTOI', 'HAVE_DECL_STRTOU', 'HAVE_ESETFUNC', 'HAVE_REALLOCARR'}
PROVENANCE = (MANIFEST_RELATIVE, 'tools/compat/Makefile', 'tools/compat/efun.mk',
              'tools/compat/README.bootstrap.md', 'tools/compat/probes/efun-control.c', *PROBE_SOURCES)


def compat_manifest(plan, program):
    reference = program['compat']
    _require(reference == dict(path=MANIFEST_RELATIVE, sha256=MANIFEST_SHA256),
             'unsupported common efun source contract')
    root = plan['program']['source']['root']
    value = _json(dict(path=os.path.join(root, MANIFEST_RELATIVE), sha256=MANIFEST_SHA256))
    _require(set(value) == {'format', 'version', 'name', 'origin', 'source_makefile', 'source_fragment',
        'sources', 'headers', 'control', 'probes', 'config_header'} and
        value['format'] == 'emberbsd-native-bootstrap-compat' and type(value['version']) is int and
        value['version'] == 1 and value['name'] == 'efun' and
        value['source_makefile'] == 'tools/compat/Makefile' and value['source_fragment'] == 'tools/compat/efun.mk' and
        value['sources'] == list(SOURCES) and value['headers'] == list(HEADERS) and
        value['control'] == 'tools/compat/probes/efun-control.c' and value['config_header'] == 'nbtool_config.h' and
        isinstance(value['probes'], list) and len(value['probes']) == 5, 'unsupported fixed efun capability probes')
    names, macros = set(), set()
    for row in value['probes']:
        _require(isinstance(row, dict) and set(row) == {'name', 'source', 'kind', 'macro'} and
                 isinstance(row['name'], str) and re.fullmatch('[a-z][a-z0-9-]*', row['name']) and
                 row['name'] not in names and row['source'] == 'tools/compat/probes/' + row['name'] + '.c' and
                 row['kind'] in ('compile', 'link') and row['macro'] in _MACROS and row['macro'] not in macros,
                 'invalid fixed efun probe fields')
        names.add(row['name'])
        macros.add(row['macro'])
    _require({row['source'] for row in value['probes']} == set(PROBE_SOURCES), 'efun probe sources differ')
    return value


def probe_sources(plan, manifest):
    root = plan['program']['source']['root']
    value = compat_manifest(plan, manifest)
    paths = [value['control'], *(row['source'] for row in value['probes'])]
    # These bytes were authenticated by the complete program source inventory.
    return {'compat-' + Path(name).name: Path(root, name).read_bytes() for name in paths}


def probe_steps(plan, manifest, commands, flags, env):
    value, work = compat_manifest(plan, manifest), plan['work_root']
    cc = list(commands['cc'])
    build = [*cc, *flags['cppflags'], *flags['cflags'], '-v']
    link = [*cc, *flags['cflags'], '-v']
    ldflags = [*flags['ldflags'], '-Wl,--threads=1', '-Wl,-v']
    def row(name, phase, argv, expected=0):
        return dict(id=name, phase=phase, cwd=work, env=dict(env), argv=argv, expected_exit=expected)
    c = os.path.join(work, 'compat-efun-control.c')
    obj, exe = os.path.join(work, 'compat-control.o'), os.path.join(work, 'compat-control')
    result = [row('compat-control-compile', 'compile', [*build, '-c', c, '-o', obj]),
              row('compat-control-link', 'link', [*link, obj, *ldflags, '-o', exe]),
              row('compat-control-run', 'run', [exe])]
    for probe in value['probes']:
        source = os.path.join(work, 'compat-' + Path(probe['source']).name)
        output = os.path.join(work, 'compat-' + probe['name'] + ('.o' if probe['kind'] == 'compile' else ''))
        args = ['-c', source] if probe['kind'] == 'compile' else [source, *ldflags]
        result.append(row('compat-' + probe['name'], 'compat-probe-' + probe['kind'],
                          [*build, *args, '-o', output], [0, 1]))
    return result


def probe_outputs(plan, manifest):
    value = compat_manifest(plan, manifest)
    return ['nbtool_config.h', 'compat-control.o', 'compat-control',
            *('compat-' + row['name'] + ('.o' if row['kind'] == 'compile' else '') for row in value['probes'])]


def configuration_bytes(completed, contract):
    """Derive facts only from the actual drained compiler observations."""
    rows = [row for row in completed if row['phase'].startswith('compat-probe-')]
    _require([row['id'] for row in rows] == ['compat-' + row['name'] for row in contract['probes']],
             'efun configuration needs every ordered actual probe result')
    defines = []
    for actual, probe in zip(rows, contract['probes']):
        macro = probe['macro']
        status = actual.get('actual_exit')
        _require(type(status) is int and status in (0, 1) and
                 actual.get('configuration_probe') == dict(macro=macro, available=status == 0),
                 'efun configuration lacks an actual capability observation')
        defines.append('#define ' + macro + ' ' + str(int(status == 0)))
    return ('/* Generated from actual SDK compiler probes; initial efun subset only. */\n'
            '#ifndef EMBERBSD_BOOTSTRAP_EFUN_CONFIG_H\n#define EMBERBSD_BOOTSTRAP_EFUN_CONFIG_H\n'
            '#include <stdio.h>\n#include <stdlib.h>\n#include <inttypes.h>\n#include <stdarg.h>\n'
            '#include "bootstrap_cdefs.h"\n' + '\n'.join(defines) + '\n'
            '#include "compat_efun.h"\n#endif\n').encode('ascii')


def verify_probe_step(step, work, plan, manifest, actual_exit, completed):
    contract = compat_manifest(plan, manifest)
    if not step['phase'].startswith('compat-probe-'):
        # Once the configuration exists, no later compiler or runtime may replace it.
        if any(row['id'] == 'compat-' + contract['probes'][-1]['name'] for row in completed):
            path = os.path.join(work, 'nbtool_config.h')
            expected = configuration_bytes(completed, contract)
            _require(entry(path) is not None and not Path(path).is_symlink() and
                     Path(path).read_bytes() == expected, 'efun configuration changed after its compiler probes')
        return {}
    name = step['id'].removeprefix('compat-')
    matches = [row for row in contract['probes'] if row['name'] == name]
    _require(len(matches) == 1 and type(actual_exit) is int and actual_exit in (0, 1),
             'unexpected efun probe observation')
    kind, macro = matches[0]['kind'], matches[0]['macro']
    _require(step['phase'] == 'compat-probe-' + kind and step['expected_exit'] == [0, 1],
             'efun probe phase differs from its contract')
    result = dict(configuration_probe=dict(macro=macro, available=actual_exit == 0))
    if name == contract['probes'][-1]['name']:
        actual = dict(step, actual_exit=actual_exit, **result)
        path = os.path.join(work, 'nbtool_config.h')
        with open(path, 'xb') as output:
            output.write(configuration_bytes([*completed, actual], contract))
        result['configuration_header'] = dict(path=path, sha256=sha256_file(path))
    return result
