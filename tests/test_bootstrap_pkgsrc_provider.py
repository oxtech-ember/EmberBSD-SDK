# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Contracts for the scoped pkgsrc bootstrap-tools producer."""

import contextlib
import importlib.util
import io
import json
import sys
import unittest
from unittest.mock import patch

from pathlib import Path

from test_bootstrap_pkgsrc_export import SourceFixture, write
from emberbsd_sdk.bootstrap_pkgsrc_provider import (
    _append_pkgsrc_tool_routes, _check_expected_exit, _normalize_internal_absolute_links,
    _validate_host_identity, _validate_work_root,
    create_bootstrap_plan,
)
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.inventory import sha256_file, snapshot_tree
from emberbsd_sdk.jsonio import write_json


class ProviderFixture(SourceFixture):
    def setUp(self):
        super().setUp()
        self.export = self.root / 'target/pkgsrc'
        for relative in ('bootstrap/bootstrap', 'devel/bmake/Makefile',
                         'pkgtools/bootstrap-mk-files/Makefile',
                         'pkgtools/pkg_install/Makefile', 'shells/mksh/Makefile',
                         'devel/gmake/Makefile'):
            write(self.export / relative, f'fixture source: {relative}\n')
        (self.export / 'bootstrap/bootstrap').chmod(0o755)
        self.source_inventory_path = self.root / 'target/source-inventory.json'
        write_json(self.source_inventory_path, snapshot_tree(str(self.export)))
        self.transfer_report = write(self.root / 'target/transfer-report.json', 'verified fixture transfer\n')
        self.export_acceptance = {
            'format': 'emberbsd-bootstrap-pkgsrc-export-acceptance', 'version': 1,
            'scope': 'pkgsrc-source-export', 'status': 'PASS',
            'evidence_level': 'independent-source-export-review',
            'producer_result': dict(path='/export/result.json', sha256='1' * 64),
            'export_plan': dict(path='/export/plan.json', sha256='2' * 64),
            'source_descriptor': dict(path='/export/source.json', sha256='3' * 64),
            'output_root': '/export/pkgsrc',
            'output_inventory': dict(path='/export/inventory.json',
                                     sha256=sha256_file(str(self.source_inventory_path))),
            'source_proof': {'ports_source': self.source['ports_source']},
            'commands': [], 'controller_command': {}, 'all_commands_drained': True,
            'inputs_revalidated_after_drain': True,
            'scope_boundary': 'independently reviewed current Ports source export only; no pkgsrc build or NativeToolset qualification',
        }
        self.export_acceptance_path = self.root / 'target/export-acceptance.json'
        write_json(self.export_acceptance_path, self.export_acceptance)
        self.export_acceptance_ref = dict(path=str(self.export_acceptance_path),
                                          sha256=sha256_file(str(self.export_acceptance_path)))
        self.transfer_path = self.root / 'target/transfer-acceptance.json'
        self.transfer = {
            'format': 'emberbsd-bootstrap-pkgsrc-transfer-acceptance', 'version': 1,
            'scope': 'pkgsrc-source-transfer', 'status': 'PASS',
            'evidence_level': 'independent-target-source-revalidation',
            'source_export_acceptance': self.export_acceptance_ref,
            'source_inventory': dict(path=str(self.source_inventory_path),
                                     sha256=sha256_file(str(self.source_inventory_path))),
            'output_root': str(self.export),
            'output_inventory': dict(path=str(self.source_inventory_path),
                                     sha256=sha256_file(str(self.source_inventory_path))),
            'transfer_report': dict(path=str(self.transfer_report),
                                    sha256=sha256_file(str(self.transfer_report))),
            'source_proof': self.export_acceptance['source_proof'],
            'normalization_policy': 'symlink-mode-ignored-v1',
            'inputs_revalidated_after_transfer': True,
            'scope_boundary': 'independently revalidated current pkgsrc source tree at target; no package build or NativeToolset qualification',
        }
        write_json(self.transfer_path, self.transfer)
        self.transfer_ref = dict(path=str(self.transfer_path), sha256=sha256_file(str(self.transfer_path)))

        refs = {}
        for name in ('accepted-record', 'prefix-manifest', 'prefix-inventory', 'compiler-acceptance'):
            path = self.root / 'inputs' / (name + '.json')
            write_json(path, {'fixture': name})
            refs[name] = dict(path=str(path), sha256=sha256_file(str(path)))
        self.compiler_seed = {
            'format': 'emberbsd-bootstrap-compiler-seed', 'version': 1,
            'scope': 'bmake-bootstrap', 'host': 'NetBSD11.0-amd64', 'prefix': '/usr/pkg',
            'accepted_record': refs['accepted-record'],
            'prefix_manifest': refs['prefix-manifest'],
            'prefix_inventory': refs['prefix-inventory'],
            'acceptance_receipt': refs['compiler-acceptance'],
            'compiler': dict(path='/usr/pkg/bin/gcc', sha256='a' * 64,
                             version='16.2.0', triplet='x86_64-unknown-netbsd11.0'),
            'driver_components': {
                'cc1': dict(path='/usr/pkg/libexec/gcc/x86_64-unknown-netbsd11.0/16.2.0/cc1', sha256='b' * 64),
                'cc1plus': dict(path='/usr/pkg/libexec/gcc/x86_64-unknown-netbsd11.0/16.2.0/cc1plus', sha256='c' * 64),
                'collect2': dict(path='/usr/pkg/libexec/gcc/x86_64-unknown-netbsd11.0/16.2.0/collect2', sha256='d' * 64),
                'libgcc': dict(path='/usr/pkg/lib/gcc/x86_64-unknown-netbsd11.0/16.2.0/libgcc.a', sha256='e' * 64),
                'as': dict(path='/usr/bin/as', sha256='f' * 64),
                'ld': dict(path='/usr/bin/ld', sha256='0' * 64),
            },
            'host_runtime': dict(ldd=dict(path='/usr/bin/ldd', sha256='1' * 64),
                                 libraries={'/usr/lib/libc.so': '2' * 64}),
        }
        self.compiler_seed_path = self.root / 'inputs/compiler-seed.json'
        write_json(self.compiler_seed_path, self.compiler_seed)
        self.compiler_seed_ref = dict(path=str(self.compiler_seed_path),
                                      sha256=sha256_file(str(self.compiler_seed_path)))
        self.host_tools = {}
        for role in ('sh', 'ar', 'as', 'awk', 'basename', 'cat', 'chgrp', 'chmod', 'chown', 'cmp', 'cp', 'cut',
                     'date', 'diff', 'dirname', 'echo', 'egrep', 'env', 'expr', 'fgrep', 'file', 'find',
                     'ftp', 'grep', 'groups', 'gzip', 'head', 'hostname', 'id', 'install', 'join', 'ld',
                     'lorder', 'ln', 'ls', 'mkdir', 'mktemp', 'mv', 'nm', 'pwd', 'ranlib', 'rm', 'sed',
                     'sort', 'tail', 'tr', 'tsort', 'uname',
                     'strip', 'uniq', 'wc', 'patch', 'printf', 'rmdir', 'sleep',
                     'tar', 'touch', 'whoami', 'xargs'):
            path = ('/bin/sh' if role == 'sh' else
                    '/sbin/chown' if role in ('chgrp', 'chown') else
                    f'/usr/bin/{role}')
            self.host_tools[role] = dict(path=path, sha256='4' * 64)


