#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""SDK-owned source export and scoped bootstrap interfaces for current pkgsrc."""

import argparse
import os
import sys

sys.dont_write_bytecode = True
SDK_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(SDK_ROOT, 'src'))

from emberbsd_sdk.bootstrap_pkgsrc_export import (
    create_export_plan, describe_export_source, export_source_manifest,
    run_export, validate_export_acceptance, validate_export_transfer_acceptance,
)
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.inventory import sha256_file
from emberbsd_sdk.jsonio import json_bytes, read_json, write_json


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest='action', required=True)
    manifest = subparsers.add_parser('make-source-manifest')
    manifest.add_argument('--ports-source', required=True)
    manifest.add_argument('--git', required=True)
    manifest.add_argument('--output', required=True)
    source = subparsers.add_parser('describe-source')
    source.add_argument('--ports-source', required=True)
    source.add_argument('--git', required=True)
    source.add_argument('--manifest', required=True)
    source.add_argument('--manifest-sha256', required=True)
    source.add_argument('--output', required=True)
    plan = subparsers.add_parser('make-export-plan')
    plan.add_argument('--source-descriptor', required=True)
    plan.add_argument('--source-descriptor-sha256', required=True)
    plan.add_argument('--work-root', required=True)
    plan.add_argument('--lock-root', required=True)
    plan.add_argument('--python', required=True)
    plan.add_argument('--git', required=True)
    plan.add_argument('--patch', required=True)
    plan.add_argument('--runtime-seconds', type=int, default=900)
    plan.add_argument('--output', required=True)
    run = subparsers.add_parser('run-export')
    for name in ('source-descriptor', 'source-descriptor-sha256', 'plan', 'plan-sha256'):
        run.add_argument('--' + name, required=True)
    acceptance = subparsers.add_parser('check-export-acceptance')
    for name in ('receipt', 'receipt-sha256', 'result', 'result-sha256',
                 'plan', 'plan-sha256', 'source-descriptor', 'source-descriptor-sha256'):
        acceptance.add_argument('--' + name, required=True)
    transfer = subparsers.add_parser('check-transfer-acceptance')
    for name in ('receipt', 'receipt-sha256', 'export-acceptance', 'export-acceptance-sha256'):
        transfer.add_argument('--' + name, required=True)
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        if args.action == 'make-source-manifest':
            result = export_source_manifest(args.ports_source, args.git, args.output)
        elif args.action == 'describe-source':
            result = describe_export_source(args.ports_source,
                dict(path=args.manifest, sha256=args.manifest_sha256), git_path=args.git)
            write_json(args.output, result, exclusive=True)
            result = dict(path=args.output, sha256=sha256_file(args.output))
        elif args.action == 'make-export-plan':
            source_ref = dict(path=args.source_descriptor,
                              sha256=args.source_descriptor_sha256)
            tools = {name: dict(path=os.path.realpath(getattr(args, name)),
                                sha256=sha256_file(getattr(args, name)))
                     for name in ('git', 'patch')}
            python = dict(path=os.path.realpath(args.python), sha256=sha256_file(args.python))
            plan = create_export_plan(source_ref, args.work_root, args.lock_root,
                                      python, tools, plan_path=os.path.abspath(args.output),
                                      runtime_seconds=args.runtime_seconds)
            write_json(args.output, plan, exclusive=True)
            result = dict(path=args.output, sha256=sha256_file(args.output))
        elif args.action == 'run-export':
            result = run_export(
                dict(path=args.source_descriptor, sha256=args.source_descriptor_sha256),
                dict(path=args.plan, sha256=args.plan_sha256))
        elif args.action == 'check-export-acceptance':
            receipt_ref, receipt = validate_export_acceptance(
                args.receipt, args.receipt_sha256,
                result_ref=dict(path=args.result, sha256=args.result_sha256),
                plan_ref=dict(path=args.plan, sha256=args.plan_sha256),
                source_ref=dict(path=args.source_descriptor, sha256=args.source_descriptor_sha256))
            result = dict(receipt=receipt_ref, scope=receipt['scope'], status=receipt['status'])
        else:
            receipt_ref, receipt = validate_export_transfer_acceptance(
                args.receipt, args.receipt_sha256,
                export_acceptance_ref=dict(path=args.export_acceptance,
                                           sha256=args.export_acceptance_sha256))
            result = dict(receipt=receipt_ref, scope=receipt['scope'], status=receipt['status'],
                          output_root=receipt['output_root'], output_inventory=receipt['output_inventory'])
        sys.stdout.buffer.write(json_bytes(result))
        return 0
    except (SDKError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f'bootstrap-pkgsrc: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
