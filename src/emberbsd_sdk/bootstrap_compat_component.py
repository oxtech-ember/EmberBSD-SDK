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
PARSEDATE_STAGE = 'compat-parsedate-bootstrap'
LISTING_STAGE = 'compat-listing-bootstrap'
STAGES = (STAGE, PARSEDATE_STAGE, LISTING_STAGE)
_PROFILES = {
    STAGE: dict(name='filetime', manifest=MANIFEST, digest=DIGEST, sources=SOURCES,
                headers=HEADERS, probes=PROBES, provenance=PROVENANCE, exports=EXPORTS,
                object='utimens.o'),
    PARSEDATE_STAGE: dict(name='parsedate', manifest='tools/compat/bootstrap-parsedate.json',
        digest='7632e1182ea47ed9a48391c0fd93926416de4c272393441d7271bf5bd8473b0e', sources=('lib/libutil/parsedate.y',),
        headers=('tools/compat/compat_parsedate.h', 'tools/compat/compat_parsedate_features.h',
                 'tools/compat/bootstrap_cdefs.h'), probes=('func-mktime_z', 'func-timegm'),
        provenance=('tools/compat/Makefile', 'tools/compat/parsedate.mk',
            'tools/compat/configure.ac', 'tools/compat/configure', 'tools/compat/nbtool_config.h.in',
            'tools/compat/compat_defs.h', 'tools/compat/README.parsedate.md', 'lib/libutil/Makefile',
            *(f'tools/compat/probes/{name}.c' for name in
              ('parsedate-control', 'parsedate-check', 'func-mktime_z', 'func-timegm'))),
        exports=('libnbcompat.a', 'nbtool_config.h', 'compat_parsedate.h',
                 'compat_parsedate_features.h', 'bootstrap_cdefs.h'), object='parsedate.o'),
    LISTING_STAGE: dict(name='listing', manifest='tools/compat/bootstrap-listing.json',
        digest='5dbdf88505b5c30d3c29ed7cebfc0209244eecae714baf4a7ca492b3e9266666',
        sources=('lib/libc/gen/getbsize.c', 'lib/libc/gen/humanize_number.c',
                 'lib/libutil/stat_flags.c', 'tools/compat/compat_acl.c'),
        headers=('tools/compat/compat_listing.h', 'tools/compat/compat_acl.h',
                 'tools/compat/compat_acl_features.h',
                 'tools/compat/namespace.h', 'tools/compat/bootstrap_cdefs.h'),
        probes=('listing-getbsize', 'listing-humanize', 'listing-native-flags',
                'listing-stat-flags', 'listing-acl-bsd', 'listing-acl-darwin'),
        provenance=('tools/compat/Makefile', 'tools/compat/listing.mk',
            'tools/compat/configure.ac', 'tools/compat/configure', 'tools/compat/nbtool_config.h.in',
            'tools/compat/compat_defs.h', 'tools/compat/README.listing.md',
            'tools/compat/test-listing.py', 'include/stdlib.h', 'include/util.h',
            'include/unistd.h', 'sys/sys/unistd.h', 'sys/sys/stat.h',
            'sys/sys/types.h', 'sys/sys/endian.h', 'sys/sys/featuretest.h',
            'tests/lib/libc/gen/t_humanize_number.c',
            *(f'tools/compat/probes/{name}.c' for name in
              ('listing-control', 'listing-check', 'listing-getbsize', 'listing-humanize',
               'listing-native-flags', 'listing-stat-flags', 'listing-acl-bsd', 'listing-acl-darwin'))),
        exports=('libnbcompat.a', 'nbtool_config.h', 'compat_listing.h',
                 'compat_acl.h', 'bootstrap_cdefs.h'),
        objects=('getbsize.o', 'humanize_number.o', 'stat_flags.o', 'compat_acl.o')),
}


def profile(plan):
    value = _PROFILES.get(plan.get('stage'))
    _require(value is not None, 'unsupported fixed OS component stage')
    return value


def objects(fixed):
    """Return the fixed, ordered archive roster; preserve old single-object profiles."""
    return fixed['objects'] if 'objects' in fixed else (fixed['object'],)


