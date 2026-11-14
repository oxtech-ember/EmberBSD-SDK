# SPDX-License-Identifier: BSD-2-Clause
from contextlib import ExitStack
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk import sdk_client
from emberbsd_sdk.errors import SDKError


class SourcePlanTests(unittest.TestCase):
    def setUp(self):
        self.config = dict(host_prefix='/native', source=dict(ports_root='/ports',
                           pkgsrc='/pkgsrc', makeconf='/mk.conf', state='/source-state'))
        self.native = Mock()
        self.native.tool.side_effect = lambda role: '/native/bin/' + role

    def test_source_plan_delegates_only_plan_and_restores_environment(self):
        def selector(arguments):
            self.assertEqual(arguments, ['--ports-root', '/ports', 'plan', '--host-prefix', '/native',
                '--state', '/source-state', '--set', 'minimal', '--package', 'devel/a@native'])
            self.assertEqual(os.environ['BMAKE'], '/native/bin/make')
            self.assertEqual(os.environ['MAKECONF'], '/mk.conf')
            self.assertEqual(os.environ['PKGSRCDIR'], '/pkgsrc')
            self.assertNotIn('CROSS_BULK_BINARY_INSTALLED', os.environ)
            return 7
        with patch.dict(os.environ, BMAKE='/previous', CROSS_BULK_BINARY_INSTALLED='/old'), \
             patch.object(sdk_client, 'load_host_prefix', return_value=self.native), \
             patch('emberbsd_sdk.package_selection.main', side_effect=selector):
            self.assertEqual(sdk_client.plan_source_selection(self.config, sets=['minimal'],
                             roots=['devel/a@native']), 7)
            self.assertEqual(os.environ['BMAKE'], '/previous')
            self.assertEqual(os.environ['CROSS_BULK_BINARY_INSTALLED'], '/old')

    def test_cli_dispatch_precedes_all_release_cache_and_install_work(self):
        with ExitStack() as stack:
            stack.enter_context(patch.object(sdk_client, 'validate_config', return_value=self.config))
            plan = stack.enter_context(patch.object(sdk_client, 'plan_source_selection', return_value=0))
            for name in ('ReleaseCrypto', 'HTTPStore', 'release_snapshot', 'PackageCache',
                         'PackageInstaller', 'install_selection', '_read_only_directories'):
                stack.enter_context(patch.object(sdk_client, name, side_effect=AssertionError(name)))
            self.assertEqual(sdk_client.main(['source-plan', '--package', 'devel/a'],
                                            default_config=self.config), 0)
            plan.assert_called_once_with(self.config, sets=[], roots=['devel/a'])

    def test_empty_selection_does_not_require_native_tools_or_source(self):
        with patch.object(sdk_client, 'load_host_prefix', side_effect=AssertionError('native')):
            self.assertEqual(sdk_client.plan_source_selection({}), 0)

    def test_source_configuration_is_required(self):
        with self.assertRaisesRegex(SDKError, 'configured local'):
            sdk_client.plan_source_selection({}, roots=['devel/a'])
        self.config['source']['state'] = 'relative'
        with self.assertRaisesRegex(SDKError, 'absolute state'):
            sdk_client.plan_source_selection(self.config, roots=['devel/a'])

    def test_environment_restored_after_selector_exception(self):
        with patch.dict(os.environ, BMAKE='/previous'), \
             patch.object(sdk_client, 'load_host_prefix', return_value=self.native), \
             patch('emberbsd_sdk.package_selection.main', side_effect=RuntimeError('fixture')):
            with self.assertRaises(RuntimeError):
                sdk_client.plan_source_selection(self.config, roots=['devel/a'])
            self.assertEqual(os.environ['BMAKE'], '/previous')


if __name__ == '__main__':
    unittest.main()
