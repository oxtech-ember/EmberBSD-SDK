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
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.package_cache import directory_lock
from emberbsd_sdk.package_selection import launch_recorded
from emberbsd_sdk.source_wave import (
    SourceWaveLock, _creation, _descendants, _process_table, _recover_killed_owner,
    _release_if_ours, _stop_recorded, _write_controller, _write_stamp)


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
                echo ready > "$FIXTURE/bmake.ready"
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
            PREFIX) echo "$FIXTURE/prefix" ;;
            PKG_DBDIR) echo "$FIXTURE/pkgdb" ;;
            CROSS_DESTDIR) echo "$FIXTURE/sysroot" ;;
            OPSYS) echo NetBSD ;;
            MACHINE_ARCH) echo aarch64 ;;
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
        ready = self.fix / 'bmake.ready'

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
            while not ready.exists():
                ended, _status = os.waitpid(client_pid, os.WNOHANG)
                if ended:
                    self.fail('client exited before the controller reached bmake')
                if time.monotonic() > deadline:
                    self.fail('real controller did not reach bmake')
                time.sleep(0.05)
            self.assertTrue(self._fixture_commands())
            lock_pid = int((state / 'build.lock' / 'pid').read_text().strip())
            self._assert_recorded_controller(state, lock_pid)
            self.assertNotEqual(lock_pid, client_pid)
            os.kill(lock_pid, 0)
            os.kill(client_pid, 9)
            while self._fixture_commands():
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

    def test_killed_lock_owner_stops_the_real_controller_without_a_namespace(self):
        """SIGKILL of the published lock PID stops the setsid controller before the lock is reusable."""
        self._bmake(sleep=True)
        state = self.fix / 'state'
        ready = self.fix / 'bmake.ready'
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
            while not ready.exists():
                ended, _status = os.waitpid(client_pid, os.WNOHANG)
                if ended:
                    self.fail('client exited before the controller reached bmake')
                if time.monotonic() > deadline:
                    self.fail('real controller did not reach bmake')
                time.sleep(0.05)
            lock_pid = int((state / 'build.lock' / 'pid').read_text().strip())
            controller = self._assert_recorded_controller(state, lock_pid)
            self._assert_same_pid_namespace(lock_pid)
            self._assert_same_pid_namespace(controller)
            os.kill(lock_pid, 9)
            while self._fixture_commands() or self._alive(controller):
                if time.monotonic() > deadline:
                    self.fail('controller kept running after the published lock owner was killed')
                time.sleep(0.05)
            while (state / 'build.lock').exists():
                if time.monotonic() > deadline:
                    self.fail('build lock remained after the controller stopped')
                time.sleep(0.05)
            (self.fix / 'overlap-check').write_text('x')
            self.assertFalse((self.fix / 'overlap').exists())
            self.assertIn('start x11/app', (state / 'progress').read_text())
            self.sleep = False
            result = self._run(state)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertNotIn('Another build', result.stderr)
        finally:
            self._reap(client_pid)
            for path in set(Path('/tmp').glob('ember-wave-*')) - private_before:
                shutil.rmtree(path, ignore_errors=True)

    def test_one_interrupt_stops_the_controller(self):
        self._bmake(sleep=True)
        state = self.fix / 'state'
        ready = self.fix / 'bmake.ready'

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
            while not ready.exists():
                ended, _status = os.waitpid(client_pid, os.WNOHANG)
                if ended:
                    self.fail('client exited before the controller reached bmake')
                if time.monotonic() > deadline:
                    self.fail('real controller did not reach bmake')
                time.sleep(0.05)
            os.kill(client_pid, signal.SIGINT)
            while self._fixture_commands():
                if time.monotonic() > deadline:
                    self.fail('one interrupt left the controller running')
                time.sleep(0.05)
            while (state / 'build.lock').exists():
                if time.monotonic() > deadline:
                    self.fail('one interrupt left the build lock')
                time.sleep(0.05)
            _pid, status = os.waitpid(client_pid, 0)
            self.assertTrue(os.WIFEXITED(status))
            self.assertIn('start x11/app', (state / 'progress').read_text())
            (self.fix / 'overlap-check').write_text('x')
            self.assertFalse((self.fix / 'overlap').exists())
        finally:
            self._reap(client_pid)

    def test_ps_parent_walk_sees_a_setsid_child(self):
        pid = os.fork()
        if pid == 0:
            try:
                os.setsid()
                while True:
                    time.sleep(30)
            finally:
                os._exit(0)
        try:
            deadline = time.monotonic() + 5
            while pid not in _descendants(os.getpid()):
                if time.monotonic() > deadline:
                    self.fail('ps parent walk missed a setsid child')
                time.sleep(0.05)
            self.assertEqual(os.getsid(pid), pid)
        finally:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(pid, 0)

    def _build_config(self):
        host = self.fix / 'host'
        (host / 'bin').mkdir(parents=True, exist_ok=True)
        for directory in ('/bin', '/usr/bin'):
            folder = Path(directory)
            if not folder.is_dir():
                continue
            for tool in folder.iterdir():
                if tool.name in ('bmake', 'sh') or not tool.is_file():
                    continue
                dest = host / 'bin' / tool.name
                if dest.exists():
                    continue
                dest.write_text(f'#!/bin/sh\nexec {tool} "$@"\n')
                dest.chmod(0o755)
        wrapper = self.fix / 'wrapper.c'
        for name, target in (('bmake', self.fix / 'bin' / 'bmake'), ('sh', Path('/bin/sh'))):
            wrapper.write_text(
                '#include <unistd.h>\n'
                'int main(int argc, char **argv) {\n'
                f'    argv[0] = "{target}";\n'
                f'    execv("{target}", argv);\n'
                '    return 127;\n'
                '}\n')
            subprocess.check_call(['cc', '-o', str(host / 'bin' / name), str(wrapper)],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ports = self.fix / 'ports'
        (ports / 'profiles' / 'package-sets').mkdir(parents=True, exist_ok=True)
        scripts = ports / 'scripts'
        if not scripts.exists():
            scripts.symlink_to(PORTS / 'scripts', target_is_directory=True)
        (self.fix / 'prefix').mkdir(exist_ok=True)
        return {
            'host_prefix': str(host),
            'repositories': {'native': {
                'host': {'os': 'NetBSD', 'arch': 'aarch64'},
                'prefix': str(self.fix / 'prefix'),
                'pkgdb': str(self.fix / 'pkgdb'),
            }},
            'source': {
                'ports_root': str(ports),
                'pkgsrc': str(self.fix / 'pkgsrc'),
                'makeconf': str(self.fix / 'make.conf'),
                'state': str(self.fix / 'state'),
            },
        }

    def _build_client(self, state):
        config = self._build_config()

        def client():
            os.environ.update(self._env())
            session = SourceWaveLock(state)
            try:
                session.build(config, ['x11/app'], [])
            finally:
                session.close()

        return client

    def _fork_client(self, client):
        pid = os.fork()
        if pid == 0:
            try:
                client()
            finally:
                os._exit(0)
        return pid

    def _wait_ready(self, client_pid, ready):
        deadline = time.monotonic() + 30
        while not ready.exists():
            ended, _status = os.waitpid(client_pid, os.WNOHANG)
            if ended:
                self.fail('client exited before the controller reached bmake')
            if time.monotonic() > deadline:
                self.fail('real controller did not reach bmake')
            time.sleep(0.05)
        return deadline

    def test_build_path_records_the_controller_and_owner_kill_drains_it(self):
        self._bmake(sleep=True)
        state = self.fix / 'state'
        client_pid = self._fork_client(self._build_client(state))
        try:
            deadline = self._wait_ready(client_pid, self.fix / 'bmake.ready')
            lock_pid = int((state / 'build.lock' / 'pid').read_text().strip())
            controller = self._assert_recorded_controller(state, lock_pid)
            self.assertNotEqual(controller, 0)
            self._assert_same_pid_namespace(lock_pid)
            os.kill(lock_pid, signal.SIGKILL)
            while self._fixture_commands() or self._alive(controller):
                if (state / 'build.lock').exists() is False and (self._fixture_commands() or self._alive(controller)):
                    self.fail('build lock was removed while the controller was still running')
                if time.monotonic() > deadline:
                    self.fail('published owner kill left the build controller running')
                time.sleep(0.05)
            while (state / 'build.lock').exists():
                if time.monotonic() > deadline:
                    self.fail('build lock remained after the recorded controller stopped')
                time.sleep(0.05)
            (self.fix / 'overlap-check').write_text('x')
            self.assertFalse((self.fix / 'overlap').exists())
            self.assertIn('start x11/app', (state / 'progress').read_text())
            self.sleep = False
            result = self._run(state)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertNotIn('Another build', result.stderr)
        finally:
            self._reap(client_pid)

    def test_rejected_second_build_lock_does_not_touch_the_first(self):
        self._bmake(sleep=True)
        state = self.fix / 'state'
        client_pid = self._fork_client(self._build_client(state))
        try:
            deadline = self._wait_ready(client_pid, self.fix / 'bmake.ready')
            lock = state / 'build.lock'
            owner = lock.joinpath('owner').read_bytes()
            controller_text = lock.joinpath('controller').read_bytes()
            inode = lock.stat().st_ino
            controller = int(controller_text.splitlines()[0])
            lock_pid = int((lock / 'pid').read_text().strip())
            for _ in range(2):
                with self.assertRaisesRegex(SDKError, 'writer is busy'):
                    SourceWaveLock(state)
                self.assertEqual(lock.joinpath('owner').read_bytes(), owner)
                self.assertEqual(lock.joinpath('controller').read_bytes(), controller_text)
                self.assertEqual(lock.stat().st_ino, inode)
                self.assertTrue(self._alive(controller))
                self.assertTrue(self._alive(lock_pid))
            os.kill(lock_pid, signal.SIGKILL)
            while self._alive(controller) or self._fixture_commands():
                self.assertTrue(lock.exists(), 'rejected writer removed the live lock')
                if time.monotonic() > deadline:
                    self.fail('owner kill did not drain the recorded build controller')
                time.sleep(0.05)
            while lock.exists():
                if time.monotonic() > deadline:
                    self.fail('lock remained after the recorded controller stopped')
                time.sleep(0.05)
            self.assertFalse(self._alive(controller))
        finally:
            self._reap(client_pid)

    def test_reused_pid_is_not_signaled_and_lock_stays(self):
        pid = os.fork()
        if pid == 0:
            try:
                while True:
                    time.sleep(30)
            finally:
                os._exit(0)
        try:
            created = _creation(pid)
            self.assertTrue(created)
            self.assertFalse(_stop_recorded(pid, 'reused-or-unrelated'))
            self.assertTrue(self._alive(pid))
            state = self.fix / 'state'
            lock = state / 'build.lock'
            lock.mkdir()
            token = 'ab' * 16
            _write_stamp(lock, token, 424242)
            _write_controller(lock, pid, 'reused-or-unrelated', Path('/tmp/ember-wave-unrelated'))
            self.assertFalse(_recover_killed_owner(state, 'cd' * 16, 424242))
            self.assertFalse(_recover_killed_owner(state, token, 424242))
            self.assertFalse(_release_if_ours(lock, 'cd' * 16, 424242))
            self.assertTrue(lock.is_dir())
            self.assertTrue(self._alive(pid))
            self.assertTrue(_stop_recorded(pid, created))
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass
            row = _process_table().get(pid)
            self.assertTrue(row is None or row[1].startswith(('Z', 'z')))
        finally:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass

    def test_controller_cannot_run_before_its_record_is_written(self):
        marker = self.fix / 'ran'
        seen = {}

        def record(pid):
            self.assertFalse(marker.exists())
            seen['created'] = _creation(pid)
            self.assertTrue(seen['created'])
            seen['pid'] = pid

        child = launch_recorded(
            ['/bin/sh', '-c', f'touch {marker}; sleep 30'], dict(os.environ), record)
        try:
            deadline = time.monotonic() + 5
            while not marker.exists():
                if time.monotonic() > deadline:
                    self.fail('recorded child did not start after the gate opened')
                time.sleep(0.02)
            self.assertEqual(_creation(seen['pid']), seen['created'])
            self.assertEqual(os.getsid(seen['pid']), seen['pid'])
        finally:
            try:
                os.kill(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()

    def test_merge_failure_keeps_the_lock_and_private_schedule(self):
        self._bmake(sleep=False)
        state = self.fix / 'state'
        (state / 'progress').mkdir()
        private_before = set(Path('/tmp').glob('ember-wave-*'))
        os.environ.update(self._env())
        session = SourceWaveLock(state)
        try:
            with self.assertRaisesRegex(SDKError, 'not a regular file'):
                session.build(self._build_config(), ['x11/app'], [])
            self.assertTrue((state / 'build.lock').is_dir())
            kept = set(Path('/tmp').glob('ember-wave-*')) - private_before
            self.assertTrue(kept)
        finally:
            session.close()
        self.assertTrue((state / 'build.lock').is_dir())
        self.assertTrue(set(Path('/tmp').glob('ember-wave-*')) - private_before)
        for path in set(Path('/tmp').glob('ember-wave-*')) - private_before:
            shutil.rmtree(path, ignore_errors=True)

    def _command_map(self):
        result = subprocess.run(
            ['ps', '-ax', '-o', 'pid=,command='], capture_output=True, text=True, check=False)
        mapping = {}
        for line in result.stdout.splitlines():
            line = line.strip()
            pid, separator, command = line.partition(' ')
            if separator and pid.isdigit():
                mapping[int(pid)] = command
        return mapping

    def _fixture_commands(self):
        needle = str(self.fix)
        return {pid: command for pid, command in self._command_map().items() if needle in command}

    def _alive(self, pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    def _assert_same_pid_namespace(self, pid):
        own = Path('/proc/self/ns/pid')
        if not own.exists():
            return
        self.assertEqual(os.stat(f'/proc/{pid}/ns/pid').st_ino, own.stat().st_ino)

    def _assert_recorded_controller(self, state, lock_pid):
        self.assertGreater(lock_pid, 1)
        self._assert_same_pid_namespace(lock_pid)
        record = (state / 'build.lock' / 'controller').read_text().splitlines()
        controller = int(record[0])
        self.assertGreater(controller, 1)
        self.assertNotEqual(controller, lock_pid)
        self.assertEqual(record[1], _creation(controller))
        self.assertEqual(os.getsid(controller), controller)
        self.assertTrue(record[2].startswith('/'))
        self.assertIn('ember-wave-', record[2])
        stamp = (state / 'build.lock' / 'owner').read_text().splitlines()
        self.assertEqual(int(stamp[1]), lock_pid)
        commands = self._command_map()
        descendants = _descendants(controller)
        self.assertIn(controller, commands)
        self.assertIn('cross-bulk.sh', commands[controller])
        self.assertTrue(any('stage-context-supervisor.py' in commands.get(pid, '') for pid in descendants))
        self.assertTrue(any('bmake' in commands.get(pid, '') for pid in descendants))
        return controller

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
