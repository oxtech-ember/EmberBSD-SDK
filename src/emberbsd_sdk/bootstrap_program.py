# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Fixed OS-owned native bootstrap programs. Never accept arbitrary argv."""

import hashlib
import os
from pathlib import Path
import re
import shlex
import socket

from .bootstrap_producer import PRODUCER_FORMAT
from .bootstrap_shell import literal_stdin
from .bootstrap_gnu_program import GNU_PROGRAM_STAGES
from .inventory import sha256_file, snapshot_tree
from .native_toolset import _absolute, _bytes, _envelope, _json, _require, _source
from .paths import canonical_existing, entry

BOOTSTRAP_PROGRAM_API = 1
MANIFEST_RELATIVE = 'tools/grep/bootstrap.json'
MANIFEST_SHA256 = 'cdb8f56fd1107b1cc9ccdbe4cc352599e8575b7984f3c6b6a074426c71e56d3e'
_SOURCES = tuple('usr.bin/grep/' + name + '.c' for name in ('fastgrep', 'file', 'grep', 'queue', 'util'))
_PROVENANCE = ('tools/grep/Makefile', 'tools/Makefile.host', 'usr.bin/grep/Makefile', 'tools/grep/README.md')
PROGRAM_STAGES = ('grep-bootstrap', 'cmp-bootstrap', 'hostname-bootstrap',
                  'basename-bootstrap', 'dirname-bootstrap',
                  'cat-bootstrap', 'head-bootstrap', 'uniq-bootstrap', 'wc-bootstrap', 'tr-bootstrap', 'printf-bootstrap', 'pwd-bootstrap')
CMP_MANIFEST_SHA256 = 'a3267d3387b08780fb1e372f5a742dccd9faa84568292506d3389f35e869ad56'
HOSTNAME_MANIFEST_SHA256 = '32450fa3c2d68e63c13f1d32795f4d3e14cab8b5beac909ee27242cef279750d'
_CMP_SOURCES = tuple('usr.bin/cmp/' + name + '.c' for name in ('cmp', 'misc', 'regular', 'special'))
_HOSTNAME_SOURCES = ('bin/hostname/hostname.c',)
_BOOTSTRAP_CDEFS = 'tools/compat/bootstrap_cdefs.h'
BASENAME_MANIFEST_SHA256 = '28d250d01b132c2fab6619094ebd2a00862baafb3610d759a0d84ad53874bd46'
DIRNAME_MANIFEST_SHA256 = '581b5d3fced84f25c3b185de626ff78a7eb45009c01b916a255438655ba1dd47'
CAT_MANIFEST_SHA256 = 'f9db202d70a9cb0b39a0486f14f137d3fb29b1c00ed75fcffe93aaf86c5faf1a'
HEAD_MANIFEST_SHA256 = '34f117e1bbcc6801597c85c682b5252075b939815527b9c47b2a04e79fbddfe6'
UNIQ_MANIFEST_SHA256 = '88376a81e9c96b455840a0fb636912442cba7325cee89fbbe9df8eb43401236e'
WC_MANIFEST_SHA256 = '38e7672e67b4b423062596252fde87ad4b09ee4977d61f53526214d0e8d6ae88'
TR_MANIFEST_SHA256 = '88114937d4d600fd4c228836657f03c296ffca4badb5dea446cc67768ac6c05b'
PRINTF_MANIFEST_SHA256 = 'f21cac7215af7aeaf2a57e0eacc78dadc67c63d934e14bf1cf6f521a02da4282'
PWD_MANIFEST_SHA256 = '598906e66805438e5386f6f12478d69147e36d020849dec655d7a1826026a6bd'
_NATIVE_DIRECTORIES = {'cmp': 'usr.bin/cmp', 'hostname': 'bin/hostname',
                       'basename': 'usr.bin/basename', 'dirname': 'usr.bin/dirname',
                       'cat': 'bin/cat', 'head': 'usr.bin/head', 'uniq': 'usr.bin/uniq',
                       'wc': 'usr.bin/wc', 'tr': 'usr.bin/tr', 'printf': 'usr.bin/printf', 'pwd': 'bin/pwd'}
