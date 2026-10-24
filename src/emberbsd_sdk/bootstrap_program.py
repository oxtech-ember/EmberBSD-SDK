# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""One OS-owned direct grep bootstrap profile. Never accept arbitrary argv."""

import hashlib
import os
from pathlib import Path
import shlex

from .bootstrap_producer import PRODUCER_FORMAT
from .inventory import sha256_file, snapshot_tree
from .native_toolset import _absolute, _bytes, _envelope, _json, _require, _source
from .paths import canonical_existing, entry

BOOTSTRAP_PROGRAM_API = 1
MANIFEST_RELATIVE = 'tools/grep/bootstrap.json'
MANIFEST_SHA256 = 'cdb8f56fd1107b1cc9ccdbe4cc352599e8575b7984f3c6b6a074426c71e56d3e'
_SOURCES = tuple('usr.bin/grep/' + name + '.c' for name in ('fastgrep', 'file', 'grep', 'queue', 'util'))
_PROVENANCE = ('tools/grep/Makefile', 'tools/Makefile.host', 'usr.bin/grep/Makefile', 'tools/grep/README.md')


def program_manifest(plan):
    source, reference = plan['program']['source'], plan['program']['manifest']
    _require(reference == dict(path=os.path.join(source['root'], MANIFEST_RELATIVE), sha256=MANIFEST_SHA256),
             'unsupported source-owner bootstrap program manifest')
    manifest = _json(reference)
    _require(set(manifest) == {'format', 'version', 'name', 'origin', 'canonical_host_recipe', 'source_makefile',
        'compiler', 'sources', 'headers', 'include_dirs', 'cppflags', 'cflags', 'link_flags', 'libraries',
        'invocations', 'files', 'runtime_cases'} and manifest['format'] == 'emberbsd-native-bootstrap-program' and
        type(manifest['version']) is int and manifest['version'] == 1 and manifest['name'] == 'grep' and
        manifest['canonical_host_recipe'] == 'tools/grep/Makefile' and
        manifest['source_makefile'] == 'usr.bin/grep/Makefile' and
        manifest['compiler'] == dict(family='gcc', major=16, abi='native') and
        manifest['sources'] == list(_SOURCES) and manifest['headers'] == ['usr.bin/grep/grep.h'] and
        manifest['include_dirs'] == ['usr.bin/grep'] and
        manifest['cppflags'] == ['-DWITHOUT_NLS', '-DWITHOUT_BZ2', '-DWITHOUT_GZIP'] and
        all(manifest[name] == [] for name in ('cflags', 'link_flags', 'libraries')) and
        manifest['invocations'] == ['grep', 'egrep', 'fgrep'] and len(manifest['runtime_cases']) == 14 and
        set(manifest['files']) == {'input.txt', 'patterns.txt'}, 'unsupported direct-source grep contract')
    return manifest


def program_sources(plan):
    return {name: value.encode('utf-8') for name, value in program_manifest(plan)['files'].items()}


def program_outputs(plan):
    manifest = program_manifest(plan)
    return [*manifest['files'], *(Path(name).stem + '.o' for name in _SOURCES), 'grep',
            *(case['name'] + suffix for case in manifest['runtime_cases'] for suffix in ('.stdout', '.stderr'))]


def program_steps(plan, descriptor, commands, flags):
    manifest, work = program_manifest(plan), _absolute(plan['work_root'])
    source = _absolute(plan['program']['source']['root'])
    directories = sorted({os.path.dirname(words[0]) for words in commands.values()})
    _require(all(os.pathsep not in path for path in directories), 'program tool directory cannot be encoded in PATH')
    env = dict(PATH=os.pathsep.join(directories), LC_ALL='C', TZ='UTC', TMPDIR=work)
    driver = [*commands['cc']]
    result, objects = [], []
    for relative in _SOURCES:
        name = Path(relative).stem
        output = os.path.join(work, name + '.o')
        objects.append(output)
        result.append(dict(id='compile-' + name, phase='compile', cwd=work, env=dict(env), expected_exit=0,
            argv=[*driver, *flags['cppflags'], *flags['cflags'], '-v', *manifest['cppflags'],
                  '-I', os.path.join(source, 'usr.bin/grep'), '-c', os.path.join(source, relative), '-o', output]))
    result.append(dict(id='link-grep', phase='link', cwd=work, env=dict(env), expected_exit=0,
        argv=[*driver, *flags['cflags'], '-v', *objects, *flags['ldflags'], '-Wl,--threads=1',
              '-o', os.path.join(work, 'grep')]))
    for case in manifest['runtime_cases']:
        command = shlex.join([os.path.join(work, case['invocation']), *case['args']])
        command = "printf '%s' " + shlex.quote(case['stdin']) + ' | ' + command
        command += ' >' + shlex.quote(os.path.join(work, case['name'] + '.stdout'))
        command += ' 2>' + shlex.quote(os.path.join(work, case['name'] + '.stderr'))
        result.append(dict(id='case-' + case['name'], phase='program-case', cwd=work, env=dict(env),
                           expected_exit=case['exit'], argv=[*commands['sh'], '-c', command]))
    observed = dict(env, DYLD_PRINT_LIBRARIES='1')
    result.append(dict(id='runtime-observation', phase='run', cwd=work, env=observed, expected_exit=0,
                       argv=[os.path.join(work, 'grep'), '-q', 'needle', 'input.txt']))
    return result


