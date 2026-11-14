# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Opt-in cross-root checks. Not an install, reuse, or publication gate."""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import signal
import threading

from .errors import SDKError

MAX_METADATA = 8 * 1024 * 1024


def qualify(root, forbidden_prefixes, metadata=(), probe=(), *, timeout=60):
    """Inspect selected consumer metadata and optionally run one explicit probe.

    Callers own package-specific selection and probe assertions. No files are
    rewritten, sources hidden, signatures checked, or target binaries executed
    implicitly. A successful host probe is not target/hardware qualification.
    """
    root = Path(root).resolve(strict=True)
    if not root.is_dir():
        raise SDKError('qualification root must be a directory')
    prefixes = []
    for value in forbidden_prefixes:
        path = Path(value)
        if not path.is_absolute() or str(path) == '/' or '..' in path.parts:
            raise SDKError('forbidden prefix must be a non-root absolute path')
        if path == root or path in root.parents or root in path.parents:
            raise SDKError('qualification and forbidden roots must not overlap')
        prefixes.append(str(path))
    if not prefixes:
        raise SDKError('at least one forbidden producer/stage prefix is required')
    checked, leaks = [], []
    for name in metadata:
        relative = Path(name)
        path = root / relative
        if relative.is_absolute() or '..' in relative.parts or path.is_symlink():
            raise SDKError('metadata must be a contained regular file: ' + str(name))
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise SDKError('metadata must be a contained regular file: ' + str(name))
        if resolved.stat().st_size > MAX_METADATA:
            raise SDKError('metadata exceeds size limit: ' + str(name))
        raw = resolved.read_bytes()
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError as error:
            raise SDKError('metadata must be UTF-8 text: ' + str(name)) from error
        if '\x00' in text:
            raise SDKError('metadata must be text, not a binary: ' + str(name))
        checked.append(dict(path=relative.as_posix(), sha256=hashlib.sha256(raw).hexdigest()))
        for prefix in sorted(set(prefixes)):
            # Deliberately conservative: this is a diagnostic, not an automatic
            # rewriting decision. Callers select files interpreted by consumers.
            if prefix in text:
                leaks.append(dict(path=relative.as_posix(), forbidden_prefix=prefix))
    report = dict(schema='sdk-path-qualification-v1', root=str(root),
                  forbidden_prefixes=sorted(set(prefixes)), metadata=checked,
                  leaks=leaks, static_check='failed' if leaks else
                  ('passed' if checked else 'not-assessed'),
                  host_probe='not-assessed', target_execution='not-assessed')
    if probe:
        # Do not hide or rename anything owned by the caller. The test fixture
        # or producer must arrange A -> B and make A unavailable beforehand.
        if any(os.path.lexists(prefix) for prefix in prefixes):
            raise SDKError('old producer/stage roots must be absent before a host probe')
        if leaks:
            return report
        if not math.isfinite(timeout) or timeout <= 0:
            raise SDKError('probe timeout must be positive')
        try:
            returncode = _run_probe(probe, root, timeout)
            report.update(host_probe='passed' if returncode == 0 else 'failed',
                          probe_argv=list(probe), probe_exit_code=returncode)
        except subprocess.TimeoutExpired:
            report.update(host_probe='failed', probe_argv=list(probe), probe_timeout=True)
    return report


def _run_probe(probe, root, timeout):
    # Signal handling is scoped to this synchronous optional host probe.
    if threading.current_thread() is not threading.main_thread():
        raise SDKError('host probes must run in the main thread for signal cleanup')
    watched = (signal.SIGTERM, signal.SIGHUP)
    previous = {number: signal.getsignal(number) for number in watched}
    process = None
    pending = None

    def interrupted(number, frame):
        nonlocal pending
        # Do not interrupt Popen before it returns the child handle.
        pending = number
        if process is not None:
            raise SystemExit(128 + number)

    try:
        for number in watched:
            signal.signal(number, interrupted)
        process = subprocess.Popen(list(probe), cwd=root, start_new_session=True)
        if pending is not None:
            raise SystemExit(128 + pending)
        return process.wait(timeout=timeout)
    finally:
        # A second termination request must not interrupt descendant cleanup.
        try:
            for number in watched:
                signal.signal(number, signal.SIG_IGN)
            if process is not None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    parser.add_argument('--forbid-prefix', action='append', required=True)
    parser.add_argument('--metadata', action='append', default=[])
    parser.add_argument('--timeout', type=float, default=60)
    parser.add_argument('probe', nargs=argparse.REMAINDER,
                        help='optional explicit host probe following --; stdout/stderr are inherited')
    args = parser.parse_args(argv)
    probe = args.probe[1:] if args.probe[:1] == ['--'] else args.probe
    try:
        report = qualify(args.root, args.forbid_prefix, args.metadata, probe, timeout=args.timeout)
    except (SDKError, OSError) as error:
        parser.exit(2, str(error) + '\n')
    print(json.dumps(report, sort_keys=True))
    return 1 if report['leaks'] or report['host_probe'] == 'failed' else 0


if __name__ == '__main__':
    raise SystemExit(main())
