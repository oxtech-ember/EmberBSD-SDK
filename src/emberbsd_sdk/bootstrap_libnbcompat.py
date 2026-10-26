# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Shared initial Ports libnbcompat input. OS component archives are distinct."""
import os
from pathlib import Path
import re
import shlex
import stat
from .bootstrap_gnu_program import _tree
from .bootstrap_make import _safe_path
from .inventory import snapshot_tree, sha256_file
from .jsonio import write_json
from .native_toolset import _absolute, _bytes, _envelope, _json, _native_image, _require
from .paths import canonical_existing, entry

STAGE = 'ports-libnbcompat-bootstrap'
MANIFEST_RELATIVE = 'profiles/common-build-tools/bootstrap/libnbcompat.json'
MANIFEST_SHA256 = '340ca308f4d7ccf4618fa7856249e520464ff1e68923bf2689eb353655ef18a0'
PATCH = 'profiles/common-build-tools/patches/pkgsrc-libnbcompat-sdk-diagnostics.patch'
HELPER = 'profiles/common-build-tools/files/emberbsd-configure-diagnostics.sh'
RECIPE = 'pkgtools/libnbcompat'
SOURCE_DIRECTORY = RECIPE + '/files'
ROLES = set('cc cpp as ld ar ranlib make sh awk basename cat chmod cp diff dirname expr grep '
            'hostname ln ls mkdir mv printf rm rmdir sed sort tr uname'.split())
_DRIVERS = {'cc', 'cpp', 'as', 'ld', 'ar', 'ranlib', 'make'}
SEED_FIELDS = ('host', 'compiler', 'sources', 'providers', 'host_abi', 'abi_flags', 'history',
               'dynamic_dependencies', 'driver_prefix', 'driver_binding', 'make_binding')


def manifest(plan):
    value = plan['program']
    _require(value['manifest'] == dict(path=os.path.join(value['owner']['root'], MANIFEST_RELATIVE),
                                      sha256=MANIFEST_SHA256), 'unsupported initial Ports library manifest')
    data = _json(value['manifest'])
    _require(set(data) == {'format', 'version', 'origin', 'name', 'recipe', 'source_directory',
        'initial_only', 'build_system', 'compiler', 'configure_args', 'exports', 'make_targets',
        'stage_target', 'required_tool_roles', 'infrastructure_patches', 'diagnostic_helper'} and
        data['format'] == 'emberbsd-native-bootstrap-component' and type(data['version']) is int and
        data['version'] == 1 and data['name'] == 'ports-libnbcompat' and data['recipe'] == RECIPE and
        data['source_directory'] == SOURCE_DIRECTORY and data['initial_only'] is True and
        data['build_system'] == 'pkgsrc-library' and data['compiler'] == dict(family='gcc', major=16, abi='native') and
        data['make_targets'] == ['all'] and data['stage_target'] == 'install' and
        set(data['required_tool_roles']) == ROLES and len(data['required_tool_roles']) == len(ROLES) and
        data['infrastructure_patches'] == [PATCH] and data['diagnostic_helper'] == HELPER and
        len(data['exports']) == len(set(data['exports'])) == 41 and
        data['exports'][-1] == 'lib/libnbcompat.a' and
        all(re.fullmatch(r'include/(?:nbcompat/)?[a-zA-Z0-9_]+\.h', name) for name in data['exports'][:-1]) and
        {'include/nbcompat.h', 'include/nbcompat/config.h', 'include/nbcompat/nbconfig.h',
         'include/nbcompat/db.h'} <= set(data['exports']), 'unsupported complete Ports library contract')
    return data


