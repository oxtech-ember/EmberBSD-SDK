# SPDX-License-Identifier: BSD-2-Clause
"""Consumer regression for the published Ports guest-marker protocol."""

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.target_acceptance import GUEST_CHECKS, parse_guest_log


def _write(path, text, executable=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if executable:
        path.chmod(0o755)


class PublishedPortsProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ports_source = Path(cls.options.ports_source).resolve(strict=True)
        cls.runner = cls.ports_source / 'profiles/development-toolchain/cross/tests/target/run-target.sh'
        if not cls.runner.is_file():
            raise RuntimeError('selected Ports source lacks the published target runner')
        result = subprocess.run(['git', '-C', str(cls.ports_source), 'status', '--porcelain'],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if result.stdout:
            raise RuntimeError('selected Ports source checkout must be clean and published')

    @classmethod
    def set_options(cls, options):
        cls.options = options

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='sdk-ports-protocol-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / 'bundle'
        self.output.mkdir()
        for name, data in (('target.txt', 'aarch64\n'),
                           ('compiler-family.txt', 'clang\n'),
                           ('os-tests.txt', 'yes\n')):
            _write(self.output / name, data)
        for name in ('atomic-tls', 'atomic-tls-lto', 'libc-aliases',
                     'base-loader-smoke', 'fork-cas', 'fork-cas-static'):
            _write(self.output / name, '#!/bin/sh\nexit 0\n', executable=True)
        _write(self.output / 'boundary',
               '#!/bin/sh\nprintf "LOADED /usr/pkg/llvm/lib/libc++.so.1.0\\n"\n',
               executable=True)
        _write(self.output / 'check-runtime.sh', '#!/bin/sh\nexit 0\n', executable=True)
        self.fakebin = self.root / 'fakebin'
        _write(self.fakebin / 'uname',
               '#!/bin/sh\ncase "$1" in -s) echo NetBSD;; -p) echo aarch64;; *) exit 2;; esac\n',
               executable=True)
        _write(self.fakebin / 'ldd',
               '#!/bin/sh\necho "libc.so.12 => /lib/libc.so.12"\n', executable=True)

    def run_ports_guest_runner(self):
        environment = dict(os.environ,
            PATH=str(self.fakebin) + os.pathsep + '/usr/bin:/bin',
            EMBERBSD_ACCEPTANCE_NONCE='ports-protocol-42')
        environment.pop('LD_LIBRARY_PATH', None)
        environment.pop('LD_PRELOAD', None)
        result = subprocess.run(['/bin/sh', str(self.runner), '--target', 'aarch64',
                                 str(self.output)], env=environment, check=False,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def serial_from_ports(self, output):
        return ('kernel boot noise\n'
                'EMBERBSD-ACCEPTANCE ports-protocol-42 ROOT /dev/ld0a ffs ro,local\n' +
                output +
                'EMBERBSD-ACCEPTANCE ports-protocol-42 COMPLETE\n').encode()

    def test_published_ports_runner_output_is_accepted_by_sdk_parser(self):
        output = self.run_ports_guest_runner()
        checks = parse_guest_log(self.serial_from_ports(output), 'ports-protocol-42', 'ld0a')
        self.assertEqual(set(checks), set(GUEST_CHECKS))

    def test_sdk_parser_rejects_host_compile_marker_from_guest_output(self):
        output = self.run_ports_guest_runner()
        bad_serial = self.serial_from_ports(
            output + 'EMBERBSD-CHECK ports-protocol-42 cross_c11 PASS\n')
        with self.assertRaisesRegex(SDKError, 'unknown guest check marker'):
            parse_guest_log(bad_serial, 'ports-protocol-42', 'ld0a')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ports-source', required=True,
                        help='published EmberBSD-Ports source checkout')
    options, remaining = parser.parse_known_args(argv)
    PublishedPortsProtocolTests.set_options(options)
    unittest.main(argv=[sys.argv[0], *remaining])


if __name__ == '__main__':
    main()
