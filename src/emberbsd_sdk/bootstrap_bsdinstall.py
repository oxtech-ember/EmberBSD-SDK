# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""One fixed initial BSD installer. The existing program runner owns children."""

import json
import os
from pathlib import Path
import shlex
import stat

from .bootstrap_gnu_program import _tree
from .bootstrap_libnbcompat import validate_input as _compat
from .bootstrap_shell import literal_stdin
from .inventory import sha256_file, snapshot_tree
from .native_toolset import _absolute, _bytes, _envelope, _json, _native_image, _require
from .paths import canonical_existing, entry

STAGE = 'bsdinstall-bootstrap'
MANIFEST_RELATIVE = 'profiles/common-build-tools/bootstrap/bsdinstall.json'
MANIFEST_SHA256 = '3fbcfcd31129ead586d82e4a22e05c0c8512139d4b0554cbfb660e9eab2cb2d9'
_PATCH = 'profiles/common-build-tools/patches/pkgsrc-bsdinstall-native-shell.patch'
_DIRECTORY = 'sysutils/bsdinstall'
_SOURCES = tuple(_DIRECTORY + '/files/' + name + '.c' for name in ('bsdinstall', 'setmode'))
_SOURCE_FILES = {_DIRECTORY + '/' + name for name in
                 ('Makefile', 'DESCR', 'PLIST', 'files/Makefile', 'files/bsdinstall.c',
                  'files/setmode.c', 'files/bsdinstall.1')}


def bsdinstall_manifest(plan):
    value = plan['program']
    _require(value['manifest'] == dict(path=os.path.join(value['owner']['root'], MANIFEST_RELATIVE),
                                      sha256=MANIFEST_SHA256), 'unsupported fixed BSD install manifest')
    manifest = _json(value['manifest'])
    _require(manifest['name'] == 'bsdinstall' and manifest['initial_only'] is True and
             manifest['build_system'] == 'direct' and manifest['sources'] == list(_SOURCES) and
             manifest['compiler'] == dict(family='gcc', major=16, abi='native') and
             manifest['required_tool_roles'] == ['cc', 'sh', 'strip'] and
             manifest['compat'] == dict(recipe='pkgtools/libnbcompat', include_dir='include',
                                        static_library='lib/libnbcompat.a') and
             manifest['selected_defines'] == dict(EMBERBSD_BSDINSTALL_SHELL='sh', TARGET_STRIP='strip') and
             len(manifest['runtime_cases']) == 19, 'unsupported fixed initial BSD install contract')
    return manifest


def bsdinstall_steps(plan, descriptor, commands, flags):
    manifest, work = bsdinstall_manifest(plan), _absolute(plan['work_root'])
    source, compat = plan['program']['source']['root'], plan['program']['compat']['root']
    for role in ('sh', 'strip'):
        _require(len(commands[role]) == 1, 'BSD install compiled child selector must be one exact executable: ' + role)
        _absolute(commands[role][0])
    _require(shlex.quote(commands['strip'][0]) == commands['strip'][0],
             'BSD install strip selector cannot require shell quoting')
    directories = sorted({os.path.dirname(words[0]) for words in commands.values()})
    _require(all(os.pathsep not in path for path in directories), 'BSD install tool directory cannot be encoded in PATH')
    env = dict(PATH=os.pathsep.join(directories), LC_ALL='C', TZ='UTC', TMPDIR=work)
    defines = ['-D' + name + '=' + json.dumps(commands[role][0])
               for name, role in manifest['selected_defines'].items()]
    result, objects = [], []
    for relative in manifest['sources']:
        name, output = Path(relative).stem, os.path.join(work, Path(relative).stem + '.o')
        objects.append(output)
        result.append(dict(id='compile-' + name, phase='compile', cwd=work, env=dict(env), expected_exit=0,
            argv=[*commands['cc'], *flags['cppflags'], *flags['cflags'], '-v', *manifest['cppflags'], *defines,
                  '-I', os.path.join(compat, 'include'), '-c', os.path.join(source, relative), '-o', output]))
    result.append(dict(id='link-bsdinstall', phase='link', cwd=work, env=dict(env), expected_exit=0,
        argv=[*commands['cc'], *flags['cflags'], '-v', *objects, os.path.join(compat, 'lib/libnbcompat.a'),
              *flags['ldflags'], '-Wl,--threads=1',
              *(['-Wl,-v'] if descriptor['host']['os'] == 'Darwin' else []),
              '-o', os.path.join(work, 'bsdinstall')]))
    for case in manifest['runtime_cases']:
        args = [os.path.join(work, 'bsdinstall') if word == '@PROGRAM@' else word for word in case['args']]
        command = 'umask 022; ' + literal_stdin(case['stdin']) + ' | '
        command += shlex.join([os.path.join(work, 'bsdinstall'), *args])
        command += ' >' + shlex.quote(os.path.join(work, case['name'] + '.stdout'))
        command += ' 2>' + shlex.quote(os.path.join(work, case['name'] + '.stderr'))
        result.append(dict(id='case-' + case['name'], phase='program-case', cwd=work, env=dict(env),
                           expected_exit=case['exit'], argv=[*commands['sh'], '-c', command]))
    result.append(dict(id='runtime-observation', phase='run', cwd=work,
        env=dict(env, DYLD_PRINT_LIBRARIES='1'), expected_exit=0,
        argv=[os.path.join(work, 'bsdinstall'), '-U', '-c', 'input.txt', 'runtime-copy.txt']))
    return result


