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
import shutil
import sys
import tempfile

from . import libedit, prepared_sources, process
from .graph import dependency_waves
from .errors import CommandError, SDKError
from .jsonio import json_bytes, loads_json, read_json, write_json
from .targets import target_definition

PROFILE_SCHEMA = 'emberbsd.build-profile.v1'
PLAN_SCHEMA = 'build-graph-v2'
SHARED_SOURCES = 'ember/shared-sources.json'
SHARED_TOOL = 'ember/tools/shared-sources.py'
NATIVE_CONTRACT = 'tools/native-tools/contract'
COMPILERS = ('clang', 'gcc')
# The Ports source producers share these helpers.
PORTS_PRODUCER_HELPERS = ('scripts/source-inputs/common', 'scripts/prepared_receipt.py', 'scripts/source_distdir.py',
                          'scripts/pkgsrc-source-identity.py', 'pkgsrc/lang/llvm/files/sdk_source.py')
# Ports producers with the common interface; they use the shared export.
COMMON_PRODUCERS = 'ports:scripts/source-inputs/'
# The SDK prepares libedit with this module, not through the script.
LIBEDIT = 'sdk:scripts/prepare-libedit.py'
LIBEDIT_MODULE = 'src/emberbsd_sdk/libedit.py'
# Ports tools: the current recipe of a source, and the shared pkgsrc export.
RECIPE_TOOL = 'scripts/prepared-source-current.py'
EXPORT_TOOL = 'scripts/pkgsrc-export-stage.py'
EXPORT_NODE = 'ports:pkgsrc-export'
# The make flags of the tools build in ember/build-kernel.sh, which builds them
# with the target compiler of the host prefix.
KERNEL_TOOLS_FLAGS = ('TOOLCHAIN_MISSING=yes', 'MKCROSSGDB=no')
# SDK-side files read by the profile node, ember/build-kernel.sh and the two
# native runners, including the per-host child launchers and bindings snapshot.
OS_SCRIPTS = ('scripts/packages.py', 'scripts/run-native-check.py', 'scripts/run-native-os.py')
HOST_MODULES = (
    '__init__', '_darwin_process', '_linux_process', '_linux_supervisor', '_netbsd_launcher', '_netbsd_process',
    'cpu_capacity', 'elf', 'errors', 'host_identity', 'host_prefix', 'host_services', 'inventory',
    'jsonio', 'native_toolset', 'paths', 'process', 'publication')
SOURCE_MODULES = (*HOST_MODULES, 'archive_metadata', 'provenance', 'sourceprep')
OS_MODULES = (
    *HOST_MODULES, 'build_profile', 'command_directory', 'compiler_bindings', 'graph', 'jobserver', 'native_checks',
    'native_os', 'native_os_routing', 'prefix_lock', 'prepared_sources', 'profile_build', 'sdk_client', 'targets',
    'workspace')
# Extra producers for the release traversal, including the SDK API used by
# the Ports LLVM runtime and target libtool producers.
SETS_MODULES = (
    'application_toolchain', 'libedit', 'llvm_runtime', 'llvm_runtime_install', 'llvm_runtime_sets',
    'llvm_source_refresh', 'llvm_stage_inputs', 'llvm_sysroot', 'native_runtime_install', 'os_release',
    'os_release_builder', 'prepared_refresh', 'provenance', 'runtime_refresh', 'sourceprep', 'sysroot_builder',
    'sysroot_transfer', 'target_toolchain', 'tarstream', 'verification_receipts')
# Outside the pkgsrc export, the Clang release invokes the prepared-source
# validators and the common cross-libtool producer, including their helpers.
PORTS_RELEASE_PATHS = (
    EXPORT_TOOL, RECIPE_TOOL, 'scripts/pkgsrc-source-identity.py',
    'scripts/source-inputs', 'scripts/prepared_receipt.py', 'scripts/source_distdir.py',
    'profiles/common-build-tools/cross',
    'profiles/common-build-tools/patches/cross-libtool-tool-paths.patch')
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


