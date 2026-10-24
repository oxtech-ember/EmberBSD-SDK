# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Receipt-bound inputs for the first native GCC16. This module runs no tools."""

from dataclasses import dataclass
import json
import os
import platform
import shlex
from types import MappingProxyType

from .errors import SDKError
from .bootstrap_binding import seed_commands, seed_reported_tools
from .inventory import sha256_file, snapshot_tree
from .native_toolset import (_absolute, _bytes, _driver_queries, _envelope, _file,
                             _gcc, _json, _native_image, _payload, _require,
                             _sha, _source, _text)
from .paths import canonical_existing, inside

BOOTSTRAP_SEED_API = 1
_SCOPE = 'gcc16-bootstrap'


def _frozen(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _frozen(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_frozen(item) for item in value)
    return value


def _relative(value):
    value = _text(value, 'source-relative path')
    _require(not os.path.isabs(value) and all(part not in ('', '.', '..')
                                             for part in value.split('/')),
             'expected a source-relative path without traversal')
    return value


def _installed_history(value, name, descriptor, file, *, inspect_inputs):
    """Pin an installed make/shell input without inventing its build history."""
    _require(isinstance(descriptor, dict), 'installed history needs its seed descriptor')
    _require(set(value) == {'kind', 'package_name', 'package_path', 'installation_prefix',
                           'package_metadata', 'installed_member_inventory', 'provisioned_members',
                           'source_correlation', 'source_archives', 'patches', 'package_archive',
                           'source_commit', 'historical_build_log', 'historical_compiler',
                           'historical_toolchain_closure', 'patches_complete', 'unknowns'},
             'unsupported installed-package history fields')
    packages = {'devel/bmake': ('bmake', {'make'}),
                'devel/gmake': ('gmake', {'gmake'}),
                'shells/mksh': ('mksh', {'sh'}),
                'pkgtools/bootstrap-mk-files': ('bootstrap-mk-files', set())}
    path = value['package_path']
    _require(path in packages, 'installed history is limited to bootstrap make and shell inputs')
    package, roles = packages[path]
    package_name = _text(value['package_name'], 'installed package name')
    _require(package_name.startswith(package + '-') and len(package_name) > len(package) + 1,
             'installed package name does not match its package path')
    _absolute(value['installation_prefix'])  # Provenance only. Never open this tree.
    metadata = value['package_metadata']
    _require(isinstance(metadata, dict) and set(metadata) == {'build_info', 'contents'},
             'original installed package metadata is required')
    info, contents = _bytes(metadata['build_info']), _bytes(metadata['contents'])
    _require([line for line in info.decode('utf-8', 'strict').splitlines()
              if line.startswith('PKGPATH=')] == ['PKGPATH=' + path] and
             [line for line in contents.decode('utf-8', 'strict').splitlines()
              if line.startswith('@name ')] == ['@name ' + package_name],
             'installed package metadata identity mismatch')
    # Source correlation is evidence of its stated scope, not a binary build log.
    _bytes(value['source_correlation'])
    inventory = _json(value['installed_member_inventory'])
    members = value['provisioned_members']
    _require(isinstance(members, list) and members, 'provisioned installed members are required')
    root, mapped = _absolute(descriptor['bundle_root']), {}
    for member in members:
        _require(isinstance(member, dict) and set(member) ==
                 {'installed_relative', 'selected_path', 'sha256'}, 'invalid installed member mapping')
        relative = _relative(member['installed_relative'])
        selected, sha = _absolute(member['selected_path']), _sha(member['sha256'])
        _require(selected != root and os.path.commonpath((selected, root)) == root and selected not in mapped,
                 'installed member mapping escapes or repeats its provisioned bundle')
        original = inventory.get(relative, {})
        _require(original.get('kind') == 'file' and original.get('sha256') == sha,
                 'installed member differs from its preserved SHA256 roster')
        if inspect_inputs:
            resolved = canonical_existing(selected)
            _require(inside(resolved, root) and sha256_file(resolved) == sha,
                     'provisioned installed member changed or escaped its bundle')
        mapped[selected] = sha
    selected_roles = {role: row for role, row in descriptor['tools'].items() if row['provider'] == name}
    _require(set(selected_roles) <= roles and (selected_roles or not roles),
             'installed package cannot provide these bootstrap roles')
    _require(all(mapped.get(row['path']) == row['sha256'] for row in selected_roles.values()),
             'installed history omits a selected provider member')
    missing = []
    protected = {metadata[key]['sha256'] for key in metadata}
    for field, label in (('package_archive', 'package-archive'),
                         ('historical_build_log', 'historical-build-log'),
                         ('historical_toolchain_closure', 'historical-toolchain-closure')):
        if value[field] is None:
            missing.append(label)
        else:
            file(value[field])
            _require(value[field]['sha256'] not in protected,
                     'package metadata cannot be relabeled as an archive or build evidence')
    if value['source_commit'] is None:
        missing.append('source-commit')
    else:
        _source(value['source_commit'])
    reported = [line for line in info.decode('utf-8', 'strict').splitlines()
                if line.startswith('CC_VERSION=')]
    if value['historical_compiler'] is None:
        _require(not reported, 'installed history omits reported compiler metadata')
        missing.append('historical-compiler')
    else:
        compiler = _text(value['historical_compiler'], 'historical compiler')
        _require(reported == ['CC_VERSION=' + compiler],
                 'historical compiler differs from original package metadata')
    _require(type(value['patches_complete']) is bool, 'declare historical patch completeness')
    if not value['patches_complete']:
        missing.append('historical-patches')
    if not value['source_archives']:
        missing.append('source-archives')
    _require(value['unknowns'] == sorted(missing), 'installed history must name every unknown input')
    return sorted(missing)


def _history(histories, providers, *, descriptor=None, inspect_inputs=True):
    _require(isinstance(histories, dict) and set(histories) == set(providers),
             'every seed provider needs explicit historical provenance')
    unknowns = {}

    def file(reference):
        if inspect_inputs:
            _file(reference)
        else:
            _require(isinstance(reference, dict) and set(reference) == {'path', 'sha256'},
                     'expected an explicit historical file reference')
            _absolute(reference['path'])
            _sha(reference['sha256'])

    def origins(rows):
        _require(isinstance(rows, list), 'expected an ordered historical input list')
        for item in rows:
            _require(isinstance(item, dict) and set(item) == {'url', 'file'},
                     'seed origin needs a URL and exact bytes')
            _text(item['url'], 'source URL')
            file(item['file'])

    for name, provider in providers.items():
        value = histories[name]
        _require(value['kind'] == provider['origin'], 'seed provider history kind mismatch')
        origins(value['source_archives'])
        origins(value['patches'])
        if value['kind'] == 'installed-package':
            unknowns[name] = _installed_history(value, name, descriptor, file, inspect_inputs=inspect_inputs)
        elif value['kind'] == 'source-build':
            _require(value['source_archives'], 'seed source archives are required')
            record = _json(value['build_record'])
            _envelope(record, 'emberbsd-bootstrap-seed-history', scope=_SCOPE)
            _text(record['historical_compiler'], 'historical compiler')
            _bytes(record['build_log'])
            unknowns[name] = []
        else:
            _require(value['kind'] == 'binary-release', 'unsupported seed origin kind')
            _text(value['release_url'], 'binary release URL')
            _text(value['release_version'], 'binary release version')
            file(value['artifact'])
            _bytes(value['release_metadata'])
            missing = []
            if value['embedded_configuration'] is None:
                missing.append('embedded-configuration')
            else:
                _bytes(value['embedded_configuration'])
            if value['source_commit'] is None:
                missing.append('source-commit')
            else:
                _source(value['source_commit'])
            for field, label in (('historical_build_log', 'historical-build-log'),
                                 ('historical_compiler', 'historical-compiler')):
                if value[field] is None:
                    missing.append(label)
                elif field == 'historical_compiler':
                    _text(value[field], 'historical compiler')
                else:
                    _bytes(value[field])
            _require(type(value['patches_complete']) is bool, 'declare historical patch completeness')
            if not value['patches_complete']:
                missing.append('historical-patches')
            if not value['source_archives']:
                missing.append('source-archives')
            signature = value['publisher_signature']
            if signature is None:
                missing.append('publisher-signature')
            else:
                file(signature['file'])
                verification = _json(signature['verification'])
                _require(verification.get('actual_exit') == 0 and
                         type(verification.get('actual_exit')) is int,
                         'publisher signature verification did not succeed')
                _bytes(verification['stdout'])
                _bytes(verification['stderr'])
            _require(value['unknowns'] == sorted(missing),
                     'binary release history must name every unknown input')
            unknowns[name] = sorted(missing)
        # Historical compiler text is provenance. It never selects a process.
    return unknowns


def _dynamic_closure(value, descriptor):
    """Check all recorded edges. An independent audit must establish completeness."""
    _require(isinstance(value, dict) and value, 'missing dynamic dependency closure')
    if any(isinstance(row, dict) and 'cache_image' in row for row in value.values()):
        from .darwin_runtime import validate_darwin_runtime_closure
        validate_darwin_runtime_closure(value, descriptor)
        return
    executable_paths = {canonical_existing(row['path'])
                        for row in descriptor['tools'].values() if row['kind'] == 'native'}
    roots = [descriptor['bundle_root']] + [item['root']
                                          for item in descriptor['host_abi']['libraries']]
    _require(executable_paths <= set(value), 'dynamic closure omits native tools')
    for path, row in value.items():
        _absolute(path)
        _require(canonical_existing(path) == path and
                 any(inside(path, root) for root in roots),
                 'dynamic dependency is outside declared native inputs')
        _require(isinstance(row, dict) and set(row) == {'sha256', 'dependencies'},
                 'invalid dynamic dependency record')
        _require(sha256_file(path) == _sha(row['sha256']), 'dynamic dependency changed')
        _require(isinstance(row['dependencies'], list) and
                 len(row['dependencies']) == len(set(row['dependencies'])) and
                 all(dep in value for dep in row['dependencies']),
                 'dynamic closure contains an undeclared dependency')
        if path not in executable_paths:
            _native_image(path, descriptor['host'], shared_library=True)


def _environment(tools, commands, flags):
    directories = sorted({os.path.dirname(path) for path in tools.values()})
    _require(all(os.pathsep not in path for path in directories),
             'bootstrap tool directory cannot be encoded in PATH')
    result = {'PATH': os.pathsep.join(directories)}
    for name, role in (('CC', 'cc'), ('CXX', 'cxx'), ('CPP', 'cpp'),
                       ('AS', 'as'), ('LD', 'ld'), ('HOST_CC', 'cc'),
                       ('HOST_CXX', 'cxx'), ('HOST_SH', 'sh'),
                       ('SHELL', 'sh'), ('CONFIG_SHELL', 'sh')):
        result[name] = shlex.join(commands[role])
    for name, role in (('AR', 'ar'), ('RANLIB', 'ranlib'), ('NM', 'nm'),
                       ('STRIP', 'strip'), ('PYTHON', 'python')):
        if role in commands:
            result[name] = shlex.join(commands[role])
    for name, key in (('CPPFLAGS', 'cppflags'), ('CFLAGS', 'cflags'),
                      ('CXXFLAGS', 'cxxflags'), ('LDFLAGS', 'ldflags'),
                      ('HOST_CPPFLAGS', 'cppflags'), ('HOST_CFLAGS', 'cflags'),
                      ('HOST_CXXFLAGS', 'cxxflags'), ('HOST_LDFLAGS', 'ldflags')):
        result[name] = shlex.join(flags[key])
    return result


def _seed_commands(descriptor, *, inspect_inputs=True):
    return seed_commands(descriptor, inspect_inputs=inspect_inputs)


def _plan(reference, descriptor, commands=None, flags=None, *, inspect_inputs=True):
    plan = _json(reference)
    if plan.get('format') == 'emberbsd-native-gcc16-pilot-plan':
        from .bootstrap_pilot import validate_pilot_plan
        commands = _seed_commands(descriptor, inspect_inputs=inspect_inputs) if commands is None else commands
        return validate_pilot_plan(plan, descriptor, commands, descriptor['abi_flags'] if flags is None else flags)
    _envelope(plan, 'emberbsd-native-gcc16-bootstrap-plan', scope=_SCOPE)
    _require(plan.get('purpose') == 'first-native-gcc16' and
             plan.get('host') == descriptor['host'], 'bootstrap plan scope mismatch')
    _gcc(plan['output_compiler'], plan['host'])
    _require(plan['sources'] == descriptor['sources'], 'bootstrap plan source pins mismatch')
    for source in plan['sources'].values():
        _source(source)
    export = plan['ports_export']
    export_root = _absolute(export['root'])
    _require(export['source'] == plan['sources']['ports'], 'Ports export source mismatch')
    export_inventory = _json(export['inventory'])
    _bytes(export['source_proof'])
    if inspect_inputs:
        _require(snapshot_tree(export_root) == export_inventory, 'Ports export inventory changed')
    recipes = plan['recipes']
    _require(isinstance(recipes, dict) and recipes and 'lang/gcc16' in recipes,
             'the common lang/gcc16 recipe is required')
    reached = set()

    def visit(name, visiting):
        _require(name in recipes and name not in visiting, 'invalid bootstrap recipe dependency graph')
        if name in reached:
            return
        row = recipes[name]
        _require(row['directory'] == name and _relative(name) == name,
                 'bootstrap recipes must use their common export directories')
        dependencies = row['dependencies']
        _require(isinstance(dependencies, list) and len(dependencies) == len(set(dependencies)),
                 'invalid recipe dependencies')
        for dependency in dependencies:
            visit(dependency, visiting | {name})
        recipe_inventory = _json(row['inventory'])
        prefix = name + '/'
        exported_recipe = {path[len(prefix):]: item for path, item in export_inventory.items()
                           if path.startswith(prefix)}
        _require(recipe_inventory == exported_recipe, 'bootstrap recipe differs from the pinned export')
        _require('Makefile' in recipe_inventory, 'recipe Makefile is required')
        _require(isinstance(row['patches'], list) and
                 len(row['patches']) == len(set(row['patches'])), 'ordered recipe patches are required')
        for patch in row['patches']:
            _require(_relative(patch).startswith('patches/') and
                     recipe_inventory.get(patch, {}).get('kind') == 'file',
                     'bootstrap patch is absent from its common recipe')
        available = {path for path, item in recipe_inventory.items()
                     if path.startswith('patches/patch-') and item.get('kind') == 'file'}
        _require(set(row['patches']) == available, 'bootstrap plan omits common recipe patches')
        _require(isinstance(row['distfiles'], list) and row['distfiles'], 'recipe distfiles are required')
        for distfile in row['distfiles']:
            _text(distfile['url'], 'distfile URL')
            if inspect_inputs:
                _file(distfile['file'])
            else:
                _absolute(distfile['file']['path'])
                _sha(distfile['file']['sha256'])
        reached.add(name)

    visit('lang/gcc16', set())
    _require(reached == set(recipes), 'bootstrap plan includes unrelated recipes')
    _require(isinstance(plan['required_roles'], list) and plan['required_roles'],
             'bootstrap native role closure is required')
    _absolute(plan['work_root'])
    _absolute(plan['output_bundle_root'])
    steps = plan['commands']
    _require(isinstance(steps, list) and steps, 'bootstrap build commands are required')
    ids, built, compared = set(), set(), False
    commands = _seed_commands(descriptor, inspect_inputs=inspect_inputs) if commands is None else commands
    flags = descriptor['abi_flags'] if flags is None else flags
    environment = _environment({role: words[0] for role, words in commands.items()}, commands, flags)
    for step in steps:
        name = _text(step['id'], 'bootstrap command ID')
        _require(name not in ids, 'duplicate bootstrap command ID')
        ids.add(name)
        _require(step['recipe'] in recipes and step['phase'] in ('build', 'compare', 'install'),
                 'command is outside the first native GCC16 closure')
        _require(step['role'] in ('make', 'gmake') and step['role'] in plan['required_roles'],
                 'bootstrap commands must use an explicit recipe make role')
        argv = step['argv']
        _require(isinstance(argv, list) and len(argv) >= 4, 'incomplete bootstrap command')
        for word in argv:
            _text(word, 'bootstrap command argument')
            _require(not word.startswith('@'), 'bootstrap response files are not supported')
        cwd = _absolute(step['cwd'])
        _require(cwd == export_root or inside(cwd, export_root) or
                 cwd == plan['work_root'] or inside(cwd, plan['work_root']),
                 'bootstrap command directory escapes its declared work')
        _require(isinstance(step['env'], dict), 'explicit bootstrap command environment is required')
        for key, value in step['env'].items():
            _require(key and key.replace('_', '').isalnum(), 'invalid bootstrap environment name')
            if value != '':
                _text(value, 'bootstrap environment value')
        _require(argv[0] == commands[step['role']][0] and
                 all(step['env'].get(key) == value for key, value in environment.items()),
                 'bootstrap command does not bind the accepted native routes')
        extra_names = {'DISTDIR', 'WRKOBJDIR', 'PACKAGES', 'PKG_DBDIR', 'LOCALBASE',
                       'PREFIX', 'PKGSRC_COMPILER', 'GCC_REQD', 'OSX_SDK_PATH',
                       'TZ', 'LC_ALL', 'LANG', 'TMPDIR'}
        _require(set(step['env']) <= set(environment) | extra_names,
                 'unsupported bootstrap environment override')
        _require(step['env'].get('PKGSRC_COMPILER', 'gcc') == 'gcc',
                 'bootstrap recipes must select GCC')
        for word in argv[3:]:
            if '=' in word:
                key, value = word.split('=', 1)
                base_key = key.rstrip('+:?!')
                search_keys = {'MAKEFLAGS', 'MFLAGS', 'GCC_EXEC_PREFIX', 'COMPILER_PATH',
                               'LIBRARY_PATH', 'CPATH', 'C_INCLUDE_PATH', 'CPLUS_INCLUDE_PATH',
                               'BASH_ENV', 'ENV'}
                selector_keys = set(environment) | search_keys | {'PKGSRC_COMPILER', 'MAKE'}
                _require(base_key not in selector_keys or key == base_key,
                         'make assignment operator replaces an accepted native route')
                _require(base_key not in environment or value == environment[base_key],
                         'make argument overrides an accepted native route')
                _require(base_key not in search_keys, 'implicit make search override')
                _require(base_key != 'MAKE', 'recursive make selection cannot be overridden')
                _require(base_key != 'PKGSRC_COMPILER' or value == 'gcc',
                         'bootstrap recipes must select GCC')
            _require(word not in ('-C', '-f', '--file', '--makefile') and
                     not word.startswith(('-C', '-f', '--file=', '--makefile=')),
                     'make argument replaces its declared recipe')
        if step['phase'] in ('build', 'install'):
            _require(argv[1:3] == ['-C', os.path.join(export_root, step['recipe'])],
                     'bootstrap must invoke the common recipe')
            if step['phase'] == 'build':
                _require(set(recipes[step['recipe']]['dependencies']) <= built,
                         'recipe build precedes its dependency closure')
                built.add(step['recipe'])
            else:
                _require(step['recipe'] in built and
                         (step['recipe'] != 'lang/gcc16' or compared),
                         'recipe install precedes its build or GCC comparison')
        if step['phase'] == 'compare':
            _require(step['recipe'] == 'lang/gcc16' and set(recipes) <= built,
                     'GCC comparison precedes its build closure')
            _require(argv[1] == '-C' and
                     (argv[2] == plan['work_root'] or inside(_absolute(argv[2]), plan['work_root'])),
                     'GCC comparison must use the declared object work')
            compared = True
    _require(built == set(recipes), 'bootstrap commands omit a recipe build')
    comparison = plan['comparison_command']
    _require(comparison in ids and any(step['id'] == comparison and step['phase'] == 'compare'
                                      and step['recipe'] == 'lang/gcc16' and 'compare' in step['argv']
                                      for step in steps), 'an explicit GCC stage comparison is required')
    return plan


@dataclass(frozen=True)
class BootstrapSeed:
    """A seed accepted only for one plan. This is never a NativeToolset."""

    descriptor_sha256: str
    accepted_seed_receipt: object
    bootstrap_plan: object
    commands: object
    abi_flags: object
    _json: str

    def command(self, role):
        if role not in self.commands:
            raise SDKError(f'bootstrap role is not selected: {role}')
        return self.commands[role]

    def as_dict(self):
        return json.loads(self._json)


def load_bootstrap_seed(path, *, descriptor_sha256, accepted_seed_receipt,
                        bootstrap_plan, required_roles):
    """Verify pinned seed inputs and one independently accepted bootstrap plan.

    The caller supplies a trusted acceptance hash. The seed cannot accept itself.
    No tools, compiler probes, downloads, installs, or build commands run here.
    """
    try:
        return _load(path, descriptor_sha256, accepted_seed_receipt, bootstrap_plan, required_roles)
    except (OSError, KeyError, TypeError, ValueError, AttributeError) as exc:
        raise SDKError(f'invalid bootstrap seed: {exc}') from exc


def _load(path, descriptor_sha256, acceptance_ref, plan_ref, required_roles):
    descriptor_sha256 = _sha(descriptor_sha256)
    descriptor = _json(dict(path=os.fspath(path), sha256=descriptor_sha256))
    _envelope(descriptor, 'emberbsd-bootstrap-seed', scope=_SCOPE)
    acceptance = _json(acceptance_ref)
    _envelope(acceptance, 'emberbsd-bootstrap-seed-acceptance', scope=_SCOPE)
    _require(acceptance.get('status') == 'PASS' and
             acceptance.get('descriptor_sha256') == descriptor_sha256 and
             acceptance.get('bootstrap_plan') == plan_ref and
             acceptance.get('accepted_input_scope') == 'bootstrap-seed',
             'seed was not accepted for this exact bootstrap plan')
    _require(descriptor['host'] == {'os': platform.system(), 'cpu': platform.machine()},
             'bootstrap seed does not match the actual native host')
    _gcc(descriptor['compiler'], descriptor['host'])
    _require({'sdk', 'ports'} <= set(descriptor['sources']), 'SDK and Ports source pins are required')
    for source in descriptor['sources'].values():
        _source(source)
    _require(isinstance(descriptor['providers'], dict) and descriptor['providers'],
             'seed member provenance is required')
    for row in descriptor['providers'].values():
        _require(row['origin'] in ('source-build', 'binary-release', 'installed-package'),
                 'invalid seed provider origin')
        if row['origin'] == 'source-build':
            _require(row['source'] in descriptor['sources'], 'unknown seed provider source')
            _relative(row['recipe'])
            _sha(row['recipe_sha256'])
    unknowns = _history(descriptor['history'], descriptor['providers'], descriptor=descriptor)
    _require(acceptance.get('accepted_history_unknowns') == unknowns,
             'seed acceptance must explicitly accept its historical unknowns')
    plan_header = _json(plan_ref)
    _require(plan_header['seed_descriptor'] == dict(path=os.fspath(path), sha256=descriptor_sha256),
             'bootstrap plan selects another seed')
    _require(isinstance(required_roles, (tuple, list, set, frozenset)) and required_roles,
             'explicit required bootstrap roles are required')
    is_pilot = plan_header.get('format') == 'emberbsd-native-gcc16-pilot-plan'
    roles = set(required_roles) | set(plan_header['required_roles']) | (set() if is_pilot else {'sh'})
    root, inventory, tools, records, abi_inventories, flags = _payload(descriptor, roles)
    prefix = _absolute(descriptor['driver_prefix'])
    _require(inside(prefix, root) and canonical_existing(prefix) == prefix and os.path.isdir(prefix),
             'GCC driver prefix must be a real in-bundle directory')
    # The selected spelling must use -B. Its symlink target may be elsewhere in
    # the bundle. _payload already checks both spellings against that bundle.
    _require(all(tools[role] != prefix and os.path.commonpath((tools[role], prefix)) == prefix
                 for role in descriptor['driver_components'].values()),
             'GCC driver components must use the explicit prefix')
    commands = _seed_commands(descriptor)
    plan = _plan(plan_ref, descriptor, commands, flags)
    _dynamic_closure(descriptor['dynamic_dependencies'], descriptor)
    for key in ('host', 'compiler', 'sources', 'providers', 'host_abi', 'abi_flags',
                'history', 'dynamic_dependencies', 'driver_prefix'):
        _require(acceptance.get(key) == descriptor[key], f'seed acceptance {key} mismatch')
    _require(acceptance.get('driver_binding') == descriptor.get('driver_binding'),
             'seed acceptance driver binding mismatch')
    _driver_queries(descriptor, acceptance['checks'], seed_reported_tools(descriptor, tools), commands)
    if is_pilot:
        _require(acceptance.get('evidence_level') == 'input-and-route-only',
                 'pilot requires explicit input-and-route-only acceptance')
        audit = _json(acceptance['input_route_audit'])
        _envelope(audit, 'emberbsd-bootstrap-seed-input-route-audit', scope=_SCOPE)
        _require(audit.get('status') == 'PASS' and
                 audit.get('descriptor_sha256') == descriptor_sha256 and
                 audit.get('bootstrap_plan') == plan_ref and audit.get('tools') == records and
                 audit.get('dynamic_dependencies') == descriptor['dynamic_dependencies'] and
                 audit.get('expected_commands') == plan['commands'] and
                 audit.get('execution_observed') is False and
                 all(audit.get(key) == [] for key in ('unresolved_tool_routes',
                                                     'unresolved_abi_inputs', 'unresolved_dsos')),
                 'pilot input routes were not independently accepted')
    else:
        _execution_audit(acceptance, descriptor, descriptor_sha256, plan_ref, records)
    _require(snapshot_tree(root) == inventory, 'bootstrap seed changed during validation')
    for abi_root, rows in abi_inventories:
        _require(snapshot_tree(abi_root) == rows, 'bootstrap ABI changed during validation')
    selection = dict(format='emberbsd-bootstrap-seed-selection', version=1, api=BOOTSTRAP_SEED_API,
                     scope=_SCOPE, descriptor_sha256=descriptor_sha256,
                     accepted_seed_receipt=acceptance_ref, bootstrap_plan=plan_ref,
                     host=descriptor['host'], compiler=descriptor['compiler'],
                     commands={role: list(argv) for role, argv in commands.items()},
                     abi_flags={key: list(words) for key, words in flags.items()}, plan=plan)
    return BootstrapSeed(descriptor_sha256, _frozen(acceptance_ref), _frozen(plan_ref),
                         _frozen({key: list(value) for key, value in commands.items()}),
                         flags, json.dumps(selection))


def _execution_audit(acceptance, descriptor, descriptor_sha256, plan_ref, records):
    audit = _json(acceptance['execution_audit'])
    _envelope(audit, 'emberbsd-bootstrap-seed-audit', scope=_SCOPE)
    _require(audit.get('status') == 'PASS' and
             audit.get('descriptor_sha256') == descriptor_sha256 and
             audit.get('bootstrap_plan') == plan_ref and audit.get('tools') == records and
             audit.get('dynamic_dependencies') == descriptor['dynamic_dependencies'] and
             all(audit.get(key) == [] for key in ('undeclared_tools', 'path_searches',
                                                 'undeclared_abi_inputs', 'undeclared_dsos')),
             'bootstrap seed execution closure was not accepted')


def verify_native_gcc16_graduation(reference, *, output_descriptor,
                                  output_descriptor_sha256, provider):
    """Check evidence already bound by the caller's accepted ordinary receipt.

    This does not issue acceptance or reselect the historical seed. Retain the
    small evidence files after seed cleanup. Ordinary probes are checked by
    load_native_toolset after this check.
    """
    try:
        _graduation(reference, output_descriptor, output_descriptor_sha256, provider)
    except (OSError, KeyError, TypeError, ValueError, AttributeError) as exc:
        raise SDKError(f'invalid native GCC16 graduation: {exc}') from exc


def _build_result(actual, expected):
    _require(all(actual.get(key) == expected[key] for key in ('id', 'argv', 'cwd', 'env')),
             'bootstrap actual command differs from its accepted plan')
    _require(type(actual.get('actual_exit')) is int and actual['actual_exit'] == 0,
             'missing successful actual bootstrap build command')
    _require('stdout' not in actual and 'stderr' not in actual,
             'bootstrap build result must preserve one combined log')
    # run_logged preserves a merged stream. Do not relabel it as stdout.
    # Full compiler logs can exceed the metadata size limit.
    _file(actual['log'])


def _graduation(reference, output, output_sha256, provider):
    receipt = _json(reference)
    _envelope(receipt, 'emberbsd-native-gcc16-bootstrap-graduation')
    _require(receipt.get('status') == 'PASS' and
             receipt.get('output_descriptor_sha256') == _sha(output_sha256),
             'bootstrap graduation does not accept this output')
    seed = _json(receipt['seed_descriptor'])
    _envelope(seed, 'emberbsd-bootstrap-seed', scope=_SCOPE)
    accepted = _json(receipt['accepted_seed_receipt'])
    _envelope(accepted, 'emberbsd-bootstrap-seed-acceptance', scope=_SCOPE)
    _require(accepted.get('status') == 'PASS' and
             accepted.get('accepted_input_scope') == 'bootstrap-seed' and
             accepted.get('descriptor_sha256') == receipt['seed_descriptor']['sha256'] and
             accepted.get('bootstrap_plan') == receipt['bootstrap_plan'],
             'graduation requires the original scoped seed acceptance')
    unknowns = _history(seed['history'], seed['providers'], descriptor=seed, inspect_inputs=False)
    _require(accepted.get('accepted_history_unknowns') == unknowns,
             'original seed acceptance does not bind its historical unknowns')
    plan = _plan(receipt['bootstrap_plan'], seed, inspect_inputs=False)
    _require(plan.get('format') == 'emberbsd-native-gcc16-bootstrap-plan',
             'a finite pilot cannot graduate an ordinary provider')
    _require(plan['seed_descriptor'] == receipt['seed_descriptor'] and
             plan['host'] == output['host'] and plan['output_compiler'] == output['compiler'] and
             plan['sources'] == output['sources'] and
             plan['output_bundle_root'] == output['bundle_root'] and
             receipt.get('output_inventory') == output['inventory'],
             'bootstrap output does not match the accepted plan')
    _require(provider['source'] == 'ports' and provider['recipe'] in plan['recipes'],
             'bootstrap graduation is limited to its common recipe closure')
    recipe = _json(plan['recipes'][provider['recipe']]['inventory'])
    _require(provider['recipe_sha256'] == recipe['Makefile']['sha256'],
             'graduated provider recipe identity mismatch')
    results = receipt['commands']
    _require(isinstance(results, list) and len(results) == len(plan['commands']),
             'bootstrap command results are incomplete')
    for actual, expected in zip(results, plan['commands']):
        _build_result(actual, expected)
    comparison = receipt['stage_comparison']
    _require(comparison.get('method') == 'gcc-bootstrap-compare' and
             comparison.get('status') == 'PASS' and
             comparison.get('command_id') == plan['comparison_command'],
             'GCC bootstrap stage comparison was not accepted')
    for name in ('stage2_inventory', 'stage3_inventory'):
        _require(bool(_json(comparison[name])), 'bootstrap stage inventory is empty')
    _bytes(comparison['comparison_log'])
    audit = _json(receipt['execution_audit'])
    _envelope(audit, 'emberbsd-native-gcc16-bootstrap-audit')
    _require(audit.get('status') == 'PASS' and
             audit.get('bootstrap_plan') == receipt['bootstrap_plan'] and
             audit.get('output_descriptor_sha256') == output_sha256 and
             audit.get('recipe_closure') == sorted(plan['recipes']) and
             audit.get('all_commands_drained') is True and
             all(audit.get(key) == [] for key in ('undeclared_tools', 'path_searches',
                                                 'undeclared_abi_inputs', 'undeclared_dsos')),
             'bootstrap production closure was not accepted')
    _dynamic_closure(receipt['dynamic_dependencies'], output)
    _require(audit.get('dynamic_dependencies') == receipt['dynamic_dependencies'],
             'graduation dynamic dependency audit mismatch')