def _fixtures(fixed):
    if fixed['name'] in ('filetime', 'listing'):
        return tuple(fixed['name'] + suffix for suffix in ('-file', '-link', '-missing'))
    return ()


def manifest(plan):
    fixed = profile(plan)
    root = plan['program']['source']['root']
    ref = dict(path=os.path.join(root, fixed['manifest']), sha256=fixed['digest'])
    _require(plan['program']['manifest'] == ref, 'unsupported OS component manifest')
    value = _json(ref)
    _require(value['format'] == 'emberbsd-native-bootstrap-component' and
             type(value['version']) is int and value['version'] == 1 and
             value['name'] == fixed['name'] and value['initial_only'] is True and
             value['sources'] == list(fixed['sources']) and value['headers'] == list(fixed['headers']) and
             value['archive'] == 'libnbcompat.a' and
             value['required_tool_roles'] == (['cc', 'yacc', 'ar', 'ranlib'] if fixed['name'] == 'parsedate'
                                             else ['cc', 'ar', 'ranlib']) and
             [row['name'] for row in value['probes']] == list(fixed['probes']),
             'unsupported fixed OS component fields')
    if fixed['name'] == 'parsedate':
        _require(value['generator'] == dict(role='yacc', arguments=['-p', '__pd', '-o', 'parsedate.c'],
                 source='lib/libutil/parsedate.y', output='parsedate.c') and
                 value['required_capabilities'] == ['HAVE_TIMEGM'], 'unsupported parsedate generation contract')
    return value


def sources(plan):
    value, root = manifest(plan), Path(plan['program']['source']['root'])
    names = [value['control'], value['check'],
             *(row['source'] for row in value['probes']),
             *profile(plan)['headers']]
    return {Path(name).name: (root / name).read_bytes() for name in names}


def outputs(plan):
    manifest(plan)
    fixed = profile(plan)
    name = fixed['name']
    return [*sources(plan), *fixed['exports'][:2], *objects(fixed), name + '-control.o',
            name + '-control', name + '-check.o', name + '-check', *fixed['probes'],
            *(['parsedate.c'] if name == 'parsedate' else [])]


def steps(plan, descriptor, commands, flags):
    value, work = manifest(plan), plan['work_root']
    fixed, name = profile(plan), profile(plan)['name']
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
    probe_cc = cc
    if name == 'parsedate':
        probe_cc = [*cc, '-I', work, '-include', path('compat_parsedate_features.h')]
    result = [
        row(name + '-control-compile', 'compile',
            [*probe_cc, '-c', path(name + '-control.c'), '-o', path(name + '-control.o')]),
        row(name + '-control-link', 'link',
            [*ld, path(name + '-control.o'), *ldflags, '-o', path(name + '-control')]),
        row(name + '-control-run', 'run', [path(name + '-control')], runtime=True)]
    for probe in value['probes']:
        result.append(row(probe['name'], 'compat-probe-link',
            [*probe_cc, path(probe['name'] + '.c'), *ldflags, '-o', path(probe['name'])], [0, 1]))
    config = ['-include', path('nbtool_config.h'), '-I', work, '-I',
              os.path.join(plan['program']['source']['root'], 'tools/compat')]
    if name == 'parsedate':
        from .bootstrap_generated import selected_yacc
        selected_yacc(descriptor, commands)
        result.append(row('generate-parsedate', 'generator', [*commands['yacc'], '-p', '__pd',
            '-o', path('parsedate.c'), os.path.join(plan['program']['source']['root'], fixed['sources'][0])]))
    for source, object_name in zip(fixed['sources'], objects(fixed)):
        result.append(row('compile-' + Path(object_name).stem, 'compile', [*cc, *config, '-c',
            (path('parsedate.c') if name == 'parsedate' else
             os.path.join(plan['program']['source']['root'], source)), '-o', path(object_name)]))
    result.extend([
        row('archive-' + name, 'component-archive',
            [*commands['ar'], 'rc', path('libnbcompat.a'), *map(path, objects(fixed))]),
        row('index-' + name, 'component-index', [*commands['ranlib'], path('libnbcompat.a')]),
        row('compile-' + name + '-check', 'compile',
            [*cc, *config, '-c', path(name + '-check.c'), '-o', path(name + '-check.o')]),
        row('link-' + name + '-check', 'link',
            [*ld, path(name + '-check.o'), path('libnbcompat.a'), *ldflags, '-o', path(name + '-check')]),
        row(name + '-check', 'run', [path(name + '-check')]),
        row('runtime-observation', 'run', [path(name + '-check')], runtime=True)])
    return result


