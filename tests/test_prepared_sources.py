# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Plan-time refusal of OS builds that reach an unselected prepared source.

A synthetic OS tree and a bmake stand-in answer ${SUBDIR} and
${.MAKE.MAKEFILES}; the makefiles carry the OS guard forms. The b504c-style
case selects only the sources a sysroot needs and must be refused at
--plan, not hours into the release when iscsi/lib reaches "includes".
"""
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_core import TemporaryCase
from emberbsd_sdk import prepared_sources
from emberbsd_sdk.errors import CommandError, SDKError
from emberbsd_sdk.llvm_sysroot import LLVMCoordinator
from emberbsd_sdk.os_release_builder import OSReleaseBuilder, flags_for
from emberbsd_sdk.sysroot_builder import SysrootBuilder

TOP = """SUBDIR= tools lib external bin distrib
.for dir in bin distrib tools
includes-${dir}: .PHONY
\t@true
.endfor
dependall-distrib all-distrib install-distrib: .PHONY
\t@true
"""
ISCSI = """.if !defined(EMBER_ISCSI_SOURCE) && empty(.MAKEFLAGS:M-V*) && make(includes)
.error Set EMBER_ISCSI_SOURCE to the prepared common Ports iSCSI source
.endif
"""
TOP_BIN = """.if defined(EMBER_TOP_SOURCE)
TOPDIR= ${EMBER_TOP_SOURCE}
.elif empty(.MAKEFLAGS:M-V*) && \\
    (make(all) || make(dependall) || make(install))
