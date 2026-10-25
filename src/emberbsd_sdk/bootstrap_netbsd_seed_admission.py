# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Retained-history checks for the NetBSD first-native GCC16 seed only."""

import os
from pathlib import PurePosixPath
import re
import stat

from .errors import SDKError
from .native_toolset import _absolute, _json, _require, _sha, _text
from .paths import canonical_existing, inside, open_regular, verify_stable


_ADMISSION = 'netbsd11-amd64-first-native-v1'
_LOCAL_KIND = 'retained-local-netbsd-build'
_BASE_KIND = 'retained-netbsd-base-tools'
_NETBSD_EXTERNAL_DRIVER_BINDING = 'netbsd11-configured-base-as-ld-seed-v1'
_EXTERNAL_BASE_DRIVERS = {'as': '/usr/bin/as', 'ld': '/usr/bin/ld'}
_DRIVERS = ('cc1', 'cc1plus', 'collect2', 'lto1', 'lto-wrapper', 'as', 'ld')
_BASE_TOOL_PATHS = {
    'as': '/usr/bin/as', 'ld': '/usr/bin/ld', 'ar': '/usr/bin/ar',
    'ranlib': '/usr/bin/ranlib', 'nm': '/usr/bin/nm', 'strip': '/usr/bin/strip',
    'awk': '/usr/bin/awk', 'grep': '/usr/bin/grep', 'sed': '/usr/bin/sed',
    'gzip': '/usr/bin/gzip', 'gunzip': '/usr/bin/gunzip', 'gzcat': '/usr/bin/gzcat',
    'xz': '/usr/bin/xz', 'xzcat': '/usr/bin/xzcat',
    'pkg_info': '/usr/sbin/pkg_info', 'pkg_admin': '/usr/sbin/pkg_admin',
    'pkg_add': '/usr/sbin/pkg_add', 'pkg_create': '/usr/sbin/pkg_create',
    'pkg_delete': '/usr/sbin/pkg_delete',
}
# Exact NetBSD 11 amd64 utility paths requested by the current 51-recipe
# pkgsrc bootstrap metadata DAG. These paths authorize only named, copied
# members; they do not authorize a system PATH or ambient command lookup.
_BASE_TOOL_PATHS.update({
    'basename': '/usr/bin/basename', 'bsdtar': '/bin/tar',
    'bzcat': '/usr/bin/bzcat', 'cat': '/bin/cat', 'chgrp': '/usr/bin/chgrp',
    'chmod': '/bin/chmod', 'chown': '/usr/sbin/chown',
    'cmp': '/usr/bin/cmp', 'cp': '/bin/cp',
    'cut': '/usr/bin/cut', 'date': '/bin/date', 'diff': '/usr/bin/diff',
    'dirname': '/usr/bin/dirname', 'egrep': '/usr/bin/egrep',
    'env': '/usr/bin/env', 'expr': '/bin/expr', 'fgrep': '/usr/bin/fgrep',
    'find': '/usr/bin/find', 'flex': '/usr/bin/lex', 'ftp': '/usr/bin/ftp',
    'gettext': '/usr/bin/gettext', 'ggrep': '/usr/bin/grep',
    'head': '/usr/bin/head', 'hostname': '/bin/hostname', 'id': '/usr/bin/id',
    'install': '/usr/bin/install', 'install-info': '/usr/bin/install-info',
    'ln': '/bin/ln', 'ls': '/bin/ls', 'mkdir': '/bin/mkdir',
    'msgfmt': '/usr/bin/msgfmt', 'msgmerge': '/usr/bin/msgmerge',
    'mv': '/bin/mv', 'patch': '/usr/bin/patch', 'printf': '/usr/bin/printf',
    'pwd': '/bin/pwd', 'readelf': '/usr/bin/readelf', 'rm': '/bin/rm',
    'rmdir': '/bin/rmdir', 'sleep': '/bin/sleep', 'sort': '/usr/bin/sort',
    'tail': '/usr/bin/tail', 'tar': '/bin/tar', 'touch': '/usr/bin/touch',
    'tr': '/usr/bin/tr', 'uname': '/usr/bin/uname', 'wc': '/usr/bin/wc',
    'xargs': '/usr/bin/xargs', 'xgettext': '/usr/bin/xgettext',
})
_R13_DIGEST_GOLDEN_FIXTURE_SHA256 = '39f4f0f48162c272a826d628eb22d134a91061e3d75c3736ef691c1ad0447d79'
_R13_DIGEST_GOLDENS = {
    'sha256': '39f4f0f48162c272a826d628eb22d134a91061e3d75c3736ef691c1ad0447d79',
    'sha512': '119f333142809b7cf3e86c2d7b36e020157053b91d651db574397694c48ea510ed07a10773de4e8549307f031d80094d6fcd1002b33df21f932f64b94aff16b8',
    'blake2s': '68042c7a00313f7cbd544f9fed8673a9cd8d1c5fdf71ff2d89f3b9b9d08fd2c2',
}
_LOCAL_UNKNOWNS = sorted(('historical-command-environment',
                          'historical-producer-attestation',
                          'historical-publisher-signature',
                          'historical-recipe-commit',
                          'historical-source-commit',
                          'ordinary-provider-qualification'))