def _file_key(path):
    """Return a key of the file, link or directory at PATH; None if it is missing.

    A file has the sha256 of its bytes. A directory has the name, size and
    modification time of each entry: an untracked build directory can be
    large, and a change of a file changes its time."""
    try:
        if os.path.islink(path):
            return 'link:' + os.readlink(path)
        if os.path.isfile(path):
            with open(path, 'rb') as stream:
                return 'file:' + hashlib.file_digest(stream, 'sha256').hexdigest()
        if os.path.isdir(path):
            entries = []
            for directory, names, files in os.walk(path):
                names.sort()
                for name in sorted(names + files):
                    item = os.lstat(os.path.join(directory, name))
                    entries.append([os.path.relpath(os.path.join(directory, name), path),
                                    item.st_mode, item.st_size, item.st_mtime_ns])
            return 'tree:' + _key(entries)
    except OSError as exc:
        # The key of a path that cannot be read changes when the error changes.
        return f'error:{exc.errno}'
    return None


def content_key(identity):
    """Return a key of the files of a checkout: its Git tree and each changed path.

    A changed submodule has the commit of its checkout: the Ports tools read
    the submodule through Git objects. Ignored files are not in the key."""
    changed = {}
    for path in identity['dirty']:
        full = os.path.join(identity['root'], path.rstrip('/'))
        if os.path.exists(os.path.join(full, '.git')):
            changed[path] = 'commit:' + _git(full, 'rev-parse', 'HEAD')
        else:
            changed[path] = _file_key(full)
    return _key(dict(tree=identity['tree'], changed=changed))


# The SDK code of the plan queries: a change of it gives other results.
_CODE = [os.path.join(os.path.dirname(__file__), name) for name in ('build_profile.py', 'prepared_sources.py',
                                                                      'process.py')]


def _cached(cache, kind, material, compute):
    """Return the result of COMPUTE() that the CACHE directory keeps for MATERIAL.

    The slow plan queries keep their results there, keyed by all of their
    inputs: MATERIAL() returns them, and it also gets the SDK code and the
    Python of the plan. Without CACHE, compute it each time. A result is kept
    only when its inputs did not change while it was computed. Remove the
    directory to remove old results."""
    if cache is None:
        return compute()

    def inputs():
        return loads_json(json_bytes(dict(material(), code=[_file_key(path) for path in _CODE],
                                          python=sys.executable)))
    before = inputs()
    path = Path(cache, f'{kind}-{_key(before)}.json')
    try:
        record = read_json(path)
        if isinstance(record, dict) and record.get('material') == before and 'result' in record:
            return record['result']
    except (OSError, SDKError, ValueError):
        pass
    result = compute()
    if inputs() == before:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, dict(material=before, result=result), exclusive=False)
    return result


def _names(value, pattern, label):
    _require(isinstance(value, list) and all(isinstance(item, str) and pattern.match(item) for item in value)
             and len(set(value)) == len(value), f'profile {label} must be a list of unique names')
    return sorted(value)


def validate_profile(value):
    """Return the normalized profile. Refuse unknown fields, paths and versions."""
    _require(isinstance(value, dict) and set(value) == {'schema', 'platform', 'os', 'ports'} and
             value['schema'] == PROFILE_SCHEMA, f'profile must be {PROFILE_SCHEMA} with platform, os and ports')
    platform, system, ports = value['platform'], value['os'], value['ports']
    _require(isinstance(platform, dict) and set(platform) == {'target', 'compiler'},
             'profile platform needs target and compiler')
    target_definition(platform['target'])
    compiler = platform['compiler']
    _require(isinstance(compiler, dict) and set(compiler) == {'host', 'target'} and
             compiler['host'] in COMPILERS and compiler['target'] in COMPILERS,
             'profile compiler needs host and target families: clang or gcc')
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
             'profile ports must be empty: select Ports roots with --set or --package of the package '
             'client (plan, source-plan or install)')
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
    # git status does not report a submodule without a checkout.
    for line in _git(root, 'submodule', 'status').splitlines():
        state, path = line[:1], line[1:].split()[1]
        _require(state != '-', f'{root}: initialize submodule {path} at its recorded commit')
        if state in '+U':
            dirty.add(path)
    return dict(root=root, commit=_git(root, 'rev-parse', 'HEAD'), tree=_git(root, 'rev-parse', 'HEAD^{tree}'),
                time=int(_git(root, 'log', '-1', '--format=%ct', 'HEAD')), dirty=sorted(dirty))


def _tree(identity, path, repository):
    """Git object of PATH at HEAD, and whether PATH has local changes."""
    dirty = any(name == path or name.startswith(path.rstrip('/') + '/') for name in identity['dirty'])
    return dict(repository=repository, path=path, object=_git(identity['root'], 'rev-parse', 'HEAD:' + path),
                dirty=dirty)


