# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Plan diagnostics and native make guards for prepared-source selection.

A synthetic OS tree and a bmake stand-in answer ${SUBDIR} and
${.MAKE.MAKEFILES}; the makefiles carry the OS guard forms. The b504c-style
case exercises the optional --plan graph diagnostic. Ordinary build dispatch
still propagates a real makefile .error when it reaches an unselected source.
"""
import os
import hashlib
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_core import TemporaryCase
from emberbsd_sdk import prepared_sources
from emberbsd_sdk import process
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
GETTEXT = """.if defined(EMBER_GETTEXT_SOURCE)
.elif empty(.MAKEFLAGS:M-V*) && (make(all) || make(install))
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
        self.set_contract(builder, 'sed sed SED\n')
        return builder

    def set_contract(self, builder, extra):
        contract = self.os / 'contract'
        contract.write_text('EmberBSD-native-tools 2\ncmp cmp -\nreadlink readlink -\nmake make MAKE\nsh sh HOST_SH\n' + extra)
        builder.native_contract = str(contract)
        builder.native_contract_sha256 = hashlib.sha256(contract.read_bytes()).hexdigest()

    @staticmethod
    def source_flags(names):
        return [f'EMBER_{name.upper()}_SOURCE=/prepared/{name}' for name in names]

    def test_guards_name_their_targets(self):
        top = prepared_sources.guards(self.os / 'external/top/bin/Makefile')[0]
        self.assertEqual(top[0], ('EMBER_TOP_SOURCE',))
        self.assertTrue(top[1].applies({'all', 'dependall', 'install'}))
        self.assertFalse(top[1].applies({'includes'}))
        iscsi = prepared_sources.guards(self.os / 'external/iscsi/lib/Makefile')[0]
        self.assertEqual(iscsi[0], ('EMBER_ISCSI_SOURCE',))
        self.assertTrue(iscsi[1].applies({'includes'}))
        self.assertFalse(iscsi[1].applies({'all', 'dependall', 'install'}))
        gettext = prepared_sources.guards(self.os / 'external/gettext/Makefile')[0]
        self.assertEqual(gettext[0], ('EMBER_GETTEXT_SOURCE', 'EMBER_LIBXML2_SOURCE'))
        self.assertTrue(gettext[1].applies({'all', 'install'}))
        self.assertFalse(gettext[1].applies({'includes'}))
        self.assertEqual(prepared_sources.guards(self.os / 'external/byacc/Makefile.inc'),
                         [(('EMBER_BYACC_SOURCE',), None)])
        self.assertEqual(prepared_sources.source_option('EMBER_TCP_WRAPPERS_SOURCE'), 'tcp-wrappers-source')

    def test_guard_context_tracks_nested_elif_else_and_unknown_conditions(self):
        source = self.os / 'external/context/Makefile'
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(
            '.if make(obj)\n'
            '.if defined(UNRELATED)\n'
            '.endif\n'
            '.elif make(clean)\n'
            '.if defined(ANOTHER_UNRELATED)\n'
            '.endif\n'
            '.elif make(all)\n'
            '.else\n'
            '.error Set EMBER_CONTEXT_SOURCE to the prepared common Ports source\n'
            '.endif\n')
        variable, guard = prepared_sources.guards(source)[0]
        self.assertEqual(variable, ('EMBER_CONTEXT_SOURCE',))
        self.assertFalse(guard.applies({'obj'}))
        self.assertFalse(guard.applies({'clean'}))
        self.assertTrue(guard.applies({'includes'}))
        self.assertFalse(guard.applies({'all'}))

        nested = self.os / 'external/context/nested.mk'
        nested.write_text(
            '.if make(all)\n'
            '.if defined(INNER_CONDITION)\n'
            '.else\n'
            '.error Set EMBER_NESTED_SOURCE to the prepared common Ports source\n'
            '.endif\n'
            '.endif\n')
        nested_guard = prepared_sources.guards(nested)[0][1]
        self.assertTrue(nested_guard.applies({'all'}))
        self.assertFalse(nested_guard.applies({'includes'}))

        unsupported = self.os / 'external/context/unsupported.mk'
        unsupported.write_text(
            '.if ${HOST_PLATFORM} == "Darwin"\n'
            '.else\n'
            '.error Set EMBER_CONTEXT_SOURCE to the prepared common Ports source\n'
            '.endif\n')
        self.assertIsNone(prepared_sources.guards(unsupported)[0][1])

        unbalanced = self.os / 'external/context/unbalanced.mk'
        unbalanced.write_text(
            '.if make(obj)\n'
            '.error Set EMBER_CONTEXT_SOURCE to the prepared common Ports source\n')
        self.assertIsNone(prepared_sources.guards(unbalanced)[0][1])

    def test_builder_graph_uses_selected_sed_before_tools_exist(self):
        bmake, sed, shell = (shutil.which(name) for name in ('bmake', 'gsed', 'mksh'))
        if not all((bmake, sed, shell)):
            self.skipTest('selected bmake, gsed and mksh must be on PATH')
        root = self.root / 'sed-os'
        (root / 'share/mk').mkdir(parents=True)
        (root / 'share/mk/sys.mk').write_text('# fixture\n')
        config = root / 'config.h'
        config.write_text('#define SYSCONFDIR "/etc"\n')
        (root / 'Makefile').write_text(
            f'.SHELL: name=sh path={shell}\n'
            'TOOL_SED?= ${TOOLDIR}/bin/nbsed\n'
            f"PATH_VALUE!= ${{TOOL_SED}} -n -e 's/.*SYSCONFDIR //p' {config}\n"
            '.if empty(PATH_VALUE)\n.error missing sed output\n.endif\n'
            '.if empty(.MAKEFLAGS:M-V*) && make(all)\n'
            '.error Set EMBER_FIXTURE_SOURCE to the prepared source\n.endif\n'
            'SUBDIR=\n.PHONY: obj\nobj:\n\t@:\n')
        builder = self.builder(())
        builder.paths['os'] = str(root)
        builder.paths['tools'] = str(root / 'absent-tools')
        builder.environment = os.environ.copy()
        traversals = (('.', frozenset(('all',))),)
        baseline = prepared_sources.bmake_query(bmake, str(root),
            ['TOOLDIR=' + builder.paths['tools']], env=builder.environment)
        with self.assertRaises(SDKError):
            baseline(str(root))
        builder.native = SimpleNamespace(tool=lambda role: {'make': bmake, 'sed': sed}[role])
        with self.assertRaisesRegex(SDKError, '--fixture-source'):
            builder.require_prepared_sources([], traversals)
        builder.paths['fixture-source'] = '/prepared/fixture'
        found = builder.require_prepared_sources([], traversals)
        self.assertEqual(found, {'EMBER_FIXTURE_SOURCE': '.'})
        builder.native = SimpleNamespace(tool=lambda role: {'make': bmake, 'sed': '/missing/sed'}[role])
        with self.assertRaisesRegex(SDKError, 'cannot inspect OS directory'):
            builder.require_prepared_sources([], traversals)
        self.assertFalse(Path(builder.paths['tools']).exists())

    def test_graph_sed_follows_declared_contract(self):
        builder = self.builder(RELEASE_SOURCES)
        self.set_contract(builder, 'sed editor SED\n')
        seen = []
        def tool(role):
            seen.append(role)
            return '/host/' + role
        builder.native = SimpleNamespace(tool=tool)
        with patch('emberbsd_sdk.prepared_sources.process.capture', side_effect=self.fake_make) as run:
            builder.require_prepared_sources(self.source_flags(RELEASE_SOURCES),
                                             (('.', prepared_sources.BUILD_TARGETS),))
        self.assertIn('editor', seen)
        self.assertTrue(all('TOOL_SED=/host/editor' in call.args[0] for call in run.call_args_list))
        self.set_contract(builder, '')
        seen.clear()
        with patch('emberbsd_sdk.prepared_sources.process.capture', side_effect=self.fake_make) as run:
            builder.require_prepared_sources(self.source_flags(RELEASE_SOURCES),
                                             (('.', prepared_sources.BUILD_TARGETS),))
        self.assertNotIn('editor', seen)
        self.assertTrue(all(not any(arg.startswith('TOOL_SED=') for arg in call.args[0])
                            for call in run.call_args_list))
        self.set_contract(builder, 'sed editor SED\n')
        def unavailable(role):
            if role == 'editor':
                raise SDKError('native tool role is not selected: editor')
            return '/host/' + role
        builder.native = SimpleNamespace(tool=unavailable)
        with self.assertRaisesRegex(SDKError, 'native tool role is not selected: editor'):
            builder.require_prepared_sources([], (('.', prepared_sources.BUILD_TARGETS),))

    def test_mdns_else_guard_is_causally_required_by_make_targets(self):
        bmake = shutil.which('bmake')
        if not bmake:
            self.skipTest('selected bmake is not on PATH')
        shell = shutil.which('mksh')
        if not shell:
            self.skipTest('selected shell is not on PATH')
        root = self.root / 'mdns-os'
        (root / 'share/mk').mkdir(parents=True)
        (root / 'Makefile').write_text(
            f'.SHELL: name=sh path={shell}\n'
            'DIST= mDNSResponder-1.0\n'
            'SUBDIR=\n'
            '.include "Makefile.inc"\n'
            '.PHONY: obj clean cleandir all dependall install includes\n'
            'obj clean cleandir all dependall install includes:\n\t@:\n')
        (root / 'Makefile.inc').write_text(
            '.if defined(EMBER_MDNS_SOURCE)\n'
            '.if ${DIST} != "mDNSResponder-1.0"\n'
            'MDNS_DIST_MISMATCH=yes\n'
            '.endif\n'
            '.elif make(obj) && empty(.TARGETS:Nobj)\n'
            '.else\n'
            '.error Set EMBER_MDNS_SOURCE to the prepared common Ports mDNSResponder source\n'
            '.endif\n')
        query = prepared_sources.bmake_query(bmake, str(root), ['-r'], env=os.environ.copy())
        self.assertFalse(isinstance(query(str(root)), prepared_sources.Refused))
        for target in ('includes', 'all'):
            result = subprocess.run([bmake, '-r', '-C', str(root), target],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Set EMBER_MDNS_SOURCE', result.stderr)
        obj_needed = prepared_sources.required_sources(str(root), query, (('.', frozenset(('obj',))),), jobs=1)
        self.assertEqual(obj_needed, {})
        build_needed = prepared_sources.required_sources(
            str(root), query, (('.', frozenset(('includes',))),), jobs=1)
        self.assertEqual(build_needed, {'EMBER_MDNS_SOURCE': '.'})
        accepted = subprocess.run([bmake, '-r', '-C', str(root),
                                   'EMBER_MDNS_SOURCE=/prepared/mdns', 'all'],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)

    def test_guard_parser_ignores_recipe_indented_directives(self):
        bmake = shutil.which('bmake')
        if not bmake:
            self.skipTest('selected bmake is not on PATH')
        shell = shutil.which('mksh')
        if not shell:
            self.skipTest('selected shell is not on PATH')
        root = self.root / 'recipe-guard'
        root.mkdir()
        makefile = root / 'Makefile'
        makefile.write_text(
            f'.SHELL: name=sh path={shell}\n'
            '.PHONY: obj includes\n'
            'obj:\n'
            '\t@:\n'
            '\t.if make(obj)\n'
            '.error Set EMBER_RECIPE_SOURCE to the prepared common Ports source\n'
            '\t.endif\n'
            'includes:\n'
            '\t@:\n')
        result = subprocess.run([bmake, '-r', '-C', str(root), '-V', 'X', 'includes'],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Set EMBER_RECIPE_SOURCE', result.stderr)
        variables, condition = prepared_sources.guards(makefile)[0]
        self.assertEqual(variables, ('EMBER_RECIPE_SOURCE',))
        self.assertIsNone(condition)

    def test_target_projection_handles_negated_guards_and_multi_target_calls(self):
        conditions = (
            ('!make(includes)', {'all'}, {'includes'}),
            ('!defined(EMBER_SOURCE) && make(includes)', {'includes'}, {'all'}),
            ('make(all) && make(install)', {'all', 'install'}, {'all', 'dependall'}),
        )
        for expression, included, excluded in conditions:
            with self.subTest(expression=expression):
                guard = prepared_sources.TargetCondition(expression)
                self.assertTrue(guard.applies(included))
                self.assertFalse(guard.applies(excluded))
        quoted = prepared_sources.TargetCondition('"make(all) || make(install)" && make(includes)')
        self.assertTrue(quoted.applies({'includes'}))
        self.assertFalse(quoted.applies({'all'}))
        function_argument = prepared_sources.TargetCondition('empty(.MAKEFLAGS:Mfoo&&bar) && make(includes)')
        self.assertTrue(function_argument.applies({'includes'}))
        self.assertFalse(function_argument.applies({'all'}))
        trailing_comment = prepared_sources.TargetCondition('make(all) # comment && make(includes)')
        self.assertTrue(trailing_comment.applies({'all'}))
        self.assertFalse(trailing_comment.applies({'includes'}))
        quoted_hash = prepared_sources.TargetCondition('make(all) && "#" == "#"')
        self.assertTrue(quoted_hash.applies({'includes'}))
        escaped_hash = prepared_sources.TargetCondition(r'make(all) \# && make(includes)')
        self.assertTrue(escaped_hash.applies({'includes'}))
        for modifier in ('N[^x]*', 'N[!x]*', 'N${TARGET}'):
            with self.subTest(modifier=modifier):
                unsupported_filter = prepared_sources.TargetCondition(
                    f'empty(.TARGETS:{modifier}) && make(obj)')
                self.assertTrue(unsupported_filter.applies({'obj'}))
        self.assertTrue(prepared_sources.TargetCondition('make(foo*)').applies({'obj'}))
        self.assertTrue(prepared_sources.TargetCondition('make(all) &&').applies({'obj'}))
        unsupported = prepared_sources.TargetCondition('make(${TARGET}) && trailing')
        self.assertTrue(unsupported.applies({'obj'}))

    def test_heimdal_negative_group_is_causally_required_by_the_graph(self):
        bmake = shutil.which('bmake')
        if not bmake:
            self.skipTest('selected bmake is not on PATH')
        shell = shutil.which('mksh')
        if not shell:
            self.skipTest('selected shell is not on PATH')
        root = self.root / 'heimdal-os'
        crypto = root / 'crypto'
        (root / 'share/mk').mkdir(parents=True)
        crypto.mkdir(parents=True)
        (root / 'Makefile').write_text('SUBDIR= crypto\n')
        (crypto / 'Makefile').write_text(
            f'.SHELL: name=sh path={shell}\n'
            'SUBDIR=\n'
            '.include "Makefile.inc"\n'
            '.PHONY: obj clean cleandir all dependall install includes\n'
            'obj clean cleandir all dependall install includes:\n\t@:\n')
        (crypto / 'Makefile.inc').write_text(
            '.if (!defined(EMBER_HEIMDAL_SOURCE) || empty(EMBER_HEIMDAL_SOURCE)) && \\\n'
            '    empty(.MAKEFLAGS:M-V*) && \\\n'
            '    !((make(obj) || make(clean) || make(cleandir)) && \\\n'
            '    empty(.TARGETS:Nobj:Nclean:Ncleandir)) # comment && make(clean)\n'
            '.error Set EMBER_HEIMDAL_SOURCE to the prepared common Heimdal source\n'
            '.endif\n')
        query = prepared_sources.bmake_query(bmake, str(root), ['-r'], env=os.environ.copy())
        # The actual bmake -V obj query is intentionally suppressed by the
        # guard, while normal build targets are refused without the source.
        query_result = query(str(crypto))
        self.assertFalse(isinstance(query_result, prepared_sources.Refused))
        for target in ('obj', 'clean', 'cleandir'):
            result = subprocess.run([bmake, '-r', '-C', str(crypto), target],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        for target in ('all', 'dependall', 'install'):
            result = subprocess.run([bmake, '-r', '-C', str(crypto), target],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Set EMBER_HEIMDAL_SOURCE', result.stderr)
        accepted = subprocess.run([bmake, '-r', '-C', str(crypto),
                                   'EMBER_HEIMDAL_SOURCE=/prepared/heimdal', 'all'],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        needed = prepared_sources.required_sources(
            str(root), query, (('crypto', prepared_sources.BUILD_TARGETS),), jobs=1)
        self.assertEqual(needed.get('EMBER_HEIMDAL_SOURCE'), 'crypto')

    def test_required_sources_uses_shared_job_resolution(self):
        with patch('emberbsd_sdk.prepared_sources.resolve_jobs', return_value=3) as resolve:
            result = prepared_sources.required_sources(str(self.os), lambda _directory: ([], []),
                                                       [('.', ('obj',))])
        self.assertEqual(result, {})
        resolve.assert_called_once_with(None)

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
        coordinator.options = {'target': 'aarch64', 'x11-source': '/x', 'base-cxx': True, 'plan': True}
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

    def test_normal_coordinator_skips_extra_graph_walk(self):
        headers = SimpleNamespace(flags=[], require_prepared_sources=lambda *_: self.fail(
            'normal build must leave prepared-source refusal to the OS make guards'))
        coordinator = LLVMCoordinator.__new__(LLVMCoordinator)
        coordinator.options = {'target': 'aarch64', 'plan': False}
        coordinator.builder_type = OSReleaseBuilder
        coordinator.headers = headers
        coordinator.require_prepared_sources()

    def test_normal_run_dispatch_propagates_native_os_source_guard(self):
        bmake = shutil.which('bmake')
        if not bmake:
            self.skipTest('native bmake is not on PATH')
        guard = self.os / 'guard'
        guard.mkdir()
        (guard / 'Makefile').write_text(
            '.if !defined(EMBER_MDNS_SOURCE) && make(all)\n'
            '.error Set EMBER_MDNS_SOURCE to the prepared common Ports source\n'
            '.endif\n')
        builder = self.builder(())
        builder.options['plan'] = False
        builder.options['runtime-work'] = '/unused/runtime-work'
        builder.options['compiler-family'] = 'gcc'
        builder.paths['destination'] = str(self.root / 'destination')
        builder.commands = []
        builder.receipt = {}
        builder.log = open(self.root / 'build.log', 'wb')
        self.addCleanup(builder.log.close)
        builder.environment = os.environ.copy()
        builder.process_inspector = process.SessionInspector(env=builder.environment)
        builder.save_receipt = lambda: None
        builder.interrupted = lambda: None
        command = [bmake, '-C', str(guard), 'all']
        builder.preflight = lambda: None
        builder._run_preflighted = lambda: builder.dispatch(command)
        with self.assertRaisesRegex(SDKError, 'build command failed'):
            builder.run()

    def test_c_foundation_preflight_includes_host_tool_sources(self):
        from emberbsd_sdk.llvm_sysroot import LLVMPhaseBuilder
        (self.os / 'tools/host/Makefile').write_text(BYACC)
        builder = LLVMPhaseBuilder.__new__(LLVMPhaseBuilder)
        builder.options, builder.llvm_phase = {'c-foundation': True}, 'headers'
        needed = self.walk([], builder.prepared_source_traversals())
        self.assertEqual(needed.get('EMBER_BYACC_SOURCE'), 'tools/host')
        self.assertIn('--byacc-source', prepared_sources.missing_sources(needed, {})[0])

    def test_c_foundation_requires_heimdal_before_plan_or_build(self):
        coordinator = LLVMCoordinator.__new__(LLVMCoordinator)
        coordinator.headers = SimpleNamespace(flags=[], require_prepared_sources=lambda *_: None)
        for plan in (True, False):
            with self.subTest(plan=plan):
                coordinator.options = {'c-foundation': True, 'plan': plan}
                with self.assertRaisesRegex(SDKError, r'C foundation requires --heimdal-source'):
                    coordinator.require_prepared_sources()

        calls = []
        coordinator.options = {'c-foundation': True, 'plan': True,
                                'heimdal-source': '/prepared/heimdal'}
        coordinator.headers = SimpleNamespace(flags=[], require_prepared_sources=lambda *args: calls.append(args))
        coordinator.require_prepared_sources()
        self.assertEqual(calls, [([], prepared_sources.TOOLS_TRAVERSALS)])

    def test_ordinary_headers_preflight_includes_host_tool_sources(self):
        from emberbsd_sdk.llvm_sysroot import LLVMPhaseBuilder
        (self.os / 'tools/host/Makefile').write_text(BYACC)
        builder = LLVMPhaseBuilder.__new__(LLVMPhaseBuilder)
        builder.options, builder.llvm_phase = {}, 'headers'
        needed = self.walk([], builder.prepared_source_traversals())
        self.assertEqual(needed.get('EMBER_BYACC_SOURCE'), 'tools/host')
        self.assertIn('--byacc-source', prepared_sources.missing_sources(needed, {})[0])

    def test_host_tool_source_query_routes_selected_yacc(self):
        (self.os / 'tools/host/Makefile').write_text(BYACC)
        builder = self.builder(('byacc',))
        import hashlib
        contract = self.os / 'contract'
        contract.write_text('EmberBSD-native-tools 2\ncmp cmp -\nreadlink readlink -\nmake make MAKE\nsh sh HOST_SH\nyacc yacc HOST_YACC\ncp cp HOST_MV_CP\nrm rm HOST_MV_RM\n')
        builder.native_contract = str(contract)
        builder.native_contract_sha256 = hashlib.sha256(contract.read_bytes()).hexdigest()
        with patch('emberbsd_sdk.prepared_sources.process.capture', side_effect=self.fake_make) as run:
            builder.require_prepared_sources(self.source_flags(('byacc',)),
                                             (('tools', prepared_sources.BUILD_TARGETS),))
        for call in run.call_args_list:
            for flag in ('HOST_YACC=/host/yacc', 'HOST_MV_CP=/host/cp', 'HOST_MV_RM=/host/rm'):
                self.assertIn(flag, call.args[0])

    def test_headers_phase_walks_host_tools_only(self):
        from emberbsd_sdk.llvm_sysroot import LLVMPhaseBuilder
        builder = LLVMPhaseBuilder.__new__(LLVMPhaseBuilder)
        builder.options, builder.llvm_phase = {}, 'headers'
        self.assertEqual(builder.prepared_source_traversals(), prepared_sources.TOOLS_TRAVERSALS)
        builder.llvm_phase = 'base'
        self.assertEqual(builder.prepared_source_traversals(), prepared_sources.TOOLS_TRAVERSALS + prepared_sources.LIBS_TRAVERSALS)
