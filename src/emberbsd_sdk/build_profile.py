# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Build profiles and the build plan of a profile.

A profile selects a platform, an OS composition and Ports roots. It contains
no paths and no versions. The plan finds the closure of a profile and writes
one document: the nodes, their input keys, the reasons that include them and
a decision for each node. The plan does not build and does not write build
results. The OS supplies the dependency facts: the makefile guards of its
prepared sources, its native-tool contract and ember/shared-sources.json.
"""

import hashlib
import os
from pathlib import Path
import re
import sys
import tempfile

from . import prepared_sources, process
from .errors import CommandError, SDKError
from .jsonio import json_bytes, loads_json, read_json
from .targets import target_definition

PROFILE_SCHEMA = 'emberbsd.build-profile.v1'
PLAN_SCHEMA = 'build-graph-v2'
SHARED_SOURCES = 'ember/shared-sources.json'
SHARED_TOOL = 'ember/tools/shared-sources.py'
NATIVE_CONTRACT = 'tools/native-tools/contract'
COMPILERS = ('clang', 'gcc')
# The Ports source producers share these helpers.
PORTS_PRODUCER_HELPERS = ('scripts/source-inputs/common', 'scripts/prepared_receipt.py',
                          'scripts/pkgsrc-source-identity.py', 'pkgsrc/lang/llvm/files/sdk_source.py')
# The make flags of the tools build in ember/build-kernel.sh, which builds them
# with the target compiler of the host prefix.
KERNEL_TOOLS_FLAGS = ('TOOLCHAIN_MISSING=yes', 'MKCROSSGDB=no')
# The external Clang toolchain programs that an OS build uses.
TARGET_REQUIRED = ('bin/clang', 'bin/ld.lld')
TARGET_TOOLCHAIN = ('bin/clang', 'bin/clang++', 'bin/ld.lld', 'bin/llvm-ar', 'bin/llvm-nm',
                    'bin/llvm-objcopy', 'bin/llvm-objdump', 'bin/llvm-ranlib', 'bin/llvm-readelf',
                    'bin/llvm-strip')
_CONFIG = re.compile(r'[A-Za-z0-9_]+\Z')
_SET = re.compile(r'[a-z][a-z0-9-]*\Z')
_MK = re.compile(r'MK[A-Z0-9_]+\Z')
# The producer names its package in one of these forms.
_PRODUCER_PACKAGE = (re.compile(r"^PACKAGE = '([^']+)'", re.M),
                     re.compile(r"(?:NetworkOwner\(|__init__\()__file__, '([^']+)'"))


def _require(condition, message):
    if not condition:
        raise SDKError(message)


def _key(value):
    return hashlib.sha256(json_bytes(value)).hexdigest()


def _names(value, pattern, label):
    _require(isinstance(value, list) and all(isinstance(item, str) and pattern.match(item) for item in value)
             and len(set(value)) == len(value), f'profile {label} must be a list of unique names')
    return sorted(value)


def validate_profile(value):
    """Return the normalized profile. Refuse unknown fields, paths and versions."""
    _require(isinstance(value, dict) and set(value) == {'schema', 'platform', 'os', 'ports'} and
             value['schema'] == PROFILE_SCHEMA, f'profile must be {PROFILE_SCHEMA} with platform, os and ports')
    platform, system, ports = value['platform'], value['os'], value['ports']
    _require(isinstance(platform, dict) and set(platform) <= {'target', 'board', 'compiler'} and
             {'target', 'compiler'} <= set(platform), 'profile platform needs target and compiler')
    target_definition(platform['target'])
    compiler = platform['compiler']
    _require(isinstance(compiler, dict) and set(compiler) == {'host', 'target'} and
             compiler['host'] in COMPILERS and compiler['target'] in COMPILERS,
             'profile compiler needs host and target families: clang or gcc')
    if 'board' in platform:
        _require(isinstance(platform['board'], str) and _SET.match(platform['board']), 'invalid profile board')
    _require(isinstance(system, dict) and set(system) <= {'kernels', 'sets', 'mk', 'buildinfo'},
             'profile os accepts kernels, sets, mk and buildinfo')
    kernels = _names(system.get('kernels', []), _CONFIG, 'os.kernels')
    sets = _names(system.get('sets', []), _SET, 'os.sets')
    mk = system.get('mk', {})
    _require(isinstance(mk, dict) and all(_MK.match(name) and value in ('yes', 'no') for name, value in mk.items()),
             'profile os.mk maps MK* names to yes or no')
    buildinfo = system.get('buildinfo', '')
    _require(isinstance(buildinfo, str) and not any(ord(c) < 32 for c in buildinfo),
             'profile os.buildinfo must be one line of text')
    _require(kernels or sets, 'profile os selects no kernel and no set')
    _require(isinstance(ports, dict) and set(ports) <= {'sets', 'packages'}, 'profile ports accepts sets and packages')
    _require(not ports.get('sets') and not ports.get('packages'),
             'Ports roots join the build plan in stage 4 (E17); plan Ports with "plan" or "source-plan"')
    return dict(schema=PROFILE_SCHEMA, platform=dict(platform, compiler=dict(compiler)),
                os=dict(kernels=kernels, sets=sets, mk=dict(sorted(mk.items())), buildinfo=buildinfo),
                ports={})


def _git(root, *arguments):
    try:
        data = process.capture(['git', '-C', root, *arguments], env=process.git_environment()).stdout
        return data.decode('utf-8').rstrip('\n')
    except CommandError as exc:
        raise SDKError(f'cannot read Git state of {root}') from exc
    except UnicodeError as exc:
        raise SDKError(f'Git reports a name in {root} that is not UTF-8') from exc


def checkout(root):
    """Return the Git identity of a checkout: commit, tree, commit time and changed paths."""
    root = os.path.realpath(root)
    _require(_git(root, 'rev-parse', '--show-toplevel') == root, f'{root} is not the top of a Git checkout')
    entries = _git(root, 'status', '--porcelain=v1', '-z', '--untracked-files=normal').split('\0')
    dirty = set()
    while entries:
        entry = entries.pop(0)
        if not entry:
            continue
        dirty.add(entry[3:])
        if entry[0] in 'RC' and entries:
            # A rename or copy also changes its source path, the next entry.
            dirty.add(entries.pop(0))
    return dict(root=root, commit=_git(root, 'rev-parse', 'HEAD'), tree=_git(root, 'rev-parse', 'HEAD^{tree}'),
                time=int(_git(root, 'log', '-1', '--format=%ct', 'HEAD')), dirty=sorted(dirty))


def _tree(identity, path, repository):
    """Git object of PATH at HEAD, and whether PATH has local changes."""
    dirty = any(name == path or name.startswith(path.rstrip('/') + '/') for name in identity['dirty'])
    return dict(repository=repository, path=path, object=_git(identity['root'], 'rev-parse', 'HEAD:' + path),
                dirty=dirty)


def read_shared_sources(os_root):
    """Return ember/shared-sources.json as the OS tool checks and prints it."""
    tool = os.path.join(os_root, SHARED_TOOL)
    _require(os.path.isfile(tool), f'the OS has no {SHARED_TOOL}')
    try:
        data = process.capture([sys.executable, '-I', '-B', tool, 'describe',
                                os.path.join(os_root, SHARED_SOURCES)]).stdout
    except CommandError as exc:
        raise SDKError(exc.stderr.decode('utf-8', 'replace').strip() or 'OS shared sources are invalid') from exc
    return loads_json(data)


def kernel_sources(document, config):
    return sorted(document['kernel']['configs'].get(config, document['kernel']['default']))


def _producer(entry, checkouts):
    """Return the Git objects that identify a source producer and its recipe."""
    repository, path = entry['producer'].split(':', 1)
    owner = checkouts(repository)
    ports = checkouts('ports')
    script = Path(owner['root'], path)
    script = script / 'source.py' if script.is_dir() else script
    if script.is_file():
        named = [match.group(1) for pattern in _PRODUCER_PACKAGE
                 for match in pattern.finditer(script.read_text(encoding='utf-8'))]
        if named and entry['package'] not in named:
            raise SDKError(f'{entry["producer"]} prepares {named[0]}, not {entry["package"]}')
    objects = [_tree(owner, path, repository), _tree(ports, 'pkgsrc/' + entry['package'], 'ports')]
    if repository == 'ports':
        objects += [_tree(ports, helper, 'ports') for helper in PORTS_PRODUCER_HELPERS
                    if Path(ports['root'], helper).exists()]
    return objects


def _owners(prefix, paths):
    """Map each tool below PREFIX to its pkgdb owner. Read pkgdb; run no tool."""
    prefix = os.path.realpath(prefix)
    wanted = {}
    for path in paths:
        for spelling in (path, os.path.realpath(path)):
            relative = os.path.relpath(spelling, prefix)
            _require(not relative.startswith('..'), f'tool is outside the prefix: {path}')
            wanted[relative] = os.path.relpath(path, prefix)
    found = {}
    try:
        for entry in sorted(Path(prefix, 'pkgdb').iterdir()):
            contents = entry / '+CONTENTS'
            if entry.is_symlink() or not contents.is_file():
                continue
            for line in contents.read_text(encoding='utf-8', errors='replace').splitlines():
                if line in wanted:
                    found.setdefault(wanted[line], set()).add(entry)
        result = {}
        for path in sorted(set(wanted.values())):
            owners = found.get(path, set())
            _require(len(owners) == 1, f'host tool has no unique package owner: {path}')
            owner = owners.pop()
            result[path] = dict(package=owner.name, **{
                name.strip('+').lower(): hashlib.sha256((owner / name).read_bytes()).hexdigest()
                for name in ('+CONTENTS', '+BUILD_VERSION')})
        return result
    except OSError as exc:
        raise SDKError(f'cannot read the pkgdb of {prefix}: {exc}') from exc


def result_path(state, node_id):
    """The record of a built node: STATE/results/<node id with ':' as '_'>.json."""
    return Path(state, 'results', node_id.replace(':', '_') + '.json')


def _record(state, node_id, key):
    if state is None or not result_path(state, node_id).is_file():
        return None
    record = read_json(result_path(state, node_id))
    return record if isinstance(record, dict) and record.get('input_key') == key else None


def make_plan(profile, *, os_root, ports_root, sdk_root, host_prefix, host_bfd_config, state=None,
              required_sources=None, native=None, jobs=None):
    """Return the build-graph-v2 plan of PROFILE. Build and write nothing.

    REQUIRED_SOURCES(traversals, flags) -> {VARIABLE: directory} can replace
    the bmake guard walk; NATIVE can replace the loaded host prefix."""
    from .native_os_routing import parse_native_os_contract
    profile = validate_profile(profile)
    target = target_definition(profile['platform']['target'])
    compiler = profile['platform']['compiler']
    _require(compiler['target'] == 'clang', 'the build plan supports the Clang target toolchain only')
    found = {'os': checkout(os_root), 'ports': checkout(ports_root)}

    def checkouts(name):
        # Read the SDK checkout only for an SDK producer: an installed SDK has no Git.
        if name not in found:
            found[name] = checkout(sdk_root)
        return found[name]

    os_identity = found['os']
    shared = read_shared_sources(os_identity['root'])
    try:
        _, rows, roles = parse_native_os_contract(Path(os_identity['root'], NATIVE_CONTRACT).read_bytes())
    except OSError as exc:
        raise SDKError(f'the OS has no {NATIVE_CONTRACT}') from exc
    if native is None:
        from .host_prefix import load_host_prefix
        native = load_host_prefix(host_prefix, required_roles=set(roles) | {'make'},
                                  compiler_family=compiler['host'])
    prefix = os.path.realpath(native.bundle_root)
    mk = [f'{name}={value}' for name, value in profile['os']['mk'].items()]
    if required_sources is None:
        def required_sources(traversals, flags):
            with tempfile.TemporaryDirectory(prefix='emberbsd-plan-') as objects:
                query = prepared_sources.os_query(
                    native, rows, os_identity['root'], objects=objects, tools=os.path.join(objects, 'tools'),
                    machine=target['machine'], arch=target['arch'], timestamp=os_identity['time'],
                    traversals=traversals, flags=flags, env=process.sanitized_environment())
                return prepared_sources.required_sources(os_identity['root'], query, traversals, jobs=jobs)

    nodes = {}

    def add(node_id, kind, identity, inputs, dependencies, outputs, reason):
        if node_id in nodes:
            nodes[node_id]['reasons'].append(reason)
            return node_id
        dirty = [item['repository'] + ':' + item['path'] for item in inputs.get('git', []) if item['dirty']]
        changed = [item for item in sorted(dependencies) if nodes[item]['input_key'] is None]
        key = None
        if dirty:
            # A local edit has no reusable identity. Stage 3 records no result for it.
            decision = dict(action='build', why='changed worktree inputs: ' + ', '.join(dirty))
        elif changed:
            decision = dict(action='build', why='changed dependencies: ' + ', '.join(changed))
        else:
            key = _key(dict(id=node_id, identity=identity, inputs=inputs,
                            dependencies={item: nodes[item]['input_key'] for item in sorted(dependencies)}))
            if _record(state, node_id, key) is not None:
                decision = dict(action='ready', why='recorded result has this input key')
            elif kind == 'runtime':
                decision = dict(action='ready', why='installed outside the build plan')
            else:
                decision = dict(action='build', why='no recorded result has this input key')
        nodes[node_id] = dict(id=node_id, kind=kind, identity=identity, input_key=key, inputs=inputs,
                              dependencies=sorted(dependencies), reasons=[reason] if reason else [],
                              decision=decision, outputs=outputs)
        return node_id

    tools = [native.tool(role) for role in sorted(native.tools)]
    host = add('runtime:host-prefix', 'runtime', dict(compiler=compiler['host']),
               dict(host=dict(native.host),
                    abi_flags={name: list(flags) for name, flags in sorted(dict(native.abi_flags).items())},
                    packages=_owners(prefix, tools)),
               [], [], 'profile platform.compiler.host')
    _require(all(os.path.exists(os.path.join(prefix, path)) for path in TARGET_REQUIRED),
             'the host prefix has no Clang target toolchain (bin/clang and bin/ld.lld)')
    toolchain = add('runtime:target-toolchain', 'runtime', dict(compiler=compiler['target']),
                    dict(packages=_owners(prefix, [os.path.join(prefix, path) for path in TARGET_TOOLCHAIN
                                                   if os.path.exists(os.path.join(prefix, path))])),
                    [], [], 'profile platform.compiler.target')
    bfd = Path(host_bfd_config)
    record = bfd.with_name('inputs.txt')
    _require(bfd.is_absolute() and bfd.is_file() and record.is_file(),
             'select the host-bfd.mk of the common Binutils build, next to its inputs.txt')
    binutils = add('runtime:host-binutils', 'runtime', dict(producer='common Ports Binutils'),
                   dict(inputs_sha256=hashlib.sha256(record.read_bytes()).hexdigest()), [], [],
                   'needed by os:tools (EMBER_HOST_BFD_CONFIG)')

    def source(variable, reason):
        entry = shared['sources'][variable]
        return add('source:' + variable, 'source', dict(variable=variable, package=entry['package'],
                                                       producer=entry['producer']),
                   dict(git=_producer(entry, checkouts)), [], [variable], reason)

    os_inputs = dict(git=[dict(repository='os', path='.', object=os_identity['tree'],
                               dirty=bool(os_identity['dirty']))],
                     target=dict(name=profile['platform']['target'], **target),
                     mk=profile['os']['mk'], repro_timestamp=os_identity['time'])
    release_inputs = dict(os_inputs, buildinfo=profile['os']['buildinfo'])

    def stage(node_id, identity, traversals, flags, extra, outputs, reason, inputs):
        needed = required_sources(traversals, flags) if traversals else {}
        names = sorted(set(needed) | set(extra))
        _require(set(names) <= set(shared['sources']),
                 'OS needs a source that ember/shared-sources.json does not name: ' +
                 ' '.join(sorted(set(names) - set(shared['sources']))))
        dependencies = [host, toolchain] + [
            source(name, f'needed by {node_id}' + (f' ({needed[name]})' if name in needed else ''))
            for name in names]
        if node_id == 'os:tools':
            dependencies.append(binutils)
        else:
            dependencies.append(tools_stage)
            nodes[tools_stage]['reasons'].append('needed by ' + node_id)
        return add(node_id, 'os-stage', identity, dict(inputs, flags=list(flags)), dependencies, outputs, reason)

    tools_stage = stage('os:tools', dict(stage='tools'), prepared_sources.TOOLS_TRAVERSALS,
                        [*KERNEL_TOOLS_FLAGS, *mk], [], ['tools'], None, os_inputs)
    for config in profile['os']['kernels']:
        _require(Path(os_identity['root'], 'sys/arch', target['machine'], 'conf', config).is_file(),
                 f'kernel configuration {config} is missing for {target["machine"]}')
        stage('os:kernel:' + config, dict(stage='kernel', config=config), (), [*KERNEL_TOOLS_FLAGS, *mk],
              kernel_sources(shared, config), [f'netbsd-{config}', f'netbsd-{config}.img'],
              'profile os.kernels', release_inputs)
    if profile['os']['sets']:
        from .os_release_builder import flags_for
        flags = [flag for flag in flags_for(profile['platform']['target'], compiler['target'])
                 if flag.split('=', 1)[0] not in profile['os']['mk']] + mk
        stage('os:sets', dict(stage='sets', sets=profile['os']['sets']), prepared_sources.RELEASE_TRAVERSALS,
              flags, [], [name + '.tar.xz' for name in profile['os']['sets']], 'profile os.sets', release_inputs)

    order, waves, done = sorted(nodes), [], set()
    while len(done) < len(order):
        wave = [item for item in order if item not in done and set(nodes[item]['dependencies']) <= done]
        _require(wave, 'build plan has a dependency cycle')
        waves.append(wave)
        done.update(wave)
    plan = dict(schema=PLAN_SCHEMA, profile=profile, profile_sha256=_key(profile),
                checkouts={name: dict(commit=item['commit'], tree=item['tree'], dirty=item['dirty'])
                           for name, item in sorted(found.items())},
                nodes=[nodes[item] for wave in waves for item in wave], waves=waves)
    plan['plan_sha256'] = _key(plan)
    return plan


def load_profile(path):
    return validate_profile(read_json(path))


def summary(plan):
    actions = {}
    for node in plan['nodes']:
        actions.setdefault(node['decision']['action'], []).append(node['id'])
    return '; '.join(f'{action} {len(ids)}' for action, ids in sorted(actions.items()))
