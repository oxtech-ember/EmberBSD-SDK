# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Admit one accepted GCC16 compiler input for the first Ports-owned bmake."""

import hashlib
import io
import os
import platform
import re
import shutil
import signal
import stat
import tarfile
import threading
import time

from .errors import SDKError
from .inventory import sha256_file, snapshot_tree
from .native_toolset import _absolute, _bytes, _json, _require, _sha
from .paths import canonical_existing, canonical_new, entry, reject_overlap, verify_stable
from .process import ContextLocks, ProcessOwnershipError, capture, run_logged
from .jsonio import json_bytes, write_json

BOOTSTRAP_BMAKE_SEED_API = 1
COMPILER_SEED_FORMAT = 'emberbsd-bootstrap-compiler-seed'
SOURCE_FORMAT = 'emberbsd-bootstrap-bmake-source'
PLAN_FORMAT = 'emberbsd-bootstrap-bmake-plan'
RESULT_FORMAT = 'emberbsd-bootstrap-bmake-result'
ACCEPTANCE_FORMAT = 'emberbsd-bootstrap-bmake-acceptance'
COMPILER_AUDIT_FORMAT = 'emberbsd-bootstrap-bmake-compiler-seed-audit'
COMPILER_ACCEPTANCE_FORMAT = 'emberbsd-bootstrap-bmake-compiler-seed-acceptance'
PREFIX_INVENTORY_FORMAT = 'emberbsd-bootstrap-compiler-prefix-inventory'
SCOPE = 'bmake-bootstrap'
PORTS_VERSION = '20260912'
PORTS_ARCHIVE = 'bmake-20260912.tar.gz'
PORTS_ARCHIVE_SHA256 = 'b6bd32964cbe451be2838822c9d200b7c7e76a2a5947c03feb71dc6bd72988bd'
PORTS_PATCH = 'pkgsrc-bmake-20260912.patch'
PORTS_PATCH_SHA256 = 'fe90dcf2d7ffcbc5f70774a87646bd99b79b34ff90d427e0e9a27f501e4e8fd8'
PORTS_PROFILE = 'profiles/common-build-tools/bmake.md'
PORTS_SOURCES = 'profiles/common-build-tools/sources.tsv'
_BASE_LIBRARY_ROOTS = ('/usr/lib/', '/lib/')
_HOST_TOOLS = ('awk', 'basename', 'cat', 'chmod', 'cmp', 'cp', 'date', 'dirname', 'expr',
               'grep', 'head', 'id', 'install', 'ln', 'ls', 'mkdir', 'mv', 'pwd', 'rm', 'sed',
               'sort', 'tail', 'tr', 'uname', 'uniq', 'wc')
_FORBIDDEN_PATH_TOOLS = {'cc', 'c++', 'gcc', 'g++', 'clang', 'clang++', 'make', 'bmake', 'gmake'}


def _hash_ref(value, label):
    _require(isinstance(value, dict) and set(value) == {'path', 'sha256'},
             f'{label} needs an exact path and SHA256 reference')
    _absolute(value['path'])
    _sha(value['sha256'])
    return value


def _manifest_row(manifest, relative, expected_path, sha):
    _require(isinstance(manifest, dict), 'accepted GCC16 prefix manifest is not an object')
    row = manifest.get(relative)
    _require(isinstance(row, dict) and set(row) == {'sha256', 'size'} and
             _sha(row['sha256']) == sha and type(row['size']) is int and row['size'] > 0,
             f'accepted GCC16 component differs from its prefix manifest: {relative}')
    return dict(path=expected_path, sha256=sha)


def verify_prefix_inventory(prefix, manifest, inventory):
    """Require the current prefix tree and legacy file manifest to be complete."""
    prefix = _absolute(prefix)
    _require(isinstance(inventory, dict) and inventory,
             'independent compiler seed receipt omits the complete prefix inventory')
    current = snapshot_tree(prefix)
    _require(current == inventory, 'complete GCC16 prefix inventory changed')
    _require(isinstance(manifest, dict), 'accepted GCC16 prefix manifest is not an object')
    files = {relative: {'sha256': row['sha256'], 'size': row['size']}
             for relative, row in current.items() if row['kind'] == 'file'}
    _require(manifest == files,
             'accepted GCC16 file manifest does not match every regular file in the prefix')
    return current


def _guard_lock_root(lock_root, protected_paths):
    """Keep ContextLocks metadata outside every input, output, and work root."""
    lock_root = _absolute(lock_root)
    for path in protected_paths:
        try:
            reject_overlap(lock_root, _absolute(path))
        except SDKError as exc:
            raise SDKError('lock root overlaps a protected input or work path') from exc
    return lock_root