def environment(plan, commands, flags):
    work = _safe_path(plan['work_root'])
    env = dict(PATH=work + '/bin', LC_ALL='C', TZ='UTC', TMPDIR=work, CONFIG_SITE='/dev/null')
    for variable, role in (('CC', 'cc'), ('CPP', 'cpp'), ('AS', 'as'), ('LD', 'ld'), ('AR', 'ar'),
        ('RANLIB', 'ranlib'), ('AWK', 'awk'), ('SHELL', 'sh'), ('CONFIG_SHELL', 'sh'), ('MAKE', 'make'),
        ('CPPROG', 'cp'), ('MVPROG', 'mv'), ('CHMODPROG', 'chmod'), ('RMPROG', 'rm'), ('MKDIRPROG', 'mkdir')):
        env[variable] = shlex.join(commands[role])
    env.update(CPPFLAGS=shlex.join(flags['cppflags']), CFLAGS=shlex.join([*flags['cflags'], '-v']),
        LDFLAGS=shlex.join([*flags['ldflags'], '-Wl,--threads=1']),
        GREP=shlex.join(commands['grep']), EGREP=shlex.join([*commands['grep'], '-E']),
        INSTALL=shlex.join([*commands['sh'], work + '/source/install-sh', '-c']),
        EMBERBSD_CONFIGURE_SDK='yes', EMBERBSD_CONFIGURE_UNAME=commands['uname'][0])
    return env


def steps(plan, descriptor, commands, flags):
    data, work = manifest(plan), _safe_path(plan['work_root'])
    source, prefix = work + '/source', work + '/stage'
    env = environment(plan, commands, flags)
    triplet = descriptor['compiler']['triplet']
    result = [dict(id='libnbcompat-configure', phase='libnbcompat-configure', cwd=source,
        env=dict(env), expected_exit=0, argv=[*commands['sh'], source + '/configure',
            '--build=' + triplet, '--host=' + triplet, '--prefix=' + prefix,
            '--cache-file=/dev/null', *data['configure_args']])]
    args = ['-j' + str(plan['resources']['cpu_jobs']), 'MAKE=' + shlex.join(commands['make'])]
    result.append(dict(id='libnbcompat-build', phase='libnbcompat-build', cwd=source, env=dict(env),
        expected_exit=0, argv=[*commands['make'], *args, *data['make_targets']]))
    result.append(dict(id='libnbcompat-stage', phase='libnbcompat-stage', cwd=source, env=dict(env),
        expected_exit=0, argv=[*commands['make'], '-j1', 'MAKE=' + shlex.join(commands['make']),
            'prefix=' + prefix, 'DESTDIR=', 'LIBMODE=644', 'INSTALL=' + env['INSTALL'], data['stage_target']]))
    return result