def verify_program_step(step, work, plan):
    """Check separate actual streams only after the owned command drains."""
    if step['phase'] == 'link':
        for name in ('egrep', 'fgrep'):
            os.symlink('grep', os.path.join(work, name))
        return {'invocation_aliases': dict(egrep='grep', fgrep='grep')}
    if step['phase'] != 'program-case':
        return {}
    case = next(row for row in program_manifest(plan)['runtime_cases'] if step['id'] == 'case-' + row['name'])
    streams = {}
    for name in ('stdout', 'stderr'):
        path = os.path.join(work, case['name'] + '.' + name)
        reference = dict(path=path, sha256=sha256_file(path))
        data = _bytes(reference)
        if name == 'stderr' and case.get('stderr_nonempty') is True:
            _require(bool(data), 'grep error case did not produce actual stderr')
        else:
            _require(data == case[name].encode('utf-8'), 'grep case actual ' + name + ' differs from its source contract')
        streams[name] = reference
    return streams


def validate_program_plan(plan, descriptor, commands, flags):
    _envelope(plan, PRODUCER_FORMAT, scope='gcc16-bootstrap')
    _require(set(plan) == {'format', 'version', 'scope', 'stage', 'seed_descriptor', 'host', 'sources',
        'work_root', 'required_roles', 'resources', 'program', 'commands'} and plan['stage'] == 'grep-bootstrap' and
        plan['host'] == descriptor['host'] and plan['sources'] == descriptor['sources'] and
        descriptor['host']['os'] == 'Darwin', 'unsupported direct-source bootstrap program stage')
    for source in plan['sources'].values():
        _source(source)
    from .bootstrap_pilot import _validate_resources
    _validate_resources(plan['resources'])
    required = {'cc', 'sh', *descriptor['driver_components'].values()}
    _require(isinstance(plan['required_roles'], list) and
             len(plan['required_roles']) == len(set(plan['required_roles'])) and
             required <= set(plan['required_roles']) <= set(commands), 'program input roles are incomplete')
    value = plan['program']
    _require(set(value) == {'source', 'manifest'}, 'unsupported program input fields')
    source = value['source']
    _require(set(source) == {'root', 'source', 'inventory', 'source_proof'} and
             source['source'] == descriptor['sources'].get('os'), 'bootstrap program OS source pin mismatch')
    _source(source['source'])
    root = _absolute(source['root'])
    inventory = _json(source['inventory'])
    _require(canonical_existing(root) == root and snapshot_tree(root) == inventory, 'bootstrap program source changed')
    manifest = program_manifest(plan)
    proof = _json(source['source_proof'])
    _require(set(proof) == {'format', 'version', 'source', 'inventory', 'members'} and
        proof['format'] == 'emberbsd-bootstrap-program-source' and type(proof['version']) is int and
        proof['version'] == 1 and proof['source'] == source['source'] and proof['inventory'] == source['inventory'],
        'bootstrap program source proof mismatch')
    members = _json(proof['members'])
    files = {name for name, row in inventory.items() if row.get('kind') == 'file'}
    expected = {*_SOURCES, 'usr.bin/grep/grep.h', MANIFEST_RELATIVE, *_PROVENANCE}
    _require(files == set(members) == expected and
             all(row.get('kind') in ('file', 'directory') for row in inventory.values()),
             'bootstrap program needs its exact complete source roster without links')
    for relative, row in members.items():
        raw = _bytes(dict(path=os.path.join(root, relative), sha256=inventory[relative]['sha256']))
        blob = hashlib.sha1(b'blob ' + str(len(raw)).encode('ascii') + b'\0' + raw).hexdigest()
        _require(set(row) == {'git_blob', 'sha256'} and row['git_blob'] == blob and
                 row['sha256'] == inventory[relative]['sha256'], 'bootstrap program Git blob or content differs')
    _require(entry(os.path.join(plan['work_root'], 'missing.txt')) is None,
             'grep missing-file control already exists')
    _require(plan['commands'] == program_steps(plan, descriptor, commands, flags),
             'program command differs from the source-owned finite route')
    return plan