def _validate_compiler_acceptance(seed, accepted_ref, manifest_ref, inventory_ref):
    receipt_ref = _hash_ref(seed['acceptance_receipt'], 'independent compiler seed acceptance receipt')
    receipt = _json(receipt_ref)
    fields = {'format', 'version', 'scope', 'status', 'audit_report', 'accepted_record',
              'prefix_manifest', 'prefix_inventory', 'host', 'prefix', 'compiler',
              'driver_components', 'host_runtime', 'source_provenance'}
    _require(isinstance(receipt, dict) and set(receipt) == fields and
             receipt.get('format') == COMPILER_ACCEPTANCE_FORMAT and
             type(receipt.get('version')) is int and receipt['version'] == 1 and
             receipt.get('scope') == SCOPE and receipt.get('status') == 'PASS',
             'independent GCC16 compiler seed acceptance receipt is missing or invalid')
    _require(_hash_ref(receipt['accepted_record'], 'accepted record') == accepted_ref and
             _hash_ref(receipt['prefix_manifest'], 'prefix manifest') == manifest_ref and
             _hash_ref(receipt['prefix_inventory'], 'full prefix inventory') == inventory_ref,
             'independent compiler seed receipt does not bind the selected record and full prefix')
    _require(receipt['host'] == seed['host'] and receipt['prefix'] == seed['prefix'] and
             receipt['compiler'] == seed['compiler'] and
             receipt['driver_components'] == seed['driver_components'] and
             receipt['host_runtime'] == seed['host_runtime'],
             'independent compiler seed receipt differs from the selected compiler closure')
    accepted = _json(accepted_ref)
    _require(receipt['source_provenance'] == accepted.get('source_plan'),
             'independent compiler seed receipt differs from historical source provenance')
    audit_ref = _hash_ref(receipt['audit_report'], 'independent compiler seed audit report')
    audit = _json(audit_ref)
    audit_fields = {'format', 'version', 'scope', 'status', 'host', 'prefix', 'accepted_record',
                    'prefix_manifest', 'prefix_inventory', 'compiler', 'driver_components',
                    'host_runtime', 'source_provenance', 'checks', 'queries', 'scope_boundary'}
    _require(isinstance(audit, dict) and set(audit) == audit_fields and
             audit.get('format') == COMPILER_AUDIT_FORMAT and
             type(audit.get('version')) is int and audit['version'] == 1 and
             audit.get('scope') == SCOPE and
             audit.get('status') == 'PENDING_INDEPENDENT_REVIEW',
             'compiler seed audit evidence is not an unaccepted scoped report')
    for name, expected in (('host', seed['host']), ('prefix', seed['prefix']),
                           ('accepted_record', accepted_ref), ('prefix_manifest', manifest_ref),
                           ('prefix_inventory', inventory_ref), ('compiler', seed['compiler']),
                           ('driver_components', seed['driver_components']),
                           ('host_runtime', seed['host_runtime']),
                           ('source_provenance', receipt['source_provenance'])):
        observed = audit[name]
        if name in ('accepted_record', 'prefix_manifest', 'prefix_inventory'):
            observed = _hash_ref(observed, name.replace('_', ' '))
        _require(observed == expected,
                 f'independent compiler seed audit differs from accepted {name}')
    _require(audit['scope_boundary'] ==
             'native GCC16 identity queries and runtime closure only; no NativeToolset qualification',
             'compiler seed audit claims an unsupported acceptance scope')
    checks = audit['checks']
    _require(isinstance(checks, dict) and set(checks) ==
             {'identity_queries', 'runtime_closure', 'prefix_inventory'} and
             checks == {'identity_queries': 'PASS', 'runtime_closure': 'PASS',
                        'prefix_inventory': 'PASS'},
             'compiler seed audit did not pass all required scoped observations')
    expected_queries = [
        ([seed['compiler']['path'], '-dumpfullversion'], seed['compiler']['version']),
        ([seed['compiler']['path'], '-dumpmachine'], seed['compiler']['triplet']),
        ([seed['compiler']['path'], '-print-prog-name=as'], seed['driver_components']['as']['path']),
        ([seed['compiler']['path'], '-print-prog-name=ld'], seed['driver_components']['ld']['path']),
    ]
    queries = audit['queries']
    _require(isinstance(queries, list) and len(queries) == len(expected_queries),
             'compiler seed audit omits native GCC16 identity queries')
    for query, (argv, expected_stdout) in zip(queries, expected_queries):
        _require(isinstance(query, dict) and set(query) == {'argv', 'actual_exit', 'stdout'} and
                 query == {'argv': argv, 'actual_exit': 0, 'stdout': expected_stdout},
                 'compiler seed audit identity query differs from the selected driver')
    return receipt_ref, receipt, audit_ref, audit


def _ldd_dependencies(ldd_path, binary):
    output = capture([ldd_path, binary], env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'TZ': 'UTC'})
    dependencies = set()
    for line in output.stdout.decode('utf-8', 'strict').splitlines():
        if '=>' not in line:
            continue
        path = line.partition('=>')[2].strip().split()[0]
        _require(path.startswith(_BASE_LIBRARY_ROOTS) and '/..' not in path,
                 f'GCC16 bootstrap runtime escaped NetBSD base library directories: {path}')
        resolved = os.path.realpath(path)
        _require(resolved.startswith(_BASE_LIBRARY_ROOTS) and '/..' not in resolved,
                 f'GCC16 bootstrap runtime escaped NetBSD base library directories: {resolved}')
        dependencies.add(resolved)
    _require(dependencies, f'cannot identify the dynamic closure for {binary}')
    return dependencies


def _runtime_binary_paths(compiler_path, components):
    """Return executable driver members; static archives have no ldd closure."""
    return [compiler_path, *[row['path'] for role, row in components.items() if role != 'libgcc']]


def _requires_executable_component(role):
    return role in ('cc1', 'cc1plus', 'collect2')