.error Set EMBER_TOP_SOURCE to the prepared common Ports top source
.endif
"""
GETTEXT = """.elif empty(.MAKEFLAGS:M-V*) && (make(all) || make(install))
.error Set EMBER_GETTEXT_SOURCE and EMBER_LIBXML2_SOURCE to the prepared common Ports sources
.endif
"""
BYACC = """.if !defined(EMBER_BYACC_SOURCE)
.error Set EMBER_BYACC_SOURCE to the prepared common Ports byacc source
.endif
"""
DISTRIB = """.if empty(.MAKEFLAGS:M-V*) && make(all)
.error Set EMBER_TEXINFO_SOURCE to the prepared common Ports Texinfo source
.endif
"""
# directory -> (SUBDIR, makefiles read); "x11" entries only with MKX11=yes.
TREE = {
    '.': (['tools', '.WAIT', 'lib', 'external', 'bin', 'distrib', 'missing'], ['Makefile']),
    'tools': (['host'], ['Makefile']),
    'tools/host': ([], ['Makefile']),
    'lib': (['libc', '../external/iscsi/lib'], ['Makefile']),
    'lib/libc': ([], ['Makefile']),
    'external': (['iscsi', 'top', 'byacc', 'gettext'], ['Makefile']),
    'external/iscsi': (['lib'], ['Makefile']),
    'external/iscsi/lib': ([], ['Makefile', '../Makefile.inc']),
    'external/top': (['bin'], ['Makefile']),
    'external/top/bin': ([], ['Makefile']),
    'external/byacc': (['bin'], ['Makefile']),
    'external/byacc/bin': ([], ['Makefile', '../Makefile.inc']),
    'external/gettext': ([], ['Makefile']),
    'bin': (['cat'], ['Makefile']),
    'bin/cat': ([], ['Makefile']),
    'distrib': ([], ['Makefile']),
}
FILES = {'Makefile': TOP, 'external/iscsi/lib/Makefile': ISCSI, 'external/top/bin/Makefile': TOP_BIN,
         'external/byacc/Makefile.inc': BYACC, 'external/gettext/Makefile': GETTEXT, 'distrib/Makefile': DISTRIB}
SYSROOT_SOURCES = ('llvm', 'libedit', 'libarchive', 'libevent', 'byacc', 'rcs')
RELEASE_SOURCES = SYSROOT_SOURCES + ('iscsi', 'top', 'gettext', 'libxml2')


class PreparedSourceTests(TemporaryCase):
    def setUp(self):
        super().setUp()
        self.os = self.root / 'os'
        for name in TREE:
            (self.os / name).mkdir(parents=True, exist_ok=True)
            (self.os / name / 'Makefile').write_text('# synthetic\n')
        for name, text in FILES.items():
            (self.os / name).write_text(text)
        self.queries = []

    def fake_make(self, argv, *, env=None):
        directory = argv[argv.index('-C') + 1]
        relative = os.path.relpath(directory, self.os)
        self.queries.append(relative)
        flags = set(argv)
        self.assertEqual(argv[-1], 'obj')
        subdirs, makefiles = TREE[relative]
        if relative == 'external/byacc/bin' and not any(f.startswith('EMBER_BYACC_SOURCE=') for f in flags):
            raise CommandError(1, argv, b'', b'bmake: ' + str(self.os).encode() +
                               b'/external/byacc/bin/../Makefile.inc:2: Set EMBER_BYACC_SOURCE to the prepared common Ports byacc source\n')
        if relative == 'external' and 'MKX11=yes' in flags:
            subdirs = subdirs + ['x11']
        return SimpleNamespace(stdout=(' '.join(subdirs) + '\n' + ' '.join(makefiles) + '\n').encode())

    def walk(self, flags, traversals):
        query = prepared_sources.bmake_query('/host/bmake', str(self.os), flags, env={})
        with patch('emberbsd_sdk.prepared_sources.process.capture', side_effect=self.fake_make):
            return prepared_sources.required_sources(str(self.os), query, traversals, jobs=2)

    def builder(self, names):
        builder = SysrootBuilder({'jobs': '4'})
        builder.paths = {'os': str(self.os), 'tools': '/build/tools', 'objects': '/build/objects'}
        builder.paths.update({name + '-source': '/prepared/' + name for name in names})
        builder.target = {'machine': 'evbarm', 'arch': 'aarch64'}
        builder.environment = {'EMBER_NATIVE_TOOLS': '/routing'}
        builder.native = SimpleNamespace(tool=lambda name: '/host/' + name)
        builder.source_timestamp = lambda: 1700000000
        return builder

    @staticmethod
    def source_flags(names):
        return [f'EMBER_{name.upper()}_SOURCE=/prepared/{name}' for name in names]

    def test_guards_name_their_targets(self):
        self.assertEqual(prepared_sources.guards(self.os / 'external/top/bin/Makefile'),
                         [(('EMBER_TOP_SOURCE',), frozenset(('all', 'dependall', 'install')))])
        self.assertEqual(prepared_sources.guards(self.os / 'external/iscsi/lib/Makefile'),
                         [(('EMBER_ISCSI_SOURCE',), frozenset(('includes',)))])
        self.assertEqual(prepared_sources.guards(self.os / 'external/gettext/Makefile'),
                         [(('EMBER_GETTEXT_SOURCE', 'EMBER_LIBXML2_SOURCE'), frozenset(('all', 'install')))])
        self.assertEqual(prepared_sources.guards(self.os / 'external/byacc/Makefile.inc'),
                         [(('EMBER_BYACC_SOURCE',), None)])
        self.assertEqual(prepared_sources.source_option('EMBER_TCP_WRAPPERS_SOURCE'), 'tcp-wrappers-source')

    def test_release_needs_follow_reached_directories_and_targets(self):
        needed = self.walk(['MKX11=no'], prepared_sources.RELEASE_TRAVERSALS)
        self.assertEqual(needed, {'EMBER_ISCSI_SOURCE': 'external/iscsi/lib', 'EMBER_TOP_SOURCE': 'external/top/bin',
                                  'EMBER_BYACC_SOURCE': 'external/byacc/bin',
                                  'EMBER_GETTEXT_SOURCE': 'external/gettext', 'EMBER_LIBXML2_SOURCE': 'external/gettext'})
        # distrib is a stub for every build target; host tools are built separately.
        self.assertNotIn('distrib', self.queries)
        self.assertNotIn('tools', self.queries)
        self.assertNotIn('missing', self.queries)

    def test_sysroot_libs_need_only_includes_and_lib_build(self):
        needed = self.walk(self.source_flags(['byacc']), prepared_sources.LIBS_TRAVERSALS)
        # iscsi/lib refuses "includes"; top/bin and gettext only refuse building.
        # A selected byacc source still counts as needed by the makefile that reads it.
        self.assertEqual(needed, {'EMBER_ISCSI_SOURCE': 'external/iscsi/lib', 'EMBER_BYACC_SOURCE': 'external/byacc/bin'})
        self.assertNotIn('bin', self.queries)

    def test_unknown_parse_failure_is_refused_with_its_diagnostic(self):
        def broken(argv, *, env=None):
            raise CommandError(1, argv, b'', b'bmake: /os/Makefile:9: Malformed conditional\nbmake: Fatal errors encountered\n')
        query = prepared_sources.bmake_query('/host/bmake', str(self.os), [], env={})
        with patch('emberbsd_sdk.prepared_sources.process.capture', side_effect=broken):
            with self.assertRaisesRegex(SDKError, r'cannot inspect OS directory \.: .*Malformed conditional'):
                prepared_sources.required_sources(str(self.os), query, prepared_sources.RELEASE_TRAVERSALS)

    def test_plan_refuses_b504c_style_release_and_accepts_all_sources(self):
        flags = flags_for('aarch64', 'clang', True)
        builder = self.builder(SYSROOT_SOURCES)
        with patch('emberbsd_sdk.prepared_sources.process.capture', side_effect=self.fake_make):
            with self.assertRaises(SDKError) as refused:
                builder.require_prepared_sources(flags + self.source_flags(SYSROOT_SOURCES),
                                                 OSReleaseBuilder.prepared_source_profile({'target': 'aarch64', 'x11-source': '/x'})[1])
        message = str(refused.exception)
        for option in ('--iscsi-source', '--top-source', '--gettext-source', '--libxml2-source'):
            self.assertIn(option, message)
        self.assertIn('EMBER_ISCSI_SOURCE for external/iscsi/lib', message)
        self.assertNotIn('--byacc-source', message)
        builder = self.builder(RELEASE_SOURCES)
        with patch('emberbsd_sdk.prepared_sources.process.capture', side_effect=self.fake_make) as run:
            needed = builder.require_prepared_sources(flags + self.source_flags(RELEASE_SOURCES),
                                                      prepared_sources.RELEASE_TRAVERSALS)
        self.assertIn('EMBER_TOP_SOURCE', needed)
        argv = run.call_args.args[0]
        self.assertIn('NETBSDOBJDIR=/build/objects', argv)
        self.assertIn('MKREPRO_TIMESTAMP=1700000000', argv)
        self.assertNotIn('EMBER_NATIVE_TOOLS', run.call_args.kwargs['env'])

    def test_unselected_parse_time_source_is_reported(self):
        builder = self.builder(('llvm', 'libedit'))
        with patch('emberbsd_sdk.prepared_sources.process.capture', side_effect=self.fake_make):
            with self.assertRaisesRegex(SDKError, r'--byacc-source \(EMBER_BYACC_SOURCE for external/byacc/bin\)'):
                builder.require_prepared_sources([], prepared_sources.LIBS_TRAVERSALS)

    def test_coordinator_checks_base_and_final_profiles_before_headers_build(self):
        calls = []
        headers = SimpleNamespace(flags=['MKX11=no', 'MKCXX=no', 'MKLIBCXX=no', 'EMBER_COMPILER_FAMILY=clang',
                                         'EMBER_BYACC_SOURCE=/prepared/byacc'],
                                  require_prepared_sources=lambda flags, traversals: calls.append((flags, traversals)))
        coordinator = LLVMCoordinator.__new__(LLVMCoordinator)
        coordinator.options = {'target': 'aarch64', 'x11-source': '/x', 'base-cxx': True}
        coordinator.builder_type = OSReleaseBuilder
        coordinator.headers = headers
        coordinator.require_prepared_sources()
        (base_flags, base_walk), (final_flags, final_walk) = calls
        self.assertEqual((base_flags, base_walk), (headers.flags, prepared_sources.LIBS_TRAVERSALS))
        self.assertEqual(final_walk, prepared_sources.RELEASE_TRAVERSALS)
        for flag in ('MKX11=yes', 'MKCXX=yes', 'MKLIBCXX=yes', 'EMBER_BYACC_SOURCE=/prepared/byacc', 'EMBER_COMPILER_FAMILY=clang'):
            self.assertEqual(final_flags.count(flag), 1, flag)
        self.assertNotIn('MKX11=no', final_flags)
        self.assertNotIn('MKCXX=no', final_flags)

    def test_headers_phase_walks_nothing(self):
        from emberbsd_sdk.llvm_sysroot import LLVMPhaseBuilder
        builder = LLVMPhaseBuilder.__new__(LLVMPhaseBuilder)
        builder.options, builder.llvm_phase = {}, 'headers'
        self.assertEqual(builder.prepared_source_traversals(), ())
        builder.llvm_phase = 'base'
        self.assertEqual(builder.prepared_source_traversals(), prepared_sources.LIBS_TRAVERSALS)