_NATIVE_SOURCES = {'cmp': _CMP_SOURCES, 'hostname': _HOSTNAME_SOURCES, 'cat': ('bin/cat/cat.c',),
                   'tr': ('usr.bin/tr/str.c', 'usr.bin/tr/tr.c'), 'pwd': ('bin/pwd/pwd.c',),
                   **{name: ('usr.bin/' + name + '/' + name + '.c',)
                      for name in ('basename', 'dirname', 'head', 'uniq', 'wc', 'printf')}}
_LITERAL_CASE_COUNTS = dict(basename=14, dirname=11, cat=18, head=16, uniq=16, wc=18, tr=18, printf=24, pwd=10)
_RUNTIME_ARGS = {'grep': ('-q', 'needle', 'input.txt'), 'cmp': ('-s', 'same.txt', 'same.txt'),
                 'hostname': ()}


def _literal_cases(manifest, count, *, work_root_output=False):
    """Check finite stream-case structure. The pinned owner supplies semantics."""
    cases = manifest['runtime_cases']
    _require(isinstance(cases, list) and len(cases) == count,
             'unsupported fixed program case count')
    names = set()
    for case in cases:
        output_key = ('stdout_work_root' if work_root_output and isinstance(case, dict) and
                      'stdout_work_root' in case else 'stdout')
        base = {'name', 'invocation', 'args', 'stdin', 'exit', output_key}
        _require(isinstance(case, dict) and
                 set(case) in (base | {'stderr'}, base | {'stderr_nonempty'}),
                 'unsupported literal program case fields')
        _require(isinstance(case['name'], str) and re.fullmatch(r'[a-z][a-z0-9-]*', case['name']) and
                 case['name'] not in names and case['invocation'] == manifest['name'] and
                 isinstance(case['args'], list) and
                 all(isinstance(word, str) and '\0' not in word for word in case['args']) and
                 isinstance(case['stdin'], str) and
                 (isinstance(case['stdout'], str) if output_key == 'stdout' else
                  case['stdout_work_root'] is True and case['exit'] == 0) and
                 type(case['exit']) is int and 0 <= case['exit'] <= 255 and
                 (isinstance(case['stderr'], str) if 'stderr' in case else case['stderr_nonempty'] is True),
                 'invalid literal program case or output path')
        names.add(case['name'])


def _profile(plan):
    stage = plan.get('stage')
    _require(stage in PROGRAM_STAGES, 'unsupported direct-source bootstrap program stage')
    if stage == 'grep-bootstrap':
        return MANIFEST_RELATIVE, MANIFEST_SHA256, _SOURCES, _PROVENANCE
    name = stage.removesuffix('-bootstrap')
    directory = _NATIVE_DIRECTORIES[name]
    digest = {'cmp': CMP_MANIFEST_SHA256, 'hostname': HOSTNAME_MANIFEST_SHA256,
              'basename': BASENAME_MANIFEST_SHA256, 'dirname': DIRNAME_MANIFEST_SHA256,
              'cat': CAT_MANIFEST_SHA256, 'head': HEAD_MANIFEST_SHA256, 'uniq': UNIQ_MANIFEST_SHA256,
              'wc': WC_MANIFEST_SHA256, 'tr': TR_MANIFEST_SHA256,
              'printf': PRINTF_MANIFEST_SHA256, 'pwd': PWD_MANIFEST_SHA256}[name]
    return ('tools/' + name + '/bootstrap.json',
            digest, _NATIVE_SOURCES[name],
            ('tools/' + name + '/Makefile', 'tools/Makefile.host', directory + '/Makefile',
             'tools/' + name + '/README.md'))