def validate_compiler_seed(seed, *, inspect_inputs=True):
    """Validate the historical accepted GCC16 compiler-only seed.

    This never turns the compiler into a NativeToolset. System assembler and
    linker inputs remain explicit, hashed dependencies of this one bootstrap.
    """
    fields = {'format', 'version', 'scope', 'host', 'prefix', 'accepted_record',
              'prefix_manifest', 'prefix_inventory', 'acceptance_receipt', 'compiler',
              'driver_components', 'host_runtime'}
    _require(isinstance(seed, dict) and set(seed) == fields and
             seed.get('format') == COMPILER_SEED_FORMAT and type(seed.get('version')) is int and
             seed['version'] == 1 and seed.get('scope') == SCOPE,
             'unsupported bootstrap compiler seed')
    _require(seed['host'] == 'NetBSD11.0-amd64', 'bmake bootstrap requires the accepted NetBSD 11.0 amd64 seed')
    prefix = _absolute(seed['prefix'])
    accepted_ref = _hash_ref(seed['accepted_record'], 'accepted GCC16 record')
    manifest_ref = _hash_ref(seed['prefix_manifest'], 'accepted GCC16 prefix manifest')
    inventory_ref = _hash_ref(seed['prefix_inventory'], 'accepted GCC16 complete prefix inventory')
    _hash_ref(seed['acceptance_receipt'], 'independent GCC16 compiler seed acceptance receipt')
    compiler = seed['compiler']
    _require(isinstance(compiler, dict) and set(compiler) == {'path', 'sha256', 'version', 'triplet'},
             'invalid accepted GCC16 compiler identity')
    compiler_path = _absolute(compiler['path'])
    _require(compiler_path == os.path.join(prefix, 'bin/gcc') and
             compiler['version'] == '16.2.0' and
             compiler['triplet'] == 'x86_64-unknown-netbsd11.0',
             'compiler must be the accepted GCC16 seed')
    compiler_sha = _sha(compiler['sha256'])
    triplet, version = compiler['triplet'], compiler['version']
    driver_root = os.path.join(prefix, 'libexec/gcc', triplet, version)
    library_root = os.path.join(prefix, 'lib/gcc', triplet, version)
    expected = {
        'cc1': os.path.join(driver_root, 'cc1'),
        'cc1plus': os.path.join(driver_root, 'cc1plus'),
        'collect2': os.path.join(driver_root, 'collect2'),
        'libgcc': os.path.join(library_root, 'libgcc.a'),
        'as': '/usr/bin/as',
        'ld': '/usr/bin/ld',
    }
    components = seed['driver_components']
    _require(isinstance(components, dict) and set(components) == set(expected),
             'accepted GCC16 driver closure is incomplete')
    for role, path in expected.items():
        row = components[role]
        _require(isinstance(row, dict) and set(row) == {'path', 'sha256'} and
                 _absolute(row['path']) == path, 'compiler must be the accepted GCC16 seed')
        _sha(row['sha256'])
    runtime = seed['host_runtime']
    _require(isinstance(runtime, dict) and set(runtime) == {'ldd', 'libraries'},
             'accepted GCC16 host runtime closure is incomplete')
    ldd_ref = _hash_ref(runtime['ldd'], 'NetBSD ldd inspection tool')
    _require(ldd_ref['path'] == '/usr/bin/ldd', 'host runtime must use the explicit NetBSD ldd path')
    libraries = runtime['libraries']
    _require(isinstance(libraries, dict) and libraries and
             all(_absolute(path).startswith(_BASE_LIBRARY_ROOTS) for path in libraries),
             'GCC16 bootstrap runtime libraries must be explicit NetBSD base inputs')
    for path, digest in libraries.items():
        _sha(digest)

    if not inspect_inputs:
        return seed

    accepted = _json(accepted_ref)
    _require(accepted.get('host') == seed['host'] and accepted.get('prefix') == prefix and
             accepted.get('version') == version and accepted.get('triplet') == triplet and
             accepted.get('manifest_sha256') == manifest_ref['sha256'],
             'accepted GCC16 record identity or prefix manifest changed')
    source_plan = accepted.get('source_plan', {})
    _require(source_plan.get('compiler') == 'GCC16.2.0' and
             source_plan.get('kind') == 'native compiler seed, no board objects' and
             re.fullmatch(r'[0-9a-f]{40}', source_plan.get('ports', '')) and
             re.fullmatch(r'[0-9a-f]{40}', source_plan.get('pkgsrc', '')),
             'accepted record is not the historical native GCC16 compiler seed')
    _require(accepted.get('checks') == {
        'accept-c-run': 'native GCC16 C PASS\n',
        'accept-cxx-run': 'native GCC16 C++ TLS thread exception PASS\n'},
        'accepted record does not contain the native GCC16 run checks')
    _require('AArch64 cross compiler and board kernel pending' in accepted.get('boundary', ''),
             'accepted GCC16 record has an unsupported acceptance boundary')

    manifest = _json(manifest_ref)
    _require(hashlib.sha256(_bytes(manifest_ref)).hexdigest() == accepted['manifest_sha256'],
             'accepted GCC16 prefix manifest digest changed')
    inventory = _json(inventory_ref)
    _require(isinstance(inventory, dict) and
             set(inventory) == {'format', 'version', 'scope', 'prefix', 'entries'} and
             inventory.get('format') == PREFIX_INVENTORY_FORMAT and
             type(inventory.get('version')) is int and inventory['version'] == 1 and
             inventory.get('scope') == SCOPE and inventory.get('prefix') == prefix,
             'independent compiler seed receipt has an invalid full prefix inventory')
    verify_prefix_inventory(prefix, manifest, inventory['entries'])
    _validate_compiler_acceptance(seed, accepted_ref, manifest_ref, inventory_ref)
    _manifest_row(manifest, 'bin/gcc', compiler_path, compiler_sha)
    for role, path in expected.items():
        sha = components[role]['sha256']
        if role in ('as', 'ld'):
            _require(sha256_file(path) == sha, f'accepted GCC16 {role} input changed')
        else:
            relative = os.path.relpath(path, prefix).replace(os.sep, '/')
            _manifest_row(manifest, relative, path, sha)
    _require(sha256_file(compiler_path) == compiler_sha, 'accepted GCC16 compiler binary changed')
    _require(os.path.isdir(prefix) and os.path.realpath(prefix) == prefix,
             'accepted GCC16 prefix must remain a real directory')
    for role, path in expected.items():
        if role not in ('as', 'ld'):
            _require(os.path.realpath(path).startswith(prefix + os.sep) and
                     stat.S_ISREG(os.stat(path).st_mode) and
                     (not _requires_executable_component(role) or os.access(path, os.X_OK)),
                     f'accepted GCC16 component is missing or escaped: {role}')
    _require(sha256_file(ldd_ref['path']) == ldd_ref['sha256'], 'NetBSD ldd input changed')
    runtime_paths = set()
    for path in _runtime_binary_paths(compiler_path, components):
        runtime_paths.update(_ldd_dependencies(ldd_ref['path'], path))
    _require(runtime_paths == set(libraries),
             'accepted GCC16, assembler, and linker runtime closure changed')
    for path, digest in libraries.items():
        _require(sha256_file(path) == digest, f'accepted GCC16 base runtime library changed: {path}')
    _require(platform.system() == 'NetBSD' and platform.machine() in ('x86_64', 'amd64'),
             'accepted GCC16 seed must be inspected on its native NetBSD amd64 host')
    for option, expected_value in (('-dumpfullversion', version), ('-dumpmachine', triplet),
                                   ('-print-prog-name=as', expected['as']),
                                   ('-print-prog-name=ld', expected['ld'])):
        query = capture([compiler_path, option], env={'PATH': os.path.dirname(compiler_path),
                                                      'LC_ALL': 'C', 'TZ': 'UTC'})
        observed = query.stdout.decode('utf-8', 'strict').strip()
        _require(observed == expected_value,
                 f'accepted GCC16 driver route changed for {option}: {observed!r}')
    return seed


