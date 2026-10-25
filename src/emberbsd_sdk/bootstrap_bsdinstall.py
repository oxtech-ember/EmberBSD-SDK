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
from .inventory import sha256_file, snapshot_tree
from .native_toolset import _absolute, _bytes, _envelope, _json, _native_image, _require
from .paths import canonical_existing, entry

STAGE = 'bsdinstall-bootstrap'
MANIFEST_RELATIVE = 'profiles/common-build-tools/bootstrap/bsdinstall.json'
MANIFEST_SHA256 = 'c00c20a8ae0de0c0b98a7bfa6bd1ed3c373bc2467f274a469dcf426f2aa5bd60'
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


def _compat(value, descriptor):
    """Check one retained component, never the failed SDK's other outputs."""
    _require(set(value) == {'root', 'inventory', 'producer_result', 'bootstrap_plan', 'component_receipt'},
             'unsupported BSD install compatibility input fields')
    root = _absolute(value['root'])
    inventory = _json(value['inventory'])
    _require(canonical_existing(root) == root and snapshot_tree(root) == inventory and
             entry(root).st_mode & 0o222 == 0 and
             all(row['kind'] in ('file', 'directory') and row['mode'] & 0o222 == 0
                 for row in inventory.values()), 'initial libnbcompat snapshot changed or is not read-only')
    files = {name for name, row in inventory.items() if row['kind'] == 'file'}
    _require({'include/nbcompat.h', 'include/nbcompat/config.h', 'lib/libnbcompat.a'} <= files and
             all(name.startswith('include/') or name == 'lib/libnbcompat.a' for name in files),
             'initial libnbcompat needs only its complete headers and static library')
    _require(_bytes(dict(path=os.path.join(root, 'lib/libnbcompat.a'),
                         sha256=inventory['lib/libnbcompat.a']['sha256'])).startswith(b'!<arch>\n'),
             'initial libnbcompat input is not a static archive')
    receipt = _json(value['component_receipt'])
    _require(set(receipt) == {'status', 'scope', 'recipe', 'root', 'inventory', 'producer_result',
        'bootstrap_plan', 'preparation', 'independent_review', 'ordinary_provider_qualified', 'initial_only'} and
        receipt['status'] == 'ACCEPTED_INITIAL_LIBNBCOMPAT_INPUT' and receipt['scope'] == 'gcc16-bootstrap' and
        receipt['recipe'] == 'pkgtools/libnbcompat' and receipt['ordinary_provider_qualified'] is False and
        receipt['initial_only'] is True and all(receipt[key] == value[key] for key in
            ('root', 'inventory', 'producer_result', 'bootstrap_plan')),
        'initial libnbcompat requires its separate accepted component input')
    review = _json(receipt['independent_review'])
    _require(review.get('status') == 'REVIEWED_INITIAL_LIBNBCOMPAT_INPUT_CANDIDATE' and
             review.get('findings') == [] and review.get('all_review_leases_closed') is True and
             review.get('preparation') == receipt['preparation'] and
             review.get('ordinary_provider_qualified') is False,
             'initial libnbcompat independent review differs from its accepted preparation')
    preparation, result, plan = (_json(receipt['preparation']), _json(value['producer_result']),
                                  _json(value['bootstrap_plan']))
    _envelope(result, 'emberbsd-native-gcc16-producer-result', scope='gcc16-bootstrap')
    _envelope(plan, 'emberbsd-native-gcc16-bootstrap-plan', scope='gcc16-bootstrap')
    historical_ref = preparation['source_seed_descriptor']
    historical = _json(historical_ref)
    roles = {'cc', *descriptor['driver_components'].values()}
    _require(historical_ref == result['seed_descriptor'] == plan['seed_descriptor'] and
             all(historical[key] == descriptor[key] for key in
                 ('host', 'compiler', 'host_abi', 'abi_flags', 'driver_components', 'driver_prefix')) and
             historical.get('driver_binding') == descriptor.get('driver_binding') and
             all(historical['tools'][role] == descriptor['tools'][role] for role in roles),
             'initial libnbcompat compiler or host ABI differs from its actual producer')
    _require(result['status'] in ('FAILED', 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW') and
             result['stage'] == 'full-recipe-bootstrap' and
             result['ordinary_provider_qualified'] is False and result['bootstrap_plan'] == value['bootstrap_plan'] and
             plan['host'] == descriptor['host'] and
             all(result.get(key) is True for key in ('all_commands_drained',
                 'inputs_revalidated_after_drain', 'all_leases_closed')),
             'libnbcompat needs actual drained component evidence, not a successful SDK claim')
    _require(len(plan['commands']) >= 2 and len(result['commands']) >= 2,
             'libnbcompat actual component commands are missing')
    for index, phase in enumerate(('build', 'install')):
        expected, actual = plan['commands'][index], result['commands'][index]
        _require(expected['id'] == 'r000-' + phase and expected['phase'] == phase and
                 expected['recipe'] == 'pkgtools/libnbcompat' and
                 set(actual) == set(expected) | {'actual_exit', 'log'} and
                 all(actual[key] == val for key, val in expected.items()) and
                 type(actual['actual_exit']) is int and actual['actual_exit'] == 0,
                 'libnbcompat first actual build/install differs or failed')
    _require(preparation['status'] == 'PREPARED_INITIAL_LIBRARY_INPUT_ONLY' and
             preparation['native_or_ordinary_qualification'] is False and
             preparation['component_recipe'] == 'pkgtools/libnbcompat' and
             preparation['root'] == root and preparation['inventory'] == value['inventory'] and
             preparation['source_producer_result'] == value['producer_result'] and
             preparation['source_bootstrap_plan'] == value['bootstrap_plan'] and
             preparation['actual_component_commands'] == result['commands'][:2],
             'initial component preparation differs from its actual producer')
    copies = preparation['copied_members']
    by_name = {os.path.relpath(row['retained']['path'], root): row for row in copies}
    _require(len(by_name) == len(copies) and set(by_name) == files,
             'initial libnbcompat copy proof omits or adds files')
    for name, row in by_name.items():
        digest = inventory[name]['sha256']
        _require(row['retained'] == dict(path=os.path.join(root, name), sha256=digest) and
                 row['original'] == dict(path=os.path.join(plan['output_bundle_root'], name), sha256=digest),
                 'initial libnbcompat retained bytes differ from the completed component')


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
              *flags['ldflags'], '-Wl,--threads=1', '-o', os.path.join(work, 'bsdinstall')]))
    for case in manifest['runtime_cases']:
        args = [os.path.join(work, 'bsdinstall') if word == '@PROGRAM@' else word for word in case['args']]
        command = "umask 022; printf '%s' " + shlex.quote(case['stdin']) + ' | '
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
