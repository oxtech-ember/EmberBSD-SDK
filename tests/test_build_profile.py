# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Build profiles and the build-graph-v2 plan."""

import ast
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import platform
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_core import TemporaryCase
from emberbsd_sdk import build_profile, prepared_sources
from emberbsd_sdk.errors import SDKError

PROFILE = {
    'schema': 'emberbsd.build-profile.v1',
    'platform': {'target': 'aarch64', 'compiler': {'host': 'clang', 'target': 'clang'}},
    'os': {'kernels': ['EMBER64']},
    'ports': {},
}
SOURCES = {
    'EMBER_BYACC_SOURCE': {'package': 'devel/byacc', 'producer': 'ports:scripts/source-inputs/byacc'},
    'EMBER_HEIMDAL_SOURCE': {'package': 'security/heimdal', 'producer': 'ports:scripts/source-inputs/heimdal'},
    'EMBER_LIBEDIT_SOURCE': {'package': 'devel/editline', 'producer': 'sdk:scripts/prepare-libedit.py'},
    'EMBER_LLVM_SOURCE': {'package': 'lang/compiler-rt', 'producer': 'ports:pkgsrc/lang/compiler-rt/files/source.py'},
}

# Ports tools of the plan. The fixture recipe identity is the hash of the
# recipe files; the export fingerprint is the hash of pkgsrc/export.conf.
RECIPE_TOOL = '''import hashlib, json, os, re, sys
ports = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
arguments = sys.argv[1:]
result = {}
for index, word in enumerate(arguments):
    if word != '--describe':
        continue
    package = arguments[index + 1]
    data = b''
    for name in ('Makefile', 'distinfo'):
        path = os.path.join(ports, 'pkgsrc', package, name)
        data += open(path, 'rb').read() if os.path.isfile(path) else b''
    names = re.findall(r'^SHA512 [(]([^)]+)[)]', data.decode(), re.M)
    result[package] = dict(package=package, effective=dict(sha256=hashlib.sha256(data).hexdigest()),
                           distfiles=sorted(set(names)))
print(json.dumps(result))
'''
EXPORT_TOOL = '''import hashlib, json, os, sys
ports = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
fingerprint = hashlib.sha256(open(os.path.join(ports, 'pkgsrc/export.conf'), 'rb').read()).hexdigest()
if sys.argv[1] == 'prepare':
    directory = os.path.join(sys.argv[2], fingerprint)
    os.makedirs(directory, exist_ok=True)
    print(json.dumps(dict(directory=directory, fingerprint=fingerprint)))
else:
    print(json.dumps(dict(fingerprint=fingerprint)))
'''


def git(root, *arguments):
    subprocess.run(['git', '-C', str(root), *arguments], check=True, capture_output=True,
                   env=dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@example.org',
                            GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@example.org',
                            GIT_AUTHOR_DATE='1700000000 +0000', GIT_COMMITTER_DATE='1700000000 +0000'))


