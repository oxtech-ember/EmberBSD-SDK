# SPDX-License-Identifier: BSD-2-Clause
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk import package_relocation as relocation
from emberbsd_sdk.bootstrap_bundle import relocate_text
from emberbsd_sdk.errors import SDKError


class RelocationTests(unittest.TestCase):
    def test_deferred_hook_at_original_prefix_needs_completion_and_retries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / 'pkgdb/fixture-1'; package.mkdir(parents=True)
            contents = package / '+CONTENTS'
            contents.write_text('@name fixture-1\n@cwd ' + str(root) + '\n' + relocation.MARKER + '\n' +
                                relocation.SOURCE + str(root) + '\n' + relocation.HOOKS + '\n')
            self.assertEqual(list(relocation.pending(root, root / 'pkgdb')), ['fixture-1'])
            with self.assertRaisesRegex(SDKError, 'pkg_add did not complete'):
                relocation.finish(root, root / 'pkgdb', 'fixture-1')
            (package / relocation.READY).write_text(str(root) + '\n')
            with patch.object(relocation, 'run_install_hooks', side_effect=SDKError('failed')):
                with self.assertRaisesRegex(SDKError, 'failed'):
                    relocation.finish(root, root / 'pkgdb', 'fixture-1')
            with patch.object(relocation, 'run_install_hooks') as run:
                relocation.finish(root, root / 'pkgdb', 'fixture-1')
                run.assert_called_once()
            self.assertEqual(list(relocation.pending(root, root / 'pkgdb')), [])
            # A crash after the final plist update leaves only harmless state.
            (package / relocation.READY).write_text('/old\n')
            relocation.finish(root, root / 'pkgdb', 'fixture-1')
            self.assertFalse((package / relocation.READY).exists())

    def test_repeated_producer_prefix_is_refused_before_text_changes(self):
        with self.assertRaisesRegex(SDKError, 'repeat its producer prefix'):
            relocate_text(b'#!/opt/sdk/bin/python', b'/opt/sdk', b'/alt/opt/sdk')
        for destination in (b'/opt', b'/alt/opt', b'/sdk', b'/sdk/other'):
            with self.subTest(destination=destination), self.assertRaisesRegex(SDKError, 'producer prefix'):
                relocate_text(b'#!/opt/sdk/sdk/bin/python', b'/opt/sdk', destination)
        with self.assertRaisesRegex(SDKError, 'producer prefix'):
            relocate_text(b'/a/a/b/a/a', b'/a/b/a', b'/b')

    def test_incomplete_pkg_add_cannot_bless_truncated_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / 'pkgdb'
            package = database / 'fixture-1'
            package.mkdir(parents=True)
            original_hash = hashlib.md5(b'complete payload').hexdigest()
            (root / 'payload').write_bytes(b'partial')
            text = ('@name fixture-1\n@cwd ' + str(root) + '\n' + relocation.MARKER + '\n' +
                    relocation.SOURCE + '/original\npayload\n@comment MD5:' + original_hash + '\n')
            contents = package / '+CONTENTS'
            contents.write_text(text)
            with self.assertRaisesRegex(SDKError, 'pkg_add did not complete'):
                relocation.finish(root, database, 'fixture-1')
            self.assertEqual(contents.read_text(), text)
            # Even after pkg_add completion, relocation must not replace MD5s
            # of files it does not change: later pkg_admin check can detect them.
            (package / relocation.READY).write_text('/original\n')
            relocation.finish(root, database, 'fixture-1')
            self.assertIn('@comment MD5:' + original_hash, contents.read_text())
            self.assertEqual((root / 'payload').read_bytes(), b'partial')
