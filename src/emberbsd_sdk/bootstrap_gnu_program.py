# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Two fixed initial GNU utility builds. The existing pilot owns every child."""

import hashlib
import os
from pathlib import Path
import re
import shlex
import shutil

from .bootstrap_make import _safe_path
from .inventory import sha256_file, snapshot_tree
from .native_toolset import _absolute, _bytes, _envelope, _json, _require, _source
from .paths import canonical_existing, entry

GNU_PROGRAM_STAGES = ('diffutils-bootstrap', 'findutils-bootstrap')
MANIFESTS = {
    'diffutils-bootstrap': ('diffutils', 'devel/diffutils', '3.12',
        '7da6d3dd3c973e9e9550f88266d8dc3b612c6319ef9b33b1d09828d56cfb0f50',
        ('patch-configure', 'patch-po_Makefile.in.in', 'patch-src_Makefile.in')),
    'findutils-bootstrap': ('findutils', 'sysutils/findutils', '4.11.0',
        'dca85a054a5a4af27925eba8b78abc2f3ec093e5e874c2b70b7321169dba27b7',
        ('patch-aa', 'patch-configure')),
}
_DRIVERS = {'cc', 'cxx', 'cpp', 'as', 'ld', 'ar', 'ranlib', 'nm', 'strip', 'gmake'}


def _patch_sha1(raw):
    # pkgsrc mk/checksum/checksum.awk and distinfo.awk remove full LF records
    # that contain a NetBSD RCS ID before they calculate the patch digest.
    lines = raw.split(b'\n')
    filtered = b''.join(line + (b'\n' if index < len(lines) - 1 else b'')
                        for index, line in enumerate(lines) if b'$NetBSD' not in line)
    return hashlib.sha1(filtered).hexdigest()


def gnu_manifest(plan):
    name, recipe, _, digest, _ = MANIFESTS[plan['stage']]
    root = plan['program']['owner']['root']
    relative = 'profiles/common-build-tools/bootstrap/gnu-' + name + '.json'
    reference = plan['program']['manifest']
    _require(reference == dict(path=os.path.join(root, relative), sha256=digest),
             'unsupported fixed GNU source-owner manifest')
    manifest = _json(reference)
    _require(set(manifest) == {'format', 'version', 'origin', 'compiler', 'build_system', 'configure_args',
        'initial_only', 'name', 'canonical_host_recipe', 'source_makefile', 'required_tool_roles',
        'make_targets', 'output_programs', 'invocations', 'files', 'runtime_cases'} and
        manifest['format'] == 'emberbsd-native-bootstrap-program' and type(manifest['version']) is int and
        manifest['version'] == 1 and manifest['name'] == name and manifest['initial_only'] is True and
        manifest['build_system'] == 'gnu-configure' and manifest['configure_args'] == ['--disable-nls'] and
        manifest['compiler'] == dict(family='gcc', major=16, abi='native') and
        manifest['canonical_host_recipe'] == 'pkgsrc/' + recipe + '/Makefile' and
        len(manifest['runtime_cases']) == (7 if name == 'diffutils' else 15) and
        manifest['output_programs'] == (dict(diff='src/diff') if name == 'diffutils' else
                                        dict(find='find/find', xargs='xargs/xargs')) and
        manifest['invocations'] == list(manifest['output_programs']), 'unsupported fixed GNU program contract')
    for case in manifest['runtime_cases']:
        _require(case['invocation'] in manifest['invocations'] and
                 ('command_role' not in case or case['command_role'] == 'sh') and
                 ('command_role' in case) == ('command_args' in case) and
                 (case['invocation'] != 'xargs' or case.get('command_role') == 'sh'),
                 'GNU semantic children require the exact selected shell; no implicit xargs command')
    return manifest


