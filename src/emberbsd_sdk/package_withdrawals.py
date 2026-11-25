# SPDX-License-Identifier: BSD-2-Clause
"""Read exact package identities and hashes from a signed release index."""

from .errors import SDKError
from .jsonio import loads_json
from .release_catalog import _pattern, _SHA256, _SLUG


def parse_withdrawals(data):
    value = loads_json(data)
    if (not isinstance(value, dict) or set(value) != {'schema', 'artifacts'} or
            value['schema'] != 'emberbsd.withdrawals.v1' or
            not isinstance(value['artifacts'], list)):
        raise SDKError('invalid signed withdrawal index')
    pairs = set()
    for item in value['artifacts']:
        if not isinstance(item, dict) or set(item) != {'id', 'sha256'}:
            raise SDKError('invalid signed withdrawal record')
        _pattern(item['id'], _SLUG, 'withdrawn artifact id')
        _pattern(item['sha256'], _SHA256, 'withdrawn artifact sha256')
        pairs.add((item['id'], item['sha256']))
    return pairs