def _native_program_contract(manifest, name):
    cmp = name == 'cmp'
    directory = _NATIVE_DIRECTORIES[name]
    _require(set(manifest) == {'format', 'version', 'name', 'origin', 'canonical_host_recipe', 'source_makefile',
        'compiler', 'sources', 'headers', 'include_dirs', 'cppflags', 'cflags', 'link_flags', 'libraries',
        'invocations', 'files', 'runtime_cases'} and manifest['format'] == 'emberbsd-native-bootstrap-program' and
        type(manifest['version']) is int and manifest['version'] == 1 and manifest['name'] == name and
        manifest['canonical_host_recipe'] == 'tools/' + name + '/Makefile' and
        manifest['source_makefile'] == directory + '/Makefile' and
        manifest['compiler'] == dict(family='gcc', major=16, abi='native') and
        manifest['sources'] == list(_NATIVE_SOURCES[name]) and
        manifest['headers'] == ([directory + '/extern.h', _BOOTSTRAP_CDEFS] if name in ('cmp', 'tr') else [_BOOTSTRAP_CDEFS]) and
        manifest['include_dirs'] == ([directory] if name in ('cmp', 'tr') else []) and
        manifest['cppflags'] == ['-include', _BOOTSTRAP_CDEFS] and
        all(manifest[key] == [] for key in ('cflags', 'link_flags', 'libraries')) and
        manifest['invocations'] == [name], 'unsupported direct-source ' + name + ' contract')
    if cmp:
        _require(set(manifest['files']) == {'same.txt', 'different.txt', 'short.txt', 'skip-left.txt', 'skip-right.txt'} and
                 all(isinstance(value, str) for value in manifest['files'].values()) and
                 len(manifest['runtime_cases']) == 18 and
                 all(case['invocation'] == 'cmp' and 'stdout_nonempty' not in case
                     for case in manifest['runtime_cases']), 'unsupported fixed cmp semantic cases')
    elif name in _LITERAL_CASE_COUNTS:
        if name in ('basename', 'dirname', 'tr', 'printf', 'pwd'):
            _require(manifest['files'] == {}, 'this program needs no fixture files')
        else:
            files = manifest['files']
            _require(isinstance(files, dict) and bool(files) and
                     all(isinstance(key, str) and re.fullmatch(r'[a-z][a-z0-9-]*\.txt', key) and
                         key != 'missing.txt' and isinstance(value, str) for key, value in files.items()),
                     'filter programs need bounded immutable fixture files')
        _literal_cases(manifest, _LITERAL_CASE_COUNTS[name], work_root_output=name == 'pwd')
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
    if plan.get('stage') == 'pkg-install-bootstrap':
        from .bootstrap_pkg_install import pkg_install_manifest
        return pkg_install_manifest(plan)
    if plan.get('stage') == 'bsdinstall-bootstrap':
        from .bootstrap_bsdinstall import bsdinstall_manifest
        return bsdinstall_manifest(plan)
    if plan.get('stage') in GNU_PROGRAM_STAGES:
        from .bootstrap_gnu_program import gnu_manifest
        return gnu_manifest(plan)
    source, reference = plan['program']['source'], plan['program']['manifest']
    relative, digest, _, _ = _profile(plan)
    _require(reference == dict(path=os.path.join(source['root'], relative), sha256=digest),
             'unsupported source-owner bootstrap program manifest')
    manifest = _json(reference)
    if plan['stage'] != 'grep-bootstrap':
        _native_program_contract(manifest, plan['stage'].removesuffix('-bootstrap'))
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
    if plan.get('stage') == 'pkg-install-bootstrap':
        from .bootstrap_pkg_install import pkg_install_sources
        return pkg_install_sources(plan)
    return {name: value.encode('utf-8') for name, value in program_manifest(plan)['files'].items()}


def program_outputs(plan):
    if plan.get('stage') == 'pkg-install-bootstrap':
        from .bootstrap_pkg_install import pkg_install_outputs
        return pkg_install_outputs(plan)
    if plan.get('stage') == 'bsdinstall-bootstrap':
        from .bootstrap_bsdinstall import bsdinstall_outputs
        return bsdinstall_outputs(plan)
    if plan.get('stage') in GNU_PROGRAM_STAGES:
        from .bootstrap_gnu_program import gnu_outputs
        return gnu_outputs(plan)
    manifest = program_manifest(plan)
    return [*manifest['files'], *(Path(name).stem + '.o' for name in manifest['sources']), manifest['name'],
            *(case['name'] + suffix for case in manifest['runtime_cases'] for suffix in ('.stdout', '.stderr'))]