def _reference(value, label):
    _require(isinstance(value, dict) and set(value) == {'path', 'sha256'},
             f'{label} needs a path and SHA256')
    _absolute(value['path'])
    _sha(value['sha256'])
    return value


def _read(reference, file, label):
    _reference(reference, label)
    file(reference)
    return _json(reference)


def _validate_legacy_seed(legacy, file):
    _require(isinstance(legacy, dict) and set(legacy) ==
             {'descriptor', 'acceptance', 'accepted_record', 'prefix_inventory'},
             'retained history needs the original compiler seed records')
    descriptor_ref = _reference(legacy['descriptor'], 'legacy compiler descriptor')
    acceptance_ref = _reference(legacy['acceptance'], 'legacy compiler acceptance')
    accepted_ref = _reference(legacy['accepted_record'], 'legacy accepted record')
    inventory_ref = _reference(legacy['prefix_inventory'], 'legacy prefix inventory')
    descriptor = _read(descriptor_ref, file, 'legacy compiler descriptor')
    receipt = _read(acceptance_ref, file, 'legacy compiler acceptance')
    accepted = _read(accepted_ref, file, 'legacy accepted record')
    inventory = _read(inventory_ref, file, 'legacy compiler prefix inventory')
    _require(descriptor.get('format') == 'emberbsd-bootstrap-compiler-seed' and
             descriptor.get('scope') == 'bmake-bootstrap' and
             type(descriptor.get('version')) is int and descriptor['version'] == 1,
             'legacy compiler seed scope must remain bmake-bootstrap')
    _require(descriptor.get('acceptance_receipt') == acceptance_ref and
             descriptor.get('accepted_record') == accepted_ref and
             descriptor.get('prefix_inventory') == inventory_ref,
             'legacy compiler seed records do not match their original descriptor')
    _require(receipt.get('format') == 'emberbsd-bootstrap-bmake-compiler-seed-acceptance' and
             type(receipt.get('version')) is int and receipt['version'] == 1 and
             receipt.get('scope') == 'bmake-bootstrap' and receipt.get('status') == 'PASS' and
             receipt.get('prefix_inventory') == inventory_ref and
             receipt.get('accepted_record') == accepted_ref and
             receipt.get('prefix_manifest') == descriptor.get('prefix_manifest') and
             receipt.get('host') == descriptor.get('host') and
             receipt.get('prefix') == descriptor.get('prefix') and
             receipt.get('compiler') == descriptor.get('compiler') and
             receipt.get('host_runtime') == descriptor.get('host_runtime') and
             receipt.get('driver_components') == descriptor.get('driver_components') and
             receipt.get('source_provenance') == accepted.get('source_plan'),
             'legacy compiler acceptance does not bind the original bmake seed')
    for key in ('audit_report', 'prefix_manifest'):
        _reference(receipt[key], 'legacy compiler ' + key.replace('_', ' '))
        file(receipt[key])
    _require(accepted.get('version') == descriptor.get('compiler', {}).get('version') == '16.2.0' and
             accepted.get('host') == descriptor.get('host') == 'NetBSD11.0-amd64' and
             accepted.get('triplet') == descriptor.get('compiler', {}).get('triplet') ==
             'x86_64-unknown-netbsd11.0',
             'legacy accepted record does not identify the retained GCC16 seed')
    _require(accepted.get('manifest_sha256') == descriptor.get('prefix_manifest', {}).get('sha256'),
             'legacy accepted record does not bind the original prefix manifest')
    _require(isinstance(inventory, dict) and
             inventory.get('format') == 'emberbsd-bootstrap-compiler-prefix-inventory' and
             inventory.get('scope') == 'bmake-bootstrap' and
             inventory.get('prefix') == descriptor.get('prefix') and
             isinstance(inventory.get('entries'), dict) and inventory['entries'],
             'legacy compiler prefix inventory is empty')
    return descriptor, receipt, accepted, inventory['entries']


