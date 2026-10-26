# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Assemble a completed recipe prefix. Never execute or qualify a provider."""

import hashlib
import os
from pathlib import Path

from .bootstrap_pkgsrc_provider import _normalize_internal_absolute_links
from .bootstrap_producer import metadata_paths
from .bootstrap_seed import _build_result
from .inventory import sha256_file, snapshot_installed_tree, snapshot_tree
from .jsonio import write_json
from .native_toolset import _absolute, _bytes, _envelope, _json, _require, _source
from .paths import canonical_existing, canonical_new, entry, inside, reject_overlap
from .process import ContextLocks

BOOTSTRAP_OUTPUT_ASSEMBLY_API = 1


def _planned_links(root, inventory):
    """Plan only normalized, prefix-local absolute targets. Do not mutate."""
    result = []
    for name, row in sorted(inventory.items()):
        if row['kind'] != 'link' or not os.path.isabs(row['target']):
            continue
        target = row['target']
        _require(os.path.normpath(target) == target and inside(target, root),
                 'absolute link target is not normalized inside the prefix: ' + name)
        member = os.path.relpath(target, root)
        _require(member in inventory, 'absolute link target is absent from the prefix: ' + name)
        result.append(dict(path=name, original_target=target,
                           target=os.path.relpath(target, os.path.dirname(os.path.join(root, name)))))
    return result


def _contents_changes(data, prefix, changes):
    """Change only paired literal Symlink comments. Preserve all other bytes."""
    text = data.decode('utf-8', 'strict')
    lines = text.splitlines(keepends=True)
    by_path = {row['path']: row for row in changes}
    cwd, updates, ignore = None, [], False
    for index, line in enumerate(lines):
        value = line.removesuffix('\n')
        if ignore:
            ignore = False
            continue
        if value == '@ignore':
            ignore = True
            continue
        if value.startswith('@cwd '):
            cwd = value[5:]
            continue
        if not value or value.startswith('@'):
            continue
        _require(cwd is not None and os.path.isabs(cwd), 'package file has no absolute @cwd')
        path = os.path.normpath(os.path.join(cwd, value))
        if not inside(path, prefix):
            continue
        relative = os.path.relpath(path, prefix)
        if relative not in by_path:
            continue
        change = by_path[relative]
        _require(value == os.path.normpath(value) and not os.path.isabs(value),
                 'normalized link has a noncanonical package member')
        _require(index + 1 < len(lines), 'package link lacks its Symlink comment')
        old = '@comment Symlink:' + change['original_target'] + '\n'
        _require(lines[index + 1] == old, 'package Symlink comment differs from the original link')
        lines[index + 1] = '@comment Symlink:' + change['target'] + '\n'
        updates.append(dict(path=relative, line=index + 2,
                            original_target=change['original_target'], target=change['target']))
    return ''.join(lines).encode('utf-8'), updates


def _equal_except_links(before, after, changes):
    _require(set(before) == set(after), 'assembly changed the output roster')
    selected = {row['path']: row for row in changes}
    for name, old in before.items():
        new = after[name]
        if name not in selected:
            _require(new == old, 'assembly changed an unselected output member: ' + name)
        else:
            _require(old['kind'] == new['kind'] == 'link' and
                     old['target'] == selected[name]['original_target'] and
                     new['target'] == selected[name]['target'] and
                     set(old) == set(new) == {'kind', 'mode', 'target'},
                     'assembly changed more than the selected link text: ' + name)
            # Symlink mode is not portable. Both modes remain in the two inventories.


def _replace_metadata(path, expected, data, mode):
    _require(sha256_file(path) == expected, 'package metadata changed before normalization')
    temporary = path + '.emberbsd-link-normalize'
    _require(entry(temporary) is None, 'package metadata normalization path exists')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        _require(sha256_file(path) == expected, 'package metadata changed during normalization')
        os.replace(temporary, path)
    finally:
        if entry(temporary) is not None:
            os.unlink(temporary)


def _save(root, name, value):
    path = os.path.join(root, name)
    write_json(path, value)
    return dict(path=path, sha256=sha256_file(path))


