# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Fixed OS compatibility component; the existing pilot owns all children."""

import os
from pathlib import Path
import stat

from .inventory import sha256_file
from .native_toolset import _bytes, _envelope, _json, _require
from .paths import entry

STAGE = 'compat-filetime-bootstrap'
MANIFEST = 'tools/compat/bootstrap-filetime.json'
DIGEST = '1152bef47f8a3dce202dae0252490350e68b29bd90adb7a7cf5942633b4faede'
SOURCES = ('lib/libc/gen/utimens.c',)
HEADERS = ('tools/compat/compat_filetime.h', 'tools/compat/namespace.h',
           'tools/compat/bootstrap_cdefs.h')
PROBES = ('func-utimens', 'func-lutimens')
PROVENANCE = ('tools/compat/Makefile', 'tools/compat/filetime.mk',
    'tools/compat/configure.ac', 'tools/compat/configure', 'tools/compat/nbtool_config.h.in',
    'tools/compat/compat_defs.h', 'tools/compat/README.filetime.md',
    *(f'tools/compat/probes/{name}.c' for name in
      ('filetime-control', 'filetime-check', *PROBES)))
EXPORTS = ('libnbcompat.a', 'nbtool_config.h', 'compat_filetime.h', 'bootstrap_cdefs.h')


def manifest(plan):
    root = plan['program']['source']['root']
    ref = dict(path=os.path.join(root, MANIFEST), sha256=DIGEST)
    _require(plan['program']['manifest'] == ref, 'unsupported OS component manifest')
    value = _json(ref)
    _require(value['format'] == 'emberbsd-native-bootstrap-component' and
             type(value['version']) is int and value['version'] == 1 and
             value['name'] == 'filetime' and value['initial_only'] is True and
             value['sources'] == list(SOURCES) and value['headers'] == list(HEADERS) and
             value['archive'] == 'libnbcompat.a' and
             value['required_tool_roles'] == ['cc', 'ar', 'ranlib'] and
             [row['name'] for row in value['probes']] == list(PROBES),
             'unsupported fixed OS component fields')
    return value


def sources(plan):
    value, root = manifest(plan), Path(plan['program']['source']['root'])
    names = [value['control'], value['check'],
             *(row['source'] for row in value['probes']),
             'tools/compat/compat_filetime.h', 'tools/compat/bootstrap_cdefs.h']
    return {Path(name).name: (root / name).read_bytes() for name in names}


def outputs(plan):
    manifest(plan)
    return [*sources(plan), *EXPORTS[:2], 'utimens.o', 'filetime-control.o',
            'filetime-control', 'filetime-check.o', 'filetime-check',
            *PROBES]