def _tree(value, descriptor, *, owner):
    _require(set(value) == {'root', 'source', 'inventory', 'source_proof'} and
             value['source'] == descriptor['sources'].get('ports'), 'GNU source Ports pin mismatch')
    root = _safe_path(value['root'])
    _require(canonical_existing(root) == root, 'GNU source root must have real ancestors')
    inventory = _json(value['inventory'])
    _require(snapshot_tree(root) == inventory and
             all(row['kind'] in ('file', 'directory') for row in inventory.values()),
             'GNU source tree changed or contains a link')
    proof = _json(value['source_proof'])
    _require(set(proof) == {'format', 'version', 'source', 'inventory', 'members'} and
             proof['format'] == 'emberbsd-bootstrap-program-source' and type(proof['version']) is int and
             proof['version'] == 1 and proof['source'] == value['source'] and
             proof['inventory'] == value['inventory'], 'GNU source proof differs from its complete tree')
    members = _json(proof['members'])
    expected = {name for name, row in inventory.items() if row['kind'] == 'file'} if owner else set(inventory)
    _require(set(members) == expected, 'GNU source proof omits or adds members')
    for relative, row in members.items():
        path, observed = os.path.join(root, relative), inventory[relative]
        if owner:
            raw = _bytes(dict(path=path, sha256=observed['sha256']))
            blob = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
            _require(row == dict(git_blob=blob, sha256=observed['sha256']),
                     'GNU owner Git blob differs from its source proof')
        else:
            # Release files belong to the archive, not to the Ports Git tree.
            fields = {'mtime_ns'} | ({'sha256'} if observed['kind'] == 'file' else set())
            _require(set(row) == fields and type(row['mtime_ns']) is int and
                     row['mtime_ns'] == os.stat(path).st_mtime_ns and
                     (observed['kind'] != 'file' or row['sha256'] == observed['sha256']),
                     'GNU release content or full mtime proof differs')
    return inventory, members


def _owner(plan, descriptor):
    value = plan['program']
    manifest = gnu_manifest(plan)
    name, recipe, version, _, patch_names = MANIFESTS[plan['stage']]
    root = value['owner']['root']
    inventory, _ = _tree(value['owner'], descriptor, owner=True)
    directory = 'pkgsrc/' + recipe
    expected = {os.path.relpath(value['manifest']['path'], root),
                *(directory + '/' + n for n in ('Makefile', 'DESCR', 'PLIST', 'distinfo')),
                *(directory + '/patches/' + n for n in patch_names)}
    if name == 'diffutils':
        expected.add(directory + '/options.mk')
    _require({n for n, row in inventory.items() if row['kind'] == 'file'} == expected,
             'GNU owner needs its exact canonical recipe, specification and patch roster')
    patches = [dict(path=os.path.join(root, directory, 'patches', n),
                    sha256=inventory[directory + '/patches/' + n]['sha256']) for n in patch_names]
    _require(value['patches'] == patches, 'GNU canonical patches are missing, reordered or replaced')
    distinfo = Path(root, directory, 'distinfo').read_text(encoding='utf-8')
    filename = name + '-' + version + '.tar.xz'
    raw = _bytes(value['archive'], limit=16 * 1024 * 1024)
    for algorithm, constructor in (('SHA256', hashlib.sha256), ('SHA512', hashlib.sha512),
                                    ('BLAKE2s', hashlib.blake2s)):
        matches = re.findall(r'^' + algorithm + r' \(' + re.escape(filename) + r'\) = ([0-9a-f]+)$',
                             distinfo, re.M)
        _require(matches == [constructor(raw).hexdigest()], 'GNU archive differs from canonical ' + algorithm)
    _require(re.findall(r'^Size \(' + re.escape(filename) + r'\) = ([0-9]+) bytes$', distinfo, re.M) ==
             [str(len(raw))], 'GNU archive size differs from canonical distinfo')
    for patch in patches:
        _require(re.findall(r'^SHA1 \(' + re.escape(Path(patch['path']).name) + r'\) = ([0-9a-f]+)$',
                            distinfo, re.M) == [_patch_sha1(_bytes(patch))],
                 'GNU canonical patch differs from distinfo')
    return manifest