def validate_retained_local_history(value, name, descriptor, file, *, inspect_inputs=True):
    """Validate immutable records for old local GCC binaries without inventing a recipe."""
    fields = {'kind', 'legacy_seed', 'producer_plan', 'producer_scripts', 'producer_fingerprints',
              'source_archives', 'patches', 'build_logs', 'historical_compiler',
              'member_mappings', 'shared_object_mappings', 'unknowns'}
    _require(isinstance(value, dict) and set(value) == fields and value.get('kind') == _LOCAL_KIND,
             'unsupported retained local NetBSD build history')
    _require(descriptor.get('admission') == _ADMISSION and
             descriptor.get('host') == {'os': 'NetBSD', 'cpu': 'amd64'},
             'retained local history is limited to the NetBSD first-native seed')
    legacy, _receipt, accepted, inventory = _validate_legacy_seed(value['legacy_seed'], file)
    producer = _read(value['producer_plan'], file, 'retained producer plan')
    _require(producer == accepted.get('source_plan'),
             'retained producer plan differs from the accepted historical record')
    _require(value['historical_compiler'] == 'GCC/G++ 12.5.0' and
             accepted.get('source_plan', {}).get('bootstrap') ==
             'existing GCC/G++12.5.0 (native package bootstrap only)',
             'historical compiler must remain historical provenance')

    archives = value['source_archives']
    patches = value['patches']
    for rows, expected, label in ((archives, producer.get('archives'), 'source archives'),
                                  (patches, producer.get('patches'), 'patches')):
        _require(isinstance(rows, list) and isinstance(expected, dict) and rows,
                 f'retained {label} are required')
        mapped = {}
        for row in rows:
            _require(isinstance(row, dict) and set(row) == {'name', 'file'},
                     f'invalid retained {label} mapping')
            name_value = _text(row['name'], f'{label} name')
            _require(name_value not in mapped, f'duplicate retained {label} mapping')
            _reference(row['file'], f'{label} file')
            _require(PurePosixPath(row['file']['path']).name == name_value,
                     f'retained {label} filename differs from its recorded name')
            file(row['file'])
            mapped[name_value] = row['file']['sha256']
        _require(mapped == expected, f'retained {label} do not match their original hashes')

    scripts = value['producer_scripts']
    _require(isinstance(scripts, list) and len(scripts) == 5,
             'all five retained local producer scripts are required')
    script_names = set()
    for script in scripts:
        _reference(script, 'retained producer script')
        basename = PurePosixPath(script['path']).name
        _require(basename not in script_names, 'duplicate retained producer script')
        file(script)
        script_names.add(basename)
    _require(script_names == {'native-gcc16-producer.py',
             'native-gcc16-resume-makeinfo.py',
             'native-gcc16-resume-target-dialect.py',
             'native-gcc16-resume-runtime-flags.py',
             'native-gcc16-resume-native-rpath.py'},
             'retained local producer script set is incomplete')
    _reference(value['producer_plan'], 'retained producer plan')
    _require(PurePosixPath(value['producer_plan']['path']).name ==
             'native-gcc16-producer-plan.json', 'retained producer plan name is invalid')
    fingerprints_ref = _reference(value['producer_fingerprints'], 'retained producer fingerprints')
    file(fingerprints_ref)
    handle, before = open_regular(fingerprints_ref['path'])
    with handle:
        fingerprint_data = handle.read(65537)
    verify_stable(fingerprints_ref['path'], before)
    _require(len(fingerprint_data) <= 65536, 'retained producer fingerprints exceed the input limit')
    try:
        fingerprint_text = fingerprint_data.decode('utf-8', 'strict')
    except UnicodeDecodeError as exc:
        raise SDKError('retained producer fingerprints are not UTF-8') from exc
    fingerprint_map = {}
    for line in fingerprint_text.splitlines():
        match = re.fullmatch(r'SHA256 \(/[^\n()]*/([^/()]+)\) = ([0-9a-f]{64})', line)
        _require(match is not None and match.group(1) not in fingerprint_map,
                 'invalid retained producer fingerprint line')
        fingerprint_map[match.group(1)] = match.group(2)
    expected_fingerprints = {PurePosixPath(row['path']).name: row['sha256'] for row in scripts}
    expected_fingerprints[PurePosixPath(value['producer_plan']['path']).name] = \
        value['producer_plan']['sha256']
    _require(fingerprint_map == expected_fingerprints,
             'retained producer files differ from their historical fingerprint record')
    logs = value['build_logs']
    _require(isinstance(logs, list),
             'retained compiler build and C/C++ acceptance logs are required')
    log_names = set()
    for log in logs:
        _reference(log, 'retained compiler build log')
        basename = PurePosixPath(log['path']).name
        _require(basename not in log_names, 'duplicate retained build log')
        file(log)
        log_names.add(basename)
    _require({'gcc-configure.log', 'gcc-build.log', 'gcc-build-resume.log',
              'gcc-build-target-default.log', 'gcc-build-runtime-flags.log',
              'gcc-build-native-rpath.log', 'gcc-install.log',
              'accept-c-build.log', 'accept-c-run.log',
              'accept-cxx-build.log', 'accept-cxx-run.log'} <= log_names,
             'retained compiler build and C/C++ acceptance logs are incomplete')

    _require(isinstance(descriptor.get('tools'), dict) and name in descriptor.get('providers', {}),
             'retained provider is absent from the seed descriptor')
    selected = {role: row for role, row in descriptor['tools'].items()
                if row.get('provider') == name}
    _require({'cc', 'cxx', 'cpp'} <= set(selected),
             'retained local compiler roles are incomplete')
    mappings = value['member_mappings']
    _require(isinstance(mappings, list), 'retained GCC member mappings are required')
    mapped = {}
    root = _absolute(descriptor['bundle_root'])
    old_prefix = _absolute(legacy['prefix'])
    for row in mappings:
        _require(isinstance(row, dict) and set(row) ==
                 {'role', 'legacy_relative', 'selected_path', 'sha256'},
                 'invalid retained GCC member mapping')
        role = _text(row['role'], 'retained GCC member role')
        selected_path = _absolute(row['selected_path'])
        sha = _sha(row['sha256'])
        relative = _text(row['legacy_relative'], 'legacy GCC member path')
        _require(not relative.startswith('/') and all(part not in ('', '.', '..')
                 for part in relative.split('/')), 'invalid legacy GCC member path')
        _require(selected_path != root and inside(selected_path, root) and role not in mapped,
                 'retained GCC member escapes or repeats the owned seed bundle')
        tool = selected.get(role)
        _require(tool is not None and tool.get('path') == selected_path and tool.get('sha256') == sha,
                 'retained GCC member differs from its selected tool record')
        old = inventory.get(relative)
        _require(isinstance(old, dict) and old.get('kind') == 'file' and old.get('sha256') == sha,
                 'retained GCC member differs from its old prefix inventory')
        if inspect_inputs:
            file(dict(path=selected_path, sha256=sha))
        mapped[role] = relative
    _require(set(mapped) == set(selected),
             'retained GCC member mappings do not cover every selected compiler tool')

    shared_objects = value['shared_object_mappings']
    _require(isinstance(shared_objects, list) and len(shared_objects) == 1,
             'retained GCC history needs the configured LTO plugin mapping')
    plugin = shared_objects[0]
    _require(isinstance(plugin, dict) and set(plugin) ==
             {'role', 'legacy_relative', 'selected_path', 'sha256'} and
             plugin['role'] == 'lto_plugin',
             'retained GCC history has an unsupported shared-object mapping')
    plugin_path = _absolute(plugin['selected_path'])
    plugin_sha = _sha(plugin['sha256'])
    plugin_relative = _text(plugin['legacy_relative'], 'retained LTO plugin path')
    old_plugin = inventory.get(plugin_relative)
    _require(not plugin_relative.startswith('/') and
             all(part not in ('', '.', '..') for part in plugin_relative.split('/')) and
             plugin_path != root and inside(plugin_path, root) and
             isinstance(old_plugin, dict) and old_plugin.get('kind') == 'file' and
             old_plugin.get('sha256') == plugin_sha,
             'retained LTO plugin differs from its old prefix inventory')
    if inspect_inputs:
        file(dict(path=plugin_path, sha256=plugin_sha))

    components = descriptor.get('driver_components')
    _require(isinstance(components, dict) and set(components) == set(_DRIVERS),
             'seven GCC driver components must be explicitly bound')
    for component in ('cc1', 'cc1plus', 'collect2', 'lto1', 'lto-wrapper'):
        _require(components[component] in selected and components[component] in mapped,
                 'seven GCC driver components must be explicitly bound')
    legacy_components = legacy.get('driver_components')
    _require(isinstance(legacy_components, dict) and {'as', 'ld'} <= set(legacy_components),
             'legacy compiler seed must retain its original assembler and linker bindings')
    _require(value['unknowns'] == _LOCAL_UNKNOWNS,
             'retained GCC history must enumerate all known historical unknowns')
    return list(_LOCAL_UNKNOWNS)


