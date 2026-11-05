# SPDX-License-Identifier: BSD-2-Clause
"""Private-root CLI signal policy with a real temporary producer."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from test_core import SDK_SOURCE


DRIVER = r'''
import signal, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from emberbsd_sdk import os_release, private_root
from emberbsd_sdk.process import run_logged
base, mode = Path(sys.argv[2]), sys.argv[3]

def personalize(options):
    if mode == 'failure':
        raise OSError('private fixture detail')
    if mode == 'writer':
        writer = """
import os, sys, time
from pathlib import Path
base = Path(sys.argv[1])
image = base / 'root.ffs'
image.write_bytes(b'initial')
(base / 'writer.pid').write_text(str(os.getpid()))
while True:
    with image.open('ab') as stream:
        stream.write(b'x')
    time.sleep(.02)
"""
        run_logged([sys.executable, '-B', '-c', writer, str(base)],
                   base / 'producer.log', timeout_seconds=30)
    (base / 'private.json').write_text('{}')
    return {'status': 'private_root_prepared', 'installation_ready': False}

# Replace only preparation: the CLI, signal policy and producer are real.
private_root.personalize = personalize
args = ['personalize']
for name in ('root-tree', 'overlay', 'authorized-key', 'boot-image',
             'kernel-version-file', 'host-prefix', 'ssh-keygen', 'tools',
             'makedev', 'os-source', 'output'):
    args.extend(('--' + name, str(base / name)))
args.extend(('--device-id', 'fixture', '--machine', 'evbarm',
             '--machine-arch', 'aarch64', '--device', 'std',
             '--byte-order', 'little', '--size', '1048576'))
previous = {number: signal.getsignal(number)
            for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
status = os_release.main(args)
assert all(signal.getsignal(number) == handler for number, handler in previous.items())
raise SystemExit(status)
'''


class PrivateRootSignalTests(unittest.TestCase):
    def command(self, root, mode):
        return [sys.executable, '-B', '-I', '-S', '-c', DRIVER,
                str(SDK_SOURCE / 'src'), str(root), mode]

    def test_term_and_hup_stop_writer_before_return(self):
        for number in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=number), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                child = subprocess.Popen(self.command(root, 'writer'),
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                writer_pid = None
                try:
                    deadline = time.monotonic() + 20
                    pidfile = root / 'writer.pid'
                    while time.monotonic() < deadline and child.poll() is None:
                        if pidfile.exists() and pidfile.stat().st_size:
                            writer_pid = int(pidfile.read_text())
                            break
                        time.sleep(.02)
                    self.assertIsNotNone(writer_pid, 'temporary producer did not start')
                    child.send_signal(number)
                    stdout, stderr = child.communicate(timeout=20)
                    self.assertEqual(child.returncode, 130, stderr)
                    self.assertEqual(stdout, '')
                    self.assertIn('interrupted', stderr)
                    self.assertNotIn(tmp, stderr)
                    self.assertFalse((root / 'private.json').exists())
                    with self.assertRaises(ProcessLookupError):
                        os.kill(writer_pid, 0)
                    image = root / 'root.ffs'
                    size = image.stat().st_size
                    time.sleep(.1)
                    self.assertEqual(image.stat().st_size, size)
                finally:
                    if child.poll() is None:
                        child.kill()
                    child.communicate(timeout=20)
                    if writer_pid is not None:
                        try:
                            os.kill(writer_pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass

    def test_success_and_failure_restore_handlers(self):
        for mode, expected in (('success', 0), ('failure', 1)):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                result = subprocess.run(self.command(Path(tmp), mode),
                                        capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertNotIn('private fixture detail', result.stderr)


if __name__ == '__main__':
    unittest.main()