def describe_compiler_seed(accepted_record, prefix_manifest, prefix,
                           acceptance_receipt, acceptance_receipt_sha256):
    """Bind the compiler-only selector to a separately issued acceptance receipt."""
    accepted_record = _absolute(accepted_record)
    prefix_manifest = _absolute(prefix_manifest)
    prefix = _absolute(prefix)
    accepted_ref = dict(path=accepted_record, sha256=sha256_file(accepted_record))
    manifest_ref = dict(path=prefix_manifest, sha256=sha256_file(prefix_manifest))
    acceptance_ref = _hash_ref(dict(path=_absolute(acceptance_receipt),
                                    sha256=acceptance_receipt_sha256),
                               'independent GCC16 compiler seed acceptance receipt')
    accepted, manifest = _json(accepted_ref), _json(manifest_ref)
    receipt = _json(acceptance_ref)
    _require(accepted.get('prefix') == prefix and accepted.get('host') == 'NetBSD11.0-amd64',
             'existing accepted record is not the NetBSD 11.0 amd64 GCC16 seed')
    version, triplet = accepted['version'], accepted['triplet']
    driver_root = os.path.join(prefix, 'libexec/gcc', triplet, version)
    library_root = os.path.join(prefix, 'lib/gcc', triplet, version)
    component_paths = {
        'cc1': os.path.join(driver_root, 'cc1'),
        'cc1plus': os.path.join(driver_root, 'cc1plus'),
        'collect2': os.path.join(driver_root, 'collect2'),
        'libgcc': os.path.join(library_root, 'libgcc.a'),
        'as': '/usr/bin/as',
        'ld': '/usr/bin/ld',
    }
    compiler_path = os.path.join(prefix, 'bin/gcc')
    compiler = dict(path=compiler_path, sha256=manifest['bin/gcc']['sha256'],
                    version=version, triplet=triplet)
    components = {}
    for role, path in component_paths.items():
        if role in ('as', 'ld'):
            digest = sha256_file(path)
        else:
            relative = os.path.relpath(path, prefix).replace(os.sep, '/')
            row = manifest.get(relative)
            _require(isinstance(row, dict), f'accepted GCC16 manifest omits {role}')
            digest = row.get('sha256')
        components[role] = dict(path=path, sha256=_sha(digest))
    ldd_path = '/usr/bin/ldd'
    runtime_paths = set()
    for path in _runtime_binary_paths(compiler_path, components):
        runtime_paths.update(_ldd_dependencies(ldd_path, path))
    runtime = dict(ldd=dict(path=ldd_path, sha256=sha256_file(ldd_path)),
                   libraries={path: sha256_file(path) for path in sorted(runtime_paths)})
    seed = dict(format=COMPILER_SEED_FORMAT, version=1, scope=SCOPE, host=accepted['host'],
                prefix=prefix, accepted_record=accepted_ref, prefix_manifest=manifest_ref,
                prefix_inventory=receipt.get('prefix_inventory'), acceptance_receipt=acceptance_ref,
                compiler=compiler, driver_components=components, host_runtime=runtime)
    validate_compiler_seed(seed, inspect_inputs=True)
    return seed


def audit_compiler_seed_candidate(accepted_record, accepted_record_sha256,
                                  prefix_manifest, prefix_manifest_sha256, prefix,
                                  inventory_output, report_output):
    """Write evidence for independent review without issuing compiler acceptance."""
    accepted_ref = _hash_ref(dict(path=_absolute(accepted_record),
                                  sha256=accepted_record_sha256), 'historical GCC16 record')
    manifest_ref = _hash_ref(dict(path=_absolute(prefix_manifest),
                                  sha256=prefix_manifest_sha256), 'historical GCC16 file manifest')
    prefix = _absolute(prefix)
    inventory_output, report_output = _absolute(inventory_output), _absolute(report_output)
    _require(inventory_output != report_output, 'compiler seed audit outputs must be separate files')
    for output in (inventory_output, report_output):
        for protected in (accepted_ref['path'], manifest_ref['path'], prefix):
            reject_overlap(output, protected)
    _require(platform.system() == 'NetBSD' and platform.machine() in ('x86_64', 'amd64'),
             'compiler seed candidate audit must run on the native NetBSD amd64 host')
    _require(sha256_file(accepted_ref['path']) == accepted_ref['sha256'] and
             sha256_file(manifest_ref['path']) == manifest_ref['sha256'],
             'historical GCC16 inputs differ from the hashes supplied by the independent gate')
    accepted, manifest = _json(accepted_ref), _json(manifest_ref)
    host, version, triplet = 'NetBSD11.0-amd64', '16.2.0', 'x86_64-unknown-netbsd11.0'
    _require(accepted.get('host') == host and accepted.get('prefix') == prefix and
             accepted.get('version') == version and accepted.get('triplet') == triplet and
             accepted.get('manifest_sha256') == manifest_ref['sha256'],
             'historical GCC16 record identity or manifest reference changed')
    source_provenance = accepted.get('source_plan', {})
    _require(source_provenance.get('compiler') == 'GCC16.2.0' and
             source_provenance.get('kind') == 'native compiler seed, no board objects' and
             re.fullmatch(r'[0-9a-f]{40}', source_provenance.get('ports', '')) and
             re.fullmatch(r'[0-9a-f]{40}', source_provenance.get('pkgsrc', '')),
             'historical record does not identify the native GCC16 compiler seed provenance')
    _require(accepted.get('checks') == {
        'accept-c-run': 'native GCC16 C PASS\n',
        'accept-cxx-run': 'native GCC16 C++ TLS thread exception PASS\n'},
        'historical record omits native GCC16 C/C++ runtime checks')
    _require('AArch64 cross compiler and board kernel pending' in accepted.get('boundary', ''),
             'historical GCC16 record has an unsupported acceptance boundary')
    inventory_entries = snapshot_tree(prefix)
    verify_prefix_inventory(prefix, manifest, inventory_entries)
    inventory_document = dict(format=PREFIX_INVENTORY_FORMAT, version=1, scope=SCOPE,
                              prefix=prefix, entries=inventory_entries)
    write_json(inventory_output, inventory_document)
    inventory_ref = dict(path=inventory_output, sha256=sha256_file(inventory_output))

    compiler_path = os.path.join(prefix, 'bin/gcc')
    compiler = dict(path=compiler_path, sha256=manifest['bin/gcc']['sha256'],
                    version=version, triplet=triplet)
    driver_root = os.path.join(prefix, 'libexec/gcc', triplet, version)
    library_root = os.path.join(prefix, 'lib/gcc', triplet, version)
    component_paths = {
        'cc1': os.path.join(driver_root, 'cc1'),
        'cc1plus': os.path.join(driver_root, 'cc1plus'),
        'collect2': os.path.join(driver_root, 'collect2'),
        'libgcc': os.path.join(library_root, 'libgcc.a'),
        'as': '/usr/bin/as',
        'ld': '/usr/bin/ld',
    }
    components = {}
    for role, path in component_paths.items():
        if role in ('as', 'ld'):
            digest = sha256_file(path)
        else:
            relative = os.path.relpath(path, prefix).replace(os.sep, '/')
            row = manifest.get(relative)
            _require(isinstance(row, dict), f'historical GCC16 file manifest omits {role}')
            digest = row.get('sha256')
        components[role] = dict(path=path, sha256=_sha(digest))

    ldd_path = '/usr/bin/ldd'
    runtime_paths = set()
    for path in _runtime_binary_paths(compiler_path, components):
        runtime_paths.update(_ldd_dependencies(ldd_path, path))
    runtime = dict(ldd=dict(path=ldd_path, sha256=sha256_file(ldd_path)),
                   libraries={path: sha256_file(path) for path in sorted(runtime_paths)})
    queries = []
    for option, expected in (('-dumpfullversion', version), ('-dumpmachine', triplet),
                             ('-print-prog-name=as', component_paths['as']),
                             ('-print-prog-name=ld', component_paths['ld'])):
        argv = [compiler_path, option]
        returned = capture(argv, env={'PATH': os.path.dirname(compiler_path),
                                      'LC_ALL': 'C', 'TZ': 'UTC'})
        observed = returned.stdout.decode('utf-8', 'strict').strip()
        _require(observed == expected, f'GCC16 candidate identity query differs: {option}')
        queries.append(dict(argv=argv, actual_exit=returned.returncode, stdout=observed))
    report = dict(format=COMPILER_AUDIT_FORMAT, version=1, scope=SCOPE,
                  status='PENDING_INDEPENDENT_REVIEW', host=host, prefix=prefix,
                  accepted_record=accepted_ref, prefix_manifest=manifest_ref,
                  prefix_inventory=inventory_ref, compiler=compiler,
                  driver_components=components, host_runtime=runtime,
                  source_provenance=source_provenance,
                  checks=dict(prefix_inventory='PASS', identity_queries='PASS',
                              runtime_closure='PASS'),
                  queries=queries,
                  scope_boundary='native GCC16 identity queries and runtime closure only; no NativeToolset qualification')
    write_json(report_output, report)
    return report


