# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Fixed common Ports Byacc. The existing pilot owns every native child."""

import hashlib
import io
import os
from pathlib import Path
import re
import shlex
import tarfile

from .bootstrap_gnu_program import _tree, _patch_sha1
from .bootstrap_shell import literal_stdin
from .inventory import sha256_file
from .native_toolset import _absolute, _bytes, _envelope, _json, _require, _source
from .paths import entry, regular_file

STAGE = 'byacc-bootstrap'
RECIPE = 'pkgsrc/devel/byacc'
MANIFEST_RELATIVE = RECIPE + '/bootstrap.json'
MANIFEST_SHA256 = '463755f98c4747e90a1d8b85ff7b237fe71cf616027ab33815ab47684d45c80f'
SOURCES = tuple(name + '.c' for name in ('closure', 'error', 'graph', 'lalr', 'lr0', 'main',
    'mkpar', 'mstring', 'output', 'reader', 'btyaccpar', 'symtab', 'verbose', 'warshall'))
OWNER_FILES = tuple(RECIPE + '/' + name for name in ('Makefile', 'DESCR', 'PLIST', 'distinfo',
    'README.md', 'bootstrap.json', 'patches/patch-aclocal.m4', 'patches/patch-configure',
    'bootstrap/capabilities.c', 'bootstrap/config.h.in', 'bootstrap/grammar.y'))


def byacc_manifest(plan):
    value = plan['program']
    _require(value['manifest'] == dict(path=os.path.join(value['owner']['root'], MANIFEST_RELATIVE),
                                      sha256=MANIFEST_SHA256), 'unsupported fixed Byacc manifest')
    manifest = _json(value['manifest'])
    _require(manifest['name'] == 'yacc' and manifest['initial_only'] is True and
             manifest['compiler'] == dict(family='gcc', major=16, abi='native') and
             manifest['canonical_host_recipe'] == RECIPE + '/Makefile' and
             manifest['sources'] == list(SOURCES) and manifest['headers'] == ['defs.h'] and
             manifest['release'] == 'byacc-20260126' and manifest['invocations'] == ['yacc'],
             'unsupported fixed Byacc recipe contract')
    from .bootstrap_program import _literal_cases
    # Parser is a test executable, never a provisionable invocation.
    _literal_cases(dict(manifest, invocations=['parser']), 8)
    return manifest


def source_members(plan):
    """Check the complete release and both exact patched members without a patch engine."""
    manifest = byacc_manifest(plan)
    archive = plan['program']['archive']
    _require(archive['sha256'] == manifest['archive_sha256'], 'Byacc archive identity differs')
    data = _bytes(archive, limit=2 * 1024 * 1024)
    expected, names, paths = {}, set(), {}
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as packed:
        for member in packed:
            parts = member.name.rstrip('/').split('/')
            _require(parts[0] == manifest['release'] and all(p not in ('', '.', '..') for p in parts)
                     and (member.isdir() or member.isfile()), 'unsupported Byacc release member')
            name = '/'.join(parts[1:])
            _require(name.casefold() not in names, 'Byacc duplicate or case-folded archive path')
            names.add(name.casefold())
            if name:
                paths[name] = ('directory', 0o555) if member.isdir() else ('file', 0o555 if member.mode & 0o111 else 0o444)
            if member.isfile():
                _require(0 <= member.size <= 2 * 1024 * 1024, 'Byacc source member exceeds bound')
                raw = packed.extractfile(member).read()
                expected[name] = manifest['patched_members'].get(name, hashlib.sha256(raw).hexdigest())
    inventory = _json(plan['program']['source']['inventory'])
    _require({name: (row['kind'], row['mode']) for name, row in inventory.items()} == paths,
             'Byacc release paths or normalized read-only modes differ')
    _require(set(manifest['patched_members']) == {'aclocal.m4', 'configure'}, 'Byacc patch output roster differs')
    return expected


def byacc_sources(plan):
    manifest = byacc_manifest(plan)
    root = Path(plan['program']['owner']['root'], RECIPE)
    return {'capabilities.c': (root / manifest['configuration']['probe']).read_bytes(),
            'grammar.y': (root / manifest['grammar']).read_bytes()}


def configuration_bytes(plan, completed):
    required = ('capability-compile', 'capability-link', 'capability-run')
    rows = [row for row in completed if row['id'] in required]
    _require([row['id'] for row in rows] == list(required) and
             all(type(row.get('actual_exit')) is int and row['actual_exit'] == 0 for row in rows),
             'Byacc configuration requires actual successful native capability compile/link/run')
    manifest = byacc_manifest(plan)
    return Path(plan['program']['owner']['root'], RECIPE, manifest['configuration']['template']).read_bytes()