def program_steps(plan, descriptor, commands, flags):
    if plan.get('stage') == 'pkg-install-bootstrap':
        from .bootstrap_pkg_install import pkg_install_steps
        return pkg_install_steps(plan, descriptor, commands, flags)
    if plan.get('stage') == 'bsdinstall-bootstrap':
        from .bootstrap_bsdinstall import bsdinstall_steps
        return bsdinstall_steps(plan, descriptor, commands, flags)
    if plan.get('stage') in GNU_PROGRAM_STAGES:
        from .bootstrap_gnu_program import gnu_steps
        return gnu_steps(plan, descriptor, commands, flags)
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
              *(['-Wl,-v'] if descriptor['host']['os'] == 'Darwin' else []),
              '-o', os.path.join(work, manifest['name'])]))
    for case in manifest['runtime_cases']:
        command = shlex.join([os.path.join(work, case['invocation']), *case['args']])
        command = literal_stdin(case['stdin']) + ' | ' + command
        command += ' >' + shlex.quote(os.path.join(work, case['name'] + '.stdout'))
        command += ' 2>' + shlex.quote(os.path.join(work, case['name'] + '.stderr'))
        result.append(dict(id='case-' + case['name'], phase='program-case', cwd=work, env=dict(env),
                           expected_exit=case['exit'], argv=[*commands['sh'], '-c', command]))
    observed = dict(env, DYLD_PRINT_LIBRARIES='1')
    tail = (manifest['runtime_cases'][0]['args'] if manifest['name'] in _LITERAL_CASE_COUNTS
            else _RUNTIME_ARGS[manifest['name']])
    result.append(dict(id='runtime-observation', phase='run', cwd=work, env=observed, expected_exit=0,
                       argv=[os.path.join(work, manifest['name']), *tail]))
    return result


def verify_program_step(step, work, plan):
    if plan.get('stage') == 'pkg-install-bootstrap':
        from .bootstrap_pkg_install import verify_pkg_install_step
        return verify_pkg_install_step(step, work, plan)
    """Check separate actual streams only after the owned command drains."""
    if plan.get('stage') == 'bsdinstall-bootstrap':
        from .bootstrap_bsdinstall import verify_bsdinstall_step
        return verify_bsdinstall_step(step, work, plan)
    if plan.get('stage') in GNU_PROGRAM_STAGES:
        from .bootstrap_gnu_program import verify_gnu_step
        return verify_gnu_step(step, work, plan)
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
        elif name == 'stdout' and program == 'pwd' and case.get('stdout_work_root') is True:
            expected = os.fsencode(canonical_existing(work)) + b'\n'
            _require(data == expected, 'pwd actual stdout differs from the controller work root')
        elif name == 'stderr' and case.get('stderr_nonempty') is True:
            _require(bool(data), program + ' error case did not produce actual stderr')
        else:
            _require(data == case[name].encode('utf-8'), program + ' case actual ' + name + ' differs from its source contract')
        streams[name] = reference
    return streams


def validate_program_plan(plan, descriptor, commands, flags):
    if plan.get('stage') == 'pkg-install-bootstrap':
        from .bootstrap_pkg_install import validate_pkg_install_plan
        return validate_pkg_install_plan(plan, descriptor, commands, flags)
    if plan.get('stage') == 'bsdinstall-bootstrap':
        from .bootstrap_bsdinstall import validate_bsdinstall_plan
        return validate_bsdinstall_plan(plan, descriptor, commands, flags)
    if plan.get('stage') in GNU_PROGRAM_STAGES:
        from .bootstrap_gnu_program import validate_gnu_plan
        return validate_gnu_plan(plan, descriptor, commands, flags)
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
    if manifest['name'] in ('grep', 'cmp', 'cat', 'head', 'uniq', 'wc'):
        _require(entry(os.path.join(plan['work_root'], 'missing.txt')) is None,
                 manifest['name'] + ' missing-file control already exists')
    _require(plan['commands'] == program_steps(plan, descriptor, commands, flags),
             'program command differs from the source-owned finite route')
    return plan
