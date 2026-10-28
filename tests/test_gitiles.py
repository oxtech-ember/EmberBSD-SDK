# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
import io
from pathlib import Path
import tarfile
import unittest

from test_core import TemporaryCase
from emberbsd_sdk import gitiles, sourceprep


def archive(entries, mtime=1):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode='w:gz', format=tarfile.PAX_FORMAT) as tar:
        for name, payload, kind in entries:
            item = tarfile.TarInfo(name); item.mtime = mtime; item.mode = 0o644
            if kind == 'link':
                item.type = tarfile.SYMTYPE; item.linkname = 'other'; tar.addfile(item)
            else:
                item.size = len(payload); tar.addfile(item, io.BytesIO(payload))
    return data.getvalue()


class GitilesTests(TemporaryCase):
    def test_explicit_limits_remain_bounded_and_validate(self):
        rows = [('dir/a', b'abcdef', 'file'), ('dir/b', b'ghijk', 'file')]
        tree = gitiles.tree_hash({'dir/a': ('100644', b'abcdef'), 'dir/b': ('100644', b'ghijk')})
        raw = archive(rows)
        expected = gitiles.normalize(raw, tree)
        self.assertEqual(expected, gitiles.normalize(raw, tree, max_archive=len(raw),
                                                    max_content=11, max_members=2))
        for limits in ({'max_archive': len(raw)-1}, {'max_content': 10},
                       {'max_members': 1}, {'max_archive': 0}, {'max_content': True}):
            with self.subTest(limits=limits), self.assertRaises(gitiles.SourceError):
                gitiles.normalize(raw, tree, **limits)
        with self.assertRaises(gitiles.SourceError):
            gitiles.normalize(archive([('../outside', b'x', 'file')]), tree,
                              max_archive=1_000_000, max_content=1_000_000, max_members=100)

    def test_timestamp_normalization_and_named_root_extraction(self):
        rows = [('dir/source', b'content', 'file')]
        tree = gitiles.tree_hash({'dir/source': ('100644', b'content')})
        one = gitiles.normalize(archive(rows, 1), tree)
        self.assertEqual(one, gitiles.normalize(archive(rows, 1234), tree))
        rooted = gitiles.normalize(archive(rows, 2), tree, root_name='source-1')
        path = self.root / 'normalized.tar'; path.write_bytes(rooted)
        sourceprep.extract_archive(path, self.root, 'source-1', 'test')
        self.assertEqual((self.root / 'source-1/dir/source').read_bytes(), b'content')
        with self.assertRaises(gitiles.SourceError):
            gitiles.normalize(archive(rows), tree, root_name='../outside')

    def test_rejects_wrong_tree_unsafe_names_links_and_collisions(self):
        tree = gitiles.tree_hash({'file': ('100644', b'x')})
        cases = [([('file', b'y', 'file')], tree),
                 ([('../file', b'x', 'file')], tree),
                 ([('file', b'', 'link')], tree),
                 ([('file', b'x', 'file'), ('File', b'x', 'file')], tree)]
        for entries, expected in cases:
            with self.subTest(entries=entries), self.assertRaises(gitiles.SourceError):
                gitiles.normalize(archive(entries), expected)
