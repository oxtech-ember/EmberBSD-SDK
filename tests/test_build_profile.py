# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Build profiles and the build-graph-v2 plan."""

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



def git(root, *arguments):
    subprocess.run(['git', '-C', str(root), *arguments], check=True, capture_output=True,
                   env=dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@example.org',
                            GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@example.org',
                            GIT_AUTHOR_DATE='1700000000 +0000', GIT_COMMITTER_DATE='1700000000 +0000'))


class BuildProfileTests(TemporaryCase):
    def setUp(self):
        super().setUp()
        table = ' \\\n    '.join(f'{name} /usr/src/external/x' for name in SOURCES)
        self.os = self.repository('os', {
            'share/mk/bsd.sys.mk': f'.for _var _path in {table} \\\n    EXTERNAL_TOOLCHAIN /usr/toolchain\n.if 1\n.endif\n',
            'ember/shared-sources.json': json.dumps({
                'schema': 'emberbsd.os-shared-sources.v1', 'sources': SOURCES,
                'kernel': {'default': ['EMBER_LLVM_SOURCE'], 'configs': {}}}),
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
            'pkgsrc/devel/editline/Makefile': 'DISTNAME= editline\n'})
        self.sdk = self.repository('sdk', {'scripts/prepare-libedit.py': 'pass\n'})
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
        return {'EMBER_BYACC_SOURCE': 'tools/yacc', 'EMBER_HEIMDAL_SOURCE': 'tools/asn1_compile'} \
            if traversals and traversals[0][0] == 'tools' else {}

    def plan(self, profile=PROFILE, **options):
        return build_profile.make_plan(
            copy.deepcopy(profile), os_root=self.os, ports_root=self.ports, sdk_root=self.sdk,
            host_prefix=self.native.bundle_root, host_bfd_config=str(self.bfd),
            required_sources=self.required_sources, native=self.native, **options)

    def nodes(self, plan):
        return {node['id']: node for node in plan['nodes']}

    def test_kernel_profile_plans_tools_sources_and_kernel_in_order(self):
        plan = self.plan()
        nodes = self.nodes(plan)
        self.assertEqual(set(nodes), {
            'runtime:host-prefix', 'runtime:target-toolchain', 'runtime:host-binutils', 'source:EMBER_BYACC_SOURCE',
            'source:EMBER_HEIMDAL_SOURCE', 'source:EMBER_LLVM_SOURCE', 'os:tools', 'os:kernel:EMBER64'})
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
            'source:EMBER_LLVM_SOURCE': 'build', 'os:tools': 'build', 'os:kernel:EMBER64': 'build'})
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
        self.write('state/results/source_EMBER_LLVM_SOURCE.json', json.dumps({'input_key': key}))
        self.write('state/results/os_tools.json', json.dumps({'input_key': '0' * 64}))
        nodes = self.nodes(self.plan(state=state))
        self.assertEqual(nodes['source:EMBER_LLVM_SOURCE']['decision']['action'], 'ready')
        self.assertEqual(nodes['os:tools']['decision']['action'], 'build')

    def test_sets_walk_the_release_tree_and_share_the_tools(self):
        profile = copy.deepcopy(PROFILE)
        profile['os']['sets'] = ['base', 'etc']
        nodes = self.nodes(self.plan(profile))
        self.assertEqual(nodes['os:sets']['outputs'], ['base.tar.xz', 'etc.tar.xz'])
        self.assertIn('os:tools', nodes['os:sets']['dependencies'])
        self.assertEqual(sorted(nodes['os:tools']['reasons']),
                         ['needed by os:kernel:EMBER64', 'needed by os:sets'])
        self.assertIn(prepared_sources.RELEASE_TRAVERSALS, self.walks)

    def test_profile_refuses_paths_versions_and_ports_roots(self):
        for change, message in (
                (lambda p: p['platform'].update(prefix='/opt/x'), 'needs target and compiler'),
                (lambda p: p['os'].update(kernels=['../EMBER64']), 'os.kernels'),
                (lambda p: p['os'].update(mk={'MKX11': '1.2'}), 'os.mk'),
                (lambda p: p['ports'].update(packages=['devel/example@native']), 'stage 4'),
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

if __name__ == '__main__':
    unittest.main()
