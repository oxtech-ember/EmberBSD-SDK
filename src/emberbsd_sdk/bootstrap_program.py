# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Fixed OS-owned native bootstrap programs. Never accept arbitrary argv."""

import hashlib
import os
from pathlib import Path
import shlex
import socket

from .bootstrap_producer import PRODUCER_FORMAT
from .inventory import sha256_file, snapshot_tree
from .native_toolset import _absolute, _bytes, _envelope, _json, _require, _source
from .paths import canonical_existing, entry

BOOTSTRAP_PROGRAM_API = 1
MANIFEST_RELATIVE = 'tools/grep/bootstrap.json'
MANIFEST_SHA256 = 'cdb8f56fd1107b1cc9ccdbe4cc352599e8575b7984f3c6b6a074426c71e56d3e'
_SOURCES = tuple('usr.bin/grep/' + name + '.c' for name in ('fastgrep', 'file', 'grep', 'queue', 'util'))
_PROVENANCE = ('tools/grep/Makefile', 'tools/Makefile.host', 'usr.bin/grep/Makefile', 'tools/grep/README.md')
PROGRAM_STAGES = ('grep-bootstrap', 'cmp-bootstrap', 'hostname-bootstrap')
CMP_MANIFEST_SHA256 = 'a3267d3387b08780fb1e372f5a742dccd9faa84568292506d3389f35e869ad56'
HOSTNAME_MANIFEST_SHA256 = '32450fa3c2d68e63c13f1d32795f4d3e14cab8b5beac909ee27242cef279750d'
_CMP_SOURCES = tuple('usr.bin/cmp/' + name + '.c' for name in ('cmp', 'misc', 'regular', 'special'))
_HOSTNAME_SOURCES = ('bin/hostname/hostname.c',)
_BOOTSTRAP_CDEFS = 'tools/compat/bootstrap_cdefs.h'


def _profile(plan):
    stage = plan.get('stage')
    _require(stage in PROGRAM_STAGES, 'unsupported direct-source bootstrap program stage')
    if stage == 'grep-bootstrap':
        return MANIFEST_RELATIVE, MANIFEST_SHA256, _SOURCES, _PROVENANCE
    name = 'cmp' if stage == 'cmp-bootstrap' else 'hostname'
    directory = 'usr.bin/cmp' if name == 'cmp' else 'bin/hostname'
    return ('tools/' + name + '/bootstrap.json',
            CMP_MANIFEST_SHA256 if name == 'cmp' else HOSTNAME_MANIFEST_SHA256,
            _CMP_SOURCES if name == 'cmp' else _HOSTNAME_SOURCES,
            ('tools/' + name + '/Makefile', 'tools/Makefile.host', directory + '/Makefile',
             'tools/' + name + '/README.md'))


def _native_program_contract(manifest, name):
    cmp = name == 'cmp'
    directory = 'usr.bin/cmp' if cmp else 'bin/hostname'
    _require(set(manifest) == {'format', 'version', 'name', 'origin', 'canonical_host_recipe', 'source_makefile',
        'compiler', 'sources', 'headers', 'include_dirs', 'cppflags', 'cflags', 'link_flags', 'libraries',
        'invocations', 'files', 'runtime_cases'} and manifest['format'] == 'emberbsd-native-bootstrap-program' and
        type(manifest['version']) is int and manifest['version'] == 1 and manifest['name'] == name and
        manifest['canonical_host_recipe'] == 'tools/' + name + '/Makefile' and
        manifest['source_makefile'] == directory + '/Makefile' and
        manifest['compiler'] == dict(family='gcc', major=16, abi='native') and
        manifest['sources'] == list(_CMP_SOURCES if cmp else _HOSTNAME_SOURCES) and
        manifest['headers'] == (['usr.bin/cmp/extern.h', _BOOTSTRAP_CDEFS] if cmp else [_BOOTSTRAP_CDEFS]) and
        manifest['include_dirs'] == (['usr.bin/cmp'] if cmp else []) and
        manifest['cppflags'] == ['-include', _BOOTSTRAP_CDEFS] and
        all(manifest[key] == [] for key in ('cflags', 'link_flags', 'libraries')) and
        manifest['invocations'] == [name], 'unsupported direct-source ' + name + ' contract')
    if cmp:
        _require(set(manifest['files']) == {'same.txt', 'different.txt', 'short.txt', 'skip-left.txt', 'skip-right.txt'} and
                 all(isinstance(value, str) for value in manifest['files'].values()) and
                 len(manifest['runtime_cases']) == 18 and
                 all(case['invocation'] == 'cmp' and 'stdout_nonempty' not in case
                     for case in manifest['runtime_cases']), 'unsupported fixed cmp semantic cases')
    else:
        # These are the only accepted tails. No hostname operand can reach a child.
        _require(manifest['files'] == {} and manifest['runtime_cases'] == [
            dict(name='read-hostname', invocation='hostname', args=[], stdin='', exit=0,
                 stdout_nonempty=True, stderr=''),
            dict(name='short-hostname', invocation='hostname', args=['-s'], stdin='', exit=0,
                 stdout_nonempty=True, stderr=''),
            dict(name='invalid-option', invocation='hostname', args=['-z'], stdin='', exit=1,
                 stdout='', stderr_nonempty=True)], 'hostname permits only its fixed read-only cases')


def program_manifest(plan):
    source, reference = plan['program']['source'], plan['program']['manifest']
    relative, digest, _, _ = _profile(plan)
    _require(reference == dict(path=os.path.join(source['root'], relative), sha256=digest),
             'unsupported source-owner bootstrap program manifest')
    manifest = _json(reference)
    if plan['stage'] != 'grep-bootstrap':
        _native_program_contract(manifest, 'cmp' if plan['stage'] == 'cmp-bootstrap' else 'hostname')
        return manifest
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
    return [*manifest['files'], *(Path(name).stem + '.o' for name in manifest['sources']), manifest['name'],
            *(case['name'] + suffix for case in manifest['runtime_cases'] for suffix in ('.stdout', '.stderr'))]


