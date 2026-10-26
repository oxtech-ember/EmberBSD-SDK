# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Source-bound initial NetBSD nawk. No make, generator, or system shell."""

import json
import os
from pathlib import Path
import shlex

from .bootstrap_binding import seed_commands
from .bootstrap_gnu_program import _tree
from .bootstrap_netbsd_gcc16 import load_pkgsrc_bootstrap_tools
from .bootstrap_shell import literal_stdin
from .inventory import sha256_file
from .native_toolset import _absolute, _bytes, _envelope, _json, _require, _source
from .paths import entry, regular_file

STAGE = 'nawk-bootstrap'
PROFILE = 'netbsd11-amd64-nawk-initial-v1'
RECIPE = 'pkgsrc/lang/nawk'
MANIFEST_RELATIVE = RECIPE + '/bootstrap.json'
MANIFEST_SHA256 = '205ad213f2a22800dcb101fa7c98f8cf6835b2b13adab48fcce4cc674407eb24'
SOURCES = tuple(RECIPE + '/files/' + name + '.c' for name in
                ('awkgram.tab', 'b', 'main', 'parse', 'proctab', 'tran', 'lib', 'run', 'lex'))
HEADERS = tuple(RECIPE + '/files/' + name for name in
                ('awk.h', 'awkgram.tab.h', 'proto.h', 'command-shell.h'))
PROVENANCE = tuple(RECIPE + '/' + name for name in
    ('Makefile', 'SOURCE.json', 'files/GENERATED.json', 'files/Makefile', 'files/LICENSE',
     'files/README.pkgsrc', 'files/awkgram.y', 'files/maketab.c'))
PROBE = RECIPE + '/bootstrap/command-shell-probe.c'


def is_initial_plan(plan):
    return (plan.get('format') == 'emberbsd-native-gcc16-producer-plan' and
            plan.get('stage') == STAGE and plan.get('netbsd_profile') == PROFILE)


def selected_shell(plan, *, inspect_inputs=True):
    reference = plan['program']['pkgsrc_tools_acceptance']
    _require(isinstance(reference, dict) and set(reference) == {'path', 'sha256'},
             'nawk needs the exact independent pkgsrc shell acceptance')
    selected = load_pkgsrc_bootstrap_tools(reference['path'],
        acceptance_sha256=reference['sha256'], inspect_inputs=inspect_inputs)
    return selected.command('sh')


def manifest(plan):
    value = plan['program']
    _require(value['manifest'] == dict(path=os.path.join(value['source']['root'], MANIFEST_RELATIVE),
                                      sha256=MANIFEST_SHA256), 'unsupported fixed nawk manifest')
    data = _json(value['manifest'])
    _require(data['format'] == 'emberbsd-native-bootstrap-program' and
        type(data['version']) is int and data['version'] == 1 and data['name'] == 'nawk' and
        data['release'] == 'nawk-20260426' and data['initial_only'] is True and
        data['compiler'] == dict(family='gcc', major=16, abi='native') and
        data['canonical_host_recipe'] == RECIPE + '/Makefile' and
        data['source_makefile'] == RECIPE + '/files/Makefile' and
        data['sources'] == list(SOURCES) and data['headers'] == list(HEADERS) and
        data['provenance'] == list(PROVENANCE) and data['probe'] == PROBE and
        data['include_dirs'] == [RECIPE + '/files'] and data['cppflags'] == ['-DYYMAXDEPTH=300'] and
        all(data[key] == [] for key in ('cflags', 'link_flags')) and data['libraries'] == ['m'] and
        data['selected_command_shell'] == dict(role='sh', macro='NAWK_COMMAND_SHELL') and
        data['invocations'] == ['nawk', 'awk'] and data['files'] == {},
        'unsupported fixed nawk source contract')
    from .bootstrap_program import _literal_cases
    _literal_cases(dict(data, invocations=['nawk', 'command-shell-probe']), 8)
    return data


