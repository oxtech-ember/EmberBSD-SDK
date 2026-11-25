# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Describe a generation of OS sets in one authenticated release."""

import re
from pathlib import Path

from .errors import SDKError
from .jsonio import loads_json
from .release_catalog import _COMMIT, _SLUG, _integer, _object, _pattern, local_artifact
from .release_signing import read_bytes


PREFIX = 'os-update-'
SCHEMA = 'emberbsd.os-update.v1'
MAX_DESCRIPTOR = 32 * 1024
MAX_GENERATION = 2**63 - 1
REQUIRED = {'boot', 'base', 'etc'}


def descriptor_generation(record):
    profile = record['id'][len(PREFIX):]
    _pattern(profile, _SLUG, 'OS update profile')
    name = Path(record['path']).name
    match = re.fullmatch(re.escape(record['id']) + r'-g([1-9][0-9]{0,18})\.json', name)
    if not match or record['kind'] != 'os-set' or not 0 < record['size'] <= MAX_DESCRIPTOR:
        raise SDKError('invalid OS update descriptor kind, name or size')
    generation = int(match[1])
    _integer(generation, 'OS update generation', maximum=MAX_GENERATION)
    return generation


def read_descriptor(data, record, artifacts):
    generation = descriptor_generation(record)
    value = loads_json(data, max_bytes=MAX_DESCRIPTOR)
    _object(value, ('schema', 'profile', 'generation', 'target', 'os_commit', 'sets'), 'OS update')
    _pattern(value['profile'], _SLUG, 'OS update profile')
    _integer(value['generation'], 'OS update generation', maximum=MAX_GENERATION)
    _pattern(value['os_commit'], _COMMIT, 'OS update source commit')
    if (value['schema'] != SCHEMA or record['id'] != PREFIX + value['profile'] or
            value['target'] != record['target'] or value['generation'] != generation):
        raise SDKError('OS update identity, generation or target differs from its release record')
    sets = value['sets']
    if not isinstance(sets, dict) or set(sets) not in (REQUIRED, REQUIRED | {'comp'}):
        raise SDKError('OS update requires boot, base and etc; only comp is optional')
    for role, member_id in sets.items():
        _pattern(member_id, _SLUG, 'OS update member ID')
        member = artifacts.get(member_id)
        if (member is None or member['kind'] != 'os-set' or member['target'] != record['target'] or
                member['depends'] or member_id.startswith(PREFIX) or not member['size'] or
                not member['path'].endswith('.tar.xz')):
            raise SDKError(f'OS update {role} must select a nonempty .tar.xz leaf OS set with the descriptor target')
    if len(set(sets.values())) != len(sets) or set(sets.values()) != set(record['depends']):
        raise SDKError('OS update roles must exactly match the distinct descriptor dependencies')
    return value


def check_publication(release, directory, previous=None, *, inherited=()):
    """Keep unchanged generations bound to the same descriptor and member records."""
    artifacts = {a['id']: a for a in release['artifacts']}
    old = {a['id']: a for a in previous['artifacts']} if previous else {}
    for name, record in artifacts.items():
        if not name.startswith(PREFIX):
            continue
        generation = descriptor_generation(record)
        if name in old:
            prior = old[name]
            floor = descriptor_generation(prior)
            if generation < floor or record['target'] != prior['target']:
                raise SDKError('OS update generation regressed or the profile target changed')
            if generation == floor and (record != prior or any(
                    artifacts[member] != old[member] for member in prior['depends'])):
                raise SDKError('changed OS update selection requires a new generation')
        if name not in inherited:
            data = read_bytes(local_artifact(directory, record['path']), MAX_DESCRIPTOR)
            read_descriptor(data, record, artifacts)