def _git_paths(checkout, repository, paths):
    """Return the selected Git files, trees or gitlinks and their worktree status."""
    paths = set(paths)
    entries = _git(checkout['root'], 'ls-tree', '-z', 'HEAD', '--', *sorted(paths))
    objects = {}
    for entry in entries.split('\0'):
        if entry:
            metadata, path = entry.split('\t', 1)
            mode, _, digest = metadata.split()
            objects[path] = dict(mode=mode, object=digest)
    # status may collapse an untracked directory into a single name.
    dirty = {path for path in paths
             if any(path == name or name.startswith(path + '/') or
                    (name.endswith('/') and path.startswith(name)) for name in checkout['dirty'])}
    return [dict(repository=repository, path=path, dirty=path in dirty,
                 **objects.get(path, dict(mode=None, object=None)))
            for path in sorted(objects.keys() | dirty)]


def _sdk_files(checkout, modules, scripts=()):
    paths = [*scripts, *(f'src/emberbsd_sdk/{name}.py' for name in modules)]
    return _git_paths(checkout, 'sdk', paths)


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
    """Return the Git objects that identify a source producer."""
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
    objects = [_tree(owner, path, repository)]
    if entry['producer'] == LIBEDIT:
        objects.append(_tree(owner, LIBEDIT_MODULE, repository))
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


def _record(state, node_id, key, outputs):
    """Return the record of NODE_ID if it has KEY and its outputs exist."""
    if state is None or not result_path(state, node_id).is_file():
        return None
    record = read_json(result_path(state, node_id))
    if not isinstance(record, dict) or record.get('input_key') != key or not isinstance(record.get('outputs'), dict):
        return None
    # os:tools is a virtual dependency. Each OS producer owns its actual tools.
    expected = set() if node_id == 'os:tools' else set(outputs)
    paths = record['outputs']
    if set(paths) != expected or not all(isinstance(path, str) and os.path.exists(path) for path in paths.values()):
        return None
    return record


def ports_tool(ports_root, sdk_root, prefix, script, *arguments):
    """Run a Ports tool that reads Git objects and prints one JSON document."""
    git = shutil.which('git')
    _require(git is not None, 'install Git on the host')
    command = [sys.executable, '-B', os.path.join(ports_root, script), *arguments, '--sdk-source', sdk_root,
               '--git', git, '--patch', os.path.join(prefix, 'bin/gpatch')]
    try:
        return loads_json(process.capture(command, env=process.sanitized_environment()).stdout)
    except CommandError as exc:
        message = exc.stderr.decode('utf-8', 'replace').strip().splitlines()
        raise SDKError(f'{script}: ' + (message[-1] if message else 'failed')) from exc


def source_manifest_digest(root):
    """Hash the manifest that identifies a prepared source for planning and execution."""
    for name in ('emberbsd-prepared-source.json', libedit.RECEIPT, 'llvm-source.json'):
        manifest = Path(root, name)
        if manifest.is_file():
            return hashlib.sha256(manifest.read_bytes()).hexdigest()
    raise SDKError(f'{root} is not a prepared source: it has no manifest')