def _source_inputs(plan, descriptor):
    data = manifest(plan)
    inventory, _ = _tree(plan['program']['source'], descriptor, owner=True)
    files = {name for name, row in inventory.items() if row['kind'] == 'file'}
    _require(files == {*SOURCES, *HEADERS, *PROVENANCE, PROBE, MANIFEST_RELATIVE},
             'nawk needs its exact source, generated inputs, license, and provenance roster')
    root = Path(plan['program']['source']['root'])
    generated = json.loads((root / RECIPE / 'files/GENERATED.json').read_text(encoding='utf-8'))
    expected = dict(inputs={'awkgram.y', 'awk.h', 'proto.h', 'maketab.c'},
                    outputs={'awkgram.tab.c', 'awkgram.tab.h', 'proctab.c'})
    for kind, names in expected.items():
        _require(set(generated[kind]) == names and all(generated[kind][name] ==
            inventory[RECIPE + '/files/' + name]['sha256'] for name in names),
            'nawk checked-in parser or generator inputs differ from GENERATED.json')
    _require(entry(root).st_mode & 0o222 == 0 and all(row['mode'] & 0o222 == 0 for row in inventory.values()),
             'nawk source snapshot must be read-only')
    return data


def steps(plan, descriptor, commands, flags):
    data, work = manifest(plan), _absolute(plan['work_root'])
    source = _absolute(plan['program']['source']['root'])
    shell = commands['sh']
    _require(len(shell) == 1 and _absolute(shell[0]) == shell[0] and
             all(char not in shell[0] for char in ('"', '\\', '\n', '\r')),
             'nawk command shell must be one literal absolute C string path')
    # Compiler components use absolute accepted routes. Semantic children use
    # only shell builtins, apart from the deliberate missing-command case.
    env = dict(PATH=os.path.join(work, 'no-tools'), LC_ALL='C', TZ='UTC', TMPDIR=work,
               SHELL=shell[0], CONFIG_SHELL=shell[0])
    cc = [*commands['cc'], *flags['cppflags'], *flags['cflags'], '-v',
          '-DYYMAXDEPTH=300', '-DNAWK_COMMAND_SHELL="' + shell[0] + '"',
          '-I', os.path.join(source, RECIPE, 'files')]
    def row(name, phase, argv, status=0):
        return dict(id=name, phase=phase, cwd=work, env=dict(env), argv=argv, expected_exit=status)
    def path(name):
        return os.path.join(work, name)
    result, objects = [], []
    for relative in [*data['sources'], data['probe']]:
        name = Path(relative).stem
        output = path(name + '.o')
        if relative != data['probe']:
            objects.append(output)
        result.append(row('compile-' + name, 'compile', [*cc, '-c', os.path.join(source, relative), '-o', output]))
    for name, selected_objects, libraries in (('nawk', objects, ['-lm']),
                                             ('command-shell-probe', [path('command-shell-probe.o')], [])):
        result.append(row('link-' + name, 'link', [*commands['cc'], *flags['cflags'], '-v',
            *selected_objects, *flags['ldflags'], *libraries, '-o', path(name)]))
    for case in data['runtime_cases']:
        command = literal_stdin(case['stdin']) + ' | ' + shlex.join([path(case['invocation']), *case['args']])
        command += ' >' + shlex.quote(path(case['name'] + '.stdout'))
        command += ' 2>' + shlex.quote(path(case['name'] + '.stderr'))
        result.append(row('case-' + case['name'], 'program-case', [*shell, '-c', command], case['exit']))
    return result


def outputs(plan):
    data = manifest(plan)
    return [*(Path(name).stem + '.o' for name in data['sources']), 'nawk',
            'command-shell-probe.o', 'command-shell-probe',
            *(case['name'] + suffix for case in data['runtime_cases'] for suffix in ('.stdout', '.stderr'))]


def verify(step, work, plan):
    data = manifest(plan)
    if step['id'] == 'link-nawk':
        os.symlink('nawk', os.path.join(work, 'awk'))
        return dict(invocation_aliases={'awk': 'nawk'})
    if step['phase'] != 'program-case':
        return {}
    case = next(row for row in data['runtime_cases'] if step['id'] == 'case-' + row['name'])
    streams = {}
    for name in ('stdout', 'stderr'):
        path = os.path.join(work, case['name'] + '.' + name)
        regular_file(path)
        reference = dict(path=path, sha256=sha256_file(path))
        _require(_bytes(reference) == case[name].encode('utf-8'), 'nawk actual ' + name + ' differs')
        streams[name] = reference
    return streams