def _file_ref(path):
    path = _absolute(path)
    return dict(path=path, sha256=sha256_file(path))


def describe_bmake_source(ports_asserted_revision, profile_path, sources_path, patch_path,
                          archive_path, source_root):
    source = dict(format=SOURCE_FORMAT, version=1, scope=SCOPE,
                  ports_source=dict(repository='https://github.com/oxtech-ember/EmberBSD-Ports',
                                    asserted_revision=ports_asserted_revision),
                  profile=_file_ref(profile_path), sources=_file_ref(sources_path),
                  patch=_file_ref(patch_path),
                  archive=dict(path=_absolute(archive_path), sha256=PORTS_ARCHIVE_SHA256,
                               url='https://www.crufty.net/ftp/pub/sjg/bmake-20260912.tar.gz',
                               version=PORTS_VERSION),
                  root=_absolute(source_root))
    validate_source_descriptor(source, inspect_inputs=True)
    return source


def select_host_utilities():
    """Select and hash only the fixed OS utility roster used by configure and sh."""
    result = {'sh': dict(path='/bin/sh', sha256=sha256_file('/bin/sh'))}
    for role in _HOST_TOOLS:
        candidates = ('/usr/bin/' + role, '/bin/' + role)
        selected = next((path for path in candidates if os.path.isfile(path) and os.access(path, os.X_OK)), None)
        _require(selected is not None, f'NetBSD base utility is unavailable: {role}')
        result[role] = dict(path=selected, sha256=sha256_file(selected))
    _tool_map(result, inspect_inputs=True)
    return result


def _source_archive_tree(reference):
    """Read every directory and regular file from the pinned bmake archive."""
    data = _bytes(reference, limit=16 * 1024 * 1024)
    files, directories, names = {}, set(), {}
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
            for member in archive:
                name = member.name[:-1] if member.isdir() and member.name.endswith('/') else member.name
                parts = name.split('/')
                _require(parts[0] == 'bmake' and not member.name.startswith('/') and
                         all(part not in ('', '.', '..') for part in parts),
                         'bmake source archive contains an unsafe path')
                relative = '/'.join(parts[1:])
                folded = relative.casefold()
                if member.isdir():
                    if relative:
                        _require(folded not in names,
                                 'bmake source archive contains a duplicate path')
                        names[folded] = 'directory'
                        directories.add(folded)
                    continue
                _require(member.isfile(), 'bmake source archive contains a non-regular member')
                _require(relative and folded not in names,
                         'bmake source archive contains a duplicate path')
                names[folded] = 'file'
                source = archive.extractfile(member)
                _require(source is not None, 'cannot read bmake source archive member')
                content = source.read(16 * 1024 * 1024 + 1)
                _require(len(content) <= 16 * 1024 * 1024,
                         'bmake source member exceeds the size limit')
                files[folded] = (relative, hashlib.sha256(content).hexdigest(),
                                 bool(member.mode & 0o111), len(content))
                parent = relative.rpartition('/')[0]
                while parent:
                    directories.add(parent.casefold())
                    parent = parent.rpartition('/')[0]
    except (OSError, tarfile.TarError) as exc:
        raise SDKError(f'invalid official bmake source archive: {exc}') from exc
    _require(files and not (files.keys() & directories),
             'official bmake source archive is empty or has a file/directory collision')
    return files, directories


def _archive_reference(archive):
    """Reduce Ports archive metadata to the exact file reference accepted by _bytes."""
    _require(isinstance(archive, dict) and set(archive) == {'path', 'sha256', 'url', 'version'},
             'invalid common Ports bmake archive metadata')
    return {'path': _absolute(archive['path']), 'sha256': _sha(archive['sha256'])}