def validate_plan(plan, descriptor, commands, flags):
    from .bootstrap_pilot import _validate_resources
    from .bootstrap_producer import PRODUCER_FORMAT, PROFILE_EXPORT, _export_proof, _inventory
    _envelope(plan, PRODUCER_FORMAT, scope='gcc16-bootstrap')
    _require(set(plan) == {'format', 'version', 'scope', 'stage', 'seed_descriptor', 'host', 'sources',
        'work_root', 'required_roles', 'resources', 'program', 'commands'} and plan['stage'] == STAGE and
        plan['host'] == descriptor['host'] and plan['sources'] == descriptor['sources'] and
        descriptor['host']['os'] == 'Darwin' and descriptor['host']['cpu'] in ('arm64', 'aarch64'),
        'unsupported initial Ports libnbcompat plan')
    _validate_resources(plan['resources'], recipe=True, parallel=True)
    _require(plan['resources']['runtime_seconds'] <= 1200, 'initial Ports library budget is not finite')
    _safe_path(plan['work_root'])
    value = plan['program']
    _require(set(value) == {'source', 'owner', 'manifest', 'export', 'profile'}, 'unsupported Ports library inputs')
    data = manifest(plan)
    owner, _ = _tree(value['owner'], descriptor, owner=True)
    _require({n for n, row in owner.items() if row['kind'] == 'file'} ==
             {MANIFEST_RELATIVE, PATCH, HELPER, 'pkgsrc/export.conf'}, 'Ports library owner roster differs')
    source, _ = _tree(value['source'], descriptor, owner=False)
    _require(entry(value['source']['root']).st_mode & 0o222 == 0 and
             all(row['mode'] & 0o222 == 0 for row in source.values()), 'Ports library source must be read-only')
    _require(set(value['export']) == {'root', 'inventory', 'source_proof'} and
             value['profile'] == dict(path=os.path.join(value['export']['root'], PROFILE_EXPORT),
                                      sha256=value['profile']['sha256']), 'Ports library profile is not canonical')
    _bytes(value['profile'])
    exported = _inventory(value['export']['inventory'])
    _export_proof(value, descriptor, exported)
    wanted = {n for n in exported if n == 'bootstrap/bootstrap' or n.startswith(RECIPE + '/')}
    wanted |= {str(parent) for name in tuple(wanted) for parent in Path(name).parents if str(parent) != '.'}
    _require(set(source) == wanted, 'Ports library source omits or adds canonical members')
    for name, row in source.items():
        keys = ('kind', 'size', 'sha256') if row['kind'] == 'file' else ('kind',)
        _require(all(row.get(k) == exported[name].get(k) for k in keys), 'Ports library exported bytes differ')
    root = Path(value['source']['root'])
    args = re.findall(r'^CONFIGURE_ARGS\+=\s*(.*)$', (root/RECIPE/'Makefile').read_text(), re.M)
    _require(len(args) == 1 and shlex.split(args[0]) == data['configure_args'] and
             ' '.join(data['configure_args']) in ' '.join((root/'bootstrap/bootstrap').read_text().replace('\\\n', '').split()),
             'Ports library options differ from its common recipe and bootstrap')
    plist = [line for line in (root/RECIPE/'PLIST').read_text().splitlines() if line and not line.startswith('@')]
    _require(plist == data['exports'], 'Ports library exports differ from the complete package list')
    _require((root/SOURCE_DIRECTORY/'emberbsd-configure-diagnostics.sh').read_bytes() ==
             Path(value['owner']['root'], HELPER).read_bytes(), 'Ports library uses a different diagnostic helper')
    required = ROLES | set(descriptor['driver_components'].values())
    _require(isinstance(plan['required_roles'], list) and len(plan['required_roles']) == len(set(plan['required_roles'])) and
             required <= set(plan['required_roles']) <= set(commands), 'Ports library selected tools are incomplete')
    for role in ROLES - _DRIVERS:
        _require(len(commands[role]) == 1 and descriptor['tools'][role]['kind'] == 'native',
                 'Ports library utility must be one accepted native executable')
        _absolute(commands[role][0])
    _require(commands['uname'][0] not in ('/usr/bin/uname', '/bin/uname') and
             [arg for arg in commands['make'] if arg.startswith('SHELL=')] == ['SHELL=' + commands['sh'][0]],
             'Ports library requires selected uname and an exact command-line make SHELL')
    _require(plan['commands'] == steps(plan, descriptor, commands, flags), 'Ports library commands differ')
    return plan


def outputs(plan):
    return ['source-copy.json', 'source/Makefile', 'source/config.log', 'source/config.status',
        'source/nbcompat/config.h', 'source/nbcompat/nbconfig.h', 'source/libnbcompat.a',
        *('stage/' + name for name in manifest(plan)['exports'])]


def prepare_work(plan, commands):
    from .bootstrap_source_work import copy_tree, tool_namespace
    source = Path(plan['program']['source']['root'], SOURCE_DIRECTORY)
    staged = copy_tree(source, Path(plan['work_root'], 'source'))
    write_json(Path(plan['work_root'], 'source-copy.json'), dict(
        source=plan['program']['source'], source_directory=SOURCE_DIRECTORY, inventory=staged))
    tool_namespace(Path(plan['work_root'], 'bin'), commands, ROLES - _DRIVERS)


def _regular(path):
    info = entry(path)
    _require(info is not None and stat.S_ISREG(info.st_mode) and info.st_size <= 16 * 1024 * 1024,
             'Ports library output must be a bounded regular file')
    return dict(path=os.fspath(path), sha256=sha256_file(path))