class ProfileFixture(TemporaryCase):
    """OS, Ports and SDK checkouts and a host prefix for build plans."""

    def setUp(self):
        super().setUp()
        table = ' \\\n    '.join(f'{name} /usr/src/external/x' for name in SOURCES)
        self.os = self.repository('os', {
            'share/mk/bsd.own.mk': 'MKCXX?= yes\nMKLINT?= no\nMKNTP?= yes\nMKX11?= no\n',
            'share/mk/bsd.sys.mk': f'.for _var _path in {table} \\\n    EXTERNAL_TOOLCHAIN /usr/toolchain\n.if 1\n.endif\n',
            'ember/shared-sources.json': json.dumps({
                'schema': 'emberbsd.os-shared-sources.v1', 'sources': SOURCES,
                'kernel': {'default': ['EMBER_LLVM_SOURCE'], 'configs': {}},
                'release': {'select': ['EMBER_LIBEDIT_SOURCE', 'EMBER_LLVM_SOURCE']}}),
            'sys/arch/evbarm/conf/EMBER64': 'include "arch/evbarm/conf/std"\n',
            'tools/native-tools/contract': 'EmberBSD-native-tools 2\ncmp cmp -\nreadlink readlink -\n'
                                           'make make -\nsh sh -\n',
            'ember/tools/shared-sources.py': self.os_tool()})
        self.ports = self.repository('ports', {
            'scripts/source-inputs/byacc/source.py': "PACKAGE = 'devel/byacc'\n",
            'scripts/source-inputs/heimdal/source.py': "PACKAGE = 'security/heimdal'\n",
            'pkgsrc/lang/compiler-rt/files/source.py': "SOURCE_MANIFEST = 'llvm-source.json'\n",
            'scripts/source-inputs/common/tar_archive.py': 'pass\n',
            'pkgsrc/devel/byacc/Makefile': 'DISTNAME= byacc\n',
            'pkgsrc/security/heimdal/Makefile': 'DISTNAME= heimdal\n',
            'pkgsrc/lang/compiler-rt/Makefile': 'DISTNAME= compiler-rt\n',
            'pkgsrc/devel/editline/Makefile': 'DISTNAME= editline\n',
            'pkgsrc/export.conf': 'export 1\n',
            'scripts/prepared-source-current.py': RECIPE_TOOL,
            'scripts/pkgsrc-export-stage.py': EXPORT_TOOL,
            **{f'pkgsrc/{package}/distinfo': f'SHA512 ({name}) = 00\nSize ({name}) = 1 bytes\n'
                                             f'SHA1 (patch-a) = 00\n'
               for package, name in (('devel/byacc', 'byacc-1.tgz'), ('security/heimdal', 'heimdal-7.tar.gz'),
                                     ('lang/compiler-rt', 'llvm-project-23.src.tar.xz'))}})
        self.sdk = self.repository('sdk', {'scripts/prepare-libedit.py': 'pass\n',
                                           'src/emberbsd_sdk/libedit.py': '# libedit 0\n'})
        prefix = self.root / 'prefix'
        for name in ('bin/clang', 'bin/bmake', 'bin/ld.lld'):
            self.write('prefix/' + name, '#!/bin/sh\n', 0o755)
        self.write('prefix/pkgdb/clang-21.1/+CONTENTS', 'bin/clang\nbin/ld.lld\n')
        self.write('prefix/pkgdb/clang-21.1/+BUILD_VERSION', 'clang 1\n')
        self.write('prefix/pkgdb/bmake-2026/+CONTENTS', 'bin/bmake\n')
        self.write('prefix/pkgdb/bmake-2026/+BUILD_VERSION', 'bmake 1\n')
        tools = {'cc': str(prefix / 'bin/clang'), 'make': str(prefix / 'bin/bmake')}
        self.native = SimpleNamespace(tools=tools, bundle_root=str(prefix), tool=tools.__getitem__,
                                      host=dict(os='Darwin', cpu='aarch64'), abi_flags={'cflags': ()})
        self.bfd = self.write('binutils/host-bfd.mk', 'BFD= /binutils\n')
        self.write('binutils/inputs.txt', 'configure=--target=aarch64--netbsd\n')
        self.walks = []

    def repository(self, name, files):
        root = self.root / name
        for path, text in files.items():
            self.write(f'{name}/{path}', text)
        git(root, 'init', '-q')
        git(root, 'add', '-A')
        git(root, 'commit', '-q', '-m', 'fixture')
        return root

    def os_tool(self):
        # The OS tool checks the file against bsd.sys.mk; its own test covers that.
        # This fixture prints the file, or refuses it with the OS message.
        return ('import json, sys\n'
                'doc = json.load(open(sys.argv[2]))\n'
                'if doc.get("refuse"):\n'
                '    sys.exit("shared-sources: " + doc["refuse"])\n'
                'print(json.dumps(doc))\n')

    def required_sources(self, traversals, flags):
        self.walks.append(traversals)
        return {'EMBER_BYACC_SOURCE': 'tools/yacc', 'EMBER_HEIMDAL_SOURCE': 'tools/asn1_compile',
                **getattr(self, 'tools_sources', {})} if traversals and traversals[0][0] == 'tools' else {}

    def plan(self, profile=PROFILE, **options):
        return build_profile.make_plan(
            copy.deepcopy(profile), os_root=self.os, ports_root=self.ports, sdk_root=self.sdk,
            host_prefix=self.native.bundle_root, host_bfd_config=str(self.bfd),
            required_sources=self.required_sources, native=self.native, **options)

    def nodes(self, plan):
        return {node['id']: node for node in plan['nodes']}


