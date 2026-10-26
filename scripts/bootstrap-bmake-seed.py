#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Build and independently audit the first common-Ports bmake bootstrap seed."""

import argparse
from pathlib import Path
import signal
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from emberbsd_sdk.bootstrap_bmake_seed import (create_plan, describe_bmake_source,
    audit_compiler_seed_candidate, describe_compiler_seed, run_producer,
    select_host_utilities)
from emberbsd_sdk.bootstrap_bmake_audit import create_audit_plan, run_acceptance
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.jsonio import json_bytes, write_json


def _reference(path, sha256):
    return dict(path=path, sha256=sha256)


def _print_and_save(path, value):
    write_json(path, value)
    sys.stdout.write(json_bytes(value).decode('utf-8'))


def main(argv=None):
    parser = argparse.ArgumentParser(description='Retired historical bootstrap interface. Normal builds use --host-prefix; see docs/native-toolset.md. ' + __doc__)
    commands = parser.add_subparsers(dest='command', required=True)

    compiler = commands.add_parser('describe-compiler', help='bind an independently accepted GCC16 compiler seed')
    compiler.add_argument('--accepted-record', required=True)
    compiler.add_argument('--prefix-manifest', required=True)
    compiler.add_argument('--prefix', required=True)
    compiler.add_argument('--acceptance-receipt', required=True)
    compiler.add_argument('--acceptance-receipt-sha256', required=True)
    compiler.add_argument('--output', required=True)

    compiler_audit = commands.add_parser(
        'audit-compiler-candidate',
        help='write scoped GCC16 prefix and runtime evidence for separate review')
    compiler_audit.add_argument('--accepted-record', required=True)
    compiler_audit.add_argument('--accepted-record-sha256', required=True)
    compiler_audit.add_argument('--prefix-manifest', required=True)
    compiler_audit.add_argument('--prefix-manifest-sha256', required=True)
    compiler_audit.add_argument('--prefix', required=True)
    compiler_audit.add_argument('--inventory-output', required=True)
    compiler_audit.add_argument('--report-output', required=True)

    source = commands.add_parser('describe-source', help='bind the current common Ports bmake source')
    source.add_argument('--ports-asserted-revision', required=True)
    source.add_argument('--ports-profile', required=True)
    source.add_argument('--ports-sources', required=True)
    source.add_argument('--ports-patch', required=True)
    source.add_argument('--archive', required=True)
    source.add_argument('--source-root', required=True)
    source.add_argument('--output', required=True)

    plan = commands.add_parser('make-plan', help='create the fixed first-bmake producer plan')
    for name in ('seed-descriptor', 'source-descriptor'):
        plan.add_argument('--' + name, required=True)
        plan.add_argument('--' + name + '-sha256', required=True)
    plan.add_argument('--work-root', required=True)
    plan.add_argument('--lock-root', required=True)
    plan.add_argument('--output', required=True)

    producer = commands.add_parser('run-producer', help='run configure and make-bootstrap.sh once')
    for name in ('seed-descriptor', 'source-descriptor', 'bootstrap-plan'):
        producer.add_argument('--' + name, required=True)
        producer.add_argument('--' + name + '-sha256', required=True)

    audit_plan = commands.add_parser('make-audit-plan', help='create the separate bmake-only acceptance plan')
    for name in ('producer-result', 'seed-descriptor', 'source-descriptor'):
        audit_plan.add_argument('--' + name, required=True)
        audit_plan.add_argument('--' + name + '-sha256', required=True)
    audit_plan.add_argument('--accepted-producer-receipt', required=True)
    audit_plan.add_argument('--accepted-producer-receipt-sha256', required=True)
    audit_plan.add_argument('--work-root', required=True)
    audit_plan.add_argument('--lock-root', required=True)
    audit_plan.add_argument('--output', required=True)

    audit = commands.add_parser('run-audit', help='independently test bmake version and behavior')
    for name in ('producer-result', 'seed-descriptor', 'source-descriptor', 'audit-plan'):
        audit.add_argument('--' + name, required=True)
        audit.add_argument('--' + name + '-sha256', required=True)
    audit.add_argument('--accepted-producer-receipt', required=True)
    audit.add_argument('--accepted-producer-receipt-sha256', required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == 'describe-compiler':
            value = describe_compiler_seed(args.accepted_record, args.prefix_manifest, args.prefix,
                args.acceptance_receipt, args.acceptance_receipt_sha256)
            _print_and_save(args.output, value)
            return 0
        if args.command == 'audit-compiler-candidate':
            value = audit_compiler_seed_candidate(
                args.accepted_record, args.accepted_record_sha256,
                args.prefix_manifest, args.prefix_manifest_sha256, args.prefix,
                args.inventory_output, args.report_output)
            sys.stdout.write(json_bytes(value).decode('utf-8'))
            return 0
        if args.command == 'describe-source':
            value = describe_bmake_source(args.ports_asserted_revision, args.ports_profile,
                args.ports_sources, args.ports_patch, args.archive, args.source_root)
            _print_and_save(args.output, value)
            return 0
        if args.command == 'make-plan':
            seed_ref = _reference(args.seed_descriptor, args.seed_descriptor_sha256)
            source_ref = _reference(args.source_descriptor, args.source_descriptor_sha256)
            value = create_plan(seed_ref, source_ref, args.work_root, args.lock_root,
                                select_host_utilities())
            _print_and_save(args.output, value)
            return 0
        if args.command == 'run-producer':
            options = [args.seed_descriptor, args.source_descriptor, args.bootstrap_plan]
            hashes = [args.seed_descriptor_sha256, args.source_descriptor_sha256,
                      args.bootstrap_plan_sha256]
            previous = {number: signal.signal(number, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
                        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
            try:
                result = run_producer(*[_reference(path, digest) for path, digest in zip(options, hashes)])
            finally:
                for number, handler in previous.items():
                    signal.signal(number, handler)
            sys.stdout.write(json_bytes(result).decode('utf-8'))
            return 0 if result['status'] == 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW' else 1
        if args.command == 'make-audit-plan':
            result_ref = _reference(args.producer_result, args.producer_result_sha256)
            seed_ref = _reference(args.seed_descriptor, args.seed_descriptor_sha256)
            source_ref = _reference(args.source_descriptor, args.source_descriptor_sha256)
            producer_acceptance_ref = _reference(args.accepted_producer_receipt,
                                                 args.accepted_producer_receipt_sha256)
            value = create_audit_plan(result_ref, seed_ref, source_ref, producer_acceptance_ref,
                args.work_root, args.lock_root, select_host_utilities())
            _print_and_save(args.output, value)
            return 0
        if args.command == 'run-audit':
            options = [args.producer_result, args.seed_descriptor,
                       args.source_descriptor, args.accepted_producer_receipt,
                       args.audit_plan]
            hashes = [args.producer_result_sha256, args.seed_descriptor_sha256,
                      args.source_descriptor_sha256, args.accepted_producer_receipt_sha256,
                      args.audit_plan_sha256]
            previous = {number: signal.signal(number, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
                        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
            try:
                result = run_acceptance(*[_reference(path, digest) for path, digest in zip(options, hashes)])
            finally:
                for number, handler in previous.items():
                    signal.signal(number, handler)
            sys.stdout.write(json_bytes(result).decode('utf-8'))
            return 0 if result['status'] == 'PASS' else 1
    except KeyboardInterrupt as exc:
        print(f'bootstrap-bmake-seed interrupted: {exc}', file=sys.stderr)
        return 130
    except (SDKError, OSError, ValueError, TypeError, KeyError) as exc:
        print(f'bootstrap-bmake-seed: {exc}', file=sys.stderr)
        return 1
    return 2


if __name__ == '__main__':
    sys.exit(main())