def steps(plan, descriptor, commands, flags):
    value, work = manifest(plan), plan['work_root']
    _require(descriptor['host']['os'] == 'Darwin', 'initial OS component requires Darwin')
    _require(all(role in commands for role in value['required_tool_roles']),
             'OS component requires selected compiler, ar and ranlib')
    dirs = sorted({os.path.dirname(words[0]) for words in commands.values()})
    _require(all(os.pathsep not in path for path in dirs), 'component tool PATH is ambiguous')
    env = dict(PATH=os.pathsep.join(dirs), LC_ALL='C', TZ='UTC', TMPDIR=work)
    cc = [*commands['cc'], *flags['cppflags'], *flags['cflags'], '-v']
    ld = [*commands['cc'], *flags['cflags'], '-v']
    ldflags = [*flags['ldflags'], '-Wl,--threads=1', '-Wl,-v']
    def path(name):
        return os.path.join(work, name)
    def row(name, phase, argv, expected=0, runtime=False):
        return dict(id=name, phase=phase, cwd=work,
                    env=dict(env, **({'DYLD_PRINT_LIBRARIES': '1'} if runtime else {})),
                    argv=argv, expected_exit=expected)
    result = [
        row('filetime-control-compile', 'compile',
            [*cc, '-c', path('filetime-control.c'), '-o', path('filetime-control.o')]),
        row('filetime-control-link', 'link',
            [*ld, path('filetime-control.o'), *ldflags, '-o', path('filetime-control')]),
        row('filetime-control-run', 'run', [path('filetime-control')])]
    for probe in value['probes']:
        result.append(row(probe['name'], 'compat-probe-link',
            [*cc, path(probe['name'] + '.c'), *ldflags, '-o', path(probe['name'])], [0, 1]))
    config = ['-include', path('nbtool_config.h'), '-I', work, '-I',
              os.path.join(plan['program']['source']['root'], 'tools/compat')]
    result.extend([
        row('compile-utimens', 'compile', [*cc, *config, '-c',
            os.path.join(plan['program']['source']['root'], SOURCES[0]), '-o', path('utimens.o')]),
        row('archive-filetime', 'component-archive',
            [*commands['ar'], 'rc', path('libnbcompat.a'), path('utimens.o')]),
        row('index-filetime', 'component-index', [*commands['ranlib'], path('libnbcompat.a')]),
        row('compile-filetime-check', 'compile',
            [*cc, *config, '-c', path('filetime-check.c'), '-o', path('filetime-check.o')]),
        row('link-filetime-check', 'link',
            [*ld, path('filetime-check.o'), path('libnbcompat.a'), *ldflags, '-o', path('filetime-check')]),
        row('filetime-check', 'run', [path('filetime-check')]),
        row('runtime-observation', 'run', [path('filetime-check')], runtime=True)])
    return result


def configuration(completed, value):
    _require([row['id'] for row in completed[:3]] ==
             ['filetime-control-compile', 'filetime-control-link', 'filetime-control-run'] and
             all(type(row.get('actual_exit')) is int and row['actual_exit'] == 0
                 for row in completed[:3]), 'component lacks successful actual control')
    actual = [row for row in completed if row['phase'] == 'compat-probe-link']
    _require([row['id'] for row in actual] == list(PROBES), 'component needs every ordered probe')
    definitions = ['#define HAVE_UTIMENSAT 1']
    for row, probe in zip(actual, value['probes']):
        status = row.get('actual_exit')
        _require(type(status) is int and status in (0, 1) and
                 row.get('configuration_probe') == dict(macro=probe['macro'], available=status == 0),
                 'component lacks an actual drained capability observation')
        definitions.append('#define ' + probe['macro'] + ' ' + str(int(status == 0)))
    return (value['config_preamble'] + '\n'.join(definitions) + '\n' +
            value['config_postamble']).encode('ascii')


def _regular(path):
    info = entry(path)
    _require(info is not None and stat.S_ISREG(info.st_mode) and info.st_size <= 16 * 1024 * 1024,
             'component output must be a bounded regular file')
    return dict(path=os.fspath(path), sha256=sha256_file(path))


def _archive(work):
    """Require one exact canonical object; permit only standard symbol indexes."""
    path = Path(work, 'libnbcompat.a')
    ref = _regular(path)
    data = _bytes(ref)
    _require(data.startswith(b'!<arch>\n'), 'component output is not a static archive')
    offset, members = 8, []
    while offset < len(data):
        header = data[offset:offset + 60]
        _require(len(header) == 60 and header[58:] == b'`\n',
                 'component archive header is invalid')
        try:
            size = int(header[48:58])
            name = header[:16].decode('ascii').strip()
        except (ValueError, UnicodeError):
            _require(False, 'component archive member is invalid')
        _require(size >= 0 and offset + 60 + size <= len(data), 'component archive member is truncated')
        body = data[offset + 60:offset + 60 + size]
        if name.startswith('#1/'):
            try:
                count = int(name[3:])
            except ValueError:
                _require(False, 'component archive extended name is invalid')
            _require(0 < count <= len(body), 'component archive extended name is invalid')
            try:
                name = body[:count].rstrip(b'\0').decode('ascii')
            except UnicodeError:
                _require(False, 'component archive extended name is invalid')
            body = body[count:]
        if name not in ('/', '/SYM64/', '__.SYMDEF', '__.SYMDEF SORTED',
                         '__.SYMDEF_64', '__.SYMDEF_64 SORTED'):
            members.append((name.rstrip('/'), body))
        offset += 60 + size + size % 2
    _require(offset == len(data) and members == [('utimens.o', Path(work, 'utimens.o').read_bytes())],
             'component archive has missing, changed or extra objects')
    return ref