def byacc_steps(plan, descriptor, commands, flags):
    manifest, work = byacc_manifest(plan), _absolute(plan['work_root'])
    source = _absolute(plan['program']['source']['root'])
    version = Path(source, 'VERSION').read_text(encoding='ascii').strip()
    _require(version == manifest['release'].removeprefix('byacc-'), 'Byacc VERSION differs from release')
    directories = sorted({os.path.dirname(words[0]) for words in commands.values()})
    _require(all(os.pathsep not in path for path in directories), 'Byacc PATH cannot encode selected directories')
    env = dict(PATH=os.pathsep.join(directories), LC_ALL='C', TZ='UTC', TMPDIR=work)
    cc = [*commands['cc'], *flags['cppflags'], *flags['cflags'], '-v']
    ld = [*commands['cc'], *flags['cflags'], '-v']
    ldflags = [*flags['ldflags'], '-Wl,--threads=1', '-Wl,-v']
    def row(name, phase, argv, status=0, environment=None):
        return dict(id=name, phase=phase, cwd=work, env=dict(env if environment is None else environment),
                    argv=argv, expected_exit=status)
    def path(name):
        return os.path.join(work, name)
    steps = [row('capability-compile', 'compile', [*cc, '-Werror=attributes',
                 '-Werror=implicit-function-declaration', '-c', path('capabilities.c'), '-o', path('capabilities.o')]),
             row('capability-link', 'link', [*ld, path('capabilities.o'), *ldflags, '-o', path('capabilities')]),
             row('capability-run', 'run', [path('capabilities')])]
    objects = []
    for name in manifest['sources']:
        obj = path(Path(name).stem + '.o')
        objects.append(obj)
        steps.append(row('compile-' + Path(name).stem, 'compile', [*cc, '-DHAVE_CONFIG_H',
            '-DYYPATCH=' + version, '-I', work, '-I', source, '-c', os.path.join(source, name), '-o', obj]))
    steps.append(row('link-yacc', 'link', [*ld, *objects, *ldflags, '-o', path('yacc')]))
    for name in ('generate', 'regenerate'):
        steps.append(row(name, 'generator', [path('yacc'), *manifest['generator_args']]))
    steps.extend([row('compile-parser', 'compile', [*cc, '-I', work, '-c', path('parser.tab.c'), '-o', path('parser.o')]),
                  row('link-parser', 'link', [*ld, path('parser.o'), *ldflags, '-o', path('parser')])])
    for case in manifest['runtime_cases']:
        command = literal_stdin(case['stdin']) + ' | ' + shlex.join([path('parser'), *case['args']])
        command += ' >' + shlex.quote(path(case['name'] + '.stdout'))
        command += ' 2>' + shlex.quote(path(case['name'] + '.stderr'))
        steps.append(row('case-' + case['name'], 'program-case', [*commands['sh'], '-c', command], case['exit']))
    steps.append(row('runtime-observation', 'run', [path('yacc'), '-V'],
                     environment=dict(env, DYLD_PRINT_LIBRARIES='1')))
    return steps


def byacc_outputs(plan):
    manifest = byacc_manifest(plan)
    return [*byacc_sources(plan), 'capabilities.o', 'capabilities', 'config.h',
            *(Path(name).stem + '.o' for name in manifest['sources']), 'yacc',
            *manifest['generated'], 'parser.o', 'parser',
            *(case['name'] + suffix for case in manifest['runtime_cases'] for suffix in ('.stdout', '.stderr'))]


def verify_byacc_step(step, work, plan, actual_exit, completed):
    manifest = byacc_manifest(plan)
    if step['id'] == 'capability-run':
        raw = configuration_bytes(plan, [*completed, dict(step, actual_exit=actual_exit)])
        path = os.path.join(work, 'config.h')
        with open(path, 'xb') as output:
            output.write(raw)
        return dict(configuration_header=dict(path=path, sha256=sha256_file(path)))
    if any(row['id'] == 'capability-run' for row in completed):
        path = os.path.join(work, 'config.h')
        regular_file(path)
        _require(Path(path).read_bytes() == configuration_bytes(plan, completed),
                 'Byacc configuration changed after capability observation')
    if step['phase'] == 'generator':
        generated = {}
        for name in manifest['generated']:
            path = os.path.join(work, name)
            regular_file(path)
            _require(0 < Path(path).stat().st_size <= 2 * 1024 * 1024, 'Byacc generated output has invalid size')
            generated[name] = dict(path=path, sha256=sha256_file(path))
        if step['id'] == 'regenerate':
            previous = next(row for row in completed if row['id'] == 'generate')
            _require(generated == previous['generated'], 'Byacc generation is not byte reproducible')
        return dict(generated=generated)
    generated = next((row['generated'] for row in completed if row['id'] == 'regenerate'), None)
    if generated is not None:
        for reference in generated.values():
            regular_file(reference['path'])
            _bytes(reference)
    if step['phase'] != 'program-case':
        return {}
    case = next(row for row in manifest['runtime_cases'] if step['id'] == 'case-' + row['name'])
    streams = {}
    for name in ('stdout', 'stderr'):
        path = os.path.join(work, case['name'] + '.' + name)
        regular_file(path)
        reference = dict(path=path, sha256=sha256_file(path))
        _require(_bytes(reference) == case[name].encode('utf-8'), 'Byacc generated parser ' + name + ' differs')
        streams[name] = reference
    return streams


