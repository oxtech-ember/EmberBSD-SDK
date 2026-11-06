# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Cross-bulk ownership around one source wave.

The controller is the reviewed Ports script, blob e4eac7cbbb1b48b3282ef834a84dc87965f251ba,
with a bmake stub. It is not a reimplementation of the scheduler.
"""

import hashlib
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk.package_cache import directory_lock
from emberbsd_sdk.source_wave import SourceWaveLock


ROOT = Path(__file__).resolve().parents[1]
PORTS = ROOT / 'tests' / 'fixtures' / 'ports-e4eac7c'
SCRIPT = PORTS / 'scripts' / 'cross-bulk.sh'
BLOB = 'e4eac7cbbb1b48b3282ef834a84dc87965f251ba'
SUPERVISOR_BLOB = '1e8b37e4d0d98fcceb9aaed75bc2b047083baa43'


def _blob(path):
    data = path.read_bytes()
    return hashlib.sha1(f'blob {len(data)}\0'.encode() + data).hexdigest()


class RealControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self._stop_fixture_processes)
        self.root = Path(self.tmp.name)
        self.fix = self.root / 'tree'
        for path in ('pkgsrc/pkgtools/pkg_install', 'pkgsrc/x11/app', 'state', 'packages/All', 'bin', 'pkgdb'):
            (self.fix / path).mkdir(parents=True)
        (self.fix / 'pkgsrc/x11/app/Makefile').write_text('# fixture\n')
        (self.fix / 'make.conf').write_text('')
        (self.fix / 'state/order').write_text('x11/app\n')
        (self.fix / 'state/edges').write_text('x11/app x11/app\n')
        (self.fix / 'roots').write_text('x11/app\n')
        self._bmake(sleep=False)
        self._pkg_info()

    def _bmake(self, *, sleep):
        path = self.fix / 'bin' / 'bmake'
        path.write_text(r'''#!/bin/sh
printf '%s\n' "$*" >> "$FIXTURE/bmake.log"
var= vars=
for arg in "$@"; do
    case "$arg" in
        VARNAME=*) var=${arg#VARNAME=} ;;
        VARNAMES=*) vars=${arg#VARNAMES=} ;;
        package-install|package|replace)
            echo "$arg" >> "$FIXTURE/targets"
            if [ -n "$BMAKE_SLEEP_ON_BUILD" ]; then
                echo $$ > "$FIXTURE/bmake.pid"
                while :; do
                    if [ -f "$FIXTURE/overlap-check" ]; then
                        echo overlap > "$FIXTURE/overlap"
                        exit 0
                    fi
                    sleep 0.05
                done
            fi
            mkdir -p "$FIXTURE/packages/All"
            echo package > "$FIXTURE/packages/All/app-9.tgz"
            exit 0
            ;;
        clean) exit 0 ;;
    esac
done
if [ -n "$var" ]; then
    case "$var" in
        EMBERBSD_NATIVE_TOOLSET_VERIFIED) echo "$FIXTURE/native" ;;
        PKG_INFO) echo "$FIXTURE/bin/pkg_info" ;;
        NATIVE_PKG_ADD_CMD) echo /bin/true ;;
        CROSSTARGETSETTINGS) echo OPSYS=NetBSD ;;
        PACKAGES) echo "$FIXTURE/packages" ;;
        WRKOBJDIR) echo ;;
        _PKG_DBDIR) echo "$FIXTURE/pkgdb" ;;
        *) echo ;;
    esac
    exit 0
fi
if [ -n "$vars" ]; then
    for name in $vars; do
        case "$name" in
            EMBERBSD_HOST_PYTHON) echo "$HOST_PYTHON" ;;
            EMBERBSD_SDK_SOURCE) echo "$SDK_SOURCE" ;;
            PKGNAME) echo app-9 ;;
            WRKDIR) echo "$FIXTURE/work/app" ;;
            PKG_SKIP_REASON) echo ;;
            PKGBASE) echo app ;;
            PKGVERSION_NOREV) echo 9 ;;
            PKGFILE) echo "$FIXTURE/packages/All/app-9.tgz" ;;
            WRKOBJDIR) echo "$FIXTURE/work" ;;
            .MAKE.MAKEFILES) echo "$FIXTURE/pkgsrc/x11/app/Makefile" ;;
            .CURDIR) echo "$FIXTURE/pkgsrc/x11/app" ;;
            *) echo ;;
        esac
    done
    exit 0
fi
exit 0
''')
        path.chmod(0o755)
        self.sleep = sleep

    def _pkg_info(self):
        path = self.fix / 'bin' / 'pkg_info'
        path.write_text('#!/bin/sh\necho "pkg_info $*" >> "$FIXTURE/pkg_info.log"\nexit 0\n')
        path.chmod(0o755)

    def _env(self):
        env = os.environ.copy()
        env.update(
            FIXTURE=str(self.fix),
            HOST_PYTHON=sys.executable,
            SDK_SOURCE=str(ROOT),
            BMAKE=str(self.fix / 'bin' / 'bmake'),
            MAKECONF=str(self.fix / 'make.conf'),
            PKGSRCDIR=str(self.fix / 'pkgsrc'),
            PATH=str(self.fix / 'bin') + os.pathsep + env.get('PATH', ''),
            CROSS_BULK_JOBS='1',
            CROSS_BULK_TRASH='no',
            CROSS_BULK_VARS_CACHE='no',
            CROSS_BULK_SHELL='/bin/sh',
            BMAKE_SLEEP_ON_BUILD='1' if self.sleep else '',
        )
        return env

    def _run(self, state):
        return subprocess.run(
            ['/bin/sh', str(SCRIPT), 'build', str(self.fix / 'roots'), str(state)],
            env=self._env(), capture_output=True, text=True, timeout=30)

    def test_fixture_is_the_reviewed_cross_bulk_blob(self):
        self.assertEqual(_blob(SCRIPT), BLOB)
        self.assertEqual(_blob(PORTS / 'scripts' / 'stage-context-supervisor.py'), SUPERVISOR_BLOB)

    def test_real_controller_replaces_a_newer_target_and_records_its_pkgname(self):
        result = self._run(self.fix / 'state')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual((self.fix / 'targets').read_text().split(), ['package', 'replace'])
        self.assertIn('pkg_info -e app\n', (self.fix / 'pkg_info.log').read_text())
        done = (self.fix / 'state' / 'done').read_text().splitlines()
        self.assertEqual(len(done), 1)
        self.assertTrue(done[0].startswith('x11/app app-9 '))
        self.assertNotIn('app-1', done[0])

    def test_open_wave_lock_rejects_a_second_real_controller(self):
        state = self.fix / 'state'
        session = SourceWaveLock(state)
        try:
            self.assertNotEqual((state / 'build.lock' / 'pid').read_text().strip(), str(os.getpid()))
            result = self._run(state)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Another build', result.stderr)
            self.assertFalse((self.fix / 'targets').exists())
        finally:
            session.close()
        self.assertFalse((state / 'build.lock').exists())
        with directory_lock(state / 'build.lock'):
            self.assertTrue((state / 'build.lock').is_dir())

    def test_killed_client_stops_the_controller_before_the_lock_can_be_taken(self):
        self._bmake(sleep=True)
        state = self.fix / 'state'
        pid_file = self.fix / 'bmake.pid'

        def client():
            session = SourceWaveLock(state)
            try:
                session.exec_controller(
                    ['/bin/sh', str(SCRIPT), 'build', str(self.fix / 'roots'), '{state}'],
                    self._env(), ['x11/app'])
            finally:
                session.close()

        client_pid = os.fork()
        if client_pid == 0:
            try:
                client()
            finally:
                os._exit(0)
        try:
            deadline = time.monotonic() + 20
            while not pid_file.exists():
                ended, _status = os.waitpid(client_pid, os.WNOHANG)
                if ended:
                    self.fail('client exited before the controller reached bmake')
                if time.monotonic() > deadline:
                    self.fail('real controller did not reach bmake')
                time.sleep(0.05)
            bmake_pid = int(pid_file.read_text().strip())
            lock_pid = int((state / 'build.lock' / 'pid').read_text().strip())
            self.assertNotEqual(lock_pid, client_pid)
            os.kill(lock_pid, 0)
            os.kill(client_pid, 9)
            while True:
                try:
                    os.kill(bmake_pid, 0)
                except ProcessLookupError:
                    break
                if time.monotonic() > deadline:
                    self.fail('controller kept running after the client was killed')
                time.sleep(0.05)
            while (state / 'build.lock').exists():
                if time.monotonic() > deadline:
                    self.fail('build lock remained after the controller stopped')
                time.sleep(0.05)
            (self.fix / 'overlap-check').write_text('x')
            self.assertFalse((self.fix / 'overlap').exists())
            self.assertEqual((state / 'order').read_text(), 'x11/app\n')
            self.assertIn('start x11/app', (state / 'progress').read_text())
            self.sleep = False
            result = self._run(state)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertNotIn('Another build', result.stderr)
        finally:
            self._reap(client_pid)

    def test_killed_supervisor_leaves_the_lock_and_the_controller(self):
        """SIGKILL of the lock owner is not recovered from this process.

        The real controller keeps running and the lock stays. Removing that
        lock while it runs would let a second build share the work directory.
        """
        self._bmake(sleep=True)
        state = self.fix / 'state'
        pid_file = self.fix / 'bmake.pid'
        private_before = set(Path('/tmp').glob('ember-wave-*'))

        def client():
            session = SourceWaveLock(state)
            try:
                session.exec_controller(
                    ['/bin/sh', str(SCRIPT), 'build', str(self.fix / 'roots'), '{state}'],
                    self._env(), ['x11/app'])
            finally:
                session.close()

        client_pid = os.fork()
        if client_pid == 0:
            try:
                client()
            finally:
                os._exit(0)
        try:
            deadline = time.monotonic() + 20
            while not pid_file.exists():
                ended, _status = os.waitpid(client_pid, os.WNOHANG)
                if ended:
                    self.fail('client exited before the controller reached bmake')
                if time.monotonic() > deadline:
                    self.fail('real controller did not reach bmake')
                time.sleep(0.05)
            bmake_pid = int(pid_file.read_text().strip())
            lock_pid = int((state / 'build.lock' / 'pid').read_text().strip())
            os.kill(lock_pid, 9)
            while True:
                try:
                    os.kill(lock_pid, 0)
                except ProcessLookupError:
                    break
                if time.monotonic() > deadline:
                    self.fail('supervisor did not die')
                time.sleep(0.05)
            time.sleep(0.4)
            self.assertTrue((state / 'build.lock').is_dir())
            os.kill(bmake_pid, 0)
        finally:
            self._reap(client_pid)
            if pid_file.exists():
                try:
                    os.kill(int(pid_file.read_text().strip()), 9)
                except (ProcessLookupError, ValueError):
                    pass
            for path in set(Path('/tmp').glob('ember-wave-*')) - private_before:
                shutil.rmtree(path, ignore_errors=True)
            lock = state / 'build.lock'
            if lock.is_dir():
                shutil.rmtree(lock, ignore_errors=True)

    def _reap(self, pid):
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass

    def _stop_fixture_processes(self):
        """Stop a controller that outlived the test, including its stage session."""
        needle = str(self.fix).encode()
        for _ in range(20):
            parents, commands = {}, {}
            for entry in os.listdir('/proc'):
                if not entry.isdigit():
                    continue
                pid = int(entry)
                if pid == os.getpid():
                    continue
                try:
                    command = Path('/proc', entry, 'cmdline').read_bytes().replace(b'\0', b' ')
                    status = Path('/proc', entry, 'status').read_text()
                except OSError:
                    continue
                commands[pid] = command
                for line in status.splitlines():
                    if line.startswith('PPid:'):
                        parents[pid] = int(line.split()[1])
                        break
            wanted = {pid for pid, command in commands.items() if needle in command}
            if not wanted:
                return
            changed = True
            while changed:
                changed = False
                for pid, parent in parents.items():
                    if parent in wanted and pid not in wanted:
                        wanted.add(pid)
                        changed = True
            for pid in wanted:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            time.sleep(0.05)


if __name__ == '__main__':
    unittest.main()