def _dependencies(completed):
    latest = {r['output']['path']: r['output'] for r in completed if 'output' in r}
    for ref in latest.values():
        _require(_regular(ref['path']) == ref, 'component dependency changed before child')


def prepare(step, plan, completed):
    manifest(plan)
    _require(len(completed) < len(plan['commands']) and step in plan['commands'] and [r['id'] for r in completed] ==
             [r['id'] for r in plan['commands'][:len(completed)]] and
             step == plan['commands'][len(completed)], 'component step order changed')
    for name, data in sources(plan).items():
        ref = _regular(Path(plan['work_root'], name))
        _require(_bytes(ref) == data, 'component work source changed before child')
    root = plan['program']['source']['root']
    for name, row in _json(plan['program']['source']['inventory']).items():
        if row['kind'] == 'file':
            _bytes(dict(path=os.path.join(root, name), sha256=row['sha256']))
    _dependencies(completed)
    if any(row['id'] == PROBES[-1] for row in completed):
        _require(_bytes(_regular(Path(plan['work_root'], 'nbtool_config.h'))) ==
                 configuration(completed, manifest(plan)), 'component configuration changed')
    if step['id'] == 'archive-filetime':
        _require(entry(os.path.join(plan['work_root'], 'libnbcompat.a')) is None,
                 'component archive already exists')
    if step['id'] in ('filetime-check', 'runtime-observation'):
        _require(all(entry(os.path.join(plan['work_root'], name)) is None
                     for name in ('filetime-file', 'filetime-link', 'filetime-missing')),
                 'component semantic fixtures already exist')


def verify(step, work, plan, actual_exit, completed):
    value = manifest(plan)
    result = {}
    if step['id'] in PROBES:
        probe = value['probes'][PROBES.index(step['id'])]
        _require(type(actual_exit) is int and actual_exit in (0, 1),
                 'unexpected component probe exit')
        result['configuration_probe'] = dict(macro=probe['macro'], available=actual_exit == 0)
        if step['id'] == PROBES[-1]:
            data = configuration([*completed, dict(step, actual_exit=actual_exit, **result)], value)
            path = Path(work, 'nbtool_config.h')
            with path.open('xb') as stream:
                stream.write(data)
            result['configuration_header'] = _regular(path)
    elif step['phase'] in ('component-archive', 'component-index'):
        result['output'] = _archive(work)
    elif step['id'] in ('filetime-check', 'runtime-observation'):
        _require(actual_exit == 0 and all(entry(os.path.join(work, name)) is None
                 for name in ('filetime-file', 'filetime-link', 'filetime-missing')),
                 'component semantic test failed or left fixtures')
    return result


