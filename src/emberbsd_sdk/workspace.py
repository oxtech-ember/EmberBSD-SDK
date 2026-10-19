# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Target layout v2 and read-only legacy inspection. No ABI qualification."""

import os
import stat
from .errors import SDKError
from .jsonio import read_json, write_json
from .paths import canonical_existing, entry, path_string, real_directory
from .targets import target_definition

REQUIRED = ('obj', 'sysroot', 'packages')
DIRECTORIES = (*REQUIRED, 'receipts')
MANIFEST_NAME = '.prebuild-sdk.json'
INCOMPLETE_NAME = '.prebuild-sdk.incomplete'
MAX_MANIFEST = 4096
MANIFEST = dict(format='emberbsd-prebuild-sdk-layout', version=1, scope='workspace-layout', directories=list(DIRECTORIES))


def target_manifest(target):
    definition = target_definition(target)
    return MANIFEST | dict(version=2, target=target, triplet=definition['triplet'])


def read_manifest(path):
    value = read_json(path, max_bytes=MAX_MANIFEST, max_depth=8)
    if not isinstance(value, dict) or type(value.get('version')) is not int:
        raise SDKError(f'unsupported or malformed layout manifest: {path}')
    expected = MANIFEST if value['version'] == 1 else target_manifest(value.get('target'))
    if value != expected:
        raise SDKError(f'unsupported or malformed layout manifest: {path}')
    return value


def inspect_layout(root):
    try:
        logical = path_string(root)
        canonical = canonical_existing(logical)
        identity = real_directory(canonical)
        marker = os.path.join(canonical, INCOMPLETE_NAME)
        if entry(marker) is not None:
            raise SDKError(f'incomplete workspace creation: {marker}')
        paths = {}
        for name in REQUIRED:
            child = os.path.join(canonical, name)
            real_directory(child)
            paths[name] = child
        receipts = os.path.join(canonical, 'receipts')
        paths['receipts'] = None
        if entry(receipts) is not None:
            real_directory(receipts)
            paths['receipts'] = receipts
        manifest = os.path.join(canonical, MANIFEST_NAME)
        managed = entry(manifest) is not None
        metadata = {}
        if managed:
            metadata = read_manifest(manifest)
            if paths['receipts'] is None:
                raise SDKError('managed layout has no receipts directory')
        current = real_directory(canonical)
        if canonical_existing(logical) != canonical or (identity.st_dev, identity.st_ino) != (current.st_dev, current.st_ino):
            raise SDKError('workspace root changed during inspection')
        return dict(format='emberbsd-prebuild-sdk-inspection', version=1, scope='workspace-layout', logical_root=logical, canonical_root=canonical, structural_status='managed' if managed else 'adoptable-existing', qualification='not-assessed', not_checked=['ABI', 'ELF', 'compiler', 'sysroot-cohort', 'packages', 'SDK-release'], manifest=manifest if managed else None, directories=paths, layout_version=metadata.get('version'), target=metadata.get('target'), triplet=metadata.get('triplet'), target_status='declared' if metadata.get('version') == 2 else 'unknown')
    except OSError as exc:
        raise SDKError(str(exc)) from exc


def require_target(layout, target):
    definition = target_definition(target)
    if layout['layout_version'] != 2 or layout['target'] is None:
        raise SDKError('workspace target is unknown; create a new target workspace; legacy migration is separate')
    if layout['target'] != target or layout['triplet'] != definition['triplet']:
        raise SDKError(f'workspace target mismatch: declared {layout["target"]}, requested {target}')


def create(root, adopt=None, *, target=None):
    try:
        if adopt is None or target is not None:
            target_definition(target)
        logical = path_string(root)
        canonical_existing(os.path.dirname(logical))
        if adopt is not None:
            return adopt_layout(logical, adopt, target=target)
        os.mkdir(logical)
        marker = os.path.join(logical, INCOMPLETE_NAME)
        with open(marker, 'xb') as handle:
            handle.write(b'workspace creation is incomplete\n')
        for name in DIRECTORIES:
            os.mkdir(os.path.join(logical, name))
        write_json(os.path.join(logical, MANIFEST_NAME), target_manifest(target))
        os.unlink(marker)
        return inspect_layout(logical)
    except OSError as exc:
        raise SDKError(str(exc)) from exc


def adopt_layout(logical, existing, *, target=None):
    source = path_string(existing)
    real_directory(source)
    source_layout = inspect_layout(source)
    if target is not None:
        require_target(source_layout, target)
    expected = source_layout['canonical_root']
    try:
        os.symlink(expected, logical)
    except NotImplementedError as exc:
        raise SDKError(f'symbolic-link adoption is unavailable: {exc}') from exc
    except FileExistsError:
        info = entry(logical)
        if info is None or not stat.S_ISLNK(info.st_mode) or canonical_existing(logical) != expected:
            raise SDKError(f'destination is not an alias to the requested workspace: {logical}')
    result = inspect_layout(logical)
    if result['canonical_root'] != expected:
        raise SDKError('alias target changed during adoption')
    if target is not None:
        require_target(result, target)
    return result