def configuration(completed, value):
    fixed = profile(dict(stage='compat-' + value['name'] + '-bootstrap'))
    name = fixed['name']
    _require([row['id'] for row in completed[:3]] ==
             [name + '-control-compile', name + '-control-link', name + '-control-run'] and
             all(type(row.get('actual_exit')) is int and row['actual_exit'] == 0
                 for row in completed[:3]), 'component lacks successful actual control')
    actual = [row for row in completed if row['phase'] == 'compat-probe-link']
    _require([row['id'] for row in actual] == list(fixed['probes']), 'component needs every ordered probe')
    definitions = ['#define HAVE_UTIMENSAT 1'] if name == 'filetime' else []
    for row, probe in zip(actual, value['probes']):
        status = row.get('actual_exit')
        _require(type(status) is int and status in (0, 1) and
                 row.get('configuration_probe') == dict(macro=probe['macro'], available=status == 0),
                 'component lacks an actual drained capability observation')
        _require(probe['macro'] not in value.get('required_capabilities', []) or status == 0,
                 'component required capability is unavailable')
        definitions.append('#define ' + probe['macro'] + ' ' + str(int(status == 0)))
    return (value['config_preamble'] + '\n'.join(definitions) + '\n' +
            value['config_postamble']).encode('ascii')


def _regular(path):
    info = entry(path)
    _require(info is not None and stat.S_ISREG(info.st_mode) and info.st_size <= 16 * 1024 * 1024,
             'component output must be a bounded regular file')
    return dict(path=os.fspath(path), sha256=sha256_file(path))


def _archive(work, object_name='utimens.o'):
    """Require exact ordered canonical objects; permit only standard symbol indexes."""
    path = Path(work, 'libnbcompat.a')
    ref = _regular(path)
    data = _bytes(ref)
    from .bootstrap_archive import members as archive_members
    members = archive_members(data)
    names = (object_name,) if isinstance(object_name, str) else object_name
    _require(members == [(name, _bytes(_regular(Path(work, name)))) for name in names],
             'component archive has missing, changed or extra objects')
    return ref


def _dependencies(completed):
    latest = {r['output']['path']: r['output'] for r in completed if 'output' in r}
    for ref in latest.values():
        _require(_regular(ref['path']) == ref, 'component dependency changed before child')


def _work_inputs(plan):
    """Bound the flat compiler search directory; reject every undeclared entry."""
    work = plan['work_root']
    allowed = {*outputs(plan), 'result.json',
               *(step['id'] + '.log' for step in plan['commands'])}
    with os.scandir(work) as entries:
        for count, item in enumerate(entries, 1):
            _require(count <= len(allowed) and item.name in allowed and
                     item.is_file(follow_symlinks=False),
                     'component work roster contains an unexpected entry')
    for name, data in sources(plan).items():
        ref = _regular(Path(work, name))
        _require(_bytes(ref) == data, 'component work source changed before child')


def _source_inputs(plan):
    from .bootstrap_program import validate_program_source
    validate_program_source(plan, plan['sources'].get('os'))


def prepare(step, plan, completed):
    fixed = profile(plan)
    manifest(plan)
    _require(len(completed) < len(plan['commands']) and step in plan['commands'] and [r['id'] for r in completed] ==
             [r['id'] for r in plan['commands'][:len(completed)]] and
             step == plan['commands'][len(completed)], 'component step order changed')
    _work_inputs(plan)
    _source_inputs(plan)
    _dependencies(completed)
    from .bootstrap_generated import before_step
    before_step(step, plan, completed, track_modes=fixed['name'] == 'parsedate')
    if any(row['id'] == fixed['probes'][-1] for row in completed):
        _require(_bytes(_regular(Path(plan['work_root'], 'nbtool_config.h'))) ==
                 configuration(completed, manifest(plan)), 'component configuration changed')
    if step['id'] == 'archive-' + fixed['name']:
        _require(entry(os.path.join(plan['work_root'], 'libnbcompat.a')) is None,
                 'component archive already exists')
    if _fixtures(fixed) and step['id'] in (fixed['name'] + '-check', 'runtime-observation'):
        _require(all(entry(os.path.join(plan['work_root'], name)) is None
                     for name in _fixtures(fixed)),
                 'component semantic fixtures already exist')