def _save_bytes(root, name, data):
    path = os.path.join(root, name)
    with open(path, 'xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    return dict(path=path, sha256=sha256_file(path))


def _assemble(plan, evidence_root):
    """Caller owns work/evidence EX leases and original producer input SH leases."""
    _envelope(plan, 'emberbsd-native-gcc16-bootstrap-plan', scope='gcc16-bootstrap')
    _require(plan.get('purpose') == 'first-native-gcc16' and
             plan.get('host', {}).get('os') == 'Darwin' and 'netbsd_profile' not in plan,
             'output assembly needs the complete Darwin recipe plan')
    paths = metadata_paths(plan['work_root'])
    prefix, pkgdb = paths['LOCALBASE'], paths['PKG_DBDIR']
    _require(plan['output_bundle_root'] == prefix, 'assembly prefix differs from the plan')
    _require(entry(evidence_root) is None, 'assembly evidence directory must be fresh')
    os.mkdir(evidence_root)
    before = snapshot_installed_tree(prefix)
    packages = snapshot_installed_tree(pkgdb)
    _require(bool(before) and bool(packages), 'completed output or package database is empty')
    changes = _planned_links(prefix, before)
    for change in changes:
        _require(change['path'] + '.emberbsd-link-normalize' not in before,
                 'output link normalization path already exists')
    # Prevalidate every link before any rewrite, including unchanged relative links.
    for name, row in before.items():
        if row['kind'] == 'link':
            _require(inside(canonical_existing(os.path.join(prefix, name)), prefix),
                     'output link resolves outside the prefix: ' + name)
    replacements, ownership = [], {}
    for name, row in sorted(packages.items()):
        if not name.endswith('/+CONTENTS'):
            continue
        _require(name.count('/') == 1 and row['kind'] == 'file',
                 'package contents must be a direct regular database member')
        path = os.path.join(pkgdb, name)
        original = _bytes(dict(path=path, sha256=row['sha256']))
        updated, members = _contents_changes(original, prefix, changes)
        for member in members:
            _require(member['path'] not in ownership, 'normalized link has multiple package owners')
            ownership[member['path']] = name
        if members:
            _require(name + '.emberbsd-link-normalize' not in packages,
                     'package metadata normalization path already exists')
            replacements.append((name, row, original, updated, members))
    _require(set(ownership) == {row['path'] for row in changes},
             'every normalized link needs one exact registered package owner')
    before_ref = _save(evidence_root, 'installed-output-before.json', before)
    packages_ref = _save(evidence_root, 'package-database-before.json', packages)
    metadata = [dict(path=name, before_sha256=row['sha256'],
                     after_sha256=hashlib.sha256(data).hexdigest(), after_size=len(data),
                     mode=row['mode'], members=members,
                     original=_save_bytes(evidence_root, f'package-{index:03d}-before.txt', original),
                     assembled=_save_bytes(evidence_root, f'package-{index:03d}-after.txt', data))
                for index, (name, row, original, data, members) in enumerate(replacements)]
    ledger = dict(format='emberbsd-bootstrap-output-assembly', version=1,
                  status='PLANNED', prefix=prefix, package_database=pkgdb,
                  original_inventory=before_ref, original_package_inventory=packages_ref,
                  normalized_links=changes, package_metadata_changes=metadata,
                  unregistered_links=[row['path'] for row in changes if row['path'] not in ownership],
                  canonical_package_archives_modified=False,
                  ordinary_provider_qualified=False)
    ledger_ref = _save(evidence_root, 'assembly-plan.json', ledger)
    # All content, package matches and resolvable destinations have been checked.
    observed = _normalize_internal_absolute_links(prefix)
    _require(sorted(observed, key=lambda row: row['path']) == changes,
             'normalizer changed a different set of output links')
    for name, row, _, data, _ in replacements:
        _replace_metadata(os.path.join(pkgdb, name), row['sha256'], data, row['mode'])
    after = snapshot_tree(prefix)
    _equal_except_links(before, after, changes)
    expected_packages = dict(packages)
    for name, row, _, data, _ in replacements:
        expected_packages[name] = dict(row, size=len(data), sha256=hashlib.sha256(data).hexdigest())
    _require(snapshot_installed_tree(pkgdb) == expected_packages,
             'assembly changed unrelated package metadata')
    inventory_ref = _save(evidence_root, 'output-inventory.json', after)
    package_ref = _save(evidence_root, 'package-database-after.json', expected_packages)
    return dict(assembly_plan=ledger_ref, output_inventory=inventory_ref,
                package_inventory=package_ref, normalized_link_count=len(changes),
                registered_link_count=len(ownership), unregistered_links=ledger['unregistered_links'],
                canonical_package_archives_modified=False, ordinary_provider_qualified=False)


def _completed_sequence(plan, prior, bootstrap_plan):
    """Check the closed producer envelope. Log bytes are checked by the caller."""
    _envelope(plan, 'emberbsd-native-gcc16-bootstrap-plan', scope='gcc16-bootstrap')
    _envelope(prior, 'emberbsd-native-gcc16-producer-result', scope='gcc16-bootstrap')
    _require(type(prior.get('api')) is int and prior['api'] == 1 and
             prior.get('ordinary_provider_qualified') is False and
             prior.get('evidence_level') == 'command-reported-routes-and-actual-exits' and
             prior.get('stage') == 'full-recipe-bootstrap' and prior.get('status') == 'FAILED' and
             all(prior.get(key) is True for key in
                 ('all_commands_drained', 'inputs_revalidated_after_drain', 'all_leases_closed')) and
             prior.get('bootstrap_plan') == bootstrap_plan and
             prior.get('seed_descriptor') == plan['seed_descriptor'] and
             'output_inventory' not in prior and 'output_assembly' not in prior,
             'assembly requires the immutable closed failed post-command result')
    _require(isinstance(prior.get('error'), str) and
             prior['error'].startswith('absolute or escaped payload link: ') and
             isinstance(plan.get('commands'), list) and bool(plan['commands']) and
             isinstance(prior.get('commands'), list) and
             len(prior['commands']) == len(plan['commands']),
             'failure is not a completed command sequence with a strict link inventory refusal')


def assemble_completed_bootstrap_output(bootstrap_plan, completed_result, *,
                                        evidence_root, finalizer_source):
    """Finish output assembly only; preserve the closed producer result.

    The caller must revalidate inputs with the ORIGINAL producer SDK under its
    original shared leases before and after this call. The caller must retain
    these leases throughout assembly. This function does not import a historical
    SDK, replace its launcher, execute its commands, or claim input revalidation.
    It acquires its own work/evidence writer leases. Finalizer source provenance
    is separate from plan.sources.sdk. Independent acceptance remains required.
    """
    _source(finalizer_source)
    plan, prior = _json(bootstrap_plan), _json(completed_result)
    _completed_sequence(plan, prior, bootstrap_plan)
    work, evidence_root = _absolute(plan['work_root']), _absolute(evidence_root)
    lock_root = canonical_existing(plan['resources']['lock_root'])
    _require(canonical_existing(work) == work and canonical_new(evidence_root) == evidence_root,
             'assembly work or evidence has aliased ancestors')
    reject_overlap(evidence_root, work)
    _require(evidence_root != lock_root and not inside(lock_root, evidence_root),
             'assembly evidence must not contain its lock root')
    sdk_root = str(Path(__file__).resolve().parents[2])
    for path in (sdk_root, plan['ports_export']['root'],
                 bootstrap_plan['path'], completed_result['path']):
        reject_overlap(evidence_root, path)
    shared = [bootstrap_plan['path'], completed_result['path'], sdk_root]
    result = dict(format='emberbsd-bootstrap-output-finalization-result', version=1,
                  status='RUNNING', bootstrap_plan=bootstrap_plan,
                  original_failed_result=completed_result, finalizer_source=finalizer_source,
                  finalizer_module_sha256=sha256_file(__file__),
                  commands_reexecuted=False, original_producer_inputs_revalidated_here=False,
                  ordinary_provider_qualified=False)
    with ContextLocks(shared, lock_root, shared=True), ContextLocks([work, evidence_root], lock_root):
        _require(_json(bootstrap_plan) == plan and _json(completed_result) == prior,
                 'assembly input changed while acquiring leases')
        _require(entry(evidence_root) is None, 'assembly evidence directory must be fresh')
        for actual, expected in zip(prior['commands'], plan['commands']):
            _require(all(actual.get(key) == value for key, value in expected.items()) and
                     'error' not in actual and 'unfinalized_log_path' not in actual,
                     'recorded command differs from the complete plan')
            _build_result(actual, expected)
        persisted = os.path.join(work, 'result.json')
        persisted_ref = dict(path=persisted, sha256=sha256_file(persisted))
        _require(dict(_json(persisted_ref), all_leases_closed=True) == prior,
                 'returned and persisted producer results differ')
        result['original_persisted_result'] = persisted_ref
        try:
            result.update(_assemble(plan, evidence_root))
            _require(_json(completed_result) == prior and
                     sha256_file(persisted) == persisted_ref['sha256'],
                     'original failed producer result changed')
            result['status'] = 'OUTPUT_ASSEMBLED_PENDING_INDEPENDENT_REVIEW'
        except (KeyboardInterrupt, SystemExit) as exc:
            result.update(status='FAILED', error=f'{type(exc).__name__}: {exc}')
            try:
                if entry(evidence_root) is not None:
                    write_json(os.path.join(evidence_root, 'result.json'), result)
            finally:
                raise exc
        except Exception as exc:
            result.update(status='FAILED', error=str(exc))
        if entry(evidence_root) is not None:
            write_json(os.path.join(evidence_root, 'result.json'), result)
    result['all_leases_closed'] = True
    return result