def validate_byacc_plan(plan, descriptor, commands, flags):
    from .bootstrap_pilot import _validate_resources
    from .bootstrap_producer import PRODUCER_FORMAT
    _envelope(plan, PRODUCER_FORMAT, scope='gcc16-bootstrap')
    _require(set(plan) == {'format', 'version', 'scope', 'stage', 'seed_descriptor', 'host', 'sources',
        'work_root', 'required_roles', 'resources', 'program', 'commands'} and plan['stage'] == STAGE and
        plan['host'] == descriptor['host'] and plan['sources'] == descriptor['sources'] and
        descriptor['host']['os'] == 'Darwin', 'unsupported initial Byacc plan')
    for value in plan['sources'].values():
        _source(value)
    _validate_resources(plan['resources'])
    value = plan['program']
    _require(set(value) == {'source', 'owner', 'manifest', 'archive', 'patches'}, 'unsupported Byacc inputs')
    manifest = byacc_manifest(plan)
    owner, _ = _tree(value['owner'], descriptor, owner=True)
    _require({name for name, row in owner.items() if row['kind'] == 'file'} == set(OWNER_FILES),
             'Byacc needs its exact complete owner roster')
    patches = [dict(path=os.path.join(value['owner']['root'], RECIPE, 'patches', name),
                    sha256=owner[RECIPE + '/patches/' + name]['sha256']) for name in manifest['patches']]
    _require(value['patches'] == patches, 'Byacc canonical patches are missing or reordered')
    distinfo = Path(value['owner']['root'], RECIPE, 'distinfo').read_text()
    data = _bytes(value['archive'], limit=2 * 1024 * 1024)
    archive_name = manifest['release'] + '.tgz'
    for name, digest in (('BLAKE2s', hashlib.blake2s(data).hexdigest()), ('SHA512', hashlib.sha512(data).hexdigest())):
        _require(re.findall(r'^' + name + r' \(' + re.escape(archive_name) + r'\) = ([0-9a-f]+)$', distinfo, re.M) == [digest],
                 'Byacc archive differs from canonical distinfo')
    _require(re.findall(r'^Size \(' + re.escape(archive_name) + r'\) = ([0-9]+) bytes$', distinfo, re.M) == [str(len(data))],
             'Byacc archive size differs')
    for patch in patches:
        _require(re.findall(r'^SHA1 \(' + re.escape(Path(patch['path']).name) + r'\) = ([0-9a-f]+)$', distinfo, re.M) ==
                 [_patch_sha1(_bytes(patch))], 'Byacc patch differs from distinfo')
    inventory, _ = _tree(value['source'], descriptor, owner=False)
    expected = source_members(plan)
    _require({name: row['sha256'] for name, row in inventory.items() if row['kind'] == 'file'} == expected,
             'Byacc needs every exact release member and both canonical patched outputs')
    _require(entry(value['source']['root']).st_mode & 0o222 == 0 and
             all(row['mode'] & 0o222 == 0 for row in inventory.values()), 'Byacc source must be read-only')
    required = {'cc', 'sh', *descriptor['driver_components'].values()}
    _require(isinstance(plan['required_roles'], list) and len(plan['required_roles']) == len(set(plan['required_roles'])) and
             required <= set(plan['required_roles']) <= set(commands), 'Byacc selected roles are incomplete')
    _require(plan['commands'] == byacc_steps(plan, descriptor, commands, flags), 'Byacc finite command changed')
    return plan


def verify_byacc_history(plan, result):
    """Revalidate retained configuration and generated source, not a retired seed."""
    rows, outputs = result['commands'], result['outputs']
    work = plan['work_root']
    config = next(row for row in rows if row['id'] == 'capability-run')['configuration_header']
    _require(config == dict(path=os.path.join(work, 'config.h'), sha256=outputs['config.h']) and
             _bytes(config) == configuration_bytes(plan, rows), 'historical Byacc capabilities differ')
    first = next(row for row in rows if row['id'] == 'generate')['generated']
    second = next(row for row in rows if row['id'] == 'regenerate')['generated']
    _require(first == second and set(first) == set(byacc_manifest(plan)['generated']),
             'historical Byacc regeneration differs')
    for name, reference in first.items():
        _require(reference == dict(path=os.path.join(work, name), sha256=outputs[name]) and bool(_bytes(reference)),
                 'historical generated parser differs from recorded outputs')


def history_origins(plan):
    manifest = byacc_manifest(plan)
    source = plan['program']['owner']['source']
    archives = [dict(url='https://invisible-island.net/archives/byacc/' + manifest['release'] + '.tgz',
                     file=plan['program']['archive'])]
    patches = [dict(url=source['repository'] + '/blob/' + source['commit'] + '/' + RECIPE +
                       '/patches/' + Path(row['path']).name, file=row) for row in plan['program']['patches']]
    return archives, patches