def gnu_environment(plan, descriptor, commands, flags):
    work = _safe_path(plan['work_root'])
    result = dict(PATH=os.path.join(work, 'bin'), LC_ALL='C', TZ='UTC', TMPDIR=work,
                  CONFIG_SITE='/dev/null')
    for variable, role in (('CC', 'cc'), ('CXX', 'cxx'), ('CPP', 'cpp'), ('AS', 'as'), ('LD', 'ld'),
            ('AR', 'ar'), ('RANLIB', 'ranlib'), ('NM', 'nm'), ('STRIP', 'strip'),
            ('SHELL', 'sh'), ('CONFIG_SHELL', 'sh'), ('MAKE', 'gmake')):
        result[variable] = shlex.join(commands[role])
    for variable, key in (('CPPFLAGS', 'cppflags'), ('CFLAGS', 'cflags'), ('CXXFLAGS', 'cxxflags')):
        result[variable] = shlex.join(flags[key])
    result['LDFLAGS'] = shlex.join([*flags['ldflags'], '-Wl,--threads=1'])
    return result


def gnu_steps(plan, descriptor, commands, flags):
    manifest, work = gnu_manifest(plan), _safe_path(plan['work_root'])
    source = os.path.join(work, 'source')
    env = gnu_environment(plan, descriptor, commands, flags)
    triplet = descriptor['compiler']['triplet']
    result = [dict(id='configure', phase='gnu-configure', cwd=source, env=dict(env), expected_exit=0,
        argv=[*commands['sh'], os.path.join(source, 'configure'), '--build=' + triplet, '--host=' + triplet,
              '--prefix=' + os.path.join(work, 'unused-prefix'), *manifest['configure_args']])]
    for index, row in enumerate(manifest['make_targets']):
        result.append(dict(id=f'build-{index:02d}', phase='gnu-build', cwd=source, env=dict(env), expected_exit=0,
            argv=[*commands['gmake'], '--no-print-directory', '-j1', '-C',
                  os.path.join(source, row['directory']), *row['targets']]))
    for case in manifest['runtime_cases']:
        argv = [os.path.join(source, manifest['output_programs'][case['invocation']]), *case['args']]
        if 'command_role' in case:
            argv += [*commands['sh'], *case['command_args']]
        command = "printf '%s' " + shlex.quote(case['stdin']) + ' | ' + shlex.join(argv)
        command += ' >' + shlex.quote(os.path.join(work, case['name'] + '.stdout'))
        command += ' 2>' + shlex.quote(os.path.join(work, case['name'] + '.stderr'))
        result.append(dict(id='case-' + case['name'], phase='program-case', cwd=work, env=dict(env),
                           expected_exit=case['exit'], argv=[*commands['sh'], '-c', command]))
    for name, relative in manifest['output_programs'].items():
        result.append(dict(id='runtime-' + name, phase='run', cwd=work,
            env=dict(env, DYLD_PRINT_LIBRARIES='1'), expected_exit=0,
            argv=[os.path.join(source, relative), '--version']))
    return result


def gnu_outputs(plan):
    manifest = gnu_manifest(plan)
    return [*manifest['files'], *('source/' + value for value in manifest['output_programs'].values()),
            *(case['name'] + suffix for case in manifest['runtime_cases'] for suffix in ('.stdout', '.stderr'))]


def prepare_gnu_work(plan, descriptor, commands):
    """Copy only admitted source data. Never apply patches or execute a source hook."""
    manifest, work = gnu_manifest(plan), plan['work_root']
    source = plan['program']['source']
    destination = os.path.join(work, 'source')
    shutil.copytree(source['root'], destination, copy_function=shutil.copy2, symlinks=False)
    inventory, members = _json(source['inventory']), _json(_json(source['source_proof'])['members'])
    for relative, row in inventory.items():
        mode = row['mode'] | (0o700 if row['kind'] == 'directory' else 0o200)
        os.chmod(os.path.join(destination, relative), mode)
    os.chmod(destination, os.stat(destination).st_mode | 0o700)
    expected = {n: dict(row, mode=row['mode'] | (0o700 if row['kind'] == 'directory' else 0o200))
                for n, row in inventory.items()}
    _require(snapshot_tree(destination) == expected and all(
        os.stat(os.path.join(destination, n)).st_mtime_ns == row['mtime_ns'] for n, row in members.items()),
        'staged GNU source differs from admitted bytes, modes or mtimes')
    os.mkdir(os.path.join(work, 'bin'))
    for role in sorted(set(manifest['required_tool_roles']) - _DRIVERS):
        os.symlink(commands[role][0], os.path.join(work, 'bin', role))
    for name in manifest['files']:
        Path(work, name).parent.mkdir(parents=True, exist_ok=True)


