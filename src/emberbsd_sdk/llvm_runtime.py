# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Portable common LLVM runtime stages, including inputs for first-base bootstrap."""

import os
import posixpath
import re

from . import inventory, jsonio, paths
from .elf import read_archive, read_elf_dynamic
from .errors import SDKError
from .targets import target_definition

LLVM_RUNTIME_API = 1
MANIFEST = 'runtime.json'
# validate_stage(content=False) and validate_bootstrap_pair(content=False).
STAGE_CONTENT_API = 1
FULL_COMPONENTS = ('builtins', 'libunwind', 'libcxxabi', 'libcxx')
_PROFILES = (('builtins',), ('builtins', 'libunwind-static'), FULL_COMPONENTS)


def _require(condition, message):
    if not condition:
        raise SDKError(message)


def _keys(value, names, label):
    _require(isinstance(value, dict) and set(value) == set(names.split()),
             'invalid LLVM runtime ' + label)


def _digest(value, size):
    _require(isinstance(value, str) and re.fullmatch('[0-9a-f]{' + str(size) + '}', value),
             'invalid LLVM runtime input digest')


def _relative(value):
    _require(isinstance(value, str) and value and not value.startswith('/') and
             '\\' not in value and all(part not in ('', '.', '..') for part in value.split('/')) and
             not any(ord(char) < 32 or ord(char) == 127 for char in value),
             'invalid LLVM runtime relative path')
    return value


def _files(value, *, nonempty=False):
    _require(isinstance(value, dict) and (not nonempty or value), 'invalid LLVM runtime files')
    for name, digest in value.items():
        _relative(name)
        _digest(digest, 64)


def _links(value):
    _require(isinstance(value, dict), 'invalid LLVM runtime symlinks')
    for name, target in value.items():
        _relative(name)
        _require(isinstance(target, str) and target and not target.startswith('/') and
                 '\\' not in target and not any(ord(char) < 32 or ord(char) == 127 for char in target),
                 'invalid LLVM runtime symlink target')
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
        _require(resolved != '..' and not resolved.startswith('../'),
                 'LLVM runtime symlink escapes its stage')


def _tree_names(files, links):
    """A regular file or symlink cannot also be an inventory directory."""
    leaves = set(files) | set(links)
    for name in leaves:
        parts = name.split('/')
        _require(not any('/'.join(parts[:i]) in leaves for i in range(1, len(parts))),
                 'LLVM runtime inventory has a non-directory ancestor')


def validate_identity(record, *, target=None, required_components=()):
    """Check portable input and artifact declarations without reading local paths."""
    fields = ('format version compiler_family llvm_version components target triplet machine_arch '
              'elf_machine prefix source recipe base artifacts files symlinks')
    _keys(record, fields + (' bootstrap' if isinstance(record, dict) and 'bootstrap' in record else ''),
          'manifest fields')
    _require(record['format'] == 'emberbsd-llvm-runtime' and type(record['version']) is int and
             record['version'] == 1 and record['compiler_family'] == 'clang',
             'unsupported LLVM runtime format or compiler family')
    _require(isinstance(record['llvm_version'], str) and
             re.fullmatch(r'[1-9][0-9]*\.[0-9]+\.[0-9]+', record['llvm_version']),
             'invalid LLVM runtime version')
    definition = target_definition(record['target'])
    _require((target is None or record['target'] == target) and
             record['triplet'] == definition['triplet'] and record['machine_arch'] == definition['arch'] and
             type(record['elf_machine']) is int and record['elf_machine'] == definition['elf'],
             'LLVM runtime target identity mismatch')
    _require(isinstance(record['components'], list) and tuple(record['components']) in _PROFILES,
             'unsupported LLVM runtime component profile')
    present = set(record['components'])
    if 'libunwind' in present:
        present.add('libunwind-static')
    _require(set(required_components) <= present, 'missing required runtime components')
    _require(record['prefix'] == 'usr/pkg/llvm', 'unsupported LLVM runtime install prefix')
    if 'bootstrap' in record:
        _keys(record['bootstrap'], 'manifest_sha256 files', 'bootstrap identity')
        _digest(record['bootstrap']['manifest_sha256'], 64)
        _files(record['bootstrap']['files'], nonempty=True)
        _require(all(name.startswith(record['prefix'] + '/') for name in record['bootstrap']['files']),
                 'LLVM bootstrap artifacts are outside the install prefix')
    _keys(record['source'], 'archive sha512', 'source identity')
    archive = _relative(record['source']['archive'])
    _require('/' not in archive, 'LLVM source archive must be a basename')
    _digest(record['source']['sha512'], 128)
    _keys(record['recipe'], 'ports_revision files', 'recipe identity')
    _digest(record['recipe']['ports_revision'], 40)
    _files(record['recipe']['files'], nonempty=True)
    _keys(record['base'], 'os_revision files symlinks', 'base identity')
    _digest(record['base']['os_revision'], 40)
    _files(record['base']['files'], nonempty=True)
    _links(record['base']['symlinks'])
    _require(not set(record['base']['files']) & set(record['base']['symlinks']),
             'duplicate LLVM base input path')
    _tree_names(record['base']['files'], record['base']['symlinks'])
    _files(record['files'], nonempty=True)
    _links(record['symlinks'])
    _require(not set(record['files']) & set(record['symlinks']), 'duplicate LLVM runtime payload path')
    _tree_names(record['files'], record['symlinks'])
    payload = set(record['files']) | set(record['symlinks'])
    _require(all(name.startswith(record['prefix'] + '/') for name in payload),
             'LLVM runtime payload is outside its install prefix')
    expected = {'builtins'}
    if 'libunwind-static' in present:
        expected.add('libunwind')
    if 'libcxx' in present:
        expected |= {'libcxx', 'libcxxabi', 'cxx_headers'}
    _keys(record['artifacts'], ' '.join(expected), 'artifact roles')
    artifacts = record['artifacts']
    artifact_files = [artifacts['builtins']]
    for component in ('libunwind', 'libcxxabi', 'libcxx'):
        if component not in artifacts:
            continue
        _keys(artifacts[component], 'static shared' if component in present else 'static', component + ' artifacts')
        artifact_files.extend(artifacts[component].values())
    for name in artifact_files:
        _relative(name)
        _require(name in payload, 'LLVM runtime artifact is absent from the inventory')
    _require(len(set(artifact_files)) == len(artifact_files), 'LLVM runtime artifact roles overlap')
    if 'cxx_headers' in artifacts:
        name = _relative(artifacts['cxx_headers'])
        _require(name.startswith(record['prefix'] + '/') and name + '/vector' in payload,
                 'LLVM C++ headers are absent from the inventory')
    return record


