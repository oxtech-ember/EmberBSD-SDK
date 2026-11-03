# SPDX-License-Identifier: BSD-2-Clause
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

SDK = Path(__file__).resolve().parents[1]


class BundleTests(unittest.TestCase):
    def test_client_runs_without_checkout_and_contains_trust_not_compiler(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'ember-sdk.pyz'
            result = subprocess.run([sys.executable, '-B', str(SDK / 'scripts/package-client.py'),
                                     '--output', str(archive)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([sys.executable, '-B', str(archive), '--help'],
                                    cwd=directory, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('install', result.stdout)
            with zipfile.ZipFile(archive) as contents:
                names = contents.namelist()
                self.assertIn('emberbsd_sdk/emberbsd-release-root.pem', names)
                self.assertFalse(any('private-key' in name or name.startswith('tests/') for name in names))
            self.assertLess(archive.stat().st_size, 1024 * 1024)


if __name__ == '__main__':
    unittest.main()