class _Planner:
    """The checkouts, OS source list, host prefix and nodes of one plan."""

    def __init__(self, *, os_root, ports_root, sdk_root, host_prefix, host_compiler, target_name, state,
                 selected_sources, required_sources, native, jobs, cache=None):
        from .native_os_routing import parse_native_os_contract
        self.state, self.cache, self.target_name = state, cache, target_name
        self.target = target_definition(target_name)
        self.selected_sources = dict(selected_sources or {})
        self.found = {'os': checkout(os_root), 'ports': checkout(ports_root)}
        self.sdk_root = sdk_root
        self.shared = read_shared_sources(self.found['os']['root'])
        _require(set(self.selected_sources) <= set(self.shared['sources']),
                 'build.sources names unknown sources: ' +
                 ' '.join(sorted(set(self.selected_sources) - set(self.shared['sources']))))
        try:
            _, self.rows, roles = parse_native_os_contract(
                Path(self.found['os']['root'], NATIVE_CONTRACT).read_bytes())
        except OSError as exc:
            raise SDKError(f'the OS has no {NATIVE_CONTRACT}') from exc
        if native is None:
            from .host_prefix import load_host_prefix
            native = load_host_prefix(host_prefix, required_roles=set(roles) | {'make'},
                                      compiler_family=host_compiler)
        self.native, self.jobs = native, jobs
        self.prefix = os.path.realpath(native.bundle_root)
        self.required = required_sources or self.walk
        self.nodes, self.recipes, self.export = {}, {}, []
        self.source_helpers = None

    def checkouts(self, name):
        if name not in self.found:
            self.found[name] = checkout(self.sdk_root)
        return self.found[name]

    def walk(self, traversals, flags):
        """Return the sources that the bmake guards of TRAVERSALS under FLAGS need.

        The result depends on the OS files, the flags, the directories, the
        target and the host make only; the plan cache keeps it."""
        os_identity = self.found['os']

        def compute():
            with tempfile.TemporaryDirectory(prefix='emberbsd-plan-') as objects:
                query = prepared_sources.os_query(
                    self.native, self.rows, os_identity['root'], objects=objects,
                    tools=os.path.join(objects, 'tools'), machine=self.target['machine'], arch=self.target['arch'],
                    timestamp=os_identity['time'], traversals=traversals, flags=flags,
                    env=process.sanitized_environment())
                return prepared_sources.required_sources(os_identity['root'], query, traversals, jobs=self.jobs)
        make = self.native.tool('make')

        def material():
            return dict(os=content_key(os_identity), root=os_identity['root'], time=os_identity['time'],
                        flags=list(flags), traversals=[[path, sorted(targets)] for path, targets in traversals],
                        machine=self.target['machine'], arch=self.target['arch'],
                        make=[make, _file_key(os.path.realpath(make))], rows=self.rows)
        return _cached(self.cache, 'walk', material, compute)

    def ports_tool(self, script, *arguments):
        """Return the output of a Ports tool. It reads the Ports and SDK files and
        the host gpatch only; the plan cache keeps it."""
        ports, sdk = self.found['ports'], self.checkouts('sdk')
        # The tool gets this path of gpatch, and the export fingerprint has it.
        gpatch = os.path.join(self.prefix, 'bin/gpatch')

        def material():
            return dict(script=script, arguments=list(arguments), ports=content_key(ports), sdk=content_key(sdk),
                        roots=[ports['root'], sdk['root']], gpatch=[gpatch, _file_key(os.path.realpath(gpatch))])
        return _cached(self.cache, 'ports-tool', material,
                       lambda: ports_tool(ports['root'], sdk['root'], self.prefix, script, *arguments))

    def add(self, node_id, kind, identity, inputs, dependencies, outputs, reason, ready=None):
        nodes = self.nodes
        if node_id in nodes:
            nodes[node_id]['reasons'].append(reason)
            return node_id
        dirty = [item['repository'] + ':' + item['path'] for item in inputs.get('git', []) if item['dirty']]
        changed = [item for item in sorted(dependencies) if nodes[item]['input_key'] is None]
        key = None
        if dirty:
            # A local edit has no reusable identity; its record never makes it ready.
            decision = dict(action='build', why='changed worktree inputs: ' + ', '.join(dirty))
        elif changed:
            decision = dict(action='build', why='changed dependencies: ' + ', '.join(changed))
        else:
            key = _key(dict(id=node_id, identity=identity, inputs=inputs,
                            dependencies={item: nodes[item]['input_key'] for item in sorted(dependencies)}))
            if ready is not None:
                decision = dict(action='ready', why=ready)
            elif _record(self.state, node_id, key, outputs) is not None:
                decision = dict(action='ready', why='recorded result has this input key')
            elif kind == 'runtime':
                decision = dict(action='ready', why='installed outside the build plan')
            else:
                decision = dict(action='build', why='no recorded result has this input key')
        nodes[node_id] = dict(id=node_id, kind=kind, identity=identity, input_key=key, inputs=inputs,
                              dependencies=sorted(dependencies), reasons=[reason] if reason else [],
                              decision=decision, outputs=outputs)
        return node_id

    def describe(self, names):
        # One Ports call reads the current recipes of a stage.
        packages = sorted({self.shared['sources'][name]['package'] for name in names
                           if name not in self.selected_sources} - set(self.recipes))
        if packages:
            self.recipes.update(self.ports_tool(RECIPE_TOOL, *[word for package in packages
                                                               for word in ('--describe', package)]))

    def pkgsrc_export(self, reason):
        # One shared pkgsrc export for all Ports producers, keyed by the Git
        # fingerprint of its inputs.
        if not self.export:
            fingerprint = self.ports_tool(EXPORT_TOOL, 'fingerprint').get('fingerprint')
            _require(isinstance(fingerprint, str) and re.fullmatch(r'[0-9a-f]{64}', fingerprint),
                     f'{EXPORT_TOOL} printed no fingerprint')
            self.export.append(fingerprint)
        return self.add(EXPORT_NODE, 'export', dict(producer='ports:' + EXPORT_TOOL),
                        dict(fingerprint=self.export[0]), [], ['pkgsrc-export'], reason)

    def source(self, variable, reason):
        entry = self.shared['sources'][variable]
        identity = dict(variable=variable, package=entry['package'], producer=entry['producer'])
        if variable in self.selected_sources:
            digest = source_manifest_digest(self.selected_sources[variable])
            return self.add('source:' + variable, 'source', identity,
                            dict(manifest_sha256=digest), [],
                            [variable], reason, ready='selected in the configuration')
        recipe = self.recipes[entry['package']]
        dependencies = ([self.pkgsrc_export('needed by source:' + variable)]
                        if entry['producer'].startswith(COMMON_PRODUCERS) else [])
        if self.source_helpers is None:
            self.source_helpers = _sdk_files(self.checkouts('sdk'), SOURCE_MODULES)
        git_inputs = [*_producer(entry, self.checkouts), *self.source_helpers]
        # The producer selects its archives in the distfiles directory and
        # names a missing one; the plan does not.
        return self.add('source:' + variable, 'source', dict(identity, distfiles=recipe['distfiles']),
                        dict(recipe=recipe['effective'], git=git_inputs), dependencies,
                        [variable], reason)

    def sources(self, names, reason):
        """Add the source nodes of NAMES; return their node ids."""
        _require(set(names) <= set(self.shared['sources']),
                 'OS needs a source that ember/shared-sources.json does not name: ' +
                 ' '.join(sorted(set(names) - set(self.shared['sources']))))
        self.describe(names)
        return [self.source(name, reason(name)) for name in sorted(names)]

    def document(self, profile):
        nodes = self.nodes
        waves = dependency_waves({name: node['dependencies'] for name, node in nodes.items()})
        plan = dict(schema=PLAN_SCHEMA, profile=profile, profile_sha256=_key(profile),
                    checkouts={name: dict(commit=item['commit'], tree=item['tree'], dirty=item['dirty'])
                               for name, item in sorted(self.found.items())},
                    nodes=[nodes[item] for wave in waves for item in wave], waves=waves)
        plan['plan_sha256'] = _key(plan)
        return plan