def _payload(rows):
    return ({name: row['sha256'] for name, row in rows.items() if row['kind'] == 'file'},
            {name: row['target'] for name, row in rows.items() if row['kind'] == 'link'})


def validate_bootstrap_pair(root, full, *, target, content=True):
    """Verify the exact bootstrap stage consumed by a full runtime build."""
    bootstrap = validate_stage(root, target=target, required_components=('builtins', 'libunwind-static'),
                               content=content)
    pin = full.get('bootstrap')
    _require(isinstance(pin, dict), 'full LLVM runtime lacks bootstrap provenance')
    names = (bootstrap['artifacts']['builtins'], bootstrap['artifacts']['libunwind']['static'])
    _require(pin.get('manifest_sha256') == inventory.sha256_file(os.path.join(root, 'runtime.json')) and
             pin.get('files') == {name: bootstrap['files'][name] for name in names},
             'LLVM bootstrap stage differs from full runtime provenance')
    _require(all(bootstrap[key] == full[key] for key in ('target', 'triplet', 'machine_arch',
             'elf_machine', 'llvm_version', 'source')), 'LLVM bootstrap and full runtime inputs differ')
    return bootstrap


def runtime_paths(root, record):
    """Derive the target resource directory from the measured builtins archive."""
    prefix = record['prefix']
    builtins = record['artifacts']['builtins']
    match = re.fullmatch(re.escape(prefix) + r'/(lib/clang/[^/]+)/lib/netbsd/libclang_rt\.builtins-[^/]+\.a', builtins)
    _require(match is not None, 'LLVM builtins do not identify a supported NetBSD resource directory')
    return {'runtime-prefix': os.path.join(root, prefix),
            'llvm-resource': os.path.join(root, prefix, match[1]),
            'llvm-unwind': os.path.join(root, record['artifacts']['libunwind']['static'])}


def validate_install(root, record, *, target=None, required_components=FULL_COMPONENTS):
    """Check a runtime subtree in a sysroot without requiring a second manifest."""
    root = paths.canonical_existing(root)
    paths.real_directory(root)
    record = validate_identity(record, target=target, required_components=required_components)
    prefix = record['prefix']
    current = root
    for component in prefix.split('/'):
        current = os.path.join(current, component)
        paths.real_directory(current)
    rows = inventory.snapshot_tree(os.path.join(root, prefix), machine=record['elf_machine'])
    rows = {prefix + '/' + name:row for name, row in rows.items()}
    files, symlinks = _payload(rows)
    resource_include = os.path.relpath(runtime_paths(root, record)['llvm-resource'], root) + '/include/'
    _require(all(files.get(name) == digest for name, digest in record['files'].items()) and
             symlinks == record['symlinks'] and
             all(name.startswith(resource_include) for name in files.keys() - record['files'].keys()),
             'LLVM runtime installed inventory changed')
    _validate_artifacts(root, record)
    return record