def bsdinstall_outputs(plan):
    manifest = bsdinstall_manifest(plan)
    return list(dict.fromkeys([*manifest['files'], 'bsdinstall.o', 'setmode.o', 'bsdinstall', 'runtime-copy.txt',
        *(case['name'] + suffix for case in manifest['runtime_cases'] for suffix in ('.stdout', '.stderr')),
        *(row['path'] for case in manifest['runtime_cases'] for row in case['checks']
          if row['kind'] in ('file', 'native_image'))]))


def prepare_bsdinstall_work(plan):
    # The directory failure controls compare the original fixture's exact mode.
    os.chmod(os.path.join(plan['work_root'], 'input.txt'), 0o644)


def verify_bsdinstall_step(step, work, plan):
    if step['phase'] != 'program-case':
        return {}
    case = next(row for row in bsdinstall_manifest(plan)['runtime_cases'] if step['id'] == 'case-' + row['name'])
    observed = {}
    for stream in ('stdout', 'stderr'):
        path = os.path.join(work, case['name'] + '.' + stream)
        reference = dict(path=path, sha256=sha256_file(path))
        raw = _bytes(reference)
        _require(bool(raw) if case.get(stream + '_nonempty') is True else raw == case[stream].encode(),
                 'BSD install actual ' + stream + ' differs from its fixed case')
        observed[stream] = reference
    checks = []
    for row in case['checks']:
        path = os.path.join(work, row['path'])
        info = entry(path)
        if row['kind'] == 'absent':
            _require(info is None, 'BSD install failure case created an output')
        else:
            _require(info is not None and stat.S_IMODE(info.st_mode) == row['mode'] and
                     (stat.S_ISDIR(info.st_mode) if row['kind'] == 'directory' else stat.S_ISREG(info.st_mode)),
                     'BSD install case output type or mode differs')
            if row['kind'] == 'native_image':
                _native_image(path, plan['host'])
            if row['kind'] == 'file':
                _require(Path(path).read_bytes() == row['content'].encode(), 'BSD install copied content differs')
        checks.append(dict(row, **({'sha256': sha256_file(path)} if info and stat.S_ISREG(info.st_mode) else {})))
    return dict(observed, checks=checks)


def validate_bsdinstall_plan(plan, descriptor, commands, flags):
    from .bootstrap_pilot import _validate_resources
    from .bootstrap_producer import PRODUCER_FORMAT
    _envelope(plan, PRODUCER_FORMAT, scope='gcc16-bootstrap')
    _require(set(plan) == {'format', 'version', 'scope', 'stage', 'seed_descriptor', 'host', 'sources',
        'work_root', 'required_roles', 'resources', 'program', 'commands'} and plan['stage'] == STAGE and
        plan['host'] == descriptor['host'] and plan['sources'] == descriptor['sources'] and
        descriptor['host']['os'] == 'Darwin' and descriptor['host']['cpu'] in ('arm64', 'aarch64'),
        'unsupported fixed BSD install bootstrap plan')
    _validate_resources(plan['resources'])
    _require(set(plan['program']) == {'source', 'owner', 'manifest', 'compat'}, 'unsupported BSD install inputs')
    manifest = bsdinstall_manifest(plan)
    owner, _ = _tree(plan['program']['owner'], descriptor, owner=True)
    source, _ = _tree(plan['program']['source'], descriptor, owner=False)
    _require({n for n, row in owner.items() if row['kind'] == 'file'} ==
             {MANIFEST_RELATIVE, _PATCH, 'pkgsrc/export.conf'} and
             {n for n, row in source.items() if row['kind'] == 'file'} == _SOURCE_FILES and
             entry(plan['program']['source']['root']).st_mode & 0o222 == 0 and
             all(row['mode'] & 0o222 == 0 for row in source.values()),
             'BSD install needs its complete fixed owner and read-only derived source roster')
    required = set(manifest['required_tool_roles']) | set(descriptor['driver_components'].values())
    _require(isinstance(plan['required_roles'], list) and len(plan['required_roles']) == len(set(plan['required_roles'])) and
             required <= set(plan['required_roles']) <= set(commands), 'BSD install input roles are incomplete')
    _compat(plan['program']['compat'], descriptor)
    _require(entry(os.path.join(plan['work_root'], 'missing.txt')) is None, 'BSD install missing-file control exists')
    _require(plan['commands'] == bsdinstall_steps(plan, descriptor, commands, flags),
             'BSD install command differs from its fixed selected route')
    return plan