class PkgsrcProviderPlanTests(ProviderFixture):
    def test_absolute_links_to_objects_inside_prefix_become_relative_and_idempotent(self):
        prefix = self.root / 'prefix'
        target = prefix / 'bin/gmake'
        link = prefix / 'gnu/bin/make'
        write(target, 'gmake fixture\n')
        link.parent.mkdir(parents=True)
        link.symlink_to(target)

        _normalize_internal_absolute_links(str(prefix))

        self.assertEqual(link.readlink(), Path('../../bin/gmake'))
        self.assertEqual(snapshot_tree(str(prefix))['gnu/bin/make']['target'], '../../bin/gmake')
        _normalize_internal_absolute_links(str(prefix))
        self.assertEqual(link.readlink(), Path('../../bin/gmake'))
        self.assertFalse(Path(str(link) + '.emberbsd-link-normalize').exists())

    def test_external_absolute_link_is_rejected_without_modification(self):
        prefix = self.root / 'prefix-external-link'
        link = prefix / 'bin/host-tool'
        link.parent.mkdir(parents=True)
        link.symlink_to('/usr/bin/env')

        with self.assertRaisesRegex(SDKError, 'absolute payload link target is outside'):
            _normalize_internal_absolute_links(str(prefix))

        self.assertEqual(link.readlink(), Path('/usr/bin/env'))

    def test_absolute_link_through_internal_alias_that_escapes_is_rejected(self):
        prefix = self.root / 'prefix-escaping-link'
        outside = self.root / 'outside-tool'
        alias = prefix / 'bin/alias'
        link = prefix / 'bin/tool'
        write(outside, 'outside fixture\n')
        alias.parent.mkdir(parents=True)
        alias.symlink_to(outside)
        link.symlink_to(alias)

        with self.assertRaisesRegex(SDKError, 'absolute payload link target is outside'):
            _normalize_internal_absolute_links(str(prefix))

        self.assertEqual(link.readlink(), alias)

    def test_relative_links_remain_unchanged(self):
        prefix = self.root / 'prefix-relative-link'
        target = prefix / 'bin/gmake'
        link = prefix / 'gnu/bin/make'
        write(target, 'gmake fixture\n')
        link.parent.mkdir(parents=True)
        link.symlink_to('../../bin/gmake')

        _normalize_internal_absolute_links(str(prefix))

        self.assertEqual(link.readlink(), Path('../../bin/gmake'))
        snapshot_tree(str(prefix))

    def test_plan_pins_the_complete_native_lorder_and_archive_tool_closure(self):
        tools = dict(self.host_tools)
        for role in ('join', 'lorder', 'mktemp', 'nm', 'ranlib', 'tsort'):
            tools[role] = dict(path=f'/usr/bin/{role}', sha256='5' * 64)
        work_root = self.root / 'provider-work-with-lorder-closure'
        plan = create_bootstrap_plan(
            self.transfer_ref, self.export_acceptance_ref, self.compiler_seed_ref,
            str(work_root), str(self.root / 'locks-with-lorder-closure'), tools,
            plan_path=str(self.root / 'inputs/provider-plan-with-lorder-closure.json'),
            cpu_jobs=2,
        )
        host_tools = plan['host_tools']
        for role in ('join', 'lorder', 'mktemp', 'nm', 'ranlib', 'tsort', 'sed', 'sort', 'rm'):
            self.assertEqual(host_tools[role]['path'], f'/usr/bin/{role}')
        routes = plan['pkgsrc_tool_routes']
        for role in ('mktemp', 'tsort'):
            self.assertEqual(routes[role], str(work_root / 'host-tools' / role))
        for role in ('ar', 'groups', 'join', 'lorder', 'nm', 'ranlib', 'uname', 'whoami'):
            self.assertNotIn(role, routes)

    def test_gcc_linker_and_assembler_are_private_path_roles_not_platform_routes(self):
        work_root = self.root / 'provider-work-with-linker-roles'
        plan = create_bootstrap_plan(
            self.transfer_ref, self.export_acceptance_ref, self.compiler_seed_ref,
            str(work_root), str(self.root / 'locks-with-linker-roles'),
            self.host_tools,
            plan_path=str(self.root / 'inputs/provider-plan-with-linker-roles.json'),
            cpu_jobs=2,
        )
        self.assertEqual(plan['host_tools']['ld']['path'], '/usr/bin/ld')
        self.assertEqual(plan['host_tools']['as']['path'], '/usr/bin/as')
        self.assertEqual(plan['commands'][0]['env']['PATH'], str(work_root / 'host-tools'))
        self.assertNotIn('ld', plan['pkgsrc_tool_routes'])
        self.assertNotIn('as', plan['pkgsrc_tool_routes'])

    def test_plan_isolates_pkgsrc_distfiles_workdirs_and_native_seed_gcc(self):
        work_root = self.root / 'provider-work-with-private-pkgsrc-dirs'
        plan = create_bootstrap_plan(
            self.transfer_ref, self.export_acceptance_ref, self.compiler_seed_ref,
            str(work_root), str(self.root / 'locks-with-private-pkgsrc-dirs'),
            self.host_tools,
            plan_path=str(self.root / 'inputs/provider-plan-with-private-pkgsrc-dirs.json'),
            cpu_jobs=2,
        )
        pkgsrc_work = str(work_root / 'pkgsrc-work')
        bootstrap_pkgsrc_work = str(work_root / 'bootstrap' / 'wrk')
        distfiles = str(work_root / 'distfiles')
        self.assertEqual(plan['pkgsrc_work'], pkgsrc_work)
        self.assertEqual(plan['bootstrap_pkgsrc_work'], bootstrap_pkgsrc_work)
        for command in plan['commands']:
            self.assertEqual(command['env']['DISTDIR'], distfiles)
            expected_work = (bootstrap_pkgsrc_work if command['phase'] == 'bootstrap'
                             else pkgsrc_work)
            self.assertEqual(command['env']['WRKOBJDIR'], expected_work)
            self.assertEqual(command['env']['USE_NATIVE_GCC'], 'yes')
            self.assertEqual(command['env']['GCCBASE'], '/usr/pkg')
            if command['phase'] == 'package':
                self.assertFalse(any(arg.startswith('-j') for arg in command['argv']))
                self.assertIn('MAKE_JOBS=2', command['argv'])
                self.assertIn(f'DISTDIR={distfiles}', command['argv'])
                self.assertIn(f'WRKOBJDIR={pkgsrc_work}', command['argv'])
                self.assertIn('USE_NATIVE_GCC=yes', command['argv'])
                self.assertIn('GCCBASE=/usr/pkg', command['argv'])

    def test_plan_binds_pkgsrc_tool_routes_to_private_aliases(self):
        work_root = self.root / 'provider-work-with-private-routes'
        plan = create_bootstrap_plan(
            self.transfer_ref, self.export_acceptance_ref, self.compiler_seed_ref,
            str(work_root), str(self.root / 'locks-with-private-routes'),
            self.host_tools,
            plan_path=str(self.root / 'inputs/provider-plan-with-private-routes.json'),
            cpu_jobs=2,
        )
        routes = plan['pkgsrc_tool_routes']
        expected_roles = {
            'basename', 'bsdtar', 'cat', 'chgrp', 'chmod', 'chown', 'cmp',
            'cp', 'cut', 'date', 'diff', 'dirname', 'echo', 'egrep', 'env',
            'expr', 'fgrep', 'file', 'find', 'ftp', 'grep', 'gzip', 'head',
            'hostname', 'id', 'install', 'ln', 'ls', 'mkdir', 'mktemp', 'mv', 'patch',
            'printf', 'pwd', 'rm', 'rmdir', 'sed', 'sh', 'sleep', 'sort',
            'strip', 'tail', 'tar', 'touch', 'tr', 'uniq', 'wc',
            'xargs', 'awk', 'tsort',
        }
        self.assertEqual(set(routes), expected_roles)
        for role in ('awk', 'install', 'sed', 'sh'):
            self.assertEqual(routes[role], str(work_root / 'bootstrap' / {
                'awk': 'bin/nawk', 'install': 'bin/install-sh',
                'sed': 'bin/sed', 'sh': 'bin/mksh',
            }[role]))
        self.assertEqual(routes['bsdtar'], str(work_root / 'host-tools' / 'tar'))
        self.assertEqual(routes['gzip'], f'{work_root}/host-tools/gzip -nf ${{GZIP}}')
        self.assertEqual(routes['mkdir'], f'{work_root}/host-tools/mkdir -p')
        for role in expected_roles - {'awk', 'install', 'sed', 'sh', 'bsdtar', 'gzip', 'mkdir'}:
            self.assertEqual(routes[role], str(work_root / 'host-tools' / role))
        self.assertTrue(routes['gzip'].startswith(str(work_root / 'host-tools' / 'gzip') + ' '))
        self.assertTrue(routes['mkdir'].startswith(str(work_root / 'host-tools' / 'mkdir') + ' '))
        self.assertEqual(plan['commands'][0]['env']['SIZE'], ':')

    def test_plan_binds_owner_group_utilities_for_install_sh(self):
        tools = dict(self.host_tools)
        for role in ('chown', 'chgrp'):
            tools[role] = dict(path='/sbin/chown', sha256='6' * 64)
        work_root = self.root / 'provider-work-with-owner-tools'
        try:
            plan = create_bootstrap_plan(
                self.transfer_ref, self.export_acceptance_ref, self.compiler_seed_ref,
                str(work_root), str(self.root / 'locks-with-owner-tools'), tools,
                plan_path=str(self.root / 'inputs/provider-plan-with-owner-tools.json'),
                cpu_jobs=2,
            )
        except SDKError as exc:
            self.fail(f'install-sh owner/group utilities were rejected: {exc}')
        self.assertEqual(plan['host_tools']['chown'], tools['chown'])
        self.assertEqual(plan['host_tools']['chgrp'], tools['chgrp'])
        self.assertIn(str(work_root / 'host-tools'),
                      plan['commands'][0]['env']['PATH'].split(':'))

    def test_plan_binds_touch_to_the_isolated_bootstrap_path(self):
        tools = dict(self.host_tools)
        tools['touch'] = dict(path='/usr/bin/touch', sha256='5' * 64)
        work_root = self.root / 'provider-work-with-touch'
        try:
            plan = create_bootstrap_plan(
                self.transfer_ref, self.export_acceptance_ref, self.compiler_seed_ref,
                str(work_root), str(self.root / 'locks-with-touch'), tools,
                plan_path=str(self.root / 'inputs/provider-plan-with-touch.json'),
                cpu_jobs=2,
            )
        except SDKError as exc:
            self.fail(f'pkgsrc bootstrap-required touch utility was rejected: {exc}')
        self.assertEqual(plan['host_tools']['touch'], tools['touch'])
        self.assertIn(str(work_root / 'host-tools'),
                      plan['commands'][0]['env']['PATH'].split(':'))

    def test_plan_binds_archive_utility_to_the_isolated_bootstrap_path(self):
        tools = dict(self.host_tools)
        tools['ar'] = dict(path='/usr/bin/ar', sha256='5' * 64)
        work_root = self.root / 'provider-work-with-ar'
        try:
            plan = create_bootstrap_plan(
                self.transfer_ref, self.export_acceptance_ref, self.compiler_seed_ref,
                str(work_root), str(self.root / 'locks-with-ar'), tools,
                plan_path=str(self.root / 'inputs/provider-plan-with-ar.json'),
                cpu_jobs=2,
            )
        except SDKError as exc:
            self.fail(f'pkgsrc bootstrap-required ar utility was rejected: {exc}')
        self.assertEqual(plan['host_tools']['ar'], tools['ar'])
        self.assertIn(str(work_root / 'host-tools'),
                      plan['commands'][0]['env']['PATH'].split(':'))

    def test_plan_binds_strip_utility_to_the_isolated_bootstrap_path(self):
        tools = dict(self.host_tools)
        tools['strip'] = dict(path='/usr/bin/strip', sha256='5' * 64)
        work_root = self.root / 'provider-work-with-strip'
        try:
            plan = create_bootstrap_plan(
                self.transfer_ref, self.export_acceptance_ref, self.compiler_seed_ref,
                str(work_root), str(self.root / 'locks-with-strip'), tools,
                plan_path=str(self.root / 'inputs/provider-plan-with-strip.json'),
                cpu_jobs=2,
            )
        except SDKError as exc:
            self.fail(f'pkgsrc bootstrap-required strip utility was rejected: {exc}')
        self.assertEqual(plan['host_tools']['strip'], tools['strip'])
        self.assertIn(str(work_root / 'host-tools'),
                      plan['commands'][0]['env']['PATH'].split(':'))

    def test_plan_binds_full_pkgsrc_bootstrap_and_only_current_ports_packages(self):
        plan_path = self.root / 'inputs/provider-plan.json'
        plan = create_bootstrap_plan(
            self.transfer_ref, self.export_acceptance_ref, self.compiler_seed_ref,
            str(self.root / 'provider-work'), str(self.root / 'locks'),
            self.host_tools, plan_path=str(plan_path), cpu_jobs=2,
        )
        self.assertEqual([step['id'] for step in plan['commands']],
                         ['bootstrap-full', 'install-mksh', 'install-gmake'])
        self.assertIn('--full', plan['commands'][0]['argv'])
        self.assertIn('--compiler', plan['commands'][0]['argv'])
        self.assertEqual(plan['commands'][0]['env']['CC'], '/usr/pkg/bin/gcc')
        self.assertEqual(plan['commands'][1]['argv'][0], plan['bmake_binary'])
        self.assertIn(str(self.export / 'shells/mksh'), plan['commands'][1]['argv'])
        self.assertIn(str(self.export / 'devel/gmake'), plan['commands'][2]['argv'])
        for command in plan['commands']:
            self.assertNotIn('/usr/bin', command['env']['PATH'].split(':'))
            self.assertNotIn('/bin', command['env']['PATH'].split(':'))
        self.assertEqual(plan['commands'][0]['env']['SIZE'], ':')
        for command in plan['commands'][1:]:
            self.assertEqual(command['env']['SIZE'], ':')
            self.assertIn('SIZE=:', command['argv'])

    def test_plan_rejects_system_compiler_or_make_substitution(self):
        for role in ('gcc', 'cc', 'make', 'bmake', 'gmake'):
            tools = dict(self.host_tools)
            tools[role] = dict(path='/usr/bin/' + role, sha256='5' * 64)
            with self.subTest(role=role), self.assertRaisesRegex(SDKError, 'tool|compiler|make|roster'):
                create_bootstrap_plan(
                    self.transfer_ref, self.export_acceptance_ref, self.compiler_seed_ref,
                    str(self.root / 'provider-work'), str(self.root / 'locks'), tools,
                    plan_path=str(self.root / 'inputs/provider-plan.json'), cpu_jobs=2,
                )

    def test_plan_is_written_and_isolated_from_all_inputs(self):
        from emberbsd_sdk.jsonio import read_json

        plan_path = self.root / 'inputs/provider-plan.json'
        work_root = self.root / 'provider-work'
        plan = create_bootstrap_plan(
            self.transfer_ref, self.export_acceptance_ref, self.compiler_seed_ref,
            str(work_root), str(self.root / 'locks'), self.host_tools,
            plan_path=str(plan_path), cpu_jobs=2,
        )
        self.assertEqual(read_json(str(plan_path)), plan)
        self.assertEqual(plan['prefix_root'], str(work_root / 'prefix'))
        self.assertEqual(plan['bootstrap_work'], str(work_root / 'bootstrap'))
        self.assertEqual(plan['mkconf'], str(work_root / 'bootstrap' / 'mk.conf'))
        self.assertEqual(plan['distfiles'], str(work_root / 'distfiles'))
        self.assertNotEqual(plan['bmake_binary'], '/usr/pkg/bin/bmake')
        for command in plan['commands'][1:]:
            self.assertEqual(command['env']['DISTDIR'], str(work_root / 'distfiles'))

    def test_plan_rejects_input_overlap_and_invalid_jobs(self):
        common = (self.transfer_ref, self.export_acceptance_ref,
                  self.compiler_seed_ref)
        with self.assertRaisesRegex(SDKError, 'overlap|work'):
            create_bootstrap_plan(
                *common, str(self.export), str(self.root / 'locks'), self.host_tools,
                plan_path=str(self.root / 'inputs/provider-plan.json'), cpu_jobs=2,
            )
        for jobs in (0, -1, True, 65):
            with self.subTest(jobs=jobs), self.assertRaisesRegex(SDKError, 'jobs|resource|CPU'):
                create_bootstrap_plan(
                    *common, str(self.root / 'provider-work'), str(self.root / 'locks'),
                    self.host_tools, plan_path=str(self.root / 'inputs/provider-plan.json'),
                    cpu_jobs=jobs,
                )

    def test_plan_rejects_preexisting_work_root(self):
        work_root = self.root / 'provider-work'
        work_root.mkdir()
        with self.assertRaisesRegex(SDKError, 'fresh|unused|canonical'):
            create_bootstrap_plan(
                self.transfer_ref, self.export_acceptance_ref, self.compiler_seed_ref,
                str(work_root), str(self.root / 'locks'), self.host_tools,
                plan_path=str(self.root / 'inputs/provider-plan.json'), cpu_jobs=2,
            )

    def test_plan_rejects_paths_unsafe_for_mkconf_before_bootstrap(self):
        with self.assertRaisesRegex(SDKError, 'unsafe for mk.conf'):
            create_bootstrap_plan(
                self.transfer_ref, self.export_acceptance_ref, self.compiler_seed_ref,
                str(self.root / 'work with spaces'), str(self.root / 'locks'), self.host_tools,
                plan_path=str(self.root / 'inputs/provider-plan-spaced.json'), cpu_jobs=2,
            )

    def test_post_run_validation_accepts_only_the_claimed_real_work_root(self):
        work_root = self.root / 'claimed-work'
        _validate_work_root(str(work_root), allow_claimed_work=False)
        work_root.mkdir()
        with self.assertRaisesRegex(SDKError, 'fresh|canonical'):
            _validate_work_root(str(work_root), allow_claimed_work=False)
        _validate_work_root(str(work_root), allow_claimed_work=True)

    def test_command_exit_must_match_the_fixed_plan(self):
        step = {'id': 'bootstrap-full', 'expected_exit': 0}
        _check_expected_exit(step, 0)
        with self.assertRaisesRegex(SDKError, 'exited 1; expected 0'):
            _check_expected_exit(step, 1)

    def test_runtime_host_must_match_netbsd_11_0_amd64(self):
        _validate_host_identity('NetBSD', '11.0', 'amd64')
        with self.assertRaisesRegex(SDKError, 'NetBSD 11.0 amd64'):
            _validate_host_identity('NetBSD', '10.1', 'amd64')
        with self.assertRaisesRegex(SDKError, 'NetBSD 11.0 amd64'):
            _validate_host_identity('NetBSD', '11.0', 'aarch64')

    def test_plan_rejects_changed_transfer_acceptance(self):
        self.transfer_path.write_text('changed receipt\n', encoding='utf-8')
        with self.assertRaisesRegex(SDKError, 'changed|SHA256|hash|acceptance'):
            create_bootstrap_plan(
                self.transfer_ref, self.export_acceptance_ref, self.compiler_seed_ref,
                str(self.root / 'provider-work'), str(self.root / 'locks'), self.host_tools,
                plan_path=str(self.root / 'inputs/provider-plan.json'), cpu_jobs=2,
            )

    def test_fixed_host_utility_may_use_netbsd_bin_or_usr_bin(self):
        tools = dict(self.host_tools)
        tools['cat'] = dict(path='/bin/cat', sha256='5' * 64)
        plan = create_bootstrap_plan(
            self.transfer_ref, self.export_acceptance_ref, self.compiler_seed_ref,
            str(self.root / 'provider-work'), str(self.root / 'locks'), tools,
            plan_path=str(self.root / 'inputs/provider-plan.json'), cpu_jobs=2,
        )
        self.assertEqual(plan['host_tools']['cat']['path'], '/bin/cat')

    def test_tool_routes_are_added_inside_pkgsrc_settings_once(self):
        plan = create_bootstrap_plan(
            self.transfer_ref, self.export_acceptance_ref, self.compiler_seed_ref,
            str(self.root / 'provider-work'), str(self.root / 'locks'), self.host_tools,
            plan_path=str(self.root / 'inputs/provider-plan.json'), cpu_jobs=2,
        )
        mkconf = self.root / 'target/mk.conf'
        mkconf.write_text(
            '.ifdef BSD_PKG_MK\t# begin pkgsrc settings\n'
            'TOOLS_PLATFORM.install?=\tbootstrap/bin/install-sh\n'
            '.endif\t\t\t# end pkgsrc settings\n',
            encoding='utf-8',
        )
        from emberbsd_sdk.bootstrap_pkgsrc_provider import _PKGSRC_TOOL_ROUTE_MARKER

        receipt = _append_pkgsrc_tool_routes(str(mkconf), plan['pkgsrc_tool_routes'])
        content = mkconf.read_text(encoding='utf-8')
        self.assertEqual(receipt['path'], str(mkconf))
        self.assertEqual(receipt['sha256'], sha256_file(str(mkconf)))
        self.assertIn(_PKGSRC_TOOL_ROUTE_MARKER, content)
        self.assertIn(f"TOOLS_PLATFORM.cut= {plan['pkgsrc_tool_routes']['cut']}\n", content)
        self.assertIn(f"TOOLS_PLATFORM.gzip= {plan['pkgsrc_tool_routes']['gzip']}\n", content)
        self.assertIn(f"TOOLS_PLATFORM.mktemp= {plan['pkgsrc_tool_routes']['mktemp']}\n", content)
        self.assertIn(f"TOOLS_PLATFORM.tsort= {plan['pkgsrc_tool_routes']['tsort']}\n", content)
        self.assertLess(content.index(_PKGSRC_TOOL_ROUTE_MARKER),
                        content.index('.endif\t\t\t# end pkgsrc settings'))
        with self.assertRaisesRegex(SDKError, 'already applied'):
            _append_pkgsrc_tool_routes(str(mkconf), plan['pkgsrc_tool_routes'])

    def test_tool_routes_reject_unpinned_shell_arguments(self):
        plan = create_bootstrap_plan(
            self.transfer_ref, self.export_acceptance_ref, self.compiler_seed_ref,
            str(self.root / 'provider-work'), str(self.root / 'locks'), self.host_tools,
            plan_path=str(self.root / 'inputs/provider-plan.json'), cpu_jobs=2,
        )
        routes = dict(plan['pkgsrc_tool_routes'])
        routes['cat'] += ' /etc/passwd'
        mkconf = self.root / 'target/mk.conf'
        mkconf.write_text('.endif\t\t\t# end pkgsrc settings\n', encoding='utf-8')
        with self.assertRaisesRegex(SDKError, 'safe fixed command'):
            _append_pkgsrc_tool_routes(str(mkconf), routes)


class PkgsrcProviderCliExitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cli_path = Path(__file__).resolve().parents[1] / 'scripts/bootstrap-pkgsrc.py'
        spec = importlib.util.spec_from_file_location('bootstrap_pkgsrc_cli_test', cli_path)
        cls.cli = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.cli
        spec.loader.exec_module(cls.cli)

    def _run_provider(self, status):
        output = type('StdoutCapture', (), {'buffer': io.BytesIO()})()
        argv = ['run-provider', '--plan', '/tmp/plan.json', '--plan-sha256', 'a' * 64]
        with patch.object(self.cli, 'run_bootstrap', return_value={'status': status}):
            with contextlib.redirect_stdout(output):
                code = self.cli.main(argv)
        self.assertEqual(json.loads(output.buffer.getvalue()), {'status': status})
        return code

    def test_cli_returns_nonzero_when_provider_result_failed(self):
        self.assertEqual(self._run_provider('FAILED'), 1)

    def test_cli_returns_zero_only_for_pending_review_result(self):
        self.assertEqual(self._run_provider('COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW'), 0)

    def _run_export(self, status):
        output = type('StdoutCapture', (), {'buffer': io.BytesIO()})()
        argv = ['run-export', '--source-descriptor', '/tmp/source.json',
                '--source-descriptor-sha256', 'a' * 64, '--plan', '/tmp/plan.json',
                '--plan-sha256', 'b' * 64]
        with patch.object(self.cli, 'run_export', return_value={'status': status,
                                                               'commands': ['kept']}) as run:
            with contextlib.redirect_stdout(output):
                code = self.cli.main(argv)
        self.assertEqual(json.loads(output.buffer.getvalue()),
                         {'status': status, 'commands': ['kept']})
        run.assert_called_once()
        return code

    def test_export_cli_propagates_failed_controller_status_without_rewriting_result(self):
        self.assertEqual(self._run_export('FAILED'), 1)

    def test_export_cli_returns_zero_only_for_pending_independent_review(self):
        self.assertEqual(self._run_export('EXPORT_COMPLETED_PENDING_INDEPENDENT_REVIEW'), 0)