def validate_retained_base_history(value, name, descriptor, file, *, inspect_inputs=True):
    """Validate copied NetBSD base primitives without claiming source-build history."""
    fields = {'kind', 'legacy_seed', 'release_identity', 'tool_members', 'unknowns'}
    _require(isinstance(value, dict) and set(value) == fields and value.get('kind') == _BASE_KIND,
             'unsupported retained NetBSD base-tool history')
    _require(descriptor.get('admission') == _ADMISSION and
             descriptor.get('host') == {'os': 'NetBSD', 'cpu': 'amd64'},
             'NetBSD base tools are limited to the first-native seed')
    legacy, _receipt, _accepted, _inventory = _validate_legacy_seed(value['legacy_seed'], file)
    _require(_text(value['release_identity'], 'NetBSD base release identity') ==
             'NetBSD 11.0 amd64', 'NetBSD base release identity is not the admitted host')
    selected = {role: row for role, row in descriptor.get('tools', {}).items()
                if row.get('provider') == name}
    forbidden = {'cc', 'cxx', 'cpp', 'gcc', 'g++', 'sh', 'make', 'gmake', 'python'}
    _require({'as', 'ld'} <= set(selected) and not (set(selected) & forbidden) and
             set(selected) <= set(_BASE_TOOL_PATHS) and
             value['unknowns'] == ['base-build-record', 'base-source-commit',
                                   'base-source-signature'],
             'NetBSD base history must disclose source unknowns and bind assembler/linker')
    old = legacy['driver_components']
    members = value['tool_members']
    _require(isinstance(members, list) and members, 'NetBSD base tool mappings are required')
    mapped = {}
    root = _absolute(descriptor['bundle_root'])
    for row in members:
        _require(isinstance(row, dict) and set(row) in (
                 {'role', 'original_path', 'selected_path', 'sha256'},
                 {'role', 'original_path', 'resolved_original_path', 'selected_path', 'sha256'}),
                 'invalid NetBSD base tool mapping')
        role = _text(row['role'], 'NetBSD base tool role')
        selected_path = _absolute(row['selected_path'])
        sha = _sha(row['sha256'])
        original = _absolute(row['original_path'])
        resolved_original = _absolute(row.get('resolved_original_path', original))
        external_driver = role in _EXTERNAL_BASE_DRIVERS
        path_policy = (selected_path == original == _EXTERNAL_BASE_DRIVERS[role] and
                       descriptor.get('driver_binding', {}).get('kind') ==
                       _NETBSD_EXTERNAL_DRIVER_BINDING) if external_driver else (
                       selected_path != root and inside(selected_path, root))
        _require(role in selected and role not in mapped and path_policy and
                 _BASE_TOOL_PATHS.get(role) == original and
                 any(inside(resolved_original, base_root) for base_root in
                     ('/bin', '/usr/bin', '/usr/sbin', '/sbin')) and
                 (not external_driver or resolved_original == original),
                 'NetBSD base member escapes its exact release-tool route or fixed base executable roots')
        _require(PurePosixPath(selected_path).name == role,
                 'selected NetBSD base alias must preserve its argv0 basename')
        tool = selected[role]
        _require(tool.get('path') == selected_path and tool.get('sha256') == sha,
                 'NetBSD base member differs from its selected tool record')
        if role in ('as', 'ld'):
            legacy_row = old.get(role)
            _require(isinstance(legacy_row, dict) and legacy_row.get('path') == original and
                     legacy_row.get('sha256') == sha,
                     'NetBSD base assembler/linker differs from the original seed binding')
        if inspect_inputs:
            observed_original = canonical_existing(original)
            _require(observed_original == resolved_original,
                     'NetBSD base alias target differs from its pinned canonical target')
            file(dict(path=resolved_original, sha256=sha))
            file(dict(path=selected_path, sha256=sha))
        mapped[role] = original
    _require(set(mapped) == set(selected),
             'NetBSD base history does not bind every selected tool')
    return value['unknowns']


