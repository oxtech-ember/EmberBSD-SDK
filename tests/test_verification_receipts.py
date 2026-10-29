# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
import os
import sys
from unittest.mock import patch

from test_core import TemporaryCase
from emberbsd_sdk import verification_receipts
from emberbsd_sdk.llvm_sysroot import LLVMCoordinator
from emberbsd_sdk.sysroot_builder import SysrootBuilder
from emberbsd_sdk.verification_receipts import RunReceipts, fingerprint

OWNER = r'''
import hashlib, json, os, sys
with open(os.environ['CALLS'], 'a') as log:
    log.write('verify\n')
source = sys.argv[sys.argv.index('--source') + 1]
digest = hashlib.sha256()
for name in sorted(os.listdir(source)):
    with open(os.path.join(source, name), 'rb') as handle:
        digest.update(name.encode() + b'\0' + handle.read())
print(json.dumps({'format': 'emberbsd-prepared-source', 'version': 1,
                  'package': 'devel/byacc', 'tree_sha256': digest.hexdigest()}))
'''


class VerificationReceiptTests(TemporaryCase):
    def builder(self, receipts=None):
        builder = SysrootBuilder({}, receipts=receipts)
        builder.paths = {'python': sys.executable, 'ports': str(self.root / 'ports'),
                         'sdk-source': str(self.root / 'sdk'), 'byacc-source': str(self.source)}
        builder.git_command = '/host/git'
        builder.environment = {'CALLS': str(self.calls), 'PATH': '/usr/bin:/bin'}
        return builder

    def setUp(self):
        super().setUp()
        self.write('ports/scripts/source-inputs/byacc/source.py', OWNER)
        self.source = self.root / 'prepared/byacc'
        self.write('prepared/byacc/main.c', 'int main(void) { return 0; }\n')
        self.write('prepared/byacc/defs.h', '#define YES 1\n')
        self.calls = self.root / 'calls.log'

    def content_checks(self):
        return len(self.calls.read_text().splitlines()) if self.calls.exists() else 0

    def test_unchanged_inputs_are_verified_by_content_once_per_run(self):
        builder = self.builder()
        # Preflight, before the build and after the build of one phase ...
        records = builder.verify_common_sources()
        for _ in range(2):
            self.assertEqual(builder.verify_common_sources(), records)
        # ... and every later phase builder of the same run.
        later = self.builder(receipts=builder.verification)
        self.assertEqual(later.verify_common_sources(), records)
        self.assertEqual(self.content_checks(), 1)

    def test_changed_input_after_plan_is_verified_again_and_fails_the_run(self):
        builder = self.builder()
        planned = builder.verify_common_sources()
        self.assertEqual(builder.verify_common_sources(), planned)
        # Same size and restored times: only the change time shows the write.
        path = self.source / 'defs.h'
        before = path.stat()
        path.write_text('#define YES 0\n')
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(path.stat().st_size, before.st_size)
        # The builder compares this with its plan and stops the run.
        self.assertNotEqual(builder.verify_common_sources(), planned)
        self.assertEqual(self.content_checks(), 2)
        # A metadata-only change is verified by content and still accepted.
        changed = builder.verify_common_sources()
        os.utime(path)
        self.assertEqual(builder.verify_common_sources(), changed)
        self.assertEqual(self.content_checks(), 3)

    def test_added_or_removed_input_file_is_verified_again(self):
        builder = self.builder()
        builder.verify_common_sources()
        self.write('prepared/byacc/extra.c', '')
        builder.verify_common_sources()
        (self.source / 'extra.c').unlink()
        builder.verify_common_sources()
        self.assertEqual(self.content_checks(), 3)

    def test_new_run_ignores_receipts_of_an_earlier_run(self):
        listing = sorted(os.listdir(self.root))
        self.builder().verify_common_sources()
        self.builder().verify_common_sources()
        self.assertEqual(self.content_checks(), 2)
        # Receipts stay in memory; nothing is written next to the inputs.
        self.assertEqual(sorted(os.listdir(self.root)), sorted(listing + ['calls.log']))
        self.assertEqual(sorted(os.listdir(self.source)), ['defs.h', 'main.c'])
        # Each coordinated run starts with its own receipts.
        options = {'sdk': str(self.root / 'sdk'), 'runtime-work': str(self.root / 'work'), 'target': 'aarch64'}
        self.assertIsNot(LLVMCoordinator(options, SysrootBuilder).receipts,
                         LLVMCoordinator(options, SysrootBuilder).receipts)

    def test_failed_verification_leaves_no_receipt(self):
        receipts = RunReceipts()
        def fail():
            raise RuntimeError('content differs')
        with self.assertRaises(RuntimeError):
            receipts.verify('key', [str(self.source)], fail)
        self.assertEqual(receipts.verify('key', [str(self.source)], lambda: 'ok'), 'ok')
        self.assertEqual(receipts.content_checks['key'], 2)

    def test_unsafe_inputs_are_always_verified_by_content(self):
        outside = self.write('outside.h', '')
        os.symlink(outside, self.source / 'link.h')
        self.assertIsNone(fingerprint([str(self.source)]))
        self.assertIsNone(fingerprint([str(self.root / 'missing')]))
        self.assertIsNone(fingerprint(['relative/path']))
        (self.source / 'link.h').unlink()
        os.symlink('main.c', self.source / 'link.h')
        self.assertIsNotNone(fingerprint([str(self.source)]))
        receipts = RunReceipts()
        for _ in range(2):
            receipts.verify('key', [str(self.root / 'missing')], lambda: 'ok')
        self.assertEqual(receipts.content_checks['key'], 2)

    def test_receipts_are_never_persisted(self):
        with patch('builtins.open', side_effect=AssertionError('receipt written')):
            receipts = RunReceipts()
            receipts.verify('key', [str(self.source)], lambda: {'record': 1})
            self.assertEqual(receipts.verify('key', [str(self.source)], lambda: {'record': 2}), {'record': 1})
        self.assertFalse(hasattr(verification_receipts, 'load'))