def source_plan(*, os_root, ports_root, sdk_root, host_prefix, host_compiler, target, flags, traversals,
                read=(), state=None, cache=None, selected_sources=None, required_sources=None, native=None,
                jobs=None):
    """Return a plan of the prepared sources that an OS build needs. Build nothing.

    FLAGS and TRAVERSALS are the make flags and directories of the build, as
    the prepared_source_profile of its builder names them. The plan also
    selects release.select of ember/shared-sources.json. It has the source
    nodes and the shared pkgsrc export only. READ names the sources that the
    builder reads in all builds. CACHE, REQUIRED_SOURCES and NATIVE are as for
    make_plan."""
    planner = _Planner(os_root=os_root, ports_root=ports_root, sdk_root=sdk_root, host_prefix=host_prefix,
                       host_compiler=host_compiler, target_name=target, state=state,
                       selected_sources=selected_sources, required_sources=required_sources, native=native,
                       jobs=jobs, cache=cache)
    _require('release' in planner.shared, 'the OS has no release.select in ember/shared-sources.json; update the OS')
    needed = planner.required(traversals, flags)
    release = set(planner.shared['release']['select'])

    def reason(name):
        if name in needed:
            return f'needed by the OS build ({needed[name]})'
        return 'needed by the OS build (release.select)' if name in release else 'read by the OS builder'
    planner.sources(set(needed) | release | set(read), reason)
    return planner.document(dict(platform=dict(target=target), os=dict(flags=list(flags))))