def _source_tree(root):
    root = _absolute(root)
    _require(canonical_existing(root) == root and os.path.isdir(root),
             'bmake source tree must be a real directory')
    files, directories, names = {}, set(), {}
    for directory, dirs, file_names in os.walk(root, topdown=True, followlinks=False):
        for name in list(dirs):
            path = os.path.join(directory, name)
            info = os.lstat(path)
            _require(stat.S_ISDIR(info.st_mode), 'bmake source tree contains a link or special directory')
            relative = os.path.relpath(path, root).replace(os.sep, '/')
            folded = relative.casefold()
            _require(folded not in names, 'bmake source tree contains a case-folding duplicate')
            names[folded] = 'directory'
            directories.add(folded)
            verify_stable(path, info)
        for name in file_names:
            path = os.path.join(directory, name)
            info = os.lstat(path)
            _require(stat.S_ISREG(info.st_mode), 'bmake source tree contains a link or special file')
            relative = os.path.relpath(path, root).replace(os.sep, '/')
            folded = relative.casefold()
            _require(folded not in names, 'bmake source tree contains a case-folding duplicate')
            names[folded] = 'file'
            files[folded] = (relative, sha256_file(path), bool(info.st_mode & 0o111), info.st_size)
            verify_stable(path, info)
    return files, directories


def validate_source_descriptor(source, *, inspect_inputs=True):
    """Bind the official bmake source to the current common Ports owner."""
    fields = {'format', 'version', 'scope', 'ports_source', 'profile', 'sources', 'patch', 'archive', 'root'}
    _require(isinstance(source, dict) and set(source) == fields and
             source.get('format') == SOURCE_FORMAT and type(source.get('version')) is int and
             source['version'] == 1 and source.get('scope') == SCOPE,
             'unsupported Ports bmake source descriptor')
    ports_source = source['ports_source']
    _require(isinstance(ports_source, dict) and
             set(ports_source) == {'repository', 'asserted_revision'} and
             ports_source.get('repository') == 'https://github.com/oxtech-ember/EmberBSD-Ports',
             'bmake source does not name the common Ports owner')
    _require(isinstance(ports_source.get('asserted_revision'), str) and
             re.fullmatch(r'[0-9a-f]{40}', ports_source['asserted_revision']),
             'Ports revision must be an asserted revision, not verified Git provenance')
    profile_ref, sources_ref, patch_ref = (
        _hash_ref(source['profile'], 'common Ports bmake profile'),
        _hash_ref(source['sources'], 'common Ports source manifest'),
        _hash_ref(source['patch'], 'common Ports bmake import patch'))
    archive = source['archive']
    _require(isinstance(archive, dict) and set(archive) == {'path', 'sha256', 'url', 'version'} and
             archive['version'] == PORTS_VERSION and archive['sha256'] == PORTS_ARCHIVE_SHA256 and
             archive['url'] == 'https://www.crufty.net/ftp/pub/sjg/bmake-20260912.tar.gz',
             'bmake source must use the common Ports 20260912 release pin')
    archive_ref = _archive_reference(archive)
    root = _absolute(source['root'])
    if not inspect_inputs:
        return source
    profile_text = _bytes(profile_ref).decode('utf-8', 'strict')
    source_text = _bytes(sources_ref).decode('utf-8', 'strict')
    patch_bytes = _bytes(patch_ref, limit=16 * 1024 * 1024)
    _require('Shared bmake 20260912 source' in profile_text and
             'Both run `configure`' in profile_text and
             '`make-bootstrap.sh` build route' in profile_text and
             'No extra `bmake/` directory' in profile_text,
             'common Ports profile no longer describes the shared bootstrap route')
    pins = {}
    for line in source_text.splitlines():
        if not line or line.startswith('#'):
            continue
        columns = line.split('\t')
        _require(len(columns) == 3 and columns[0] not in pins,
                 'common Ports source manifest is malformed or ambiguous')
        pins[columns[0]] = (columns[1], columns[2])
    _require(pins.get(PORTS_ARCHIVE) == (PORTS_ARCHIVE_SHA256, archive['url']) and
             pins.get(PORTS_PATCH) == (PORTS_PATCH_SHA256,
                 'https://github.com/oxtech-ember/EmberBSD-Ports/blob/main/profiles/common-build-tools/patches/' + PORTS_PATCH),
             'common Ports source manifest does not bind the exact bmake release and import patch')
    _require(hashlib.sha256(patch_bytes).hexdigest() == PORTS_PATCH_SHA256 and
             b'PKGNAME=\tbmake-20260912' in patch_bytes,
             'common Ports import patch does not select the pinned bmake recipe')
    _require(sha256_file(archive_ref['path']) == PORTS_ARCHIVE_SHA256,
             'official bmake archive differs from the common Ports pin')
    expected_files, expected_directories = _source_archive_tree(archive_ref)
    actual_files, actual_directories = _source_tree(root)
    expected = expected_files
    actual = actual_files
    _require(actual == expected, 'extracted bmake tree differs from every pinned archive byte and mode')
    _require(actual_directories == expected_directories,
             'extracted bmake directory layout differs from its pinned archive')
    required = {'version', 'configure', 'make-bootstrap.sh.in', 'boot-strap', 'license'}
    _require(required <= set(expected), 'pinned bmake source omits a required bootstrap file')
    version_text = open(os.path.join(root, 'VERSION'), 'rb').read(4096).decode('ascii', 'strict')
    _require('20260912' in version_text, 'extracted bmake source version differs from Ports')
    return source


def _tool_map(host_tools, *, inspect_inputs):
    _require(isinstance(host_tools, dict) and set(host_tools) == {'sh', *_HOST_TOOLS},
             'first-bmake build requires its complete fixed host utility roster')
    for role, row in host_tools.items():
        _require(isinstance(row, dict) and set(row) == {'path', 'sha256'},
                 'first-bmake host utility needs an exact path and hash')
        path = _absolute(row['path'])
        _sha(row['sha256'])
        allowed = ('/bin/', '/usr/bin/')
        _require(path.startswith(allowed) and os.path.basename(path) == role and
                 role not in _FORBIDDEN_PATH_TOOLS,
                 'first-bmake PATH may contain only the fixed non-compiler OS utilities')
        if role == 'sh':
            _require(path == '/bin/sh', 'first-bmake shell must be the explicit NetBSD base shell')
        if inspect_inputs:
            info = os.stat(path)
            resolved = canonical_existing(path)
            _require((resolved.startswith('/bin/') or resolved.startswith('/usr/bin/')) and
                     stat.S_ISREG(info.st_mode) and os.access(path, os.X_OK) and
                     sha256_file(path) == row['sha256'], f'first-bmake host utility changed: {role}')
    return host_tools


