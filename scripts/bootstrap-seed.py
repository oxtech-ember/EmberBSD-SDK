#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Check seed inputs or run one independently accepted finite probe."""

import argparse
from pathlib import Path
import signal
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from emberbsd_sdk.bootstrap_seed import load_bootstrap_seed
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.jsonio import json_bytes


def main(argv=None):
    parser = argparse.ArgumentParser(description='Retired historical bootstrap interface. Normal builds use --host-prefix; see docs/native-toolset.md. ' + __doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    check = commands.add_parser('check', help='check current inputs; do not execute the plan')
    pilot = commands.add_parser('run-pilot', help='run the finite accepted C/C++ pilot once')
    make_probe = commands.add_parser('run-make-probe', help='run the finite accepted make/shell probe once')
    producer = commands.add_parser('run-producer', help='run one recognized accepted bootstrap producer stage')
    bootstrap = commands.add_parser('run-bootstrap', help='run the complete accepted fixed bound-make recipe plan')
    for command in (check, pilot, make_probe, producer, bootstrap):
        for name in ('descriptor', 'accepted-seed-receipt', 'bootstrap-plan'):
            command.add_argument('--' + name, required=True)
            command.add_argument('--' + name + '-sha256', required=True)
        command.add_argument('--required-role', action='append', required=True)
    nawk = commands.add_parser('plan-nawk', help='prepare the fixed initial NetBSD nawk plan; no acceptance or execution')
    for name in ('descriptor', 'program-source', 'pkgsrc-tools-acceptance', 'shell-runtime'):
        nawk.add_argument('--' + name, required=True)
        nawk.add_argument('--' + name + '-sha256', required=True)
    for name in ('work-root', 'lock-root', 'heavy-link-context', 'output'):
        nawk.add_argument('--' + name, required=True)
    nawk.add_argument('--runtime-seconds', type=int, default=120)
    args = parser.parse_args(argv)
    try:
        if args.command == 'plan-nawk':
            from emberbsd_sdk.bootstrap_nawk import make_plan
            from emberbsd_sdk.jsonio import write_json
            from emberbsd_sdk.inventory import sha256_file
            result = make_plan(dict(path=args.descriptor, sha256=args.descriptor_sha256),
                dict(path=args.program_source, sha256=args.program_source_sha256),
                dict(path=args.pkgsrc_tools_acceptance, sha256=args.pkgsrc_tools_acceptance_sha256),
                dict(path=args.shell_runtime, sha256=args.shell_runtime_sha256),
                work_root=args.work_root, resources=dict(cpu_jobs=1, link_jobs=1,
                    runtime_seconds=args.runtime_seconds, lock_root=args.lock_root,
                    heavy_link_context=args.heavy_link_context))
            write_json(args.output, result)
            sys.stdout.write(json_bytes(dict(path=str(Path(args.output).resolve()),
                                           sha256=sha256_file(args.output))).decode('utf-8'))
            return 0
        options = dict(descriptor_sha256=args.descriptor_sha256,
            accepted_seed_receipt=dict(path=args.accepted_seed_receipt,
                                       sha256=args.accepted_seed_receipt_sha256),
            bootstrap_plan=dict(path=args.bootstrap_plan, sha256=args.bootstrap_plan_sha256),
            required_roles=args.required_role)
        if args.command in ('run-pilot', 'run-make-probe', 'run-producer', 'run-bootstrap'):
            from emberbsd_sdk.bootstrap_pilot import run_pilot, run_make_probe, run_producer, run_bootstrap
            runner = {'run-pilot': run_pilot, 'run-make-probe': run_make_probe,
                      'run-producer': run_producer, 'run-bootstrap': run_bootstrap}[args.command]
            def interrupt(number, frame):
                raise KeyboardInterrupt(signal.Signals(number).name)
            previous = {number: signal.signal(number, interrupt)
                        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
            try:
                result = runner(args.descriptor, **options)
            finally:
                for number, handler in previous.items():
                    signal.signal(number, handler)
            sys.stdout.write(json_bytes(result).decode('utf-8'))
            return 0 if result['status'] == 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW' else 1
        selected = load_bootstrap_seed(args.descriptor, **options)
        sys.stdout.write(json_bytes(selected.as_dict()).decode('utf-8'))
        return 0
    except KeyboardInterrupt as exc:
        print(f'bootstrap-seed interrupted: {exc}', file=sys.stderr)
        return 130
    except (SDKError, OSError, ValueError, TypeError, KeyError) as exc:
        print(f'bootstrap-seed: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