def validate_plan(plan, descriptor, commands, flags, *, inspect_shell=True):
    from .bootstrap_pilot import _validate_resources
    _envelope(plan, 'emberbsd-native-gcc16-producer-plan', scope='gcc16-bootstrap')
    _require(set(plan) == {'format', 'version', 'scope', 'stage', 'netbsd_profile', 'seed_descriptor',
        'host', 'sources', 'work_root', 'required_roles', 'resources', 'program', 'commands'} and
        is_initial_plan(plan) and plan['host'] == descriptor['host'] == dict(os='NetBSD', cpu='amd64') and
        plan['sources'] == descriptor['sources'] and
        descriptor.get('admission') == 'netbsd11-amd64-first-native-v1',
        'nawk initial producer requires the narrow NetBSD first-native profile')
    _require(set(plan['program']) == {'source', 'manifest', 'pkgsrc_tools_acceptance', 'shell_runtime'},
             'unsupported initial nawk inputs')
    for source in plan['sources'].values():
        _source(source)
    _validate_resources(plan['resources'])
    _source_inputs(plan, descriptor)
    _require(tuple(commands['sh']) == selected_shell(plan, inspect_inputs=inspect_shell),
             'nawk must use exactly the independently accepted pkgsrc shell')
    from .bootstrap_nawk_runtime import validate_shell_runtime
    validate_shell_runtime(plan, descriptor, inspect_inputs=inspect_shell)
    required = {'cc', 'sh', *descriptor['driver_components'].values()}
    _require(isinstance(plan['required_roles'], list) and
             len(plan['required_roles']) == len(set(plan['required_roles'])) and
             set(plan['required_roles']) == required and required <= set(commands),
             'nawk requires only its compiler, driver components, and selected shell')
    _require(entry(os.path.join(plan['work_root'], 'no-tools')) is None,
             'nawk semantic PATH must contain no tools')
    _require(plan['commands'] == steps(plan, descriptor, commands, flags),
             'nawk command differs from its source-owned finite route')
    return plan


def make_plan(descriptor_reference, source_reference, pkgsrc_reference, shell_runtime_reference, *, work_root, resources):
    """Prepare finite commands. This does not accept the seed or run a child."""
    descriptor, source = _json(descriptor_reference), _json(source_reference)
    _envelope(descriptor, 'emberbsd-bootstrap-seed', scope='gcc16-bootstrap')
    commands = seed_commands(descriptor)
    plan = dict(format='emberbsd-native-gcc16-producer-plan', version=1, scope='gcc16-bootstrap',
        stage=STAGE, netbsd_profile=PROFILE, seed_descriptor=descriptor_reference,
        host=descriptor['host'], sources=descriptor['sources'], work_root=_absolute(work_root),
        required_roles=sorted({'cc', 'sh', *descriptor['driver_components'].values()}), resources=resources,
        program=dict(source=source, manifest=dict(path=os.path.join(source['root'], MANIFEST_RELATIVE),
                     sha256=MANIFEST_SHA256), pkgsrc_tools_acceptance=pkgsrc_reference, shell_runtime=shell_runtime_reference))
    commands['sh'] = selected_shell(plan)
    plan['commands'] = steps(plan, descriptor, commands, descriptor['abi_flags'])
    return validate_plan(plan, descriptor, commands, descriptor['abi_flags'])


def read_inputs(plan):
    """Expose the selected shell producer's complete read fences to the runner."""
    receipt = _json(plan['program']['pkgsrc_tools_acceptance'])
    documents = [receipt]
    for key in ('plan', 'result', 'output_inventory'):
        documents.append(_json(receipt[key]))
    selected = documents[1]
    runtime = _json(plan['program']['shell_runtime'])
    documents.append(runtime)
    paths = {selected['work_root'], *runtime['nodes']}
    for row in runtime['nodes'].values():
        for key in ('interpreter', 'interpreter_target'):
            if row[key] is not None:
                paths.add(_absolute(row[key]))
    return sorted(paths), documents