def _require_make_options(os_root, mk):
    """Refuse an MK* name that no OS file of share/mk names. make ignores
    an unknown name, but it changes the options of the build: the build then
    uses new object directories and builds all again."""
    if not mk:
        return
    known = set()
    for path in Path(os_root, 'share/mk').glob('*.mk'):
        known.update(re.findall(r'\bMK[A-Z0-9_]+\b', path.read_text(errors='replace')))
    unknown = sorted(set(mk) - known)
    _require(not unknown, 'profile os.mk names options that the OS does not have (share/mk): ' + ', '.join(unknown))


def kernel_config(os_root, machine, directories, name):
    """Return the path of kernel configuration NAME in DIRECTORIES, or None
    when it is a configuration of the OS. Refuse a name in both: the kernel
    objects and outputs are named by it."""
    in_os = Path(os_root, 'sys/arch', machine, 'conf', name).is_file()
    found = [Path(directory, name) for directory in directories or () if Path(directory, name).is_file()]
    _require(not (in_os and found), f'kernel configuration {name} is in the OS and in {found[0].parent if found else ""}: '
             'give the trial another name')
    _require(len(found) <= 1, f'kernel configuration {name} is in more than one of build.kernel_configs')
    _require(in_os or found, f'kernel configuration {name} is missing for {machine}' +
             (' and in build.kernel_configs' if directories else ''))
    return found[0] if found else None