def bmake_steps(seed, plan):
    """Create the only supported first-bmake command route; no make is called."""
    compiler = seed['compiler']['path']
    shell = plan['host_tools']['sh']['path']
    utility_bin, work = plan['utility_bin'], plan['work_root']
    env = {'PATH': utility_bin, 'CC': compiler, 'CFLAGS': '-O2 -g', 'CPPFLAGS': '',
           'LDFLAGS': '', 'TR': os.path.join(utility_bin, 'tr'),
           'SHELL': shell, 'CONFIG_SHELL': shell, 'LC_ALL': 'C', 'TZ': 'UTC',
           'TMPDIR': os.path.join(work, 'tmp')}
    options = ['--prefix=/usr/pkg', '--with-default-sys-path=/usr/pkg/share/mk',
               '--with-force-machine-arch=' + plan['machine_arch'],
               '--sysconfdir=/usr/pkg/etc', '--with-defshell=sh=' + shell]
    source_root = os.path.join(work, 'source')
    return [
        dict(id='configure', phase='configure', cwd=source_root,
             argv=[shell, './configure', *options], env=dict(env), expected_exit=0),
        dict(id='make-bootstrap', phase='bootstrap', cwd=source_root,
             argv=[shell, 'make-bootstrap.sh'], env=dict(env), expected_exit=0),
    ]


