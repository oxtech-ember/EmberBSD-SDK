# SPDX-License-Identifier: BSD-2-Clause
import json
import signal
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.package_relocation import relocate_declared_text
from emberbsd_sdk.path_qualification import qualify


class PathQualificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.old = self.base / 'producer-A'
        self.new = self.base / 'consumer B longer'
        self.old.mkdir(); self.new.mkdir()

    def test_existing_relocation_then_probe_with_old_root_absent(self):
        source = self.old / 'fixture.py'
        source.write_text('from pathlib import Path\nassert Path(' +
                          repr(str(self.old / 'resource')) + ').read_text() == "ok"\n')
        (self.old / 'resource').write_text('ok')
        shutil.copytree(self.old, self.new, dirs_exist_ok=True)
        relocate_declared_text(self.new, ['fixture.py'], str(self.old), self.new)
        shutil.rmtree(self.old)
        report = qualify(self.new, [self.old], ['fixture.py'],
                         [sys.executable, '-I', str(self.new / 'fixture.py')])
        self.assertEqual(report['static_check'], 'passed')
        self.assertEqual(report['host_probe'], 'passed')
        self.assertEqual(report['target_execution'], 'not-assessed')

    def test_leaks_do_not_get_rewritten_or_run_probe(self):
        path = self.new / 'lib.pc'; original = 'prefix=' + str(self.old)
        path.write_text(original)
        shutil.rmtree(self.old)
        report = qualify(self.new, [self.old], ['lib.pc'], ['does-not-exist'])
        self.assertEqual(report['static_check'], 'failed')
        self.assertEqual(report['host_probe'], 'not-assessed')
        self.assertEqual(path.read_text(), original)

    def test_static_only_does_not_require_hiding_producer(self):
        (self.new / 'config.cmake').write_text('prefix=${CMAKE_CURRENT_LIST_DIR}')
        report = qualify(self.new, [self.old], ['config.cmake'])
        self.assertEqual(report['static_check'], 'passed')
        self.assertEqual(report['host_probe'], 'not-assessed')
        with self.assertRaisesRegex(SDKError, 'must be absent'):
            qualify(self.new, [self.old], [], [sys.executable, '-c', 'pass'])

    def test_probe_failure_timeout_and_no_assessment(self):
        shutil.rmtree(self.old)
        report = qualify(self.new, [self.old])
        self.assertEqual(report['static_check'], 'not-assessed')
        report = qualify(self.new, [self.old], [], [sys.executable, '-c', 'raise SystemExit(3)'])
        self.assertEqual(report['probe_exit_code'], 3)
        self.assertEqual(report['host_probe'], 'failed')
        report = qualify(self.new, [self.old], [],
                         [sys.executable, '-c', 'import time; time.sleep(2)'], timeout=.02)
        self.assertTrue(report['probe_timeout'])

    def test_escape_symlink_binary_and_overlapping_roots_rejected(self):
        (self.new / 'escape').symlink_to(self.old)
        (self.old / 'outside').write_text('data')
        (self.new / 'binary').write_bytes(b'\x00binary')
        for name in ('../producer-A/outside', 'escape/outside', str(self.old / 'outside'), 'binary'):
            with self.subTest(name=name), self.assertRaises(SDKError):
                qualify(self.new, [self.old], [name])
        for prefix in ('/', 'relative', str(self.new), str(self.base)):
            with self.subTest(prefix=prefix), self.assertRaises(SDKError):
                qualify(self.new, [prefix])

    def test_alias_overlap_uses_canonical_paths_but_scans_raw_spelling(self):
        alias = self.base / 'alias'
        alias.symlink_to(self.base, target_is_directory=True)
        for prefix in (alias, alias / self.new.name, alias / self.new.name / 'child'):
            with self.subTest(prefix=prefix), self.assertRaisesRegex(SDKError, 'must not overlap'):
                qualify(self.new, [prefix])
        raw = str(alias / self.old.name)
        metadata = self.new / 'aliased.pc'
        metadata.write_text('prefix=' + raw)
        report = qualify(alias / self.new.name, [raw], ['aliased.pc'])
        self.assertEqual(report['static_check'], 'failed')
        self.assertEqual(report['forbidden_prefixes'], [raw])
        self.assertEqual(report['leaks'][0]['forbidden_prefix'], raw)
        shutil.rmtree(self.old)
        report = qualify(self.new, [raw], ['aliased.pc'])
        self.assertEqual(report['static_check'], 'failed')

    def test_timeout_kills_probe_descendants(self):
        shutil.rmtree(self.old)
        marker = self.new / 'escaped-child'
        child = 'import time; from pathlib import Path; time.sleep(.3); Path(' + repr(str(marker)) + ').touch()'
        parent = 'import subprocess, sys, time; subprocess.Popen([sys.executable, "-c", ' + repr(child) + ']); time.sleep(5)'
        report = qualify(self.new, [self.old], [], [sys.executable, '-c', parent], timeout=.1)
        self.assertTrue(report['probe_timeout'])
        time.sleep(.4)
        self.assertFalse(marker.exists())

    def test_interruption_cleans_up_probe_group(self):
        from emberbsd_sdk.path_qualification import _run_probe
        with patch('emberbsd_sdk.path_qualification.subprocess.Popen') as popen, \
             patch('emberbsd_sdk.path_qualification.os.killpg') as kill:
            process = popen.return_value
            process.pid = 98765
            process.wait.side_effect = [KeyboardInterrupt(), -9]
            with self.assertRaises(KeyboardInterrupt):
                _run_probe(['explicit-probe'], self.new, 1)
            kill.assert_called_once()
            self.assertEqual(kill.call_args.args[0], process.pid)
            self.assertEqual(process.wait.call_count, 2)

    def test_termination_signals_kill_descendants(self):
        shutil.rmtree(self.old)
        script = Path(__file__).resolve().parents[1] / 'scripts/qualify-package-paths.py'
        for number in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=number):
                ready = self.new / ('ready-' + str(number))
                marker = self.new / ('escaped-' + str(number))
                child = ('import time; from pathlib import Path; Path(' + repr(str(ready)) +
                         ').touch(); time.sleep(.5); Path(' + repr(str(marker)) + ').touch()')
                parent = ('import subprocess, sys, time; subprocess.Popen([sys.executable, "-c", ' +
                          repr(child) + ']); time.sleep(5)')
                process = subprocess.Popen([sys.executable, str(script), '--root', str(self.new),
                    '--forbid-prefix', str(self.old), '--', sys.executable, '-c', parent],
                    stdout=subprocess.DEVNULL)
                try:
                    deadline = time.monotonic() + 3
                    while not ready.exists() and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertTrue(ready.exists(), 'fixture did not become ready')
                    process.send_signal(number)
                    self.assertEqual(process.wait(timeout=3), 128 + number)
                    time.sleep(.6)
                    self.assertFalse(marker.exists())
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=3)

    def test_probe_restores_signal_handlers(self):
        shutil.rmtree(self.old)
        previous = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGHUP)}
        qualify(self.new, [self.old], [], [sys.executable, '-c', 'pass'])
        self.assertEqual(previous, {number: signal.getsignal(number) for number in previous})

    def test_nonfinite_probe_timeouts_rejected(self):
        shutil.rmtree(self.old)
        for timeout in (float('nan'), float('inf'), -1, 0):
            with self.subTest(timeout=timeout), self.assertRaises(SDKError):
                qualify(self.new, [self.old], [], [sys.executable, '-c', 'pass'], timeout=timeout)

    def test_cli_static_failure_exit_code(self):
        (self.new / 'config.la').write_text('libdir=' + str(self.old))
        script = Path(__file__).resolve().parents[1] / 'scripts/qualify-package-paths.py'
        result = subprocess.run([sys.executable, str(script), '--root', str(self.new),
                                 '--forbid-prefix', str(self.old), '--metadata', 'config.la'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)['static_check'], 'failed')


if __name__ == '__main__':
    unittest.main()
