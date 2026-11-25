# SPDX-License-Identifier: BSD-2-Clause
import copy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk.catalog_recovery import recover_catalogs


def entry(name, digest, *, dependencies=(), requires=()):
    item = dict(id=name, sha256=digest, depends=list(dependencies))
    return dict(name=name + '-1', artifact=name, release_artifact=item,
                record={'REQUIRES': list(requires)})


def repository(items, sets):
    return dict(host=None, target={'arch': 'x86_64'}, entries=items,
                catalog=dict(repository='test', packages=[
                    dict(root=root, artifact=item['artifact'], pkgname=item['name'])
                    for root, item in items.items()], sets=sets))


def snapshot(items, catalogs, revoked=()):
    import json
    indexes = {'test/withdrawals.json': json.dumps(dict(schema='emberbsd.withdrawals.v1',
        artifacts=[dict(id=ident, sha256=digest) for ident, digest in revoked])).encode()}
    return dict(artifacts=[v['release_artifact'] for v in items.values()]), {'test': catalogs}, indexes


class RecoveryTests(unittest.TestCase):
    def test_restore_only_available_signed_consumers_and_set_members(self):
        old = dict(base=entry('base', 'a'*64), app=entry('app', 'b'*64, dependencies=['base']),
                   stale=entry('stale', 'c'*64, dependencies=['app'], requires=['/lib/libc.so.12.224']),
                   upper=entry('upper', 'd'*64, dependencies=['stale']),
                   retired=entry('retired', 'e'*64),
                   strict=entry('strict', '6'*64, dependencies=['base'], requires=['/usr/pkg/lib/removed.so.1']),
                   indirect=entry('indirect', '7'*64, dependencies=['strict']))
        old['base']['record']['PROVIDES'] = ['/usr/pkg/lib/removed.so.1']
        denied = [('base', 'a'*64)]
        before = snapshot(old, repository(old, {'desktop': ['app', 'stale', 'retired']}))
        after = snapshot(old, repository({}, {'desktop': []}), denied)
        live = copy.deepcopy(old)
        live['base'] = entry('base', 'f'*64)
        live['extra'] = entry('extra', '0'*64)
        current = snapshot(live, repository({k: live[k] for k in ('base','extra')},
                           {'desktop': ['extra']}), denied)
        unselected = copy.deepcopy(current)
        self.assertFalse(recover_catalogs(None, unselected, [(before, after)], {}))
        with tempfile.TemporaryDirectory() as root:
            bad_provider = copy.deepcopy(current)
            bad_provider[1]['test']['entries']['base']['record']['REQUIRES'] = ['/lib/libc.so.12.224']
            self.assertFalse(recover_catalogs(None, bad_provider, [(before, after)], {'test': Path(root)}))
            changed = recover_catalogs(None, current, [(before, after)], {'test': Path(root)})
        self.assertEqual(changed, {'test'})
        self.assertEqual(set(current[1]['test']['entries']), {'base', 'extra', 'app'})
        self.assertEqual(current[1]['test']['catalog']['sets']['desktop'], ['extra', 'app'])
        self.assertEqual(current[1]['test']['entries']['app']['release_artifact'],
                         old['app']['release_artifact'])


if __name__ == '__main__':
    unittest.main()