def validate_plan(plan, seed, source, *, seed_ref=None, source_ref=None, inspect_inputs=True):
    fields = {'format', 'version', 'scope', 'purpose', 'seed_descriptor', 'source_descriptor',
              'host', 'machine_arch', 'work_root', 'utility_bin', 'output_binary',
              'host_tools', 'resources', 'commands'}
    _require(isinstance(plan, dict) and set(plan) == fields and
             plan.get('format') == PLAN_FORMAT and type(plan.get('version')) is int and
             plan['version'] == 1 and plan.get('scope') == SCOPE and
             plan.get('purpose') == 'produce-first-bmake',
             'unsupported first-bmake producer plan')
    _require(plan['host'] == seed['host'] and plan['machine_arch'] == 'x86_64',
             'first-bmake plan has an unsupported target host or machine architecture')
    for name, expected_ref in (('seed_descriptor', seed_ref), ('source_descriptor', source_ref)):
        ref = _hash_ref(plan[name], name.replace('_', ' '))
        if expected_ref is not None:
            _require(ref == expected_ref, 'first-bmake plan descriptor reference changed')
    work = _absolute(plan['work_root'])
    utility_bin = _absolute(plan['utility_bin'])
    output = _absolute(plan['output_binary'])
    _require(utility_bin == os.path.join(work, 'bin') and
             output == os.path.join(work, 'source/bmake') and
             plan['commands'] == bmake_steps(seed, plan),
             'first-bmake commands differ from the fixed Ports build route')
    _tool_map(plan['host_tools'], inspect_inputs=inspect_inputs)
    resources = plan['resources']
    _require(isinstance(resources, dict) and set(resources) == {'lock_root', 'runtime_seconds'} and
             type(resources['runtime_seconds']) is int and 0 < resources['runtime_seconds'] <= 1800,
             'first-bmake build budget is invalid')
    sdk_root = os.path.realpath(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    seed_refs = (_compiler_seed_references(seed) if inspect_inputs else list(_references(seed)))
    protected = [work, utility_bin, output, seed['prefix'], source['root'],
                 _archive_reference(source['archive'])['path'], sdk_root,
                 *seed_refs, *list(_references(source)),
                 *seed['host_runtime']['libraries'],
                 *(row['path'] for row in plan['host_tools'].values())]
    _guard_lock_root(resources['lock_root'], protected)
    _require(not (_FORBIDDEN_PATH_TOOLS & set(plan['host_tools'])),
             'system compiler or make may not substitute through PATH')
    if inspect_inputs:
        _require(canonical_new(work) == work, 'first-bmake work directory has aliased ancestors')
        for path in (seed['prefix'], source['root'], source['archive']['path'],
                     source['profile']['path'], source['sources']['path'], source['patch']['path']):
            reject_overlap(work, path)
    return plan


def create_plan(seed_ref, source_ref, work_root, lock_root, host_tools, *, runtime_seconds=900):
    """Create the fixed producer plan after checking its data-only selectors."""
    seed_ref, source_ref = _hash_ref(seed_ref, 'compiler seed'), _hash_ref(source_ref, 'source descriptor')
    seed, source = _json(seed_ref), _json(source_ref)
    validate_compiler_seed(seed, inspect_inputs=False)
    validate_source_descriptor(source, inspect_inputs=False)
    work = _absolute(work_root)
    plan = dict(format=PLAN_FORMAT, version=1, scope=SCOPE, purpose='produce-first-bmake',
                seed_descriptor=seed_ref, source_descriptor=source_ref, host=seed['host'],
                machine_arch='x86_64', work_root=work, utility_bin=os.path.join(work, 'bin'),
                output_binary=os.path.join(work, 'source/bmake'), host_tools=host_tools,
                resources=dict(lock_root=_absolute(lock_root), runtime_seconds=runtime_seconds),
                commands=[])
    plan['commands'] = bmake_steps(seed, plan)
    return validate_plan(plan, seed, source, seed_ref=seed_ref, source_ref=source_ref,
                         inspect_inputs=False)


def _references(value):
    if isinstance(value, dict):
        if set(value) == {'path', 'sha256'}:
            yield _absolute(value['path'])
        for child in value.values():
            yield from _references(child)
    elif isinstance(value, list):
        for child in value:
            yield from _references(child)


def _compiler_seed_references(seed):
    """Include receipt and audit references loaded through the selected seed."""
    references = list(_references(seed))
    receipt_ref = _hash_ref(seed['acceptance_receipt'], 'independent compiler seed acceptance receipt')
    receipt = _json(receipt_ref)
    references.extend(_references(receipt))
    audit_ref = _hash_ref(receipt['audit_report'], 'independent compiler seed audit report')
    references.extend(_references(_json(audit_ref)))
    return references


def _source_script_guard(path, seed, plan):
    with open(path, 'rb') as handle:
        script = handle.read(1024 * 1024 + 1)
    force_arch = (b'-DFORCE_MACHINE_ARCH=\\"' +
                  plan['machine_arch'].encode('ascii') + b'\\"')
    _require(len(script) <= 1024 * 1024 and
             b'CC="' + seed['compiler']['path'].encode('ascii') + b'"' in script and
             b'DEFAULT_SYS_PATH="/usr/pkg/share/mk"' in script and
             force_arch in script,
             'configure did not bind the accepted GCC16 driver and Ports recipe inputs')
    _require(b'CC="cc"' not in script and b'CC="gcc"' not in script,
             'configured bmake bootstrap script selected a system compiler')


def run_producer(seed_ref, source_ref, plan_ref):
    """Run the fixed first-bmake source route once; never qualify ordinary tools."""
    _require(threading.current_thread() is threading.main_thread(),
             'first-bmake producer must run on the main thread')
    seed_ref = _hash_ref(seed_ref, 'compiler seed')
    source_ref = _hash_ref(source_ref, 'source descriptor')
    plan_ref = _hash_ref(plan_ref, 'producer plan')
    seed, source, plan = _json(seed_ref), _json(source_ref), _json(plan_ref)
    validate_compiler_seed(seed, inspect_inputs=True)
    validate_source_descriptor(source, inspect_inputs=True)
    validate_plan(plan, seed, source, seed_ref=seed_ref, source_ref=source_ref,
                  inspect_inputs=True)
    work = plan['work_root']
    lock_root = canonical_existing(plan['resources']['lock_root'])
    _require(entry(work) is None and canonical_new(work) == work,
             'first-bmake producer work must be a fresh directory')
    shared = set([*list(_references(seed_ref)), *list(_references(source_ref)),
                  *list(_references(plan_ref)), *list(_references(seed)),
                  *list(_references(source)), _archive_reference(source['archive'])['path'],
                  *list(_references(plan)),
                  seed['prefix'], source['root']])
    shared.update(_compiler_seed_references(seed))
    for row in seed['driver_components'].values():
        shared.add(row['path'])
    shared.update(seed['host_runtime']['libraries'])
    shared.update(row['path'] for row in plan['host_tools'].values())
    sdk_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    shared.add(os.path.realpath(sdk_root))
    for path in shared:
        reject_overlap(work, path)
    _guard_lock_root(lock_root, [work, *shared])
    with ContextLocks(sorted(shared), lock_root, shared=True), ContextLocks([work], lock_root):
        validate_compiler_seed(_json(seed_ref), inspect_inputs=True)
        source = _json(source_ref)
        validate_source_descriptor(source, inspect_inputs=True)
        plan = _json(plan_ref)
        validate_plan(plan, seed, source, seed_ref=seed_ref, source_ref=source_ref,
                      inspect_inputs=True)
        _require(entry(work) is None, 'first-bmake producer work was claimed before dispatch')
        os.mkdir(work)
        os.mkdir(os.path.join(work, 'bin'))
        os.mkdir(os.path.join(work, 'tmp'))
        source_work = os.path.join(work, 'source')
        shutil.copytree(source['root'], source_work, symlinks=False)
        _require(_source_tree(source_work) == _source_archive_tree(_archive_reference(source['archive'])),
                 'staged bmake source differs from the pinned Ports archive')
        for role, row in plan['host_tools'].items():
            os.symlink(row['path'], os.path.join(plan['utility_bin'], role))
        result_path = os.path.join(work, 'result.json')
        result = dict(format=RESULT_FORMAT, version=1, scope=SCOPE,
                      status='RUNNING', seed_descriptor=seed_ref, source_descriptor=source_ref,
                      bootstrap_plan=plan_ref, commands=[], all_commands_drained=False,
                      evidence_level='accepted-GCC16-command-routes-and-actual-exits',
                      ordinary_provider_qualified=False, output_binary=plan['output_binary'])
        write_json(result_path, result)
        deadline = time.monotonic() + plan['resources']['runtime_seconds']
        try:
            for step in plan['commands']:
                actual = dict(step)
                log = os.path.join(work, step['id'] + '.log')
                try:
                    remaining = deadline - time.monotonic()
                    _require(remaining > 0, 'first-bmake runtime budget expired before next command')
                    try:
                        returned = run_logged(step['argv'], log, cwd=step['cwd'], env=step['env'],
                                              timeout_seconds=remaining)
                    except ProcessOwnershipError as exc:
                        actual.update(actual_exit=None, error=str(exc), unfinalized_log_path=log)
                        result['commands'].append(actual)
                        result.update(status='HOLD_UNPROVEN_DRAIN', error=str(exc), owned_sid=exc.sid)
                        write_json(result_path, result, exclusive=False)
                        print(f'first-bmake HOLD: {exc}; all input and work leases retained',
                              file=__import__('sys').stderr, flush=True)
                        from .bootstrap_pilot import _hold
                        _hold()
                    except Exception as exc:
                        actual['actual_exit'] = getattr(exc, 'returncode', None)
                        raise
                    actual['actual_exit'] = returned.returncode
                    if step['id'] == 'configure':
                        _source_script_guard(os.path.join(source_work, 'make-bootstrap.sh'), seed, plan)
                except BaseException as exc:
                    actual.setdefault('actual_exit', getattr(exc, 'returncode', None))
                    actual['error'] = str(exc)
                    raise
                finally:
                    if result['status'] != 'HOLD_UNPROVEN_DRAIN':
                        if entry(log) is not None:
                            actual['log'] = dict(path=log, sha256=sha256_file(log))
                        result['commands'].append(actual)
                        write_json(result_path, result, exclusive=False)
            info = os.lstat(plan['output_binary'])
            _require(stat.S_ISREG(info.st_mode) and os.access(plan['output_binary'], os.X_OK),
                     'first-bmake source route did not produce an executable regular file')
            result.update(status='COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW',
                          output=dict(path=plan['output_binary'], sha256=sha256_file(plan['output_binary']),
                                      mode=stat.S_IMODE(info.st_mode)))
        except BaseException as exc:
            if result['status'] != 'HOLD_UNPROVEN_DRAIN':
                result.update(status='FAILED', error=str(exc))
        result['all_commands_drained'] = result['status'] != 'HOLD_UNPROVEN_DRAIN'
        if result['all_commands_drained']:
            try:
                validate_compiler_seed(_json(seed_ref), inspect_inputs=True)
                validate_source_descriptor(_json(source_ref), inspect_inputs=True)
                validate_plan(_json(plan_ref), seed, source, seed_ref=seed_ref,
                              source_ref=source_ref, inspect_inputs=True)
                result['inputs_revalidated_after_drain'] = True
            except SDKError as exc:
                result.update(status='FAILED', error=str(exc), inputs_revalidated_after_drain=False)
        write_json(result_path, result, exclusive=False)
    result['all_leases_closed'] = True
    return result
