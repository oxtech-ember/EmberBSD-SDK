# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""The LLVM stage key follows the OS files the stage build read."""

import json
from pathlib import Path

from test_core import TemporaryCase
from test_builder import write, git, commit
from emberbsd_sdk import llvm_stage_inputs as inputs
from emberbsd_sdk.errors import SDKError

M4 = b'#!/bin/sh\n# fixture m4 program\n'
CONFIG = b'#!/bin/sh\n# fixture config program\n'


class StageInputTests(TemporaryCase):
    def setUp(self):
        super().setUp()
        self.os = self.root / 'os'
        files = {
            'build.sh': '#!/bin/sh\n', 'Makefile': 'all:\n', 'share/mk/bsd.own.mk': 'MK=1\n',
            'tools/m4/Makefile': 'HOSTPROG=m4\n', 'tools/config/Makefile': 'HOSTPROG=config\n',
            'tools/make/Makefile': 'HOSTPROG=bmake\n',
            'usr.bin/m4/main.c': 'int m4;\n', 'usr.bin/config/main.c': 'int config;\n',
            'usr.bin/config/Makefile': 'PROG=config\n',
            'lib/libc/Makefile': 'LIB=c\n', 'lib/libc/gen/a.c': 'int a;\n',
            'lib/libc/gen/gen.awk': '{ print }\n', 'common/lib/libc/rule.awk': '{ print }\n',
            'include/stdio.h': '/* stdio */\n', 'sys/sys/types.h': '/* types */\n',
            'sys/arch/evbarm/conf/GENERIC': 'options X\n',
        }
        for name, text in files.items():
            write(self.os / name, text)
        git(self.os, 'init', '-q')
        commit(self.os)
        self.objects = self.root / 'obj'
        self.tools = self.root / 'tooldir'
        root = str(self.os)
        write(self.objects / 'lib/libc/gen/a.o', 'object')
        write(self.objects / 'lib/libc/gen/a.d',
              f'a.o: {root}/lib/libc/gen/a.c \\\n {root}/include/stdio.h\n')
        write(self.objects / 'tools/m4/m4', M4, 0o755)
        write(self.objects / 'tools/m4/main.d', f'main.o: {root}/tools/m4/../../usr.bin/m4/main.c\n')
        write(self.objects / 'tools/config/config', CONFIG, 0o755)
        write(self.objects / 'tools/config/main.d', f'main.o: {root}/usr.bin/config/main.c\n')
        write(self.objects / 'tools/make/bmake', 'bmake', 0o755)
        write(self.tools / 'bin/nbm4', M4, 0o755)
        write(self.tools / 'bin/nbconfig', CONFIG, 0o755)
        write(self.tools / 'bin/nbmake-evbarm', '#!/bin/sh\nexec bmake "$@"\n', 0o755)
        bin_ = f'{self.tools}/bin'
        self.log_lines = [
            f'===> Updated makewrapper: {bin_}/nbmake-evbarm',
            # The stage builds every host tool, config(1) included, but runs only m4.
            f'cc -c {root}/usr.bin/config/main.c',
            f'--- {bin_}/nbconfig ---',
            f'#   install  {bin_}/nbconfig',
            f'{bin_}/nbm4 -D X {root}/common/lib/libc/rule.awk > out',
        ]
        self.destination = self.root / 'stage'
        self.write_log()

    def write_log(self):
        for phase in inputs.PHASES:
            write(self.destination / phase / '.prebuild-sdk-build.log', '\n'.join(self.log_lines) + '\n')

    def receipts(self):
        commit_id = git(self.os, 'rev-parse', 'HEAD')
        result = {}
        for phase in inputs.PHASES:
            result[phase] = dict(
                format='emberbsd-llvm-os-stage', phase=phase, qualification='built-and-inspected',
                paths=dict(os=str(self.os), objects=str(self.objects), tools=str(self.tools),
                           destination=str(self.destination / phase)),
                sources=dict(os=dict(commit=commit_id)),
                signature=dict(llvm_source=dict(tree='1' * 64), compiler=dict(version='23.1.4')))
        return result

    def record(self):
        return inputs.make_record(self.receipts(), git, 'aarch64')

    def digest(self, record):
        rows = inputs.tree_rows(git, str(self.os), git(self.os, 'rev-parse', 'HEAD'))
        return inputs.inputs_digest(rows, record['directories'])

    def change(self, name, text='/* changed */\n'):
        write(self.os / name, text)
        commit(self.os)

    def test_record_lists_used_directories_and_only_run_tools(self):
        record = self.record()
        directories = set(record['directories'])
        self.assertIn('lib/libc/gen', directories)
        self.assertIn('include', directories)
        self.assertIn('usr.bin/m4', directories)
        self.assertIn('tools/m4', directories)
        self.assertIn('common/lib/libc', directories)
        self.assertNotIn('usr.bin/config', directories)
        self.assertNotIn('tools/config', directories)
        self.assertEqual(record['phases']['base']['invoked'], ['nbm4'])
        self.assertEqual(record['phases']['base']['unmatched'], [])
        self.assertEqual(record['phases']['base']['tools'], ['tools/m4', 'tools/make'])
        self.assertEqual(inputs.read_record(self.write_record(record)), record)

    def write_record(self, record):
        path = self.root / 'inputs.json'
        inputs.write_record(str(path), record)
        return str(path)

    def test_unrelated_os_change_keeps_the_key(self):
        record = self.record()
        for name in ('usr.bin/config/main.c', 'usr.bin/config/Makefile', 'sys/arch/evbarm/conf/GENERIC',
                     'usr.bin/config/new.c'):
            with self.subTest(name=name):
                self.change(name)
                self.assertEqual(self.digest(record), record['inputs'])

    def test_relevant_os_change_changes_the_key(self):
        record = self.record()
        for name in ('lib/libc/gen/a.c', 'lib/libc/gen/new.c', 'include/stdio.h', 'share/mk/bsd.own.mk',
                     'build.sh', 'usr.bin/m4/main.c', 'tools/config/Makefile', 'sys/sys/new.h',
                     'common/lib/libc/rule.awk', 'usr.bin/config/config.mk'):
            with self.subTest(name=name):
                before = self.digest(record)
                self.change(name)
                self.assertNotEqual(self.digest(record), before)

    def test_key_covers_llvm_source_and_compiler(self):
        record = self.record()
        key = inputs.stage_key('aarch64', dict(tree='1' * 64), dict(version='23.1.4'), record['inputs'])
        self.assertEqual(key, record['key'])
        self.assertNotEqual(inputs.stage_key('aarch64', dict(tree='2' * 64), dict(version='23.1.4'),
                                             record['inputs']), key)
        self.assertNotEqual(inputs.stage_key('aarch64', dict(tree='1' * 64), dict(version='23.1.5'),
                                             record['inputs']), key)

    def test_unmatched_program_keeps_every_tool(self):
        write(self.tools / 'bin/nbmystery', b'#!/bin/sh\n# no object directory\n', 0o755)
        self.log_lines.append(f'{self.tools}/bin/nbmystery input')
        self.write_log()
        record = self.record()
        self.assertEqual(record['phases']['base']['unmatched'], ['nbmystery'])
        self.assertIn('usr.bin/config', record['directories'])
        self.assertIn('tools/config', record['directories'])

    def test_record_is_published_once(self):
        record = self.record()
        path = self.write_record(record)
        inputs.write_record(path, record)
        with self.assertRaisesRegex(SDKError, 'record differs'):
            inputs.write_record(path, record | {'inputs': '0' * 64})
        self.assertEqual(sorted(p.name for p in self.root.iterdir() if p.name.startswith('inputs')),
                         ['inputs.json'])

    def test_incomplete_or_mixed_receipts_are_refused(self):
        receipts = self.receipts()
        receipts['base']['qualification'] = 'planned'
        with self.assertRaisesRegex(SDKError, 'completed stage receipts'):
            inputs.make_record(receipts, git, 'aarch64')
        receipts = self.receipts()
        receipts['base']['sources']['os']['commit'] = '0' * 40
        with self.assertRaisesRegex(SDKError, 'different OS commits'):
            inputs.make_record(receipts, git, 'aarch64')

    def test_invalid_record_is_refused(self):
        record = self.record()
        path = self.root / 'bad.json'
        for bad in (record | {'directories': ['../outside']}, record | {'key': 'short'},
                    record | {'version': 2}):
            path.write_text(json.dumps(bad))
            with self.assertRaisesRegex(SDKError, 'invalid LLVM stage input record'):
                inputs.read_record(str(path))