def verify_gnu_step(step, work, plan):
    manifest = gnu_manifest(plan)
    if step['phase'] == 'gnu-build':
        from .bootstrap_pilot import _native_image
        index = int(step['id'].removeprefix('build-'))
        row = manifest['make_targets'][index]
        outputs = {}
        for name, relative in manifest['output_programs'].items():
            if row['directory'] == str(Path(relative).parent) and Path(relative).name in row['targets']:
                path = os.path.join(work, 'source', relative)
                _native_image(path, plan['host'])
                outputs[name] = dict(path=path, sha256=sha256_file(path))
        return dict(program_outputs=outputs) if outputs else {}
    if step['phase'] != 'program-case':
        return {}
    case = next(row for row in manifest['runtime_cases'] if step['id'] == 'case-' + row['name'])
    streams = {}
    for name in ('stdout', 'stderr'):
        path = os.path.join(work, case['name'] + '.' + name)
        reference = dict(path=path, sha256=sha256_file(path))
        data = _bytes(reference)
        _require(bool(data) if name == 'stderr' and case.get('stderr_nonempty') is True else
                 data == case[name].encode('utf-8'), 'GNU semantic actual ' + name + ' differs from source contract')
        streams[name] = reference
    return streams


def validate_gnu_plan(plan, descriptor, commands, flags):
    from .bootstrap_pilot import _validate_resources
    from .bootstrap_producer import PRODUCER_FORMAT
    _envelope(plan, PRODUCER_FORMAT, scope='gcc16-bootstrap')
    _require(set(plan) == {'format', 'version', 'scope', 'stage', 'seed_descriptor', 'host', 'sources',
        'work_root', 'required_roles', 'resources', 'program', 'commands'} and plan['stage'] in GNU_PROGRAM_STAGES and
        plan['host'] == descriptor['host'] and plan['sources'] == descriptor['sources'] and
        descriptor['host']['os'] == 'Darwin', 'unsupported fixed initial GNU program plan')
    for source in plan['sources'].values():
        _source(source)
    _validate_resources(plan['resources'], metadata=True)
    _require(plan['resources']['cpu_jobs'] == 1, 'initial GNU programs require serial make and one outer link lease')
    value = plan['program']
    _require(set(value) == {'source', 'owner', 'manifest', 'archive', 'patches'}, 'unsupported fixed GNU input fields')
    manifest = _owner(plan, descriptor)
    inventory, _ = _tree(value['source'], descriptor, owner=False)
    sentinels = {'configure', 'Makefile.in', 'COPYING', manifest['source_makefile']}
    _require(all(inventory.get(name, {}).get('kind') == 'file' for name in sentinels),
             'GNU release omits a required canonical source file')
    required = set(manifest['required_tool_roles']) | set(descriptor['driver_components'].values())
    _require(isinstance(plan['required_roles'], list) and len(plan['required_roles']) == len(set(plan['required_roles']))
             and required <= set(plan['required_roles']) <= set(commands), 'GNU input roles are incomplete')
    for role in set(manifest['required_tool_roles']) - _DRIVERS:
        _require(len(commands[role]) == 1 and descriptor['tools'].get(role, {}).get('kind') == 'native',
                 'GNU raw utility must retain its exact accepted native spelling: ' + role)
    for role in ('cmp', 'hostname') + (('diff',) if manifest['name'] == 'findutils' else ()):
        provider = descriptor['tools'][role]['provider']
        _require(descriptor['history'][provider]['kind'] == 'source-build',
                 'GNU prerequisites must be accepted source builds: ' + role)
    _require(entry(os.path.join(plan['work_root'], 'missing.txt')) is None, 'GNU missing-file control already exists')
    _require(plan['commands'] == gnu_steps(plan, descriptor, commands, flags),
             'GNU command differs from the fixed source-owned configure/make route')
    return plan