def _source_unchanged(plan):
    original = Path(plan['program']['source']['root'], SOURCE_DIRECTORY)
    work = Path(plan['work_root'], 'source')
    before = snapshot_tree(str(original))
    copied = _json(_regular(Path(plan['work_root'], 'source-copy.json')))
    expected = {name: dict(row, mode=row['mode'] | (0o200 if row['kind'] == 'file' else 0o700))
                for name, row in before.items()}
    _require(copied == dict(source=plan['program']['source'], source_directory=SOURCE_DIRECTORY,
                           inventory=expected), 'Ports library source copy proof differs')
    for name, row in before.items():
        info = entry(work/name)
        _require(info is not None and (stat.S_ISREG(info.st_mode) if row['kind'] == 'file'
                 else stat.S_ISDIR(info.st_mode)) and
                 stat.S_IMODE(info.st_mode) == row['mode'] | (0o200 if row['kind'] == 'file' else 0o700),
                 'Ports library staged source type or mode changed')
        if row['kind'] == 'file':
            _require(sha256_file(work/name) == row['sha256'] and
                     info.st_mtime_ns == os.stat(original/name).st_mtime_ns,
                     'Ports library staged canonical source or timestamp changed')


def prepare(step, plan, completed):
    from .bootstrap_binding import seed_commands
    from .bootstrap_source_work import validate_namespace
    validate_namespace(Path(plan['work_root'], 'bin'),
                       seed_commands(_json(plan['seed_descriptor']), inspect_inputs=False), ROLES - _DRIVERS)
    _source_unchanged(plan)
    for row in completed:
        for key in ('configuration', 'generated'):
            for name, ref in row.get(key, {}).items():
                _require(_regular(ref['path']) == ref, 'Ports library generated input changed before child')
                _require(stat.S_IMODE(os.stat(ref['path']).st_mode) == row.get(key + '_modes', {}).get(name),
                         'Ports library generated input mode changed before child')
        if step['phase'] == 'libnbcompat-stage':
            for ref in row.get('built_objects', {}).values():
                _require(_regular(ref['path']) == ref, 'Ports library object changed before installation')
            if 'archive' in row:
                ref = row['archive']
                _require(_regular(ref['path']) == ref and
                         stat.S_IMODE(os.stat(ref['path']).st_mode) == row['archive_mode'],
                         'Ports library archive changed before installation')


def _objects(plan, archive_path):
    from .bootstrap_archive import members
    root = Path(plan['work_root'], 'source')
    makefile = (root/'Makefile').read_text()
    _require(re.findall(r'^WITH_DB=\s*(\S+)\s*$', makefile, re.M) == ['yes'],
             'Ports library configured DB implementation is missing')
    def objects(text, operator):
        flat = text.replace('\\\n', ' ')
        rows = re.findall(r'^OBJS' + re.escape(operator) + r'\s*([^\n]*)$', flat, re.M)
        _require(len(rows) == 1, 'Ports library needs the canonical configured object list')
        words = rows[0].split()
        _require(all(re.fullmatch(r'[a-zA-Z0-9_]+\.o', n) for n in words), 'invalid Ports object spelling')
        return words
    wanted = objects(makefile, '=') + objects((root/'db/Makefile.inc').read_text(), '+=')
    _require(wanted and len(wanted) == len(set(wanted)), 'duplicate or empty Ports library objects')
    actual = members(_bytes(_regular(archive_path)))
    _require([name for name, _ in actual] == wanted, 'Ports library archive omits or adds configured DB objects')
    refs = {}
    for name, raw in actual:
        ref = _regular(root/name)
        _native_image(ref['path'], plan['host'], object_file=True)
        _require(_bytes(ref) == raw, 'Ports library archive object differs from its native object')
        refs[name] = ref
    return refs