def _directory_digest(directory):
    """SHA256 of the names and bytes of the kernel configurations in DIRECTORY.
    Its other files, such as a manifest or a README, do not change the kernel."""
    digest = hashlib.sha256()
    for path in sorted(Path(directory).iterdir()):
        if path.is_file() and _CONFIG.match(path.name):
            digest.update(path.name.encode() + b'\0' + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def make_plan(profile, *, os_root, ports_root, sdk_root, host_prefix, host_bfd_config, state=None, cache=None,
              selected_sources=None, required_sources=None, native=None, jobs=None, kernel_configs=()):
    """Return the build-graph-v2 plan of PROFILE. Build nothing.

    The CACHE directory keeps the results of the bmake guard walks and the
    Ports tools for the same inputs; the plan writes nothing else.

    SELECTED_SOURCES maps EMBER_*_SOURCE to a prepared tree that the
    configuration selects; the plan does not prepare it. Ports names the
    current recipe and the distfiles of each source. REQUIRED_SOURCES(traversals,
    flags) -> {VARIABLE: directory} can replace the bmake guard walk;
    NATIVE can replace the loaded host prefix. KERNEL_CONFIGS lists directories
    of local trial kernel configurations."""
    profile = validate_profile(profile)
    compiler = profile['platform']['compiler']
    _require(compiler['target'] == 'clang', 'the build plan supports the Clang target toolchain only')
    planner = _Planner(os_root=os_root, ports_root=ports_root, sdk_root=sdk_root, host_prefix=host_prefix,
                       host_compiler=compiler['host'], target_name=profile['platform']['target'], state=state,
                       selected_sources=selected_sources, required_sources=required_sources, native=native,
                       jobs=jobs, cache=cache)
    native, prefix, shared, target = planner.native, planner.prefix, planner.shared, planner.target
    os_identity, add, nodes = planner.found['os'], planner.add, planner.nodes
    _require_make_options(os_identity['root'], profile['os']['mk'])
    mk = [f'{name}={value}' for name, value in profile['os']['mk'].items()]
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
    _require(bfd.is_absolute() and bfd.name == 'host-bfd.mk' and bfd.is_file() and record.is_file(),
             'select the host-bfd.mk of the common Binutils build, next to its inputs.txt')
    binutils = add('runtime:host-binutils', 'runtime', dict(producer='common Ports Binutils'),
                   dict(inputs_sha256=hashlib.sha256(record.read_bytes()).hexdigest()), [], [],
                   'needed by os:tools (EMBER_HOST_BFD_CONFIG)')

    sdk = planner.checkouts('sdk')
    os_inputs = dict(git=[dict(repository='os', path='.', object=os_identity['tree'],
                               dirty=bool(os_identity['dirty'])), *_sdk_files(sdk, OS_MODULES, OS_SCRIPTS)],
                     target=dict(name=profile['platform']['target'], **target),
                     mk=profile['os']['mk'], repro_timestamp=os_identity['time'])
    release_inputs = dict(os_inputs, buildinfo=profile['os']['buildinfo'])

    def stage(node_id, identity, traversals, flags, extra, outputs, reason, inputs):
        needed = planner.required(traversals, flags) if traversals else {}
        dependencies = [host, toolchain] + planner.sources(
            set(needed) | set(extra),
            lambda name: f'needed by {node_id}' + (f' ({needed[name]})' if name in needed else ''))
        if node_id == 'os:tools':
            dependencies.append(binutils)
        else:
            dependencies.append(tools_stage)
            nodes[tools_stage]['reasons'].append('needed by ' + node_id)
        if node_id == 'os:sets':
            # The release verifies and uses the shared export even when all prepared sources were selected.
            dependencies.append(planner.pkgsrc_export('needed by os:sets'))
        return add(node_id, 'os-stage', identity, dict(inputs, flags=list(flags)), dependencies, outputs, reason)

    tools_stage = stage('os:tools', dict(stage='tools'), prepared_sources.TOOLS_TRAVERSALS,
                        [*KERNEL_TOOLS_FLAGS, *mk], [], ['tools'], None, os_inputs)
    for config in profile['os']['kernels']:
        path = kernel_config(os_identity['root'], target['machine'], kernel_configs, config)
        # A configuration outside the OS can include the other configurations of its directory.
        inputs = release_inputs if path is None else dict(release_inputs, kernel_config=_directory_digest(path.parent))
        stage('os:kernel:' + config, dict(stage='kernel', config=config), (), [*KERNEL_TOOLS_FLAGS, *mk],
              kernel_sources(shared, config), [f'netbsd-{config}', f'netbsd-{config}.img'],
              'profile os.kernels', inputs)
    if profile['os']['sets']:
        from .os_release_builder import flags_for, release_sets
        names = sorted(name.removesuffix('.tar.xz') for name in release_sets())
        _require(profile['platform']['target'] == 'aarch64' and compiler['target'] == 'clang',
                 'profile os.sets needs the aarch64 target and Clang: the plan uses the aarch64-sysroot release')
        _require(sorted(profile['os']['sets']) == names,
                 'profile os.sets must be ' + ', '.join(names) + ': the OS release builder makes them together')
        _require(not profile['os']['buildinfo'],
                 'profile os.sets does not accept os.buildinfo yet: the OS release builder sets BUILDINFO itself')
        owned = {'MKCXX', 'MKLIBCXX', 'MKLLVM', 'MKLLVMRT', 'MKX11', 'MKREPRO'} & set(profile['os']['mk'])
        _require(not owned, 'profile os.sets does not accept ' + ', '.join(sorted(owned)) +
                 ': the release builder sets the C++ and LLVM runtime, X11 and reproducible build options')
        # The final release build adds the libc++ of the LLVM runtime to the base.
        flags = [flag for flag in flags_for('aarch64', 'clang', mk=mk) if not flag.startswith(('MKCXX=', 'MKLIBCXX='))]
        flags += ['MKCXX=yes', 'MKLIBCXX=yes']
        _require('release' in shared, 'profile os.sets needs an OS whose ember/shared-sources.json '
                 'names release.select; update the OS')
        # The OS names the sources that a release selects although no make guard
        # requires them (release.select of ember/shared-sources.json).
        # The release builder also reads the Ports producers and names the OS commit.
        ports_identity = planner.checkouts('ports')
        sets_inputs = dict(release_inputs, os_commit=os_identity['commit'], git=[*release_inputs['git'],
            *_sdk_files(sdk, SETS_MODULES),
            *_git_paths(ports_identity, 'ports', PORTS_RELEASE_PATHS)])
        stage('os:sets', dict(stage='sets', sets=profile['os']['sets']), prepared_sources.RELEASE_TRAVERSALS,
              flags, shared['release']['select'], [name + '.tar.xz' for name in profile['os']['sets']],
              'profile os.sets', sets_inputs)

    return planner.document(profile)


def load_profile(path):
    return validate_profile(read_json(path))


def summary(plan):
    """Return the node counts of each action, then one line for each node to build, with its reason."""
    actions = {}
    for node in plan['nodes']:
        actions.setdefault(node['decision']['action'], []).append(node)
    lines = ['; '.join(f'{action} {len(nodes)}' for action, nodes in sorted(actions.items()))]
    lines += [f'  build {node["id"]}: {node["decision"]["why"]}' for node in actions.get('build', [])]
    return '\n'.join(lines)
