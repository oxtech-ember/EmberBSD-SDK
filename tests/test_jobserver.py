# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""The FIFO jobserver of profile-build."""

import os
import signal
import subprocess
import sys
from pathlib import Path
import unittest

from test_core import TemporaryCase
from emberbsd_sdk import jobserver
from emberbsd_sdk.errors import SDKError


class JobserverTests(TemporaryCase):
    def test_a_job_takes_the_free_tokens_and_gives_them_back(self):
        path = self.root / 'jobserver'
        with jobserver.serve(path, 4) as makeflags:
            self.assertEqual(makeflags, f'-j4 --jobserver-auth=fifo:{path}')
            with jobserver.slots(makeflags, 8) as first:
                # One implicit slot and the three tokens.
                self.assertEqual(first, 4)
                with jobserver.slots(makeflags, 8) as second:
                    self.assertEqual(second, 1)
            with jobserver.slots(makeflags, 2) as third:
                self.assertEqual(third, 2)
            with jobserver.slots(makeflags, 8) as fourth:
                self.assertEqual(fourth, 4)
        self.assertFalse(path.exists())

    def test_unsupported_parent_pipe_and_explicit_serial_budget(self):
        for flags in ('-j --jobserver-auth=3,4', ' -j1', '--jobs=1', '-j1 --jobserver-auth=fifo:/unused'):
            with self.subTest(flags=flags), jobserver.budget(self.root / 'fifo', 8, flags) as child:
                with jobserver.slots(child, 8) as jobs:
                    self.assertEqual(jobs, 1)

    def test_smaller_local_budget_reserves_parent_slots_and_returns_their_bytes(self):
        parent = self.root / 'parent'
        with jobserver.serve(parent, 5) as outer:
            descriptor = os.open(parent, os.O_RDWR | os.O_NONBLOCK)
            try:
                self.assertEqual(os.read(descriptor, 4), b'++++')
                os.write(descriptor, b'ABCD')
                with jobserver.budget(self.root / 'child', 3, outer) as child:
                    self.assertNotEqual(jobserver.fifo(child), str(parent))
                    with jobserver.slots(child, 9) as jobs:
                        self.assertEqual(jobs, 3)
                    self.assertEqual(os.read(descriptor, 2), b'CD')
                self.assertEqual(os.read(descriptor, 2), b'AB')
            finally:
                os.close(descriptor)

    def test_a_workspace_with_spaces_gets_a_usable_fifo(self):
        with jobserver.budget(self.root / 'name with space', 3, '') as flags:
            with jobserver.slots(flags, 8) as jobs:
                self.assertEqual(jobs, 3)

    def test_without_a_jobserver_a_job_uses_its_budget(self):
        with jobserver.slots('', 6) as jobs:
            self.assertEqual(jobs, 6)
        with jobserver.slots('-k', 1) as jobs:
            self.assertEqual(jobs, 1)

    def test_a_named_jobserver_must_be_a_fifo(self):
        regular = self.write('regular', '')
        with self.assertRaisesRegex(SDKError, 'is not a FIFO'):
            with jobserver.slots(f'--jobserver-auth=fifo:{regular}', 2):
                pass
        with self.assertRaisesRegex(SDKError, 'positive'):
            with jobserver.serve(self.root / 'x', 0):
                pass

    def test_a_leftover_path_is_replaced_and_a_directory_is_refused(self):
        path = self.write('jobserver', 'old')
        with jobserver.serve(path, 2) as makeflags:
            with jobserver.slots(makeflags, 4) as jobs:
                self.assertEqual(jobs, 2)
        (self.root / 'directory').mkdir()
        with self.assertRaisesRegex(SDKError, 'is a directory'):
            with jobserver.serve(self.root / 'directory', 2):
                pass
        with self.assertRaisesRegex(SDKError, 'cannot open the jobserver'):
            with jobserver.slots(f'--jobserver-auth=fifo:{self.root / "missing"}', 2):
                pass

    def test_a_job_stopped_by_sigterm_gives_its_tokens_back(self):
        holder = '\n'.join([
            'import sys, time', f'sys.path.insert(0, {str(Path(jobserver.__file__).parents[1])!r})',
            'from emberbsd_sdk import jobserver',
            'with jobserver.slots(sys.argv[1], 8) as jobs:',
            '    print(jobs, flush=True)', '    time.sleep(60)', ''])
        with jobserver.serve(self.root / 'jobserver', 3) as makeflags:
            child = subprocess.Popen([sys.executable, '-c', holder, makeflags], stdout=subprocess.PIPE, text=True)
            self.assertEqual(child.stdout.readline().strip(), '3')
            with jobserver.slots(makeflags, 8) as jobs:
                self.assertEqual(jobs, 1)
            child.send_signal(signal.SIGTERM)
            child.communicate(timeout=30)
            with jobserver.slots(makeflags, 8) as jobs:
                self.assertEqual(jobs, 3)


if __name__ == '__main__':
    unittest.main()