def verify(step, work, plan, actual_exit, completed):
    _require(type(actual_exit) is int and actual_exit == 0, 'Ports library command failed')
    prepare(step, plan, completed)
    root = Path(work, 'source')
    if step['phase'] == 'libnbcompat-configure':
        text = (root/'Makefile').read_text()
        expected = {k: step['env'][k] for k in ('CC', 'CPPFLAGS', 'LDFLAGS', 'AWK', 'AR', 'RANLIB', 'INSTALL')}
        expected.update(CCLD=step['env']['CC'], srcdir=str(root), prefix=os.path.join(work, 'stage'))
        for name, value in expected.items():
            rows = re.findall(r'^' + re.escape(name) + r'=[ \t]*([^\n]*)$', text, re.M)
            _require(len(rows) == 1 and rows[0].strip() == value,
                     'Ports library configured tool or path differs: ' + name)
        cflags = re.findall(r'^CFLAGS=[ \t]*([^\n]*)$', text, re.M)
        _require(len(cflags) == 1 and shlex.split(cflags[0]) ==
                 ['-I$(srcdir)', '-I.', *shlex.split(step['env']['CFLAGS'])],
                 'Ports library configured compile flags differ')
        names = ('config.log', 'config.status', 'Makefile', 'nbcompat/config.h')
        return dict(configuration={name: _regular(root/name) for name in names},
                    configuration_modes={name: stat.S_IMODE(os.stat(root/name).st_mode) for name in names})
    objects = _objects(plan, root/'libnbcompat.a')
    generated = {'nbcompat/nbconfig.h': _regular(root/'nbcompat/nbconfig.h')}
    generated_modes = {'nbcompat/nbconfig.h': stat.S_IMODE(os.stat(root/'nbcompat/nbconfig.h').st_mode)}
    if step['phase'] == 'libnbcompat-build':
        return dict(built_objects=objects, generated=generated, generated_modes=generated_modes,
                    archive=_regular(root/'libnbcompat.a'),
                    archive_mode=stat.S_IMODE(os.stat(root/'libnbcompat.a').st_mode))
    stage = Path(work, 'stage')
    inventory = snapshot_tree(str(stage))
    files = {n for n, row in inventory.items() if row['kind'] == 'file'}
    wanted = manifest(plan)['exports']
    directories = {str(p) for name in wanted for p in Path(name).parents if str(p) != '.'}
    _require(files == set(wanted) and set(inventory) == files | directories and
             all(row['kind'] in ('file', 'directory') for row in inventory.values()),
             'Ports library stage has missing, additional or linked outputs')
    copies = {}
    for name in wanted:
        expected_mode = 0o644 if name.endswith('.a') else 0o444
        _require(inventory[name]['mode'] in (expected_mode, expected_mode & ~0o222),
                 'Ports library installed file mode differs from canonical install')
        original = root/(name.removeprefix('include/') if name.startswith('include/') else 'libnbcompat.a')
        copies[name] = dict(original=_regular(original), staged=_regular(stage/name))
        if name.startswith('include/'):
            _require(copies[name]['original']['sha256'] == copies[name]['staged']['sha256'],
                     'Ports library installed header differs from its source or generated bytes')
    _require(_objects(plan, stage/'lib/libnbcompat.a') == objects,
             'Ports library installed archive changed its native members')
    _require(all(inventory[name]['mode'] in (0o755, 0o555) for name in directories),
             'Ports library installed directory mode differs')
    return dict(staged_inventory=inventory, copies=copies, built_objects=objects, generated=generated,
                generated_modes=generated_modes)

