# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted); Libtool scalar and selected-tag contracts.
"""Pure text fixtures. These tests do not run a compiler or Libtool."""

from pathlib import Path
import sys
import unittest

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.libtool import LIBTOOL_CONFIG_API, parse_libtool_config

DEFAULT = 'host=example--os\nhost_os=os\nCC="default-cc"\nbuild_libtool_libs=yes\n'


def block(tag='FC', body='CC="selected-cc"\n'):
    return '# ### BEGIN LIBTOOL TAG CONFIG: ' + tag + '\n' + body + '# ### END LIBTOOL TAG CONFIG: ' + tag + '\n'


class LibtoolTests(unittest.TestCase):
    def test_api(self):
        self.assertIs(type(LIBTOOL_CONFIG_API), int)
        self.assertEqual(LIBTOOL_CONFIG_API, 1)

    def test_default_and_explicit_selected_compiler_are_separate(self):
        result = parse_libtool_config(DEFAULT + block(), 'FC')
        self.assertEqual(result['tag'], 'FC')
        self.assertEqual(result['defaults'], {'host': 'example--os', 'host_os': 'os', 'CC': 'default-cc', 'build_libtool_libs': 'yes'})
        self.assertEqual(result['overrides'], {'CC': 'selected-cc'})
        self.assertEqual(result['effective'], {'host': 'example--os', 'host_os': 'os', 'CC': 'selected-cc', 'build_libtool_libs': 'yes'})

    def test_all_requested_tags_and_cc_default_only(self):
        for tag in ('FC', 'F77', 'CXX'):
            with self.subTest(tag=tag):
                self.assertEqual(parse_libtool_config(DEFAULT + block(tag), tag)['effective']['CC'], 'selected-cc')
        result = parse_libtool_config(DEFAULT, 'CC')
        self.assertEqual(result['overrides'], {})
        self.assertEqual(result['effective'], result['defaults'])

    def test_explicit_common_overrides_are_reported_without_target_policy(self):
        result = parse_libtool_config(DEFAULT + block(body='CC="other cc"\nhost=other--os\nhost_os=other\nbuild_libtool_libs=no\n'), 'FC')
        self.assertEqual(result['effective'], {'host': 'other--os', 'host_os': 'other', 'CC': 'other cc', 'build_libtool_libs': 'no'})
        self.assertEqual(result['defaults']['host'], 'example--os')

    def test_unrelated_multiline_fields_do_not_change_selected_scalars(self):
        self.assertEqual(parse_libtool_config(DEFAULT + "unrelated='line1\nline2'\n" + block(), 'FC')['effective']['CC'], 'selected-cc')

    def test_multiline_quoted_values_cannot_supply_a_required_field(self):
        text = DEFAULT.replace('CC="default-cc"\n', '') + "unrelated='line1\nCC=forged\n'\n" + block()
        with self.assertRaises(SDKError):
            parse_libtool_config(text, 'FC')

    def test_adjacent_quotes_and_quoted_punctuation_are_data(self):
        for value, expected in [('"selected-"\'cc\'', 'selected-cc'), ('"cc;data"', 'cc;data')]:
            with self.subTest(value=value):
                self.assertEqual(parse_libtool_config(DEFAULT + block(body='CC=' + value + '\n'), 'FC')['effective']['CC'], expected)

    def test_required_fields_and_scope_duplicates_refuse(self):
        cases = [DEFAULT.replace('CC="default-cc"\n', '') + block(), DEFAULT + 'CC=duplicate\n' + block(),
                 DEFAULT + block(body=''), DEFAULT + block(body='CC=a\nCC=b\n'),
                 DEFAULT + block(body='CC=a\nhost=x\nhost=y\n'),
                 DEFAULT.replace('host_os=os\n', '') + block()]
        for text in cases:
            with self.subTest(text=text), self.assertRaises(SDKError):
                parse_libtool_config(text, 'FC')

    def test_missing_foreign_duplicate_reversed_and_truncated_blocks_refuse(self):
        cases = [DEFAULT, DEFAULT + block('F77'), DEFAULT + block() + block(), DEFAULT + block() + block('CXX'),
                 DEFAULT + block().replace('BEGIN', 'TEMP').replace('END', 'BEGIN').replace('TEMP', 'END'),
                 DEFAULT + block().replace('# ### END LIBTOOL TAG CONFIG: FC\n', ''),
                 DEFAULT + block().replace('CC="selected-cc"', '# ### BEGIN LIBTOOL TAG CONFIG: FC\nCC=x')]
        for text in cases:
            with self.subTest(text=text), self.assertRaises(SDKError):
                parse_libtool_config(text, 'FC')
        with self.assertRaises(SDKError):
            parse_libtool_config(DEFAULT + block(), 'CC')

    def test_unknown_marker_and_nonempty_tail_refuse(self):
        for suffix in ('CC=tail\n', '# extra output\n'):
            with self.subTest(suffix=suffix), self.assertRaises(SDKError):
                parse_libtool_config(DEFAULT + block() + suffix, 'FC')
        for marker in ('# ### MIDDLE LIBTOOL TAG CONFIG: FC', ' # ### BEGIN LIBTOOL TAG CONFIG: FC', '# ### BEGIN LIBTOOL CONFIG'):
            with self.subTest(marker=marker), self.assertRaises(SDKError):
                parse_libtool_config(DEFAULT + block().replace('# ### BEGIN LIBTOOL TAG CONFIG: FC', marker), 'FC')

    def test_scalar_syntax_does_not_accept_shell_command_lists(self):
        for value in ('', '""', 'a b', 'cc;id', 'cc && id', 'cc | id', 'cc > file', '$(id)', '"unclosed'):
            with self.subTest(value=value), self.assertRaises(SDKError):
                parse_libtool_config(DEFAULT + block(body='CC=' + value + '\n'), 'FC')

    def test_invalid_inputs_refuse(self):
        for text, tag in [(None, 'FC'), ('\0', 'FC'), (DEFAULT, ''), (DEFAULT, None), (DEFAULT, 'FC;id')]:
            with self.subTest(text=text, tag=tag), self.assertRaises(SDKError):
                parse_libtool_config(text, tag)


if __name__ == '__main__':
    unittest.main()