def validate_retained_r13_digest_history(value, name, descriptor, file, *, inspect_inputs=True):
    """Bind the r13 pkgsrc-built digest artifact without promoting its old receipt."""
    fields = {'kind', 'bootstrap_tools_acceptance', 'provider_acceptance',
              'provider_plan', 'provider_result', 'output_inventory',
              'semantic_report', 'member', 'unknowns'}
    _require(isinstance(value, dict) and set(value) == fields and
             value.get('kind') == 'retained-r13-pkgsrc-bootstrap-artifact',
             'unsupported retained r13 pkgsrc artifact history')
    _require(descriptor.get('admission') == _ADMISSION and
             descriptor.get('host') == {'os': 'NetBSD', 'cpu': 'amd64'},
             'retained r13 artifacts are limited to the NetBSD first-native seed')
    for key in ('provider_acceptance', 'provider_plan', 'provider_result', 'output_inventory'):
        _reference(value[key], 'r13 digest ' + key.replace('_', ' '))
    _reference(value['bootstrap_tools_acceptance'], 'existing pkgsrc bootstrap-tools acceptance')
    _reference(value['semantic_report'], 'r13 digest semantic report')
    from .bootstrap_netbsd_gcc16 import load_pkgsrc_bootstrap_tools
    bootstrap_tools = load_pkgsrc_bootstrap_tools(
        value['bootstrap_tools_acceptance']['path'],
        acceptance_sha256=value['bootstrap_tools_acceptance']['sha256'],
        inspect_inputs=inspect_inputs)
    _require(value['provider_plan'] == dict(bootstrap_tools.plan) and
             value['provider_result'] == dict(bootstrap_tools.result) and
             value['output_inventory'] == dict(bootstrap_tools.output_inventory),
             'digest history must retain the original pkgsrc provider records')
    bootstrap_plan = _read(dict(bootstrap_tools.plan), file, 'existing pkgsrc bootstrap-tools plan')
    bootstrap_result = _read(dict(bootstrap_tools.result), file, 'existing pkgsrc bootstrap-tools result')
    _read(dict(bootstrap_tools.output_inventory), file,
          'existing pkgsrc bootstrap-tools output inventory')
    acceptance = _read(value['provider_acceptance'], file, 'r13 digest acceptance')
    plan = _read(value['provider_plan'], file, 'r13 digest provider plan')
    result = _read(value['provider_result'], file, 'r13 digest provider result')
    inventory = _read(value['output_inventory'], file, 'r13 digest output inventory')
    _require(isinstance(acceptance, dict) and set(acceptance) == {
             'format', 'version', 'scope', 'status', 'plan', 'result', 'output_inventory',
             'export_acceptance', 'transfer_acceptance', 'compiler_seed', 'accepted_roles',
             'accepted_history_unknowns', 'checks', 'bootstrap_tools_acceptance',
             'semantic_report'} and
             acceptance.get('format') == 'emberbsd-bootstrap-pkgsrc-provider-acceptance' and
             type(acceptance.get('version')) is int and acceptance['version'] == 1 and
             acceptance.get('scope') == 'pkgsrc-bootstrap-tools' and
             acceptance.get('status') == 'PASS' and
             acceptance.get('accepted_roles') == ['digest'],
             'r13 digest requires a fresh digest-only independent acceptance receipt')
    _require(isinstance(plan, dict) and plan.get('format') ==
             'emberbsd-bootstrap-pkgsrc-provider-plan' and
             type(plan.get('version')) is int and plan['version'] == 1 and
             plan.get('scope') == 'pkgsrc-bootstrap-tools' and
             plan.get('host') == 'NetBSD11.0-amd64' and
             plan.get('purpose') == 'bootstrap-current-pkgsrc-tools',
             'r13 digest provider plan has an unsupported source scope')
    _require(all(plan.get(key) == bootstrap_plan.get(key) and
                 plan.get(key) == bootstrap_result.get(key)
                 for key in ('export_acceptance', 'transfer_acceptance', 'compiler_seed')),
             'r13 digest plan differs from the existing accepted pkgsrc tools source pins')
    _require(isinstance(result, dict) and result.get('format') ==
             'emberbsd-bootstrap-pkgsrc-provider-result' and
             type(result.get('version')) is int and result['version'] == 1 and
             result.get('scope') == 'pkgsrc-bootstrap-tools' and
             result.get('status') == 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW' and
             result.get('ordinary_provider_qualified') is False and
             result.get('bootstrap_plan') == value['provider_plan'] and
             result.get('export_acceptance') == plan.get('export_acceptance') and
             result.get('transfer_acceptance') == plan.get('transfer_acceptance') and
             result.get('compiler_seed') == plan.get('compiler_seed') and
             result.get('output_inventory') == value['output_inventory'] and
             result.get('output_root') == plan.get('prefix_root') and
             result.get('all_commands_drained') is True and
             result.get('inputs_revalidated_after_drain') is True and
             result.get('all_leases_closed') is True,
             'r13 digest provider result is not a drained, non-ordinary source build')
    checks = {'all_commands_drained': True, 'inputs_revalidated_after_drain': True,
              'all_leases_closed': True, 'source_export_matches': True,
              'output_inventory_matches': True}
    _require(acceptance.get('plan') == value['provider_plan'] and
             acceptance.get('result') == value['provider_result'] and
             acceptance.get('output_inventory') == value['output_inventory'] and
             acceptance.get('bootstrap_tools_acceptance') == value['bootstrap_tools_acceptance'] and
             acceptance.get('semantic_report') == value['semantic_report'] and
             acceptance.get('export_acceptance') == plan.get('export_acceptance') and
             acceptance.get('transfer_acceptance') == plan.get('transfer_acceptance') and
             acceptance.get('compiler_seed') == plan.get('compiler_seed') and
             acceptance.get('checks') == checks and
             all(type(acceptance['checks'].get(key)) is bool for key in checks),
             'r13 digest acceptance does not bind the exact source, result and output inputs')
    unknowns = value['unknowns']
    _require(isinstance(unknowns, list) and unknowns and
             all(isinstance(item, str) and item for item in unknowns) and
             unknowns == sorted(set(unknowns)) and
             acceptance.get('accepted_history_unknowns') == unknowns,
             'r13 digest history unknowns must be sorted and independently accepted')
    commands = result.get('commands')
    planned_commands = plan.get('commands')
    _require(isinstance(commands, list) and commands and isinstance(planned_commands, list) and
             len(commands) == len(planned_commands) and
             all(isinstance(step, dict) and set(step) ==
                 {'id', 'phase', 'cwd', 'argv', 'env', 'expected_exit'} for step in planned_commands) and
             len({step['id'] for step in planned_commands}) == len(planned_commands),
             'r13 digest provider result does not match a complete fixed command plan')
    fixed_fields = ('id', 'phase', 'cwd', 'argv', 'env', 'expected_exit')
    for planned, command in zip(planned_commands, commands):
        _require(isinstance(command, dict) and set(command) ==
                 set(fixed_fields) | {'actual_exit', 'log'} and
                 all(command.get(field) == planned[field] for field in fixed_fields) and
                 type(command.get('actual_exit')) is int and
                 command['actual_exit'] == planned['expected_exit'] == 0,
                 'r13 digest provider command differs from its exact plan or exit')
        _reference(command.get('log'), 'r13 digest provider command log')
        file(command['log'])
    prefix = _absolute(plan['prefix_root'])
    original_path = os.path.join(prefix, 'bin', 'digest')
    member = value['member']
    _require(isinstance(member, dict) and set(member) ==
             {'original_path', 'selected_path', 'sha256'} and
             member.get('original_path') == original_path,
             'r13 digest member must retain the source-built prefix/bin/digest')
    selected_path = _absolute(member['selected_path'])
    bundle_root = _absolute(descriptor['bundle_root'])
    _require(selected_path != bundle_root and inside(selected_path, bundle_root),
             'r13 digest selected copy must stay inside the seed bundle')
    _sha(member['sha256'])
    entry = inventory.get('bin/digest') if isinstance(inventory, dict) else None
    _require(isinstance(entry, dict) and entry.get('kind') == 'file' and
             entry.get('mode') == 0o755 and entry.get('sha256') == member['sha256'] and
             isinstance(entry.get('size'), int) and entry['size'] > 0,
             'r13 digest member differs from the complete provider output inventory')
    selected = descriptor.get('tools', {}).get('digest')
    _require(isinstance(selected, dict) and selected.get('provider') == name and
             selected.get('path') == selected_path and selected.get('sha256') == member['sha256'],
             'selected digest differs from the accepted r13 prefix artifact')
    semantic_report = _read(value['semantic_report'], file, 'r13 digest semantic report')
    _require(isinstance(semantic_report, dict) and set(semantic_report) ==
             {'format', 'version', 'tool', 'fixture', 'outputs'} and
             semantic_report.get('format') == 'emberbsd-netbsd-r13-digest-semantic-report' and
             type(semantic_report.get('version')) is int and semantic_report['version'] == 1 and
             semantic_report.get('tool') == dict(path=original_path, sha256=member['sha256']) and
             semantic_report.get('outputs') == _R13_DIGEST_GOLDENS,
             'r13 digest semantic report does not match pinned digest goldens')
    fixture = _reference(semantic_report['fixture'], 'r13 digest golden fixture')
    _require(fixture['sha256'] == _R13_DIGEST_GOLDEN_FIXTURE_SHA256,
             'r13 digest semantic fixture differs from the pinned golden bytes')
    if inspect_inputs:
        file(fixture)
    if inspect_inputs:
        file(dict(path=original_path, sha256=member['sha256']))
        file(dict(path=selected_path, sha256=member['sha256']))
        for observed_path in (original_path, selected_path):
            try:
                info = os.lstat(observed_path)
            except OSError as exc:
                raise SDKError('r13 source-built digest executable is unavailable') from exc
            _require(stat.S_ISREG(info.st_mode) and info.st_size == entry['size'] and
                     stat.S_IMODE(info.st_mode) == entry['mode'],
                     'r13 digest source or selected copy differs from the executable inventory')
        for key in ('export_acceptance', 'transfer_acceptance', 'compiler_seed'):
            _reference(plan.get(key), 'r13 digest provider ' + key.replace('_', ' '))
            file(plan[key])
    return unknowns