class ProducerContractTests(unittest.TestCase):
    def test_sdk_producer_groups_cover_imports_and_python_helpers(self):
        # The CLI also dispatches package installation, publishing and unrelated
        # source producers. These imports have separate entry points.
        excluded = {
            'scripts/packages.py': {'package_selection'},
            'build_profile': {'libedit', 'os_release_builder'},
            'profile_build': {'libedit', 'os_release_builder', 'sysroot_builder'},
            'sdk_client': {'binary_catalog', 'binary_release', 'desktop_apps', 'package_auto_update',
                           'package_automatic', 'package_cache', 'package_install', 'package_runtime', 'package_selection',
                           'package_state', 'package_updates', 'release_delivery', 'release_s3',
                           'release_signing', 'source_wave', 'sysroot_package'},
            'os_release': {'os_release_github', 'private_root'},
        }
        package = Path(build_profile.__file__).parent
        available = {path.stem for path in package.glob('*.py')}
        common = set(build_profile.OS_MODULES)
        groups = [(build_profile.SOURCE_MODULES, set(build_profile.SOURCE_MODULES)),
                  (build_profile.OS_SCRIPTS, common), (build_profile.OS_MODULES, common),
                  (build_profile.SETS_MODULES, common | set(build_profile.SETS_MODULES))]
        for names, covered in groups:
            for name in names:
                path = package.parents[1] / name if name.endswith('.py') else package / (name + '.py')
                imports = set()
                for node in ast.walk(ast.parse(path.read_bytes())):
                    if isinstance(node, ast.ImportFrom):
                        if (node.level == 1 and not node.module) or node.module == 'emberbsd_sdk':
                            imports.update(alias.name for alias in node.names)
                        elif node.level == 1 or (node.module or '').startswith('emberbsd_sdk.'):
                            imports.add(node.module.split('.')[-1])
                    elif isinstance(node, ast.Import):
                        imports.update(x.name.split('.')[1] for x in node.names if x.name.startswith('emberbsd_sdk.'))
                    elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.endswith('.py'):
                        # Bindings snapshot and process helpers executed as separate Python programs.
                        imports.add(node.value.removesuffix('.py'))
                with self.subTest(producer=name):
                    missing = (imports & available) - covered - excluded.get(name, set())
                    self.assertEqual(missing, set(), 'declare the invoked SDK module in its producer group')


