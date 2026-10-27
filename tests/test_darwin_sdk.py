# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Explicit SDK metadata, mixed roots and absent ambient tool boundaries."""
import hashlib
import json
import plistlib
import subprocess
import sys
from unittest.mock import patch
from test_core import TemporaryCase, SDK_SOURCE
from emberbsd_sdk.darwin_sdk import DARWIN_SDK_API, read_metadata
from emberbsd_sdk.errors import SDKError


class DarwinSDKTests(TemporaryCase):
    def setUp(self):
        super().setUp()
        self.sdk = self.root / 'SDK with spaces'
        self.settings = self.write('SDK with spaces/SDKSettings.plist',
            plistlib.dumps(dict(Version='27.0', CanonicalName='macosx27.0')))
        self.system = self.write('SDK with spaces/System/Library/CoreServices/SystemVersion.plist',
            plistlib.dumps(dict(ProductVersion='27.0', ProductBuildVersion='26A425')))
        self.xcode = self.write('Selected Xcode/version.plist',
            plistlib.dumps(dict(CFBundleShortVersionString='27.0', ProductBuildVersion='27A266a'),
                          fmt=plistlib.FMT_BINARY))

    def read(self):
        return read_metadata(self.sdk, xcode_version_plist=self.xcode)

    def test_xml_and_binary_plists_without_tool_execution(self):
        with patch('subprocess.run', side_effect=AssertionError('ambient tool')):
            result = self.read()
        self.assertEqual(DARWIN_SDK_API, 1)
        self.assertEqual(result['sdk_path'], str(self.sdk))
        self.assertEqual(result['sdk_name'], 'macosx27.0')
        self.assertEqual(result['sdk_build'], '26A425')
        self.assertEqual(result['xcode_version'], '27.0')
        self.assertEqual(result['xcode_build'], '27A266a')
        self.assertEqual(result['inputs'], {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                                          for p in (self.settings, self.system, self.xcode)})

    def test_explicit_sdk_alias_is_canonical(self):
        alias = self.root / 'MacOSX.sdk'
        alias.symlink_to(self.sdk, target_is_directory=True)
        self.assertEqual(read_metadata(alias, xcode_version_plist=self.xcode), self.read())

    def test_metadata_link_cannot_select_another_sdk(self):
        other = self.write('other/SDKSettings.plist', self.settings.read_bytes())
        self.settings.unlink()
        self.settings.symlink_to(other)
        with self.assertRaisesRegex(SDKError, 'escapes'):
            self.read()

    def test_mixed_sdk_and_malformed_values_are_refused(self):
        original = self.settings.read_bytes()
        for value in (dict(Version='27.0', CanonicalName='macosx26.0'),
                      dict(Version='26.0', CanonicalName='macosx26.0'),
                      dict(Version='27.0', CanonicalName='iphoneos27.0'),
                      dict(Version=27, CanonicalName='macosx27.0'),
                      dict(Version='27.0\n', CanonicalName='macosx27.0'),
                      dict(CanonicalName='macosx27.0')):
            with self.subTest(value=value):
                self.settings.write_bytes(plistlib.dumps(value))
                with self.assertRaises(SDKError):
                    self.read()
        self.settings.write_bytes(original)
        for data in (b'not a plist', b'<?xml version="1.0"?><plist><dict>',
                     plistlib.dumps(['27.0'])):
            self.xcode.write_bytes(data)
            with self.assertRaises(SDKError):
                self.read()

    def test_paths_are_explicit_and_missing_inputs_fail(self):
        with self.assertRaisesRegex(SDKError, 'absolute'):
            read_metadata('MacOSX.sdk', xcode_version_plist=self.xcode)
        self.system.unlink()
        with self.assertRaises(SDKError):
            self.read()

    def test_cli_with_empty_path(self):
        result = subprocess.run([sys.executable, '-B', str(SDK_SOURCE/'scripts/darwin-sdk.py'),
            '--macos-sdk', str(self.sdk), '--xcode-version-plist', str(self.xcode)],
            env=dict(PATH='', PYTHONDONTWRITEBYTECODE='1'), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), self.read())

    def test_malformed_xml_dates_have_normal_cli_errors(self):
        for date in ('<date>invalid</date>', '<date/>'):
            self.xcode.write_text('<plist><dict><key>Unused</key>' + date + '</dict></plist>')
            with self.subTest(date=date), self.assertRaisesRegex(SDKError, 'invalid Darwin metadata plist'):
                self.read()
            result = subprocess.run([sys.executable, '-B', str(SDK_SOURCE/'scripts/darwin-sdk.py'),
                '--macos-sdk', str(self.sdk), '--xcode-version-plist', str(self.xcode)],
                env=dict(PATH='', PYTHONDONTWRITEBYTECODE='1'), capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, '')
            self.assertIn('invalid Darwin metadata plist', result.stderr)
            self.assertNotIn('Traceback', result.stderr)