def _legacy_input(value, descriptor):
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
    old_binding, new_binding = historical.get('driver_binding'), descriptor.get('driver_binding')
    same_binding = old_binding is None and new_binding is None
    if isinstance(old_binding, dict) and isinstance(new_binding, dict):
        same_binding = (old_binding.get('ports_source') == historical['sources']['ports'] and
            new_binding.get('ports_source') == descriptor['sources']['ports'] and
            {key: val for key, val in old_binding.items() if key != 'ports_source'} ==
            {key: val for key, val in new_binding.items() if key != 'ports_source'})
    _require(historical_ref == result['seed_descriptor'] == plan['seed_descriptor'] and
             all(historical[key] == descriptor[key] for key in
                 ('host', 'compiler', 'host_abi', 'abi_flags', 'driver_components', 'driver_prefix')) and
             same_binding and
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


def candidate(plan_ref, result_ref):
    """Return pending complete Ports evidence. Never issue independent acceptance."""
    from .bootstrap_binding import seed_commands
    from .native_toolset import _gcc
    plan, result = _json(plan_ref), _json(result_ref)
    _envelope(result, 'emberbsd-native-gcc16-producer-result', scope='gcc16-bootstrap')
    _require(type(result.get('api')) is int and result['api'] == 1 and result.get('stage') == STAGE and
        result.get('status') == 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW' and
        result.get('ordinary_provider_qualified') is False and result.get('bootstrap_plan') == plan_ref and
        result.get('seed_descriptor') == plan.get('seed_descriptor') and
        all(result.get(k) is True for k in ('all_commands_drained', 'all_leases_closed', 'inputs_revalidated_after_drain')),
        'Ports library requires its complete successful drained producer')
    seed = _json(plan['seed_descriptor'])
    _envelope(seed, 'emberbsd-bootstrap-seed', scope='gcc16-bootstrap')
    _gcc(seed['compiler'], seed['host'])
    accepted = _json(result['accepted_seed_receipt'])
    _envelope(accepted, 'emberbsd-bootstrap-seed-acceptance', scope='gcc16-bootstrap')
    _require(accepted.get('status') == 'PASS' and accepted.get('accepted_input_scope') == 'bootstrap-seed' and
        accepted.get('descriptor_sha256') == plan['seed_descriptor']['sha256'] and
        accepted.get('bootstrap_plan') == plan_ref and all(accepted.get(k) == seed.get(k) for k in SEED_FIELDS),
        'Ports library lacks its exact original input acceptance')
    # The old seed is provenance. Do not reopen its retired tool or ABI files.
    validate_plan(plan, seed, seed_commands(seed, inspect_inputs=False), seed['abi_flags'])
    actual = result['commands']
    _require(len(actual) == len(plan['commands']) == 3, 'Ports library commands are missing or added')
    completed = []
    for row, expected in zip(actual, plan['commands']):
        _require(all(row.get(k) == v for k, v in expected.items()) and
                 type(row.get('actual_exit')) is int and row['actual_exit'] == 0 and
                 row.get('log', {}).get('path') == os.path.join(plan['work_root'], row['id'] + '.log'),
                 'Ports library actual command differs or failed')
        _bytes(row['log'])
        observed = verify(expected, plan['work_root'], plan, 0, completed)
        for key, value in observed.items():
            if key == 'staged_inventory':
                recorded = row.get(key, {})
                _require(set(value) == set(recorded) and all(
                    entry_row == recorded[name] or entry_row == dict(recorded[name], mode=recorded[name]['mode'] & ~0o222)
                    for name, entry_row in value.items()), 'Ports stage changed beyond read-only sealing')
            else:
                _require(row.get(key) == value, 'Ports library actual generated or staged evidence differs')
        _require(set(row) == set(expected) | {'actual_exit', 'log'} | set(observed),
                 'Ports library actual record has unsupported evidence')
        completed.append(row)
    refs = {name: _regular(Path(plan['work_root'], name)) for name in outputs(plan)}
    _require(result['outputs'] == {name: ref['sha256'] for name, ref in refs.items()},
             'Ports library result omits or changes its output roster')
    return dict(format='emberbsd-ports-libnbcompat-component-candidate', version=1,
        status='PENDING_INDEPENDENT_COMPONENT_REVIEW', recipe=RECIPE, initial_only=True,
        ordinary_provider_qualified=False, bootstrap_plan=plan_ref, producer_result=result_ref,
        seed_descriptor=plan['seed_descriptor'], accepted_seed_receipt=result['accepted_seed_receipt'],
        source=plan['program']['source'], owner=plan['program']['owner'], export=plan['program']['export'],
        profile=plan['program']['profile'], manifest=plan['program']['manifest'],
        root=os.path.join(plan['work_root'], 'stage'), staged_inventory=observed['staged_inventory'],
        configuration=actual[0]['configuration'], generated=actual[1]['generated'],
        built_objects=actual[1]['built_objects'], copies=actual[2]['copies'], outputs=refs)


def validate_input(value, descriptor):
    """Accept one exact reviewed Ports component, retaining the legacy branch."""
    _require(isinstance(value, dict) and set(value) ==
        {'root', 'inventory', 'producer_result', 'bootstrap_plan', 'component_receipt'},
        'unsupported Ports library input fields')
    receipt = _json(value['component_receipt'])
    if receipt.get('status') == 'ACCEPTED_INITIAL_LIBNBCOMPAT_INPUT':
        return _legacy_input(value, descriptor)
    _require(set(receipt) == {'format', 'version', 'scope', 'status', 'recipe', 'candidate',
        'independent_review', 'initial_only', 'ordinary_provider_qualified'} and
        receipt['format'] == 'emberbsd-ports-libnbcompat-component-acceptance' and
        type(receipt['version']) is int and receipt['version'] == 1 and receipt['scope'] == 'gcc16-bootstrap' and
        receipt['status'] == 'ACCEPTED_INITIAL_PORTS_LIBNBCOMPAT_COMPONENT' and receipt['recipe'] == RECIPE and
        receipt['initial_only'] is True and receipt['ordinary_provider_qualified'] is False,
        'unsupported initial Ports library acceptance; OS archives cannot replace this recipe')
    saved = _json(receipt['candidate'])
    _require(saved['bootstrap_plan'] == value['bootstrap_plan'] and saved['producer_result'] == value['producer_result'] and
        saved == candidate(value['bootstrap_plan'], value['producer_result']), 'Ports library candidate changed')
    root = _absolute(value['root'])
    inventory = _json(value['inventory'])
    _require(root == saved['root'] == canonical_existing(root) and
        inventory == saved['staged_inventory'] == snapshot_tree(root) and entry(root).st_mode & 0o222 == 0 and
        all(row['mode'] & 0o222 == 0 for row in inventory.values()), 'Ports component must be its sealed exact stage')
    review = _json(receipt['independent_review'])
    _require(set(review) == {'status', 'candidate', 'findings', 'all_leases_closed', 'ordinary_provider_qualified'} and
        review['status'] == 'REVIEWED_INITIAL_PORTS_LIBNBCOMPAT_COMPONENT' and review['candidate'] == receipt['candidate'] and
        review['findings'] == [] and review['all_leases_closed'] is True and review['ordinary_provider_qualified'] is False,
        'Ports library requires separate independent output review')
    historical = _json(saved['seed_descriptor'])
    _require(all(historical.get(k) == descriptor.get(k) for k in
        ('host', 'compiler', 'host_abi', 'abi_flags', 'driver_components', 'driver_prefix')) and
        all(historical['tools'][role] == descriptor['tools'][role] for role in
            {'cc', *descriptor['driver_components'].values()}), 'Ports library compiler or host ABI changed')
    old, new = historical.get('driver_binding'), descriptor.get('driver_binding')
    same = old is None and new is None
    if isinstance(old, dict) and isinstance(new, dict):
        same = (old.get('ports_source') == historical['sources']['ports'] and
                new.get('ports_source') == descriptor['sources']['ports'] and
                {k: v for k, v in old.items() if k != 'ports_source'} ==
                {k: v for k, v in new.items() if k != 'ports_source'})
    _require(same, 'Ports library driver binding changed')


def input_documents(value):
    """Retain all component evidence under the consumer's existing shared leases."""
    receipt = _json(value['component_receipt'])
    if receipt.get('status') == 'ACCEPTED_INITIAL_LIBNBCOMPAT_INPUT':
        return [receipt, _json(_json(receipt['preparation'])['source_seed_descriptor'])], []
    saved = _json(receipt['candidate'])
    plan = _json(saved['bootstrap_plan'])
    documents = [receipt, saved, plan, _json(saved['producer_result']), _json(saved['seed_descriptor']),
        _json(saved['accepted_seed_receipt']), _json(receipt['independent_review'])]
    roots = [plan['work_root']]
    for name in ('source', 'owner', 'export'):
        roots.append(plan['program'][name]['root'])
        documents.append(_json(plan['program'][name]['source_proof']))
    return documents, roots

