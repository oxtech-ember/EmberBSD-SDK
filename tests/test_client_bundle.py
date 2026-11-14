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

    def test_zipimport_netbsd_launcher_lifetime_and_abort(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'ember-sdk.pyz'
            subprocess.run([sys.executable, '-B', str(SDK / 'scripts/package-client.py'),
                            '--output', str(archive)], check=True, capture_output=True)
            program = r"""
import os, pathlib, select, subprocess, sys, time, types
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
from emberbsd_sdk import _netbsd_process as n
assert '.pyz/' in n.__file__
class Queue:
    def __init__(self, fail=False): self.fail = fail
    def control(self, changes, *unused):
        if changes and self.fail: raise PermissionError('registration denied')
        return []
    def close(self): pass
for fail in (False, True):
    paths = []
    real = subprocess.Popen
    def capture(argv, **kw):
        paths.append(pathlib.Path(argv[4]))
        assert paths[-1].is_file(), paths[-1]
        return real(argv, **kw)
    native = types.SimpleNamespace(uid=os.geteuid(), supervised={})
    native.rows = lambda: {p:n.ProcessRow(p, os.getpid(), p, native.uid, 2) for p in native.supervised}
    child = n.NetBSDChild(native, ['/usr/bin/true'], {})
    with patch.object(n.select, 'kevent', create=True), patch.object(n.select, 'KQ_EV_ADD', 1, create=True), patch.object(n.select, 'KQ_EV_ENABLE', 4, create=True), patch.object(n.select, 'kqueue', create=True, return_value=Queue(fail)), patch.object(n.subprocess, 'Popen', side_effect=capture):
        if fail:
            try: child.start()
            except n.SDKError: pass
            else: raise AssertionError('tracking registration must fail')
            assert child.reaped and not paths[0].exists()
        else:
            child.start()
            assert paths[0].is_file()
            deadline = time.monotonic() + 5
            while child.poll() is None and time.monotonic() < deadline: time.sleep(.01)
            assert child.poll() == 0
            child.tree.active.clear()
            child.reap()
            assert not paths[0].exists()
"""
            result = subprocess.run([sys.executable, '-B', '-I', '-c', program, str(archive)],
                                    cwd=directory, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