def validate_stage(stage, *, target=None, required_components=(), ports=None,
                   ports_revision=None, base=None, content=True):
    """Verify stage bytes and target object types, not runtime execution or PIC.

    Optional base checks compare actual preexisting headers/libraries. They do
    not require a completed SDK receipt. The owning Ports producer checks its
    build options and real linking before using the stage for an application.

    With content false a reused stage is identified by its manifest, its
    input key: the file bytes are not hashed again, while names, links,
    modes, artifact object types and the recipe and base inputs are checked.
    """
    root = paths.canonical_existing(stage)
    paths.real_directory(root)
    record = validate_identity(jsonio.read_json(os.path.join(root, MANIFEST)), target=target,
                               required_components=required_components)
    rows = inventory.snapshot_tree(root, machine=record['elf_machine'], exclude=(MANIFEST,),
                                   digests=None if content else record['files'])
    _require(_payload(rows) == (record['files'], record['symlinks']), 'LLVM runtime inventory changed')
    prefix = record['prefix']
    _require(all(name == prefix or name.startswith(prefix + '/') or prefix.startswith(name + '/')
                 for name in rows), 'LLVM runtime stage contains an unrelated directory')
    _validate_artifacts(root, record)
    if ports_revision is not None:
        _digest(ports_revision, 40)
        _require(ports_revision == record['recipe']['ports_revision'], 'LLVM runtime Ports revision changed')
    if ports is not None:
        _require(ports_revision is not None, 'explicit Ports revision is required for recipe input checks')
        ports = paths.canonical_existing(ports)
        for name, digest in record['recipe']['files'].items():
            path = paths.canonical_existing(os.path.join(ports, name))
            _require(paths.inside(path, ports) and inventory.sha256_file(path) == digest,
                     'LLVM runtime recipe input changed: ' + name)
    if base is not None:
        base = paths.canonical_existing(base)
        for name, digest in record['base']['files'].items():
            path = paths.canonical_existing(os.path.join(base, name))
            _require(paths.inside(path, base) and inventory.sha256_file(path) == digest,
                     'LLVM runtime base inputs changed: ' + name)
        for name, target in record['base']['symlinks'].items():
            path = os.path.join(base, name)
            _require(os.path.islink(path) and os.readlink(path) == target and
                     paths.inside(paths.canonical_existing(path), base),
                     'LLVM runtime base inputs changed: ' + name)
    return record


def _validate_artifacts(root, record):
    artifacts = record['artifacts']
    archives = [artifacts['builtins']]
    for component in ('libunwind', 'libcxxabi', 'libcxx'):
        if component in artifacts:
            archives.append(artifacts[component]['static'])
            if 'shared' in artifacts[component]:
                name = artifacts[component]['shared']
                inventory.required_elf(root, name, record['elf_machine'], 3)
                shared_path = inventory.required_file(root, name)
                dynamic = read_elf_dynamic(shared_path, machine=record['elf_machine'])
                soname = {'libunwind': 'libunwind', 'libcxxabi': 'libc++abi', 'libcxx': 'libc++'}[component]
                _require(isinstance(dynamic['soname'], str) and
                         re.fullmatch(re.escape(soname) + r'\.so\.[0-9]+(?:\.[0-9]+)*', dynamic['soname']),
                         'LLVM runtime shared library has a different SONAME role')
                loader_name = posixpath.join(posixpath.dirname(name), dynamic['soname'])
                _require((loader_name in record['files'] or loader_name in record['symlinks']) and
                         inventory.required_file(root, loader_name) == shared_path,
                         'LLVM runtime SONAME does not select its shared artifact in the payload')
                _require(all('/' not in name and not name.startswith(('libgcc_s.', 'libstdc++.', 'libsupc++.'))
                             for name in dynamic['needed']), 'LLVM runtime selects a foreign compiler runtime')
                allowed = {'$ORIGIN', '${ORIGIN}', '/usr/pkg/llvm/lib', '/usr/lib', '/lib'}
                for field in ('rpath', 'runpath'):
                    _require(dynamic[field] is None or set(dynamic[field].split(':')) <= allowed,
                             'LLVM runtime has a foreign library search path')
    for name in archives:
        _require(read_archive(inventory.required_file(root, name), record['elf_machine'],
                              riscv32_soft_float=record['target'] == 'riscv32')['elf_members'] > 0,
                 'empty LLVM runtime archive: ' + name)
    if 'cxx_headers' in artifacts:
        inventory.required_file(root, artifacts['cxx_headers'] + '/vector')
