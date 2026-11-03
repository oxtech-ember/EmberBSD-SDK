#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Package selection stays a thin frontend to the Ports bulk controller."""

import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest import mock


SDK = Path(__file__).resolve().parents[1]
SCRIPT = SDK / 'scripts' / 'packages.py'
sys.path.insert(0, os.fspath(SDK / 'src'))
from emberbsd_sdk import package_selection


class SelectedHostPrefix:
    def __init__(self, bmake, shell):
        self.tools = {'make': os.fspath(bmake), 'sh': os.fspath(shell)}

    def tool(self, role):
        return self.tools[role]

    @property
    def path(self):
        return os.pathsep.join(sorted({os.path.dirname(path) for path in self.tools.values()}))


class PackageSelectionTests(unittest.TestCase):
    def test_controller_stdout_can_be_separated_from_frontend_output(self):
        native = SelectedHostPrefix(self.bmake, self.shell)
        output = StringIO()
        with tempfile.TemporaryFile(mode='w+') as child_output, \
                mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(package_selection, 'load_host_prefix', return_value=native), \
                redirect_stdout(output):
            status = package_selection.main(['--ports-root', str(self.ports), 'plan',
                '--host-prefix', str(self.prefix), '--state', str(self.state), '--set', 'plasma'],
                controller_stdout=child_output)
            child_output.seek(0)
            self.assertIn('Dependency closure:', child_output.read())
        self.assertEqual(status, 0)
        self.assertNotIn('Dependency closure:', output.getvalue())

    def setUp(self):
        tmp = Path(os.environ.get('TMPDIR', SDK / 'tmp'))
        tmp.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix='packages-ui-', dir=tmp)
        self.root = Path(self.tmp.name)
        self.ports = self.root / 'ports'
        (self.ports / 'profiles' / 'package-sets').mkdir(parents=True)
        (self.ports / 'scripts').mkdir()
        (self.ports / 'profiles' / 'package-sets' / 'plasma.txt').write_text(
            '# minimal Plasma session\nsysutils/ember-plasma-session\n', encoding='utf-8')
        self.state = self.root / 'state'
        self.calls = self.root / 'calls'
        self.controller_output = self.root / 'controller-output'
        self.prefix = self.root / 'host-prefix'
        (self.prefix / 'bin').mkdir(parents=True)
        for utility in ('cp', 'mkdir', 'sleep'):
            (self.prefix / 'bin' / utility).symlink_to('/bin/' + utility)
        self.cross_bulk = self.ports / 'scripts' / 'cross-bulk.sh'
        self.cross_bulk.write_text('''#!/bin/sh
mode=$1 roots=$2 state=$3
printf '%s\\n' "$mode $roots $state" >> "$CALL_LOG"
cp "$roots" "$ROOT_CAPTURE.$mode"
case "$mode" in
    selected-order|selected-build)
        mkdir -p "$state"
        printf '%s\\n' 'Target: NetBSD aarch64 (aarch64--netbsd)' \\
            'Selected roots: 3' 'Dependency closure: 3 packages (2 target, 1 native)'
        printf '%s\\n' 'Target: NetBSD aarch64 (aarch64--netbsd)' \\
            'Selected roots: 3' 'Dependency closure: 3 packages (2 target, 1 native)' >> "$CONTROLLER_OUTPUT"
        if [ "$mode" = selected-build ]; then
            printf '%s\\n' build-started >> "$CONTROLLER_OUTPUT"
        fi
        ;;
    *) exit 2 ;;
esac
''', encoding='utf-8')
        self.cross_bulk.chmod(0o755)
        self.shell = self.prefix / 'bin' / 'sh'
        self.shell.write_text('''#!/bin/sh
printf '%s\\n' "$*" >> "$SHELL_LOG"
printf '%s\\n' "$PATH" >> "$PATH_LOG"
exec "$REAL_SHELL" "$@"
''', encoding='utf-8')
        self.shell.chmod(0o755)
        self.bmake = self.prefix / 'bin' / 'bmake'
        self.bmake.write_text('''#!/bin/sh
echo "$*" >> "$BMAKE_LOG"
printf '%s\\n' "$PATH" >> "$PATH_LOG"
for arg do
    if [ "$arg" = 'VARNAMES=OPSYS MACHINE_ARCH MACHINE_GNU_PLATFORM' ]; then
        printf '%s\\n' NetBSD aarch64 aarch64--netbsd
        exit 0
    fi
done
exit 2
''', encoding='utf-8')
        self.bmake.chmod(0o755)
        self.env = os.environ.copy()
        self.env.update({
            'CALL_LOG': str(self.calls),
            'CONTROLLER_OUTPUT': str(self.controller_output),
            'ROOT_CAPTURE': str(self.root / 'captured-roots'),
            'SHELL_LOG': str(self.root / 'shell-calls'),
            'PATH_LOG': str(self.root / 'paths'),
            'REAL_SHELL': os.path.realpath('/bin/sh'),
            'BMAKE_LOG': str(self.root / 'bmake-calls'),
            'WORKER_STARTED': str(self.root / 'worker-started'),
            'CONTROLLER_STOPPED': str(self.root / 'controller-stopped'),
            'WORKER_FINISHED': str(self.root / 'worker-finished'),
            'WORKER_SIGNALLED': str(self.root / 'worker-signalled'),
            'ROOTS_PRESENT': str(self.root / 'roots-present'),
            'CONTROLLER_DRAINED': str(self.root / 'controller-drained'),
            'BMAKE': str(self.bmake),
            'MAKECONF': str(self.root / 'cross.mk.conf'),
            'PKGSRCDIR': str(self.root / 'pkgsrc'),
            'TMPDIR': str(self.root / 'tmp'),
        })
        (self.root / 'cross.mk.conf').write_text('', encoding='utf-8')
        (self.root / 'pkgsrc' / 'pkgtools' / 'pkg_install').mkdir(parents=True)
        (self.root / 'tmp').mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args, env=None):
        return subprocess.run(
            [os.fspath(sys.executable), os.fspath(SCRIPT),
             '--ports-root', os.fspath(self.ports), *args],
            cwd=SDK, env=env or self.env, text=True, capture_output=True,
            check=False)

    def run_main(self, *args, bmake=None):
        output = StringIO()
        native = SelectedHostPrefix(bmake or self.bmake, self.shell)
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(package_selection, 'load_host_prefix',
                                  return_value=native) as selector, \
                redirect_stdout(output), redirect_stderr(output):
            status = package_selection.main([
                '--ports-root', os.fspath(self.ports), *args])
        return status, output.getvalue(), selector

    def test_list_and_show_need_no_sdk_or_bmake(self):
        env = os.environ.copy()
        env['BMAKE'] = '/missing/bmake'
        listed = self.run_cli('list', env=env)
        shown = self.run_cli('show', '--set', 'plasma', env=env)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn('plasma', listed.stdout)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertEqual(shown.stdout.splitlines(), [
            'target sysutils/ember-plasma-session'])
        self.assertFalse(self.calls.exists())

    def test_help_is_independent_of_the_calling_directory(self):
        result = subprocess.run(
            [os.fspath(sys.executable), os.fspath(SCRIPT), '--help'],
            cwd=self.root, text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Select explicit EmberBSD Ports package roots', result.stdout)

    def test_empty_selection_does_not_build_an_implicit_world(self):
        for command in ('plan', 'build'):
            status, output, selector = self.run_main(
                command, '--state', os.fspath(self.state))
            self.assertEqual(status, 0)
            self.assertIn('nothing selected', output.lower())
            selector.assert_not_called()
        self.assertFalse(self.state.exists())
        self.assertFalse(self.calls.exists())
        self.assertFalse((self.root / 'bmake-calls').exists())

    def test_plan_and_build_send_only_explicit_roots_to_controller(self):
        common = ('--state', os.fspath(self.state), '--host-prefix', os.fspath(self.prefix),
                  '--set', 'plasma', '--package', 'cat/app',
                  '--package', 'lang/tool@native', '--package', 'cat/app')
        planned_status, planned_output, selector = self.run_main('plan', *common)
        self.assertEqual(planned_status, 0, planned_output)
        controller_lines = self.controller_output.read_text().splitlines()
        self.assertEqual(controller_lines[:3], [
            'Target: NetBSD aarch64 (aarch64--netbsd)', 'Selected roots: 3',
            'Dependency closure: 3 packages (2 target, 1 native)'])
        selector.assert_called_once_with(os.fspath(self.prefix), required_roles={'make', 'sh'})
        self.assertFalse((self.state / 'order').exists())
        self.assertEqual((self.root / 'captured-roots.selected-order').read_text().splitlines(), [
            'sysutils/ember-plasma-session', 'cat/app', 'lang/tool@native'])

        built_status, built_output, selector = self.run_main('build', *common)
        self.assertEqual(built_status, 0, built_output)
        self.assertIn('build prefixes/sysroot', built_output)
        controller_lines = self.controller_output.read_text().splitlines()
        self.assertEqual(controller_lines[3:], [
            'Target: NetBSD aarch64 (aarch64--netbsd)', 'Selected roots: 3',
            'Dependency closure: 3 packages (2 target, 1 native)', 'build-started'])
        modes = [line.split()[0] for line in self.calls.read_text().splitlines()]
        self.assertEqual(modes, ['selected-order', 'selected-build'])
        self.assertEqual((self.root / 'captured-roots.selected-build').read_text().splitlines(), [
            'sysutils/ember-plasma-session', 'cat/app', 'lang/tool@native'])
        shell_calls = (self.root / 'shell-calls').read_text().splitlines()
        self.assertEqual(len(shell_calls), 2)
        self.assertTrue(all(Path(line.split()[0]).samefile(self.cross_bulk)
                            for line in shell_calls))
        selector.assert_called_once_with(os.fspath(self.prefix), required_roles={'make', 'sh'})
        self.assertFalse((self.root / 'bmake-calls').exists())
        for call in self.calls.read_text().splitlines():
            roots_path = Path(call.split()[1])
            self.assertFalse(roots_path.is_relative_to(self.state))
            self.assertFalse(roots_path.exists())
        self.assertEqual((self.root / 'paths').read_text().splitlines(),
                         [os.fspath(self.prefix / 'bin')] * 2)

    def test_bmake_must_match_selected_native_prefix(self):
        other = self.root / 'other-bmake'
        other.write_text('#!/bin/sh\nexit 1\n', encoding='utf-8')
        other.chmod(0o755)
        env = dict(self.env, BMAKE=os.fspath(other))
        output = StringIO()
        native = SelectedHostPrefix(self.bmake, self.shell)
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(package_selection, 'load_host_prefix',
                                  return_value=native), \
                redirect_stdout(output), redirect_stderr(output):
            status = package_selection.main([
                '--ports-root', os.fspath(self.ports), 'plan',
                '--state', os.fspath(self.state), '--host-prefix', os.fspath(self.prefix),
                '--package', 'cat/app'])
        self.assertEqual(status, 2)
        self.assertIn('BMAKE differs', output.getvalue())
        self.assertFalse(self.calls.exists())

    def test_state_must_be_absolute(self):
        status, output, selector = self.run_main(
            'plan', '--state', 'relative-state', '--host-prefix', os.fspath(self.prefix),
            '--package', 'cat/app')
        self.assertEqual(status, 2)
        self.assertIn('--state must be an absolute path', output)
        selector.assert_not_called()
        self.assertFalse(self.calls.exists())

    def test_interrupt_drains_controller_before_removing_roots_file(self):
        self.cross_bulk.write_text('''#!/bin/sh
mode=$1 roots=$2 state=$3
printf '%s\\n' "$mode $roots $state" >> "$CALL_LOG"
(
    trap 'printf signalled > "$WORKER_SIGNALLED"; exit 90' TERM
    sleep 1
    printf done > "$WORKER_FINISHED"
    if [ -f "$roots" ]; then printf present > "$ROOTS_PRESENT"; fi
) &
worker=$!
on_term() {
    printf stopped > "$CONTROLLER_STOPPED"
    wait "$worker"
    printf drained > "$CONTROLLER_DRAINED"
    exit 1
}
trap on_term TERM
printf ready > "$WORKER_STARTED"
wait "$worker"
''', encoding='utf-8')
        runner = self.root / 'runner.py'
        runner.write_text(
            "import sys\n"
            f"sys.path.insert(0, {os.fspath(SDK / 'src')!r})\n"
            "from emberbsd_sdk import package_selection\n"
            "class Prefix:\n"
            f"    tools = {{'make': {os.fspath(self.bmake)!r}, 'sh': {os.fspath(self.shell)!r}}}\n"
            "    def tool(self, role): return self.tools[role]\n"
            f"    path = {os.fspath(self.prefix / 'bin')!r}\n"
            "package_selection.load_host_prefix = lambda *a, **k: Prefix()\n"
            "raise SystemExit(package_selection.main(sys.argv[1:]))\n",
            encoding='utf-8')
        result = subprocess.Popen(
            [os.fspath(sys.executable), os.fspath(runner),
             '--ports-root', os.fspath(self.ports), 'plan',
             '--state', os.fspath(self.state), '--host-prefix', os.fspath(self.prefix),
             '--package', 'cat/app'],
            cwd=SDK, env=self.env, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True)
        try:
            deadline = time.monotonic() + 4
            while not Path(self.env['WORKER_STARTED']).exists():
                if result.poll() is not None:
                    stdout, stderr = result.communicate()
                    self.fail(f'controller exited before worker start: {stdout} {stderr}')
                if time.monotonic() >= deadline:
                    self.fail('controller worker did not start')
                time.sleep(0.01)
            roots_path = Path(self.calls.read_text().split()[1])
            self.assertTrue(roots_path.is_file())
            result.send_signal(signal.SIGTERM)
            stdout, stderr = result.communicate(timeout=5)
        finally:
            if result.poll() is None:
                result.send_signal(signal.SIGTERM)
                try:
                    result.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    result.kill()
                    result.communicate()
        self.assertEqual(result.returncode, 1, (stdout, stderr))
        for marker in ('CONTROLLER_STOPPED', 'WORKER_FINISHED',
                       'ROOTS_PRESENT', 'CONTROLLER_DRAINED'):
            self.assertTrue(Path(self.env[marker]).is_file(), marker)
        self.assertFalse(Path(self.env['WORKER_SIGNALLED']).exists())
        self.assertFalse(roots_path.exists())

    def test_unknown_set_and_unsafe_package_root_fail_before_controller(self):
        common = ('--state', os.fspath(self.state), '--host-prefix', os.fspath(self.prefix))
        for selection in (('--set', 'unknown'), ('--package', '../escape')):
            status, output, selector = self.run_main('plan', *common, *selection)
            self.assertEqual(status, 2)
            self.assertTrue(output)
            selector.assert_not_called()
        self.assertFalse(self.calls.exists())


if __name__ == '__main__':
    unittest.main()
