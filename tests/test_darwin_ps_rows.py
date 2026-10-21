# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Parse Darwin ps identities without weakening session ownership checks."""

import os
from unittest.mock import Mock, patch
from test_core import TemporaryCase
from emberbsd_sdk import process
from emberbsd_sdk.errors import SDKError


class DarwinRowsTests(TemporaryCase):
    def inspector(self):
        inspector = object.__new__(process.SessionInspector)
        inspector.argv = ['/bin/ps', '-e', '-o', 'pid=,ppid=,uid=,stat=']
        inspector.environment = {}
        inspector.uid = 501
        inspector.controller_sid = 7
        inspector.known = {}
        inspector.identity = Mock()
        inspector.identity.now.return_value = 200
        inspector.identity.birth.return_value = 100
        return inspector

    def output(self, row, inspector=None, platform='darwin'):
        inspector = self.inspector() if inspector is None else inspector
        probe = Mock(pid=987654, returncode=0)
        probe.communicate.return_value = (
            f'{os.getpid()} 1 501 S\n'.encode() + row + b'\n', b'')
        with patch.object(process.sys, 'platform', platform), patch.object(process.subprocess, 'Popen', return_value=probe):
            return inspector._rows()

    def test_signed_uid_is_normalized_without_changing_pid_or_parent(self):
        for text, expected in ((b'-2', 4294967294), (b'-1', 4294967295),
                               (b'-2147483648', 2147483648), (b'0', 0),
                               (b'501', 501), (b'2147483647', 2147483647),
                               (b'4294967295', 4294967295)):
            with self.subTest(uid=text):
                self.assertEqual(self.output(b'89487 1 ' + text + b' Ss')[89487], (1, expected, b'Ss'))

    def test_malformed_or_out_of_range_uid_fails_closed(self):
        for uid in (b'-2147483649', b'-4294967294', b'4294967296', b'-0',
                    b'+501', b'--2', b'-', b'5x', b'9' * 21):
            with self.subTest(uid=uid), self.assertRaises(SDKError):
                self.output(b'89487 1 ' + uid + b' Ss')

    def test_pid_parent_state_and_row_validation_stays_strict(self):
        for row in (b'-2 1 501 S', b'89487 -2 501 S', b'0 1 501 S',
                    b'2147483648 1 501 S', b'89487 2147483648 501 S',
                    b'89487 +1 501 S', b'89487 1 501 S extra', b'89487 1 501',
                    b'89487 1 501 S\x00', b'89487 1 501 \xff',
                    b'89487 1 501 S\n89487 1 501 S'):
            with self.subTest(row=row), self.assertRaises(SDKError):
                self.output(row)

    def test_signed_uid_is_not_a_generic_posix_extension(self):
        with self.assertRaises(SDKError):
            self.output(b'89487 1 -2 Ss', platform='linux')

    def test_normalized_foreign_uid_cannot_enter_owned_session(self):
        inspector = self.inspector()
        rows = self.output(b'89487 1 -2 Ss', inspector)
        with patch.object(inspector, '_rows', return_value={89487: rows[89487]}), patch.object(process.os, 'getsid', return_value=42):
            with self.assertRaisesRegex(SDKError, 'effective UID'):
                inspector._scan_darwin(42)

    def test_normalized_uid_matches_its_unsigned_owner(self):
        inspector = self.inspector()
        inspector.uid = 4294967294
        rows = self.output(b'89487 1 -2 Ss', inspector)
        with patch.object(inspector, '_rows', return_value={89487: rows[89487]}), patch.object(process.os, 'getsid', return_value=42):
            self.assertEqual(inspector._scan_darwin(42), ({89487}, False))