def program_steps(plan, descriptor, commands, flags):
    manifest, work = program_manifest(plan), _absolute(plan['work_root'])
    source = _absolute(plan['program']['source']['root'])
    directories = sorted({os.path.dirname(words[0]) for words in commands.values()})
    _require(all(os.pathsep not in path for path in directories), 'program tool directory cannot be encoded in PATH')
    env = dict(PATH=os.pathsep.join(directories), LC_ALL='C', TZ='UTC', TMPDIR=work)
    driver = [*commands['cc']]
    result, objects = [], []
    cppflags = manifest['cppflags'] if manifest['name'] == 'grep' else ['-include', os.path.join(source, _BOOTSTRAP_CDEFS)]
    includes = [word for directory in manifest['include_dirs'] for word in ('-I', os.path.join(source, directory))]
    for relative in manifest['sources']:
        name = Path(relative).stem
        output = os.path.join(work, name + '.o')
        objects.append(output)
        result.append(dict(id='compile-' + name, phase='compile', cwd=work, env=dict(env), expected_exit=0,
            argv=[*driver, *flags['cppflags'], *flags['cflags'], '-v', *cppflags,
                  *includes, '-c', os.path.join(source, relative), '-o', output]))
    result.append(dict(id='link-' + manifest['name'], phase='link', cwd=work, env=dict(env), expected_exit=0,
        argv=[*driver, *flags['cflags'], '-v', *objects, *flags['ldflags'], '-Wl,--threads=1',
              '-o', os.path.join(work, manifest['name'])]))
    for case in manifest['runtime_cases']:
        command = shlex.join([os.path.join(work, case['invocation']), *case['args']])
        command = "printf '%s' " + shlex.quote(case['stdin']) + ' | ' + command
        command += ' >' + shlex.quote(os.path.join(work, case['name'] + '.stdout'))
        command += ' 2>' + shlex.quote(os.path.join(work, case['name'] + '.stderr'))
        result.append(dict(id='case-' + case['name'], phase='program-case', cwd=work, env=dict(env),
                           expected_exit=case['exit'], argv=[*commands['sh'], '-c', command]))
    observed = dict(env, DYLD_PRINT_LIBRARIES='1')
    tail = ['-q', 'needle', 'input.txt'] if manifest['name'] == 'grep' else (
        ['-s', 'same.txt', 'same.txt'] if manifest['name'] == 'cmp' else [])
    result.append(dict(id='runtime-observation', phase='run', cwd=work, env=observed, expected_exit=0,
                       argv=[os.path.join(work, manifest['name']), *tail]))
    return result


def verify_program_step(step, work, plan):
    """Check separate actual streams only after the owned command drains."""
    if step['phase'] == 'link':
        if plan['stage'] != 'grep-bootstrap':
            return {}
        for name in ('egrep', 'fgrep'):
            os.symlink('grep', os.path.join(work, name))
        return {'invocation_aliases': dict(egrep='grep', fgrep='grep')}
    if step['phase'] != 'program-case':
        return {}
    case = next(row for row in program_manifest(plan)['runtime_cases'] if step['id'] == 'case-' + row['name'])
    program = program_manifest(plan)['name']
    streams = {}
    for name in ('stdout', 'stderr'):
        path = os.path.join(work, case['name'] + '.' + name)
        reference = dict(path=path, sha256=sha256_file(path))
        data = _bytes(reference)
        if name == 'stdout' and program == 'hostname' and case.get('stdout_nonempty') is True:
            current = socket.gethostname()
            expected = current if case['args'] == [] else current.split('.', 1)[0]
            _require(data == os.fsencode(expected) + b'\n',
                     'hostname actual stdout differs from the read-only control-plane hostname')
        elif name == 'stderr' and case.get('stderr_nonempty') is True:
            _require(bool(data), program + ' error case did not produce actual stderr')
        else:
            _require(data == case[name].encode('utf-8'), program + ' case actual ' + name + ' differs from its source contract')
        streams[name] = reference
    return streams


def validate_program_plan(plan, descriptor, commands, flags):
    _envelope(plan, PRODUCER_FORMAT, scope='gcc16-bootstrap')
    _require(set(plan) == {'format', 'version', 'scope', 'stage', 'seed_descriptor', 'host', 'sources',
        'work_root', 'required_roles', 'resources', 'program', 'commands'} and plan['stage'] in PROGRAM_STAGES and
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
    relative, _, sources, provenance = _profile(plan)
    expected = {*sources, *manifest['headers'], relative, *provenance}
    _require(files == set(members) == expected and
             all(row.get('kind') in ('file', 'directory') for row in inventory.values()),
             'bootstrap program needs its exact complete source roster without links')
    for relative, row in members.items():
        raw = _bytes(dict(path=os.path.join(root, relative), sha256=inventory[relative]['sha256']))
        blob = hashlib.sha1(b'blob ' + str(len(raw)).encode('ascii') + b'\0' + raw).hexdigest()
        _require(set(row) == {'git_blob', 'sha256'} and row['git_blob'] == blob and
                 row['sha256'] == inventory[relative]['sha256'], 'bootstrap program Git blob or content differs')
    if manifest['name'] in ('grep', 'cmp'):
        _require(entry(os.path.join(plan['work_root'], 'missing.txt')) is None,
                 manifest['name'] + ' missing-file control already exists')
    _require(plan['commands'] == program_steps(plan, descriptor, commands, flags),
             'program command differs from the source-owned finite route')
    return plan
