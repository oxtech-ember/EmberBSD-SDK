# SPDX-License-Identifier: BSD-2-Clause
"""Check only the bound comparison environment. Do not load native inputs."""
import unittest

import test_core  # Adds the SDK source to sys.path.
from emberbsd_sdk.bootstrap_producer import metadata_environment
from emberbsd_sdk.errors import SDKError


class ComparisonEnvironmentTests(unittest.TestCase):
    def plan(self, format):
        return dict(format=format, work_root='/source-test/work', output_bundle_root='/source-test/prefix',
            metadata=dict(utility_bin='/source-test/utility',
                          makeconf=dict(path='/source-test/mk.conf')))

    def commands(self):
        return dict(sh=['/source-test/sh'], strip=['/source-test/llvm-strip'],
                    uname=['/source-test/uname'])

    def test_metadata_environment_stays_unchanged(self):
        result = metadata_environment(self.plan('emberbsd-native-gcc16-producer-plan'), self.commands())
        self.assertEqual(result, dict(PATH='/source-test/utility', LC_ALL='C', TZ='UTC',
            TMPDIR='/source-test/work', MAKECONF='/source-test/mk.conf',
            SHELL='/source-test/sh', CONFIG_SHELL='/source-test/sh', PATH_SEPARATOR=':'))

    def test_full_plan_binds_selected_strip(self):
        result = metadata_environment(self.plan('emberbsd-native-gcc16-bootstrap-plan'), self.commands())
        self.assertEqual(result['GCC_CONFIGURE_SDK'], 'yes')
        self.assertEqual(result['GCC_CONFIGURE_UNAME'], '/source-test/uname')
        self.assertEqual(result['GCC_COMPARE_DEBUG_STRIP'], '/source-test/llvm-strip')
        self.assertEqual(result['GCC_COMPARE_DEBUG_OBJDUMP'], '/source-test/prefix/bin/llvm-objdump')
        self.assertEqual(result['GCC_COMPARE_DEBUG_OBJCOPY'], '/source-test/prefix/bin/llvm-objcopy')

    def test_missing_strip_refuses(self):
        with self.assertRaises(SDKError):
            metadata_environment(self.plan('emberbsd-native-gcc16-bootstrap-plan'),
                                 dict(sh=['/source-test/sh']))

    def test_strip_argv_tail_refuses(self):
        commands = self.commands()
        commands['strip'].append('--extra')
        with self.assertRaises(SDKError):
            metadata_environment(self.plan('emberbsd-native-gcc16-bootstrap-plan'), commands)