def verify(step, work, plan, actual_exit, completed):
    value = manifest(plan)
    fixed = profile(plan)
    _work_inputs(plan)
    _source_inputs(plan)
    from .bootstrap_generated import after_step
    result = after_step(step, plan, actual_exit, completed, track_modes=fixed['name'] == 'parsedate')
    if step['id'] in fixed['probes']:
        probe = value['probes'][fixed['probes'].index(step['id'])]
        _require(type(actual_exit) is int and actual_exit in (0, 1),
                 'unexpected component probe exit')
        result['configuration_probe'] = dict(macro=probe['macro'], available=actual_exit == 0)
        if step['id'] == fixed['probes'][-1]:
            data = configuration([*completed, dict(step, actual_exit=actual_exit, **result)], value)
            path = Path(work, 'nbtool_config.h')
            with path.open('xb') as stream:
                stream.write(data)
            result['configuration_header'] = _regular(path)
    elif step['phase'] in ('component-archive', 'component-index'):
        result['output'] = _archive(work, objects(fixed))
    elif _fixtures(fixed) and step['id'] in (fixed['name'] + '-check', 'runtime-observation'):
        _require(actual_exit == 0 and all(entry(os.path.join(work, name)) is None
                 for name in _fixtures(fixed)),
                 'component semantic test failed or left fixtures')
    return result


def candidate(plan_ref, result_ref):
    """Return retained output evidence, never an independent acceptance."""
    plan, result = _json(plan_ref), _json(result_ref)
    _envelope(plan, 'emberbsd-native-gcc16-producer-plan', scope='gcc16-bootstrap')
    _envelope(result, 'emberbsd-native-gcc16-producer-result', scope='gcc16-bootstrap')
    manifest(plan)
    fixed = profile(plan)
    _source_inputs(plan)
    _work_inputs(plan)
    _require(plan['stage'] in STAGES and result['format'] == 'emberbsd-native-gcc16-producer-result' and
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
    from .bootstrap_generated import verify_history
    verify_history(plan, result, track_modes=fixed['name'] == 'parsedate')
    _archive(plan['work_root'], objects(fixed))
    exports = {name: _regular(Path(plan['work_root'], name)) for name in fixed['exports']}
    _require(all(result['outputs'].get(name) == ref['sha256'] for name, ref in exports.items()),
             'OS component retained output differs from producer')
    return dict(format='emberbsd-os-compat-component-candidate', version=1,
                status='PENDING_INDEPENDENT_COMPONENT_REVIEW', component=fixed['name'],
                initial_only=True, ordinary_provider_qualified=False,
                bootstrap_plan=plan_ref, producer_result=result_ref,
                seed_descriptor=plan['seed_descriptor'], source=plan['program']['source'],
                exports=exports)


def validate_input(reference, descriptor, *, component='filetime'):
    """Check a separately reviewed initial OS component. Never issue acceptance."""
    _require(component in ('filetime', 'parsedate', 'listing'), 'unsupported requested OS component')
    receipt = _json(reference)
    _require(set(receipt) == {'format', 'version', 'scope', 'status', 'component',
        'candidate', 'independent_review', 'initial_only', 'ordinary_provider_qualified'} and
        receipt['format'] == 'emberbsd-os-compat-component-acceptance' and
        type(receipt['version']) is int and receipt['version'] == 1 and
        receipt['scope'] == 'gcc16-bootstrap' and
        receipt['status'] == 'ACCEPTED_INITIAL_OS_COMPAT_COMPONENT' and
        receipt['component'] == component and receipt['initial_only'] is True and
        receipt['ordinary_provider_qualified'] is False,
        'unsupported initial OS component acceptance')
    saved = _json(receipt['candidate'])
    _require(saved['component'] == component and
             saved == candidate(saved['bootstrap_plan'], saved['producer_result']),
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