class BuildProfileTests(ProfileFixture):
    def test_kernel_profile_plans_tools_sources_and_kernel_in_order(self):
        plan = self.plan()
        nodes = self.nodes(plan)
        self.assertEqual(set(nodes), {
            'runtime:host-prefix', 'runtime:target-toolchain', 'runtime:host-binutils', 'source:EMBER_BYACC_SOURCE',
            'source:EMBER_HEIMDAL_SOURCE', 'source:EMBER_LLVM_SOURCE', 'os:tools', 'os:kernel:EMBER64',
            'ports:pkgsrc-export'})
        # The Ports producers share one pkgsrc export.
        self.assertEqual(nodes['source:EMBER_BYACC_SOURCE']['dependencies'], ['ports:pkgsrc-export'])
        self.assertEqual(nodes['source:EMBER_BYACC_SOURCE']['identity']['distfiles'], ['byacc-1.tgz'])
        kernel = nodes['os:kernel:EMBER64']
        self.assertEqual(kernel['dependencies'], ['os:tools', 'runtime:host-prefix', 'runtime:target-toolchain',
                                                  'source:EMBER_LLVM_SOURCE'])
        self.assertEqual(kernel['reasons'], ['profile os.kernels'])
        self.assertEqual(kernel['outputs'], ['netbsd-EMBER64', 'netbsd-EMBER64.img'])
        self.assertEqual(nodes['source:EMBER_BYACC_SOURCE']['reasons'], ['needed by os:tools (tools/yacc)'])
        self.assertEqual(nodes['source:EMBER_LLVM_SOURCE']['reasons'], ['needed by os:kernel:EMBER64'])
        self.assertEqual(nodes['os:tools']['reasons'], ['needed by os:kernel:EMBER64'])
        self.assertEqual({node['id']: node['decision']['action'] for node in plan['nodes']}, {
            'runtime:host-prefix': 'ready', 'runtime:target-toolchain': 'ready', 'runtime:host-binutils': 'ready',
            'source:EMBER_BYACC_SOURCE': 'build', 'source:EMBER_HEIMDAL_SOURCE': 'build',
            'source:EMBER_LLVM_SOURCE': 'build', 'os:tools': 'build', 'os:kernel:EMBER64': 'build',
            'ports:pkgsrc-export': 'build'})
        position = {node_id: index for index, wave in enumerate(plan['waves']) for node_id in wave}
        for node in plan['nodes']:
            for dependency in node['dependencies']:
                self.assertLess(position[dependency], position[node['id']])
        # Keys use prefix-relative tool paths, so a moved prefix keeps its keys.
        self.assertEqual(nodes['runtime:host-prefix']['inputs']['packages']['bin/clang']['package'], 'clang-21.1')
        self.assertEqual(set(nodes['runtime:target-toolchain']['inputs']['packages']), {'bin/clang', 'bin/ld.lld'})

    def test_prefix_without_the_target_linker_is_refused(self):
        (Path(self.native.bundle_root) / 'bin/ld.lld').unlink()
        with self.assertRaisesRegex(SDKError, 'no Clang target toolchain'):
            self.plan()

    def test_same_inputs_give_the_same_plan(self):
        self.assertEqual(self.plan(), self.plan())

    def test_producer_change_rekeys_only_its_source_and_consumers(self):
        before = self.nodes(self.plan())
        self.write('ports/pkgsrc/devel/byacc/Makefile', 'DISTNAME= byacc-2\n')
        git(self.ports, 'commit', '-q', '-am', 'byacc 2')
        after = self.nodes(self.plan())
        changed = {node_id for node_id in before if before[node_id]['input_key'] != after[node_id]['input_key']}
        self.assertEqual(changed, {'source:EMBER_BYACC_SOURCE', 'os:tools', 'os:kernel:EMBER64'})

    def test_new_export_inputs_rekey_every_ports_source(self):
        before = self.nodes(self.plan())
        self.write('ports/pkgsrc/export.conf', 'export 2\n')
        git(self.ports, 'commit', '-q', '-am', 'export 2')
        after = self.nodes(self.plan())
        changed = {node_id for node_id in before if before[node_id]['input_key'] != after[node_id]['input_key']}
        # The LLVM producer does not use the shared export.
        self.assertEqual(changed, {'ports:pkgsrc-export', 'source:EMBER_BYACC_SOURCE', 'source:EMBER_HEIMDAL_SOURCE',
                                   'os:tools', 'os:kernel:EMBER64'})

    def test_refusal_of_a_ports_tool_stops_the_plan(self):
        self.write('ports/scripts/prepared-source-current.py', 'import sys\nsys.exit("recipe of x is unknown")\n')
        git(self.ports, 'commit', '-q', '-am', 'broken tool')
        with self.assertRaisesRegex(SDKError, 'prepared-source-current.py: recipe of x is unknown'):
            self.plan()

    def test_buildinfo_rekeys_the_kernel_but_not_the_tools(self):
        before = self.nodes(self.plan())
        profile = copy.deepcopy(PROFILE)
        profile['os']['buildinfo'] = 'EmberBSD:test-1'
        after = self.nodes(self.plan(profile))
        self.assertEqual(before['os:tools']['input_key'], after['os:tools']['input_key'])
        self.assertNotEqual(before['os:kernel:EMBER64']['input_key'], after['os:kernel:EMBER64']['input_key'])

    def test_changed_producer_files_force_a_build_and_are_named(self):
        self.write('ports/scripts/source-inputs/heimdal/source.py', "PACKAGE = 'security/heimdal'\n# local\n")
        nodes = self.nodes(self.plan())
        self.assertEqual(nodes['source:EMBER_HEIMDAL_SOURCE']['decision'], dict(
            action='build', why='changed worktree inputs: ports:scripts/source-inputs/heimdal'))
        # A local edit has no reusable key, and neither do its consumers.
        self.assertIsNone(nodes['source:EMBER_HEIMDAL_SOURCE']['input_key'])
        self.assertIsNone(nodes['os:tools']['input_key'])
        self.assertEqual(nodes['os:tools']['decision']['why'], 'changed dependencies: source:EMBER_HEIMDAL_SOURCE')
        self.assertIsNotNone(nodes['source:EMBER_BYACC_SOURCE']['input_key'])

    def test_a_rename_into_a_producer_path_is_a_change(self):
        self.write('ports/notes.txt', 'note\n')
        git(self.ports, 'add', 'notes.txt')
        git(self.ports, 'commit', '-q', '-m', 'notes')
        git(self.ports, 'mv', 'notes.txt', 'scripts/source-inputs/byacc/notes.txt')
        self.assertIsNone(self.nodes(self.plan())['source:EMBER_BYACC_SOURCE']['input_key'])

    def test_recorded_result_with_the_same_key_is_ready(self):
        key = self.nodes(self.plan())['source:EMBER_LLVM_SOURCE']['input_key']
        state = self.root / 'state'
        output = self.write('state/llvm/llvm-source.json', '{}').parent
        record = self.write('state/results/source_EMBER_LLVM_SOURCE.json', json.dumps(
            {'input_key': key, 'outputs': {'EMBER_LLVM_SOURCE': str(output)}}))
        self.write('state/results/os_tools.json', json.dumps({'input_key': '0' * 64, 'outputs': {}}))
        nodes = self.nodes(self.plan(state=state))
        self.assertEqual(nodes['source:EMBER_LLVM_SOURCE']['decision']['action'], 'ready')
        self.assertEqual(nodes['os:tools']['decision']['action'], 'build')
        # A record whose outputs are gone is not a result.
        (output / 'llvm-source.json').unlink()
        output.rmdir()
        self.assertEqual(self.nodes(self.plan(state=state))['source:EMBER_LLVM_SOURCE']['decision']['action'], 'build')
        self.assertTrue(record.is_file())

    def test_sets_walk_the_release_tree_and_share_the_tools(self):
        profile = copy.deepcopy(PROFILE)
        profile['os']['sets'] = ['base', 'comp', 'etc']
        nodes = self.nodes(self.plan(profile))
        self.assertEqual(nodes['os:sets']['outputs'], ['base.tar.xz', 'comp.tar.xz', 'etc.tar.xz'])
        self.assertIn('os:tools', nodes['os:sets']['dependencies'])
        self.assertEqual(sorted(nodes['os:tools']['reasons']),
                         ['needed by os:kernel:EMBER64', 'needed by os:sets'])
        self.assertIn(prepared_sources.RELEASE_TRAVERSALS, self.walks)

    def test_sets_select_the_release_sources_that_the_os_names(self):
        profile = copy.deepcopy(PROFILE)
        profile['os']['sets'] = ['base', 'comp', 'etc']
        sets = self.nodes(self.plan(profile))['os:sets']
        self.assertIn('source:EMBER_LIBEDIT_SOURCE', sets['dependencies'])
        self.assertIn('source:EMBER_LLVM_SOURCE', sets['dependencies'])
        document = json.loads((self.os / 'ember/shared-sources.json').read_text())
        document['release']['select'] = ['EMBER_LLVM_SOURCE']
        self.write('os/ember/shared-sources.json', json.dumps(document))
        git(self.os, 'commit', '-q', '-am', 'release selects LLVM only')
        sets = self.nodes(self.plan(profile))['os:sets']
        self.assertNotIn('source:EMBER_LIBEDIT_SOURCE', sets['dependencies'])

    def test_sets_are_the_release_sets_without_buildinfo_and_cxx_options(self):
        # The OS release builder makes base, comp and etc together, sets
        # BUILDINFO itself and has the C++ runtime of LLVM.
        for change, message in (
                (dict(sets=['base', 'etc']), 'os.sets must be base, comp, etc'),
                (dict(sets=['base', 'comp', 'etc'], buildinfo='EmberBSD:test-1'), 'os.buildinfo'),
                (dict(sets=['base', 'comp', 'etc'], mk={'MKCXX': 'no', 'MKX11': 'yes'}), 'accept MKCXX, MKX11:')):
            profile = copy.deepcopy(PROFILE)
            profile['os'].update(change)
            with self.subTest(change=change), self.assertRaisesRegex(SDKError, message):
                self.plan(profile)

    def test_documented_profiles_are_valid(self):
        for path in sorted(Path(__file__).resolve().parents[1].glob('docs/profiles/*.json')):
            with self.subTest(path=path.name):
                build_profile.load_profile(path)

    def test_summary_names_each_node_to_build_and_its_reason(self):
        lines = build_profile.summary(self.plan()).splitlines()
        self.assertRegex(lines[0], r'\Abuild \d+; ready \d+\Z')
        self.assertIn('  build os:kernel:EMBER64: no recorded result has this input key', lines)

    def test_profile_refuses_make_options_that_the_os_does_not_have(self):
        profile = copy.deepcopy(PROFILE)
        profile['os'].update(mk={'MKNTP': 'no', 'MKNPT': 'no'})
        with self.assertRaisesRegex(SDKError, 'does not have \\(share/mk\\): MKNPT\\Z'):
            self.plan(profile)

    def test_profile_make_options_replace_the_release_options_of_the_sets(self):
        profile = copy.deepcopy(PROFILE)
        profile['os'].update(sets=['base', 'comp', 'etc'], mk={'MKNTP': 'no', 'MKLINT': 'yes'})
        flags = self.nodes(self.plan(profile))['os:sets']['inputs']['flags']
        self.assertIn('MKNTP=no', flags)
        self.assertEqual([flag for flag in flags if flag.startswith('MKLINT=')], ['MKLINT=yes'])

    def test_profile_refuses_paths_versions_and_ports_roots(self):
        for change, message in (
                (lambda p: p['platform'].update(prefix='/opt/x'), 'needs target and compiler'),
                (lambda p: p['os'].update(kernels=['../EMBER64']), 'os.kernels'),
                (lambda p: p['os'].update(mk={'MKX11': '1.2'}), 'os.mk'),
                (lambda p: p['ports'].update(packages=['devel/example@native']), 'ports must be empty'),
                (lambda p: p['platform'].update(board='a133'), 'needs target and compiler'),
                (lambda p: p['os'].update(kernels=[]), 'no kernel and no set'),
                (lambda p: p.update(extra=1), 'platform, os and ports')):
            profile = copy.deepcopy(PROFILE)
            change(profile)
            with self.subTest(message=message), self.assertRaisesRegex(SDKError, message):
                build_profile.validate_profile(profile)

    def test_os_refusal_of_its_shared_sources_stops_the_plan(self):
        document = json.loads((self.os / 'ember/shared-sources.json').read_text())
        document['refuse'] = 'the sources differ from the bsd.sys.mk table: EMBER_X_SOURCE'
        self.write('os/ember/shared-sources.json', json.dumps(document))
        with self.assertRaisesRegex(SDKError, 'differ from the bsd.sys.mk table: EMBER_X_SOURCE'):
            self.plan()

    def test_producer_must_prepare_the_declared_package(self):
        self.write('ports/scripts/source-inputs/byacc/source.py', "PACKAGE = 'devel/bison'\n")
        with self.assertRaisesRegex(SDKError, 'prepares devel/bison, not devel/byacc'):
            self.plan()

    def test_a_kernel_configuration_outside_the_os_is_keyed_by_its_directory(self):
        trials = Path(self.root, 'devboards/kernels')
        trials.mkdir(parents=True)
        (trials / 'EMBER64_TRIAL').write_text('include "arch/evbarm/conf/EMBER64"\n')
        profile = copy.deepcopy(PROFILE)
        profile['os']['kernels'] = ['EMBER64_TRIAL']
        before = self.nodes(self.plan(profile, kernel_configs=[str(trials)]))['os:kernel:EMBER64_TRIAL']
        (trials / 'README.md').write_text('A trial.\n')
        same = self.nodes(self.plan(profile, kernel_configs=[str(trials)]))['os:kernel:EMBER64_TRIAL']
        self.assertEqual(before['input_key'], same['input_key'])
        (trials / 'TRIAL_OPTIONS').write_text('options TRIAL\n')
        after = self.nodes(self.plan(profile, kernel_configs=[str(trials)]))['os:kernel:EMBER64_TRIAL']
        self.assertNotEqual(before['input_key'], after['input_key'])
        with self.assertRaisesRegex(SDKError, 'EMBER64_TRIAL is missing for evbarm'):
            self.plan(profile)
        (trials / 'EMBER64').write_text('include "arch/evbarm/conf/GENERIC64"\n')
        with self.assertRaisesRegex(SDKError, 'EMBER64 is in the OS and in .*: give the trial another name'):
            self.plan(kernel_configs=[str(trials)])

    def test_missing_kernel_configuration_is_refused(self):
        profile = copy.deepcopy(PROFILE)
        profile['os']['kernels'] = ['EMBER64', 'MISSING']
        with self.assertRaisesRegex(SDKError, 'MISSING is missing for evbarm'):
            self.plan(profile)


    def test_cli_takes_checkouts_from_the_configuration(self):
        from emberbsd_sdk.sdk_client import main
        host = dict(os=platform.system(), arch=platform.machine(), abi='test', runtime='native')
        config = dict(schema='emberbsd.sdk-client.v1', host_prefix=self.native.bundle_root,
                      cache_dir=str(self.root / 'cache'), state_dir=str(self.root / 'state'),
                      repositories={'native': dict(host=host, prefix=str(self.root / 'prefix'),
                                                   pkgdb=str(self.root / 'prefix/pkgdb'))},
                      source=dict(ports_root=str(self.ports), pkgsrc='/p', makeconf='/m', state='/s'),
                      build=dict(os_source=str(self.os), host_bfd_config=str(self.bfd)))
        path = self.write('client.json', json.dumps(config))
        profile = self.write('profile.json', json.dumps(PROFILE))
        output = io.BytesIO()
        with patch('emberbsd_sdk.build_profile.make_plan', return_value={'nodes': []}) as plan, \
                patch('sys.stdout', SimpleNamespace(buffer=output)), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(['--config', str(path), 'profile-plan', '--profile', str(profile)]), 0)
        self.assertEqual(json.loads(output.getvalue()), {'nodes': []})
        options = plan.call_args.kwargs
        self.assertEqual((options['os_root'], options['ports_root'], options['host_bfd_config'], options['state']),
                         (str(self.os), str(self.ports), str(self.bfd), str(self.root / 'state/build')))
        for arguments in (['profile-plan'], ['plan', '--profile', str(profile)],
                          ['profile-plan', '--profile', str(profile), '--package', 'net/x@native']):
            with self.subTest(arguments=arguments), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(['--config', str(path), *arguments]), 1)

    def test_selected_source_is_ready_and_its_manifest_is_an_input(self):
        llvm = self.write('llvm/llvm-source.json', '{"format": "emberbsd-llvm-source"}').parent
        nodes = self.nodes(self.plan(selected_sources={'EMBER_LLVM_SOURCE': llvm}))
        self.assertEqual(nodes['source:EMBER_LLVM_SOURCE']['decision'], dict(
            action='ready', why='selected in the configuration'))
        # Another selected tree is another input.
        before = nodes['os:kernel:EMBER64']['input_key']
        self.write('llvm/llvm-source.json', '{"format": "emberbsd-llvm-source", "version": 2}')
        after = self.nodes(self.plan(selected_sources={'EMBER_LLVM_SOURCE': llvm}))['os:kernel:EMBER64']['input_key']
        self.assertNotEqual(before, after)

    def test_libedit_binds_the_sdk_module_that_prepares_it(self):
        self.tools_sources = {'EMBER_LIBEDIT_SOURCE': 'lib/libedit'}
        self.write('sdk/src/emberbsd_sdk/libedit.py', '# libedit 1\n')
        git(self.sdk, 'add', '-A')
        git(self.sdk, 'commit', '-q', '-m', 'libedit 1')
        libedit = self.nodes(self.plan())['source:EMBER_LIBEDIT_SOURCE']
        # The SDK producer uses no pkgsrc export; its recipe has no distfile here.
        self.assertEqual(libedit['dependencies'], [])
        self.assertEqual(libedit['identity']['distfiles'], [])
        self.write('sdk/src/emberbsd_sdk/libedit.py', '# libedit 2\n')
        git(self.sdk, 'commit', '-q', '-am', 'libedit 2')
        self.assertNotEqual(self.nodes(self.plan())['source:EMBER_LIBEDIT_SOURCE']['input_key'], libedit['input_key'])

    def test_ports_submodule_must_be_checked_out(self):
        upstream = self.repository('upstream', {'mk/bsd.prefs.mk': '# pkgsrc\n'})
        git(self.ports, '-c', 'protocol.file.allow=always', 'submodule', 'add', '-q', str(upstream), 'upstream/pkgsrc')
        git(self.ports, 'commit', '-q', '-m', 'pkgsrc')
        self.assertEqual(self.nodes(self.plan())['source:EMBER_BYACC_SOURCE']['decision']['action'], 'build')
        git(self.ports, 'submodule', 'deinit', '-q', '-f', 'upstream/pkgsrc')
        with self.assertRaisesRegex(SDKError, 'initialize submodule upstream/pkgsrc'):
            self.plan()

    def test_os_stages_key_their_sdk_producers(self):
        profile = copy.deepcopy(PROFILE)
        profile['os']['sets'] = ['base', 'comp', 'etc']

        def keys():
            nodes = self.nodes(self.plan(profile))
            return tuple(nodes[name]['input_key'] for name in ('os:tools', 'os:kernel:EMBER64', 'os:sets'))

        before = keys()
        self.write('sdk/docs/notes.md', 'notes\n')
        self.write('sdk/scripts/qml-compile.py', '# qml command\n')
        self.write('sdk/src/emberbsd_sdk/qml.py', '# qml\n')
        git(self.sdk, 'add', '-A')
        git(self.sdk, 'commit', '-q', '-m', 'unrelated files')
        self.assertEqual(keys(), before)
        self.write('sdk/src/emberbsd_sdk/os_release_builder.py', '# release-only SDK module\n')
        git(self.sdk, 'add', '-A')
        git(self.sdk, 'commit', '-q', '-m', 'sets')
        release = keys()
        self.assertEqual(release[:2], before[:2])
        self.assertNotEqual(release[2], before[2])
        self.write('sdk/src/emberbsd_sdk/host_prefix.py', '# common native tool selection\n')
        self.assertEqual(keys(), (None, None, None))
        git(self.sdk, 'add', '-A')
        git(self.sdk, 'commit', '-q', '-m', 'shared runner')
        self.assertTrue(all(a != b for a, b in zip(release, keys())))

    def test_sdk_preparation_changes_a_built_source_but_preserves_a_selected_tree(self):
        llvm = self.write('llvm/llvm-source.json', '{}').parent
        selected = {'EMBER_LLVM_SOURCE': llvm}
        before = self.nodes(self.plan(selected_sources=selected))
        self.write('sdk/src/emberbsd_sdk/sourceprep.py', '# new SDK preparation implementation')
        git(self.sdk, 'add', '-A')
        git(self.sdk, 'commit', '-qm', 'source preparation')
        after = self.nodes(self.plan(selected_sources=selected))
        built, ready = 'source:EMBER_BYACC_SOURCE', 'source:EMBER_LLVM_SOURCE'
        self.assertNotEqual(after[built]['input_key'], before[built]['input_key'])
        self.assertNotEqual(after['os:kernel:EMBER64']['input_key'], before['os:kernel:EMBER64']['input_key'])
        self.assertEqual(after[ready]['input_key'], before[ready]['input_key'])
        self.assertEqual(after[ready]['decision']['action'], 'ready')

    def test_sets_bind_export_and_release_helpers_with_all_sources_selected(self):
        selected = {name: self.write('prepared/' + name + '/emberbsd-prepared-source.json', '{}').parent
                    for name in SOURCES}
        profile = copy.deepcopy(PROFILE)
        profile['os']['sets'] = ['base', 'comp', 'etc']

        def node():
            return self.nodes(self.plan(profile, selected_sources=selected))['os:sets']

        before = node()
        self.assertIn(build_profile.EXPORT_NODE, before['dependencies'])
        self.write('ports/docs/readme.md', 'notes')
        git(self.ports, 'add', '-A')
        git(self.ports, 'commit', '-qm', 'documentation')
        self.assertEqual(node()['input_key'], before['input_key'])
        self.write('ports/profiles/common-build-tools/cross/prepare-libtool.py', '# new producer')
        self.assertIsNone(node()['input_key'])
        git(self.ports, 'add', '-A')
        git(self.ports, 'commit', '-qm', 'libtool')
        updated = node()['input_key']
        self.assertNotEqual(updated, before['input_key'])
        self.write('ports/pkgsrc/export.conf', 'another export')
        git(self.ports, 'commit', '-qam', 'export')
        self.assertNotEqual(node()['input_key'], updated)

    def test_selected_libedit_uses_the_release_manifest(self):
        self.tools_sources = {'EMBER_LIBEDIT_SOURCE': 'lib/libedit'}
        path = self.write('libedit/libedit-source.json', '{}')
        selected = {'EMBER_LIBEDIT_SOURCE': path.parent}
        nodes = self.nodes(self.plan(selected_sources=selected))
        self.assertEqual(nodes['source:EMBER_LIBEDIT_SOURCE']['decision']['action'], 'ready')
        self.assertEqual(nodes['source:EMBER_LIBEDIT_SOURCE']['inputs']['manifest_sha256'],
                         build_profile.source_manifest_digest(path.parent))
        path.write_text('{"revision": 2}')
        self.assertNotEqual(self.nodes(self.plan(selected_sources=selected))['os:tools']['input_key'],
                            nodes['os:tools']['input_key'])


