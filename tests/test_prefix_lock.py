# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Readers and writers of one package prefix in separate process trees."""

import os
import signal
from pathlib import Path
import subprocess
import sys
import time

from test_core import TemporaryCase, SDK_SOURCE

TOOL = SDK_SOURCE / 'scripts/prefix-lock.py'
# Hold until the release file exists. A nested writer changes the prefix.
HOLD = '''import os,subprocess,sys,time
from pathlib import Path
ready,release=Path(sys.argv[1]),Path(sys.argv[2])
if len(sys.argv)>3:
    result=subprocess.run(sys.argv[3:])
    ready.write_text('nested %d' % result.returncode)
else:
    ready.write_text('held')
deadline=time.monotonic()+30
while not release.exists() and time.monotonic()<deadline:
    time.sleep(.01)
'''
# Read the prefix, start a child with the reader descriptors, then die.
ORPHAN = '''import os,signal,subprocess,sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from emberbsd_sdk import prefix_lock
with prefix_lock.reading([sys.argv[2]]) as readers:
    child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],pass_fds=readers,start_new_session=True)
    Path(sys.argv[3]).write_text(str(child.pid))
    os.kill(os.getpid(),signal.SIGKILL)
'''
CHANGE = '''import sys
from pathlib import Path
Path(sys.argv[1]).write_text('changed')
'''


class PrefixLockTest(TemporaryCase):
    def setUp(self):
        super().setUp()
        self.hold = self.write('hold.py', HOLD)
        self.change = self.write('change.py', CHANGE)

    def database(self, name):
        path = self.root / name / 'pkgdb'
        path.mkdir(parents=True)
        return path

    def wrap(self, mode, databases, *command):
        argv = [sys.executable, '-B', str(TOOL), mode]
        for database in databases:
            argv += ['--database', str(database)]
        return argv + ['--', *command]

    def start_reader(self, databases, *nested):
        ready, release = self.root / f'ready-{time.monotonic_ns()}', self.root / f'release-{time.monotonic_ns()}'
        env = {k: v for k, v in os.environ.items() if k != 'EMBERBSD_PREFIX_READERS'}
        process = subprocess.Popen(self.wrap('read', databases, sys.executable, str(self.hold),
                                             str(ready), str(release), *nested), env=env)
        self.addCleanup(self.stop, process, release)
        deadline = time.monotonic() + 20
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        return process, ready, release

    def stop(self, process, release):
        release.touch()
        process.wait(timeout=20)

    def change_prefix(self, database, target, env=None):
        return subprocess.run(self.wrap('write', [database], sys.executable, str(self.change), str(target)),
                              capture_output=True, text=True, env=env, timeout=20)

    def test_two_readers_share_and_a_foreign_writer_stops_before_change(self):
        database = self.database('host')
        first, first_ready, _ = self.start_reader([database])
        second, second_ready, _ = self.start_reader([database])
        self.assertEqual(first_ready.read_text(), 'held')
        self.assertEqual(second_ready.read_text(), 'held')
        target = self.root / 'host/changed'
        result = self.change_prefix(database, target)
        self.assertEqual(result.returncode, 75, result.stderr)
        self.assertIn('another build uses the prefix', result.stderr)
        self.assertFalse(target.exists())

    def test_the_writer_of_the_reading_build_changes_its_prefix(self):
        database = self.database('host')
        target = self.root / 'host/changed'
        nested = self.wrap('write', [database], sys.executable, str(self.change), str(target))
        _, ready, _ = self.start_reader([database], *nested)
        self.assertEqual(ready.read_text(), 'nested 0')
        self.assertEqual(target.read_text(), 'changed')

    def test_a_stopped_reader_does_not_block_a_writer(self):
        database = self.database('host')
        process, _, release = self.start_reader([database])
        self.stop(process, release)
        target = self.root / 'host/changed'
        result = self.change_prefix(database, target)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(target.read_text(), 'changed')

    def test_a_reader_of_another_prefix_does_not_block_a_writer(self):
        host, sysroot = self.database('host'), self.database('sysroot')
        self.start_reader([sysroot])
        target = self.root / 'host/changed'
        result = self.change_prefix(host, target)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_reader_waits_while_a_writer_changes_the_prefix(self):
        database = self.database('host')
        ready, release = self.root / 'writer-ready', self.root / 'writer-release'
        writer = subprocess.Popen(self.wrap('write', [database], sys.executable, str(self.hold),
                                            str(ready), str(release)))
        self.addCleanup(self.stop, writer, release)
        deadline = time.monotonic() + 20
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        reader_ready, reader_release = self.root / 'reader-ready', self.root / 'reader-release'
        reader = subprocess.Popen(self.wrap('read', [database], sys.executable, str(self.hold),
                                            str(reader_ready), str(reader_release)))
        self.addCleanup(self.stop, reader, reader_release)
        time.sleep(.5)
        self.assertFalse(reader_ready.exists())
        release.touch()
        writer.wait(timeout=20)
        deadline = time.monotonic() + 20
        while not reader_ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(reader_ready.read_text(), 'held')

    def test_a_child_with_the_reader_descriptors_keeps_the_prefix_after_its_parent_dies(self):
        database = self.database('host')
        orphan, started = self.write('orphan.py', ORPHAN), self.root / 'started'
        subprocess.run([sys.executable, '-B', str(orphan), str(SDK_SOURCE / 'src'), str(database), str(started)],
                       timeout=20)
        child = int(started.read_text())
        self.addCleanup(os.kill, child, signal.SIGKILL)
        target = self.root / 'host/changed'
        result = self.change_prefix(database, target)
        self.assertEqual(result.returncode, 75, result.stderr)
        self.assertFalse(target.exists())
