# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Select explicit versionless Ports roots and delegate to cross-bulk."""

import argparse
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time

from .errors import SDKError
from .host_prefix import load_host_prefix


_SET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")
_PKGPATH = re.compile(r"[A-Za-z0-9_+.-]+(?:/[A-Za-z0-9_+.-]+)+\Z")


def _package_sets(ports_root):
    directory = ports_root / "profiles" / "package-sets"
    if not directory.is_dir():
        raise ValueError(f"package-set catalog is missing: {directory}")
    return {path.stem: path for path in sorted(directory.glob("*.txt")) if path.is_file()}


def _parse_root(value, source):
    native = value.endswith("@native")
    pkgpath = value[:-7] if native else value
    if (not _PKGPATH.fullmatch(pkgpath) or
            any(part in (".", "..") for part in pkgpath.split("/"))):
        raise ValueError(f"{source}: unsafe versionless PKGPATH {value!r}")
    return pkgpath + ("@native" if native else "")


def _read_roots(path):
    roots = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        value = raw.split("#", 1)[0].strip()
        if value:
            roots.append(_parse_root(value, f"{path}:{number}"))
    return list(dict.fromkeys(roots))


def _selected_roots(args, catalog):
    roots = []
    for name in args.sets:
        if not _SET_NAME.fullmatch(name) or name not in catalog:
            raise ValueError(f"unknown package set: {name}")
        roots.extend(_read_roots(catalog[name]))
    roots.extend(_parse_root(value, "--package") for value in args.packages)
    return list(dict.fromkeys(roots))


def _parser():
    parser = argparse.ArgumentParser(
        description="Select explicit EmberBSD Ports package roots")
    parser.add_argument("--ports-root", required=True, type=Path,
                        help="Ports source checkout with package sets and cross-bulk")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="list available versionless package sets")
    show = commands.add_parser("show", help="show roots in a package set")
    show.add_argument("--set", required=True, dest="set_name")
    for name in ("plan", "build"):
        command = commands.add_parser(name,
                                      help=f"{name} explicitly selected package roots")
        command.add_argument("--state", required=True,
                             help="absolute shared cross-bulk state directory")
        command.add_argument("--host-prefix",
                             help="selected native Ports prefix containing bmake and shell")
        command.add_argument("--set", action="append", default=[], dest="sets")
        command.add_argument("--package", action="append", default=[], dest="packages")
    return parser


def _require_environment(args):
    state = Path(args.state)
    if not state.is_absolute():
        raise ValueError("--state must be an absolute path")
    if state.exists() and not state.is_dir():
        raise ValueError("--state exists and is not a directory")
    values = {}
    for name in ("BMAKE", "MAKECONF", "PKGSRCDIR"):
        value = os.environ.get(name, "")
        if not value or not Path(value).is_absolute():
            raise ValueError(f"Set {name} to an absolute selected build path")
        values[name] = Path(value)
    if not values["BMAKE"].is_file() or not os.access(values["BMAKE"], os.X_OK):
        raise ValueError("BMAKE must be the selected executable bmake")
    if not values["MAKECONF"].is_file():
        raise ValueError("MAKECONF must name the selected build configuration")
    if not (values["PKGSRCDIR"] / "pkgtools" / "pkg_install").is_dir():
        raise ValueError("PKGSRCDIR must be the prepared pkgsrc tree")
    if not args.host_prefix:
        raise ValueError("--host-prefix is required for a nonempty package selection")
    try:
        native = load_host_prefix(args.host_prefix, required_roles={"make", "sh"})
    except SDKError as error:
        raise ValueError(f"cannot select native bmake and shell: {error}") from error
    selected_bmake = Path(native.tool("make"))
    shell = Path(native.tool("sh"))
    try:
        matches = os.path.samefile(values["BMAKE"], selected_bmake)
    except OSError:
        matches = False
    if not matches:
        raise ValueError("BMAKE differs from the bmake selected in --host-prefix")
    return values, native, shell, selected_bmake, state


def _close_inherited_fds():
    """Close inherited descriptors before exec. A leaked death pipe would hide the client."""
    for folder in ('/proc/self/fd', '/dev/fd'):
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            if not name.isdigit():
                continue
            number = int(name)
            if number > 2:
                try:
                    os.close(number)
                except OSError:
                    pass
        return
    for number in range(3, 256):
        try:
            os.close(number)
        except OSError:
            pass


def _wait_status(status):
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return -os.WTERMSIG(status)
    return 1


class _RecordedChild:
    def __init__(self, pid):
        self.pid = pid

    def send_signal(self, number):
        os.kill(self.pid, number)

    def wait(self):
        """Wait for the recorded child.

        The lock owner installs a pulse so this wait can notice the death pipe
        without a second Python thread. ``os.fork`` then stays on that
        single-threaded owner. Callers outside the owner block as before.
        """
        pulse = None
        try:
            from .source_wave import current_controller_pulse
        except ImportError:
            current_controller_pulse = None
        if current_controller_pulse is not None:
            pulse = current_controller_pulse()
        if pulse is None:
            while True:
                try:
                    _pid, status = os.waitpid(self.pid, 0)
                except InterruptedError:
                    continue
                return _wait_status(status)
        while True:
            pulse()
            try:
                pid, status = os.waitpid(self.pid, os.WNOHANG)
            except InterruptedError:
                continue
            except ChildProcessError:
                return 0
            if pid:
                return _wait_status(status)
            time.sleep(0.02)