class PlanCacheTests(ProfileFixture):
    """The plan cache keeps the guard walks and the Ports tool output for the same files."""

    def setUp(self):
        super().setUp()
        self.cache = self.root / 'cache'

    def cached_plan(self, **options):
        return build_profile.make_plan(
            copy.deepcopy(PROFILE), os_root=self.os, ports_root=self.ports, sdk_root=self.sdk,
            host_prefix=self.native.bundle_root, host_bfd_config=str(self.bfd), native=self.native,
            cache=self.cache, **options)

    def test_the_same_files_do_not_run_the_walk_and_the_ports_tools_again(self):
        walks = []

        def query(*arguments, **options):
            return None

        def required(root, query, traversals, jobs=None):
            walks.append(traversals)
            return {'EMBER_BYACC_SOURCE': 'tools/yacc'} if traversals[0][0] == 'tools' else {}

        with patch.object(prepared_sources, 'os_query', query), \
                patch.object(prepared_sources, 'required_sources', required):
            first = self.cached_plan()
            walked = list(walks)
            self.assertTrue(walked)
            # The second plan takes all slow results from the cache.
            with patch.object(build_profile, 'ports_tool', side_effect=AssertionError('the Ports tool ran')):
                self.assertEqual(self.cached_plan()['nodes'], first['nodes'])
            self.assertEqual(walks, walked)
            # A local change of a Ports recipe gives the plan the new recipe.
            self.write('ports/pkgsrc/devel/byacc/Makefile', 'DISTNAME= byacc-2\n')
            changed = self.nodes(self.cached_plan())
            self.assertNotEqual(changed['source:EMBER_BYACC_SOURCE']['inputs']['recipe'],
                                self.nodes(first)['source:EMBER_BYACC_SOURCE']['inputs']['recipe'])
            # A Ports change does not walk the OS again.
            self.assertEqual(walks, walked)
            # A local change of an OS file walks the OS again, and so does a new untracked file.
            for path in ('os/sys/arch/evbarm/conf/EMBER64', 'os/sys/arch/evbarm/conf/LOCAL'):
                before = len(walks)
                self.write(path, 'include "arch/evbarm/conf/std"\n# local\n')
                self.cached_plan()
                self.assertEqual(walks[before:], walked)

    def test_other_sdk_code_runs_the_queries_again(self):
        code = self.write('code.py', 'pass\n')
        with patch.object(build_profile, '_CODE', [str(code)]):
            self.cached_plan(required_sources=self.required_sources)
            code.write_text('# changed\n')
            with patch.object(build_profile, 'ports_tool', side_effect=AssertionError('the Ports tool ran')):
                with self.assertRaisesRegex(AssertionError, 'the Ports tool ran'):
                    self.cached_plan(required_sources=self.required_sources)

    def test_a_damaged_cache_file_is_computed_again(self):
        self.cached_plan(required_sources=self.required_sources)
        for path in self.cache.iterdir():
            path.write_text('{')
        self.assertEqual(self.nodes(self.cached_plan(required_sources=self.required_sources)),
                         self.nodes(self.plan()))


if __name__ == '__main__':
    unittest.main()