def candidate(plan_ref, result_ref):
    """Return retained output evidence, never an independent acceptance."""
    plan, result = _json(plan_ref), _json(result_ref)
    _envelope(plan, 'emberbsd-native-gcc16-producer-plan', scope='gcc16-bootstrap')
    _envelope(result, 'emberbsd-native-gcc16-producer-result', scope='gcc16-bootstrap')
    manifest(plan)
    _require(plan['stage'] == STAGE and result['format'] == 'emberbsd-native-gcc16-producer-result' and
             result['bootstrap_plan'] == plan_ref and
             result['seed_descriptor'] == plan['seed_descriptor'] and
             result['status'] == 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW' and
             result['ordinary_provider_qualified'] is False and
             all(result.get(key) is True for key in
                 ('all_commands_drained', 'inputs_revalidated_after_drain', 'all_leases_closed')),
             'OS component requires its drained exact producer result')
    actual = result['commands']
    _require(len(actual) == len(plan['commands']) and
             all(all(row.get(k) == v for k, v in expected.items()) and
                 type(row.get('actual_exit')) is int and row['actual_exit'] in
                 (expected['expected_exit'] if isinstance(expected['expected_exit'], list)
                  else [expected['expected_exit']])
                 for row, expected in zip(actual, plan['commands'])),
             'OS component actual commands differ or failed')
    for row in actual:
        _require(row['log']['path'] == os.path.join(plan['work_root'], row['id'] + '.log'),
                 'OS component log path differs')
        _bytes(row['log'])
        if row['actual_exit'] == 0 and row['phase'] in ('compile', 'link', 'compat-probe-link'):
            _require(row.get('output', {}).get('path') == row['argv'][-1],
                     'OS component lacks an actual compiler output')
    _dependencies(actual)
    _require(_bytes(_regular(Path(plan['work_root'], 'nbtool_config.h'))) ==
             configuration(actual, manifest(plan)), 'OS component retained configuration differs')
    _archive(plan['work_root'])
    exports = {name: _regular(Path(plan['work_root'], name)) for name in EXPORTS}
    _require(all(result['outputs'].get(name) == ref['sha256'] for name, ref in exports.items()),
             'OS component retained output differs from producer')
    return dict(format='emberbsd-os-compat-component-candidate', version=1,
                status='PENDING_INDEPENDENT_COMPONENT_REVIEW', component='filetime',
                initial_only=True, ordinary_provider_qualified=False,
                bootstrap_plan=plan_ref, producer_result=result_ref,
                seed_descriptor=plan['seed_descriptor'], source=plan['program']['source'],
                exports=exports)


def validate_input(reference, descriptor):
    """Check a separately reviewed initial OS component. Never issue acceptance."""
    receipt = _json(reference)
    _require(set(receipt) == {'format', 'version', 'scope', 'status', 'component',
        'candidate', 'independent_review', 'initial_only', 'ordinary_provider_qualified'} and
        receipt['format'] == 'emberbsd-os-compat-component-acceptance' and
        type(receipt['version']) is int and receipt['version'] == 1 and
        receipt['scope'] == 'gcc16-bootstrap' and
        receipt['status'] == 'ACCEPTED_INITIAL_OS_COMPAT_COMPONENT' and
        receipt['component'] == 'filetime' and receipt['initial_only'] is True and
        receipt['ordinary_provider_qualified'] is False,
        'unsupported initial OS component acceptance')
    saved = _json(receipt['candidate'])
    _require(saved == candidate(saved['bootstrap_plan'], saved['producer_result']),
             'initial OS component candidate changed')
    review = _json(receipt['independent_review'])
    _require(set(review) == {'status', 'candidate', 'findings', 'all_leases_closed',
                            'ordinary_provider_qualified'} and
             review['status'] == 'REVIEWED_INITIAL_OS_COMPAT_COMPONENT' and
             review['candidate'] == receipt['candidate'] and review['findings'] == [] and
             review['all_leases_closed'] is True and review['ordinary_provider_qualified'] is False,
             'initial OS component needs its independent review')
    historical = _json(saved['seed_descriptor'])
    _require(all(historical.get(key) == descriptor.get(key) for key in
                 ('host', 'compiler', 'host_abi', 'abi_flags', 'driver_components',
                  'driver_prefix', 'driver_binding')) and
             all(historical['tools'][role] == descriptor['tools'][role] for role in
                 {'cc', *descriptor['driver_components'].values()}),
             'initial OS component compiler or ABI differs')
    _require(all(entry(ref['path']).st_mode & 0o222 == 0 for ref in saved['exports'].values()),
             'initial OS component exports must be read-only')
    return saved['exports']