def launch_recorded(argv, env, record, stdout=None):
    """Fork ``argv`` and call ``record(pid)`` before that child may exec.

    The child exits without running the command when the parent dies or
    ``record`` fails. ``record`` therefore publishes the identity first.
    """
    if not argv or any(not isinstance(part, str) or '\0' in part for part in argv):
        raise ValueError('invalid controller command')
    stdout_fd = None if stdout is None else stdout.fileno()
    gate_read, gate_write = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(gate_write)
            try:
                allowed = os.read(gate_read, 1)
            except OSError:
                os._exit(0)
            os.close(gate_read)
            if allowed != b'G':
                os._exit(0)
            if stdout_fd not in (None, 1):
                os.dup2(stdout_fd, 1)
            _close_inherited_fds()
            os.setsid()
            os.execvpe(argv[0], argv, env)
        except OSError:
            os._exit(127)
        os._exit(127)
    os.close(gate_read)
    try:
        record(pid)
    except BaseException:
        os.close(gate_write)
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        raise
    os.write(gate_write, b'G')
    os.close(gate_write)
    return _RecordedChild(pid)


def _run_controller(ports_root, state, native, roots, command, build_environment, *,
                    stdout=None, controller_ready=None):
    tmp_root = Path(os.environ.get("TMPDIR") or state.parent)
    if not tmp_root.is_absolute():
        raise ValueError("TMPDIR must be absolute when set")
    state = state.resolve()
    tmp_root = tmp_root.resolve()
    if tmp_root == state or tmp_root.is_relative_to(state):
        raise ValueError("TMPDIR must be outside --state")
    tmp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix="emberbsd-package-roots-",
            dir=tmp_root, delete=False) as stream:
        roots_path = Path(stream.name)
        stream.write("\n".join(roots) + "\n")
    try:
        if roots_path.is_relative_to(state):
            raise ValueError("temporary selected roots file must be outside --state")
        shell = native.tool("sh")
        environment = dict(build_environment)
        environment["CROSS_BULK_SHELL"] = shell
        forwarded = []
        previous = {}
        child = None

        def forward(signum, _frame):
            if not forwarded:
                forwarded.append(signum)
                if child is not None:
                    child.send_signal(signum)

        try:
            for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                previous[number] = signal.signal(number, forward)
            command_argv = [os.fspath(shell), os.fspath(ports_root / "scripts" / "cross-bulk.sh"),
                            command, os.fspath(roots_path), os.fspath(state)]
            if controller_ready is None:
                child = subprocess.Popen(command_argv, env=environment, stdout=stdout, start_new_session=True)
            else:
                child = launch_recorded(command_argv, environment, controller_ready, stdout=stdout)
            if forwarded:
                child.send_signal(forwarded[0])
            while True:
                try:
                    status = child.wait()
                    break
                except InterruptedError:
                    continue
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)
        return status
    finally:
        roots_path.unlink(missing_ok=True)


def main(argv=None, *, controller_stdout=None, controller_mode=None, controller_ready=None):
    args = _parser().parse_args(argv)
    try:
        ports_root = args.ports_root.resolve(strict=True)
        catalog = _package_sets(ports_root)
        if args.command == "list":
            print("\n".join(catalog))
            return 0
        if args.command == "show":
            if not _SET_NAME.fullmatch(args.set_name) or args.set_name not in catalog:
                raise ValueError(f"unknown package set: {args.set_name}")
            for root in _read_roots(catalog[args.set_name]):
                role = "native" if root.endswith("@native") else "target"
                print(f"{role} {root.removesuffix('@native')}")
            return 0

        roots = _selected_roots(args, catalog)
        if not roots:
            print("Nothing selected; no package build was requested.")
            return 0
        environment, native, shell, selected_bmake, state = _require_environment(args)
        build_environment = os.environ.copy()
        build_environment.update(
            BMAKE=os.fspath(selected_bmake),
            MAKECONF=os.fspath(environment["MAKECONF"]),
            PKGSRCDIR=os.fspath(environment["PKGSRCDIR"]),
            PATH=native.path,
            SHELL=os.fspath(shell),
        )
        print("Sets: " + (", ".join(args.sets) if args.sets else "none"))
        print("Roots: " + (", ".join(roots) if roots else "none"), flush=True)
        # `build` reads the saved order and edges. `selected-build` orders the
        # roots it is given, which drops target-fed settings when those roots
        # are only the ready native wave.
        if controller_mode is None:
            mode = "selected-order" if args.command == "plan" else "selected-build"
        elif controller_mode == "build" and args.command == "build":
            mode = "build"
        else:
            raise ValueError("unsupported controller mode")
        status = _run_controller(ports_root, state, native, roots, mode, build_environment,
                                 stdout=controller_stdout, controller_ready=controller_ready)
        if status:
            return status
        if args.command == "build":
            print("Selected packages were installed into the configured build prefixes/sysroot.")
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(f"packages.py: {error}", file=sys.stderr)
        return 2
