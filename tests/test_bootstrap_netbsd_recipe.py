# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Closed NetBSD recipe route controls; no native compiler is run here."""

import json
from pathlib import Path
import tempfile
import unittest
import subprocess
from unittest.mock import patch

from emberbsd_sdk import bootstrap_netbsd_recipe, bootstrap_seed, bootstrap_pilot
from emberbsd_sdk.errors import CommandError, SDKError
from emberbsd_sdk.inventory import sha256_file, snapshot_tree


class NetBSDRecipeStepTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.localbase = self.root / 'output'
        self.export = self.root / 'ports'
        self.work = self.root / 'work'
        self.seed = self.root / 'seed'
        self.seed.mkdir()
        self.pkgtools = self.root / 'pkgsrc'
        self.makeconf = self.root / 'mk.conf'
        self.utility_bin = self.root / 'utilities'
        self.utility_bin.mkdir()
        recipes = {
            'devel/binutils': dict(dependencies=[]),
            'lang/gcc16': dict(dependencies=['devel/binutils'],
                               object_directory=str(self.work / 'gcc-obj')),
        }
        self.plan = dict(recipes=recipes, work_root=str(self.work), ports_export={'root': str(self.export)},
            output_bundle_root=str(self.localbase), resources={'cpu_jobs': 1},
            netbsd={'pkgsrc_tools': {'make': str(self.pkgtools / 'bmake'),
                                     'gmake': str(self.pkgtools / 'gmake'),
                                     'sh': str(self.pkgtools / 'mksh')},
                    'makeconf': {'path': str(self.makeconf)},
                    'utility_bin': str(self.utility_bin),
                    'binutils_as': str(self.localbase / 'bin/gas'),
                    'binutils_ld': str(self.localbase / 'bin/gld'),
                    'pkgsrc_mk': str(self.pkgtools / 'share/mk')})
        self.descriptor = {'driver_prefix': str(self.seed / 'gcc-driver'),
                           'bundle_root': str(self.root),
                           'tools': {role: {'path': path[0]} for role, path in {
                               'cc': (str(self.seed / 'bin/gcc'),),
                               'cxx': (str(self.seed / 'bin/g++'),)}.items()}}
        self.commands = {'cc': (str(self.seed / 'bin/gcc'),),
                         'cxx': (str(self.seed / 'bin/g++'),),
                         'cpp': (str(self.seed / 'bin/gcc'), '-E'),
                         'as': (str(self.seed / 'bin/as'),),
                         'ld': (str(self.seed / 'bin/ld'),),
                         'ar': (str(self.seed / 'bin/ar'),),
                         'ranlib': (str(self.seed / 'bin/ranlib'),),
                         'nm': (str(self.seed / 'bin/nm'),),
                         'strip': (str(self.seed / 'bin/strip'),),
                         'python': (str(self.seed / 'bin/python3'),)}

    def test_closed_steps_use_scoped_pkgsrc_tools_and_source_built_binutils(self):
        environment = {key: value[0] for key, value in self.commands.items()}
        with patch('emberbsd_sdk.bootstrap_recipe.recipe_order',
                   return_value=['devel/binutils', 'lang/gcc16']), \
             patch('emberbsd_sdk.bootstrap_seed._environment', return_value=environment):
            steps = bootstrap_netbsd_recipe.netbsd_recipe_steps(
                self.plan, self.descriptor, self.commands, {})
        self.assertEqual([row['id'] for row in steps],
                         ['r000-build', 'r000-install', 'r001-build', 'gcc16-compare', 'r001-install'])
        gcc_build = steps[2]
        self.assertEqual(gcc_build['env']['AS'], str(self.localbase / 'bin/gas'))
        self.assertEqual(gcc_build['env']['LD'], str(self.localbase / 'bin/gld'))
        self.assertIn('AS=' + str(self.localbase / 'bin/gas'), gcc_build['argv'])
        self.assertIn('LD=' + str(self.localbase / 'bin/gld'), gcc_build['argv'])
        self.assertEqual(steps[0]['argv'][0], str(self.pkgtools / 'bmake'))
        self.assertEqual(steps[0]['argv'][1:3], ['-m', str(self.pkgtools / 'share/mk')])
        self.assertNotIn('-j1', steps[0]['argv'])
        self.assertEqual(steps[0]['env']['MAKE_JOBS'], '1')
        self.assertEqual(steps[3]['argv'][0], str(self.pkgtools / 'gmake'))
        self.assertTrue(all(row['env']['SHELL'] == str(self.pkgtools / 'mksh') for row in steps))

    def test_gcc_refuses_seed_assembler_paths_and_unsatisfied_binutils_edge(self):
        with patch('emberbsd_sdk.bootstrap_recipe.recipe_order',
                   return_value=['lang/gcc16']), \
             patch('emberbsd_sdk.bootstrap_seed._environment', return_value={}):
            self.plan['netbsd'].update(binutils_as='/usr/bin/as', binutils_ld='/usr/bin/ld')
            with self.assertRaisesRegex(SDKError, 'source-built binutils'):
                bootstrap_netbsd_recipe.netbsd_recipe_steps(
                    self.plan, self.descriptor, self.commands, {})
        with patch('emberbsd_sdk.bootstrap_recipe.recipe_order',
                   return_value=['lang/gcc16']), \
             patch('emberbsd_sdk.bootstrap_seed._environment', return_value={}):
            self.plan['netbsd'].update(binutils_as=str(self.localbase / 'bin/gas'),
                                       binutils_ld=str(self.localbase / 'bin/gld'))
            with self.assertRaisesRegex(SDKError, 'source-built binutils provider'):
                bootstrap_netbsd_recipe.netbsd_recipe_steps(
                    self.plan, self.descriptor, self.commands, {})

    def test_bootstrap_plan_dispatches_to_netbsd_validator(self):
        value = {'format': bootstrap_netbsd_recipe.BOOTSTRAP_FORMAT if
                 hasattr(bootstrap_netbsd_recipe, 'BOOTSTRAP_FORMAT') else
                 'emberbsd-native-gcc16-bootstrap-plan',
                 'netbsd_profile': bootstrap_netbsd_recipe.PROFILE}
        path = self.root / 'plan.json'
        path.write_text(json.dumps(value), encoding='utf-8')
        ref = {'path': str(path), 'sha256': sha256_file(str(path))}
        with patch('emberbsd_sdk.bootstrap_netbsd_recipe.validate_netbsd_recipe_plan',
                   return_value=value) as validate:
            result = bootstrap_seed._plan(ref, {'abi_flags': {}}, {}, {})
        self.assertIs(result, value)
        validate.assert_called_once()

    def test_utility_roster_rejects_compiler_shell_and_make_roles(self):
        for role in ('cc', 'cxx', 'cpp', 'gcc', 'g++', 'clang', 'clang++',
                     'sh', 'make', 'bmake', 'gmake', 'as', 'ld', 'ar'):
            with self.subTest(role=role), self.assertRaisesRegex(SDKError, 'reserved role'):
                bootstrap_netbsd_recipe._validate_utility_roster(
                    str(self.utility_bin),
                    {role: dict(path=str(self.utility_bin / role), sha256='a' * 64)},
                    inspect_inputs=False)

    def test_utility_roster_requires_exact_executable_regular_members(self):
        awk = self.utility_bin / 'awk'
        awk.write_text('#!/bin/sh\nexit 0\n', encoding='ascii')
        reference = dict(path=str(awk), sha256=sha256_file(str(awk)))
        with self.assertRaisesRegex(SDKError, 'undeclared or missing'):
            (self.utility_bin / 'cc').write_text('#!/bin/sh\nexit 0\n', encoding='ascii')
            (self.utility_bin / 'cc').chmod(0o755)
            bootstrap_netbsd_recipe._validate_utility_roster(
                str(self.utility_bin), {'awk': reference})
        (self.utility_bin / 'cc').unlink()
        awk.chmod(0o644)
        with self.assertRaisesRegex(SDKError, 'regular executable'):
            bootstrap_netbsd_recipe._validate_utility_roster(
                str(self.utility_bin), {'awk': reference})
        awk.chmod(0o755)
        outside = self.root / 'outside-awk'
        outside.write_text('#!/bin/sh\nexit 0\n', encoding='ascii')
        outside.chmod(0o755)
        awk.unlink()
        awk.symlink_to(outside)
        reference['sha256'] = sha256_file(str(outside))
        with self.assertRaisesRegex(SDKError, 'regular executable'):
            bootstrap_netbsd_recipe._validate_utility_roster(
                str(self.utility_bin), {'awk': reference})

    def test_synthetic_complete_netbsd_plan_is_accepted_only_with_closed_binutils_edge(self):
        export_root = self.root / 'accepted-ports'
        recipes = {
            'devel/binutils': dict(dependencies=[]),
            'lang/gcc16': dict(dependencies=['devel/binutils'],
                               object_directory=str(self.root / 'build/gcc-obj')),
        }
        distfile = self.root / 'source.tar'
        distfile.write_bytes(b'synthetic pinned source archive')
        for name in recipes:
            recipe_root = export_root / name
            (recipe_root / 'patches').mkdir(parents=True)
            (recipe_root / 'Makefile').write_text('# common recipe fixture\n', encoding='utf-8')
            (recipe_root / 'patches/patch-fixture').write_text('fixed patch bytes\n', encoding='utf-8')
            recipes[name].update(directory=name, inventory=None, patches=['patches/patch-fixture'],
                distfiles=[dict(url='https://example.invalid/' + name + '.tar',
                    file=dict(path=str(distfile), sha256=sha256_file(str(distfile))),
                    relative=name.replace('/', '-') + '.tar')])
        profile_path = export_root / bootstrap_netbsd_recipe.PROFILE_FILE
        profile_path.write_text('fixed source-owned NetBSD profile fixture\n', encoding='utf-8')
        export_inventory = snapshot_tree(str(export_root))
        export_inventory.update({f'roster-padding/{index:06d}':
            {'kind': 'file', 'mode': 0o644, 'size': 1, 'sha256': '0' * 64}
            for index in range(175000)})
        inventory_path = self.root / 'source-inventory.json'
        inventory_path.write_text(json.dumps(export_inventory, sort_keys=True), encoding='utf-8')
        self.assertGreater(inventory_path.stat().st_size, 16 * 1024 * 1024)
        inventory_ref = dict(path=str(inventory_path), sha256=sha256_file(str(inventory_path)))
        for name in recipes:
            relative = name + '/'
            subtree = {path[len(relative):]: row for path, row in export_inventory.items()
                       if path.startswith(relative)}
            path = self.root / ('recipe-' + name.replace('/', '-') + '.json')
            path.write_text(json.dumps(subtree, sort_keys=True), encoding='utf-8')
            recipes[name]['inventory'] = dict(path=str(path), sha256=sha256_file(str(path)))
        makeconf = self.root / 'netbsd.mk.conf'
        utility_bin = self.root / 'utility-bin'
        utility_bin.mkdir()
        utility = utility_bin / 'awk'
        utility.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
        utility.chmod(0o755)
        pkgtools = dict(make=str(self.pkgtools / 'bmake'), gmake=str(self.pkgtools / 'gmake'),
                        sh=str(self.pkgtools / 'mksh'))
        sources = {
            'sdk': dict(repository='https://github.com/oxtech-ember/EmberBSD-SDK', commit='a' * 40),
            'ports': dict(repository='https://github.com/oxtech-ember/EmberBSD-Ports', commit='b' * 40),
        }
        seed_root = self.root / 'compiler-bundle'
        compiler_prefix = seed_root / 'prefix'
        (compiler_prefix / 'bin').mkdir(parents=True)
        descriptor = dict(host={'os': 'NetBSD', 'cpu': 'amd64'}, sources=sources,
            bundle_root=str(seed_root), tools={'cc': {'path': str(compiler_prefix / 'bin/gcc')},
                'cxx': {'path': str(compiler_prefix / 'bin/g++')},
                **{role: {'path': str(compiler_prefix / 'bin' / name)} for role, name in {
                    'as':'as', 'ld':'ld', 'ar':'ar', 'ranlib':'ranlib', 'nm':'nm', 'strip':'strip'}.items()}},
            driver_prefix=str(compiler_prefix / 'libexec/gcc16'),
            abi_flags={'cppflags': [], 'cflags': [], 'cxxflags': [], 'ldflags': []},
            make_binding=None)
        descriptor['tools']['awk'] = dict(path=str(utility), sha256=sha256_file(str(utility)))
        commands = {role: (str(self.seed / ('bin/' + name)),) for role, name in {
            'cc':'gcc', 'cxx':'g++', 'cpp':'cpp', 'as':'as', 'ld':'ld', 'ar':'ar',
            'ranlib':'ranlib', 'nm':'nm', 'strip':'strip', 'python':'python3', 'sh':'sh'}.items()}
        for role in ('cc', 'cxx', 'as', 'ld', 'ar', 'ranlib', 'nm', 'strip'):
            commands[role] = (descriptor['tools'][role]['path'],)
        work = self.root / 'build'
        resource_roots = (self.root / 'locks', self.root / 'heavy')
        for path in resource_roots:
            path.mkdir()
        plan = dict(format='emberbsd-native-gcc16-bootstrap-plan', version=1,
            scope='gcc16-bootstrap', purpose='first-native-gcc16', seed_descriptor={},
            host=descriptor['host'], sources=sources,
            output_compiler=dict(family='gcc', version='16.2.0', triplet='x86_64--netbsd'),
            ports_export=dict(root=str(export_root), source=sources['ports'],
                inventory=inventory_ref, source_proof=dict(path=str(inventory_path),
                    sha256=inventory_ref['sha256'])),
            recipes=recipes, work_root=str(work), output_bundle_root=str(work / 'output'),
            required_roles=sorted((*commands, 'make', 'gmake')),
            commands=[], comparison_command='gcc16-compare',
            resources=dict(cpu_jobs=1, link_jobs=1, runtime_seconds=100,
                lock_root=str(resource_roots[0]), heavy_link_context=str(resource_roots[1])),
            netbsd_profile=bootstrap_netbsd_recipe.PROFILE,
            netbsd=dict(pkgsrc_tools_acceptance=dict(path=str(self.root / 'pkgsrc-accept.json'), sha256='c' * 64),
                source_export_acceptance=dict(path=str(self.root / 'export-accept.json'), sha256='d' * 64),
                source_transfer_acceptance=dict(path=str(self.root / 'transfer-accept.json'), sha256='e' * 64),
                source_inventory=inventory_ref, ports_export_root=str(export_root),
                ports_source=sources['ports'], profile=dict(path=str(profile_path),
                    sha256=sha256_file(str(profile_path)),),
                host_facts=dict(os='NetBSD', cpu='x86_64', kernel_version='11.0',
                                pkgsrc_opsys_version=110000),
                utility_bin=str(utility_bin),
                utility_tools={'awk': dict(path=str(utility), sha256=sha256_file(str(utility)))},
                makeconf=dict(path=str(makeconf), sha256='f' * 64),
                pkgsrc_tools=pkgtools, pkgsrc_mk=str(self.root / 'pkgsrc/share/mk'),
                binutils_as=str(work / 'output/bin/gas'),
                binutils_ld=str(work / 'output/bin/gld')))
        makeconf.write_bytes(bootstrap_netbsd_recipe.render_netbsd_makeconf(plan, descriptor, commands))
        rendered_makeconf = makeconf.read_text(encoding='ascii')
        for binding in (
                'EMBERBSD_NETBSD_BOOTSTRAP=yes',
                'EMBERBSD_NETBSD_HOST_OS=NetBSD',
                'EMBERBSD_NETBSD_HOST_CPU=x86_64',
                'EMBERBSD_NETBSD_KERNEL_VERSION=11.0',
                'EMBERBSD_NETBSD_PKGSRC_OPSYS_VERSION=110000',
                'EMBERBSD_NETBSD_SH=' + pkgtools['sh'],
                'EMBERBSD_NETBSD_MAKE=' + pkgtools['make'],
                'EMBERBSD_NETBSD_GMAKE=' + pkgtools['gmake'],
                'EMBERBSD_NETBSD_UTILITY_BIN=' + str(utility_bin),
                'EMBERBSD_NETBSD_NATIVE_TOOLS=awk',
                'EMBERBSD_NETBSD_JOBS=1',
                'EMBERBSD_GCC16_SEED_CC=' + commands['cc'][0],
                'EMBERBSD_GCC16_SEED_CXX=' + commands['cxx'][0],
                'EMBERBSD_GCC16_SEED_CPP=' + commands['cpp'][0],
                'EMBERBSD_GCC16_AS=' + commands['as'][0],
                'EMBERBSD_GCC16_LD=' + commands['ld'][0],
                'EMBERBSD_NETBSD_TOOL.awk=' + str(utility),
                'MAKE_JOBS=1',
                'PKGSRC_COMPILER=gcc',
                'GCC_REQD=16',
                'GCCBASE=' + str(compiler_prefix),
                'LOCALBASE=' + str(work / 'output'),
        ):
            self.assertIn(binding, rendered_makeconf)
        self.assertNotIn('\nHOST_OS=', rendered_makeconf)
        self.assertNotIn('\nJOBS=', rendered_makeconf)
        plan['netbsd']['makeconf']['sha256'] = sha256_file(str(makeconf))
        plan['commands'] = bootstrap_netbsd_recipe.netbsd_recipe_steps(plan, descriptor, commands,
                                                                        descriptor['abi_flags'])
        source_proof = {'ports_source': sources['ports'], 'fixture': True}
        transfer = dict(scope='pkgsrc-source-transfer', source_proof=source_proof,
            output_root=str(export_root), output_inventory=inventory_ref)
        with patch('emberbsd_sdk.bootstrap_netbsd_recipe.platform.system', return_value='NetBSD'), \
             patch('emberbsd_sdk.bootstrap_netbsd_recipe.platform.machine', return_value='amd64'), \
             patch('emberbsd_sdk.bootstrap_netbsd_recipe.platform.release', return_value='11.0'), \
             patch('emberbsd_sdk.bootstrap_netbsd_recipe._accepted_sources',
                   return_value=(plan['netbsd']['source_export_acceptance'],
                                 plan['netbsd']['source_transfer_acceptance'], transfer)), \
             patch('emberbsd_sdk.bootstrap_netbsd_gcc16.load_pkgsrc_bootstrap_tools',
                   return_value=type('Tools', (), {'command': lambda self, role: (pkgtools[role],),
                                                  'pkgsrc_mk': plan['netbsd']['pkgsrc_mk']})()), \
             patch('emberbsd_sdk.bootstrap_netbsd_recipe.snapshot_tree',
                   side_effect=lambda path: export_inventory if path == str(export_root) else
                   snapshot_tree(path)):
            self.assertIs(bootstrap_netbsd_recipe.validate_netbsd_recipe_plan(
                plan, descriptor, commands, descriptor['abi_flags']), plan)
            admitted_awk = descriptor['tools'].pop('awk')
            with self.assertRaisesRegex(SDKError, 'utility.*accepted seed'):
                bootstrap_netbsd_recipe.validate_netbsd_recipe_plan(
                    plan, descriptor, commands, descriptor['abi_flags'])
            descriptor['tools']['awk'] = admitted_awk
            for key, value in (('path', str(self.root / 'other-awk')),
                               ('sha256', '0' * 64)):
                with self.subTest(seed_utility_mismatch=key):
                    descriptor['tools']['awk'] = dict(admitted_awk, **{key: value})
                    with self.assertRaisesRegex(SDKError, 'utility.*accepted seed'):
                        bootstrap_netbsd_recipe.validate_netbsd_recipe_plan(
                            plan, descriptor, commands, descriptor['abi_flags'])
            descriptor['tools']['awk'] = admitted_awk
            # SDK controller Python is separate from native recipe generators.
            # This graph consumes make roles and needs no initial Python tool.
            del commands['python']
            plan['required_roles'].remove('python')
            plan['commands'] = bootstrap_netbsd_recipe.netbsd_recipe_steps(
                plan, descriptor, commands, descriptor['abi_flags'])
            self.assertIs(bootstrap_netbsd_recipe.validate_netbsd_recipe_plan(
                plan, descriptor, commands, descriptor['abi_flags']), plan)
            self.assertNotIn('PYTHON', plan['commands'][0]['env'])
            plan['required_roles'].append('python')
            with self.assertRaisesRegex(SDKError, 'required native role'):
                bootstrap_netbsd_recipe.validate_netbsd_recipe_plan(
                    plan, descriptor, commands, descriptor['abi_flags'])
            plan['required_roles'].remove('python')
            plan['recipes']['lang/gcc16']['dependencies'] = []
            with self.assertRaises(SDKError):
                bootstrap_netbsd_recipe.validate_netbsd_recipe_plan(
                    plan, descriptor, commands, descriptor['abi_flags'])


class NetBSDRecipeRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.work = self.root / 'work'
        self.output = self.work / 'output'
        self.lock = self.root / 'locks'
        self.heavy = self.root / 'heavy-link'
        for path in (self.root / 'bundle', self.root / 'ports', self.lock, self.heavy):
            path.mkdir()
        self.makeconf = self.root / 'mk.conf'
        self.makeconf.write_bytes(b'# synthetic fixed profile\n')
        self.receipts = {}
        for name in ('pkgsrc', 'export', 'transfer', 'accepted-seed'):
            path = self.root / f'{name}.json'
            path.write_text('{}\n', encoding='utf-8')
            self.receipts[name] = dict(path=str(path), sha256=sha256_file(str(path)))
        self.descriptor = dict(host={'os': 'NetBSD', 'cpu': 'amd64'},
            bundle_root=str(self.root / 'bundle'), host_abi={'headers': [], 'libraries': []},
            make_binding=None)
        self.descriptor_path = self.root / 'seed.json'
        self.descriptor_path.write_text(json.dumps(self.descriptor), encoding='utf-8')
        self.descriptor_ref = dict(path=str(self.descriptor_path),
                                   sha256=sha256_file(str(self.descriptor_path)))
        self.acceptance_ref = self.receipts['accepted-seed']
        self.work.mkdir() if self.work.exists() else None
        self.plan = dict(format='emberbsd-native-gcc16-bootstrap-plan', version=1,
            scope='gcc16-bootstrap', purpose='first-native-gcc16',
            seed_descriptor=self.descriptor_ref, host=self.descriptor['host'], sources={},
            output_compiler={}, ports_export={'root': str(self.root / 'ports')}, recipes={},
            work_root=str(self.work), output_bundle_root=str(self.output), required_roles=['make'],
            commands=[dict(id='r000-build', recipe='devel/binutils', phase='build', role='make',
                           argv=['/accepted/bmake', '-C', '/ports/devel/binutils', 'build'],
                           cwd=str(self.work), env={'PATH': '/accepted'})],
            comparison_command='gcc16-compare', netbsd_profile=bootstrap_netbsd_recipe.PROFILE,
            netbsd=dict(pkgsrc_tools_acceptance=self.receipts['pkgsrc'],
                source_export_acceptance=self.receipts['export'],
                source_transfer_acceptance=self.receipts['transfer'],
                source_inventory=self.receipts['transfer'], ports_export_root=str(self.root / 'ports'),
                ports_source={}, makeconf=dict(path=str(self.makeconf), sha256=sha256_file(str(self.makeconf))),
                pkgsrc_tools={}, binutils_as=str(self.output / 'bin/gas'),
                binutils_ld=str(self.output / 'bin/gld')),
            resources=dict(cpu_jobs=1, link_jobs=1, runtime_seconds=90,
                           lock_root=str(self.lock), heavy_link_context=str(self.heavy)))
        self.plan_path = self.root / 'plan.json'
        self.plan_path.write_text(json.dumps(self.plan), encoding='utf-8')
        self.plan_ref = dict(path=str(self.plan_path), sha256=sha256_file(str(self.plan_path)))

    def _selection(self):
        class Selection:
            def as_dict(inner):
                return {'plan': self.plan}
        return Selection()

    def _prepare(self, plan):
        for name in ('distfiles', 'work', 'pkgdb', 'packages', 'home'):
            Path(plan['work_root'], name).mkdir()
        Path(plan['output_bundle_root']).mkdir()

    def _invoke(self, run_logged):
        with patch('emberbsd_sdk.bootstrap_seed.load_bootstrap_seed', return_value=self._selection()), \
             patch('emberbsd_sdk.bootstrap_netbsd_recipe.prepare_netbsd_recipe_work',
                   side_effect=self._prepare), \
             patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=run_logged):
            return bootstrap_pilot.run_bootstrap(self.descriptor_ref['path'],
                descriptor_sha256=self.descriptor_ref['sha256'],
                accepted_seed_receipt=self.acceptance_ref, bootstrap_plan=self.plan_ref,
                required_roles=('make',))

    def test_netbsd_controller_dispatches_drains_and_never_qualifies(self):
        def fake(argv, log, **kwargs):
            Path(log).write_bytes(b'closed synthetic route log\n')
            (self.output / 'bin').mkdir(parents=True, exist_ok=True)
            (self.output / 'bin/gas').write_bytes(b'synthetic output evidence\n')
            return subprocess.CompletedProcess(argv, 0)
        result = self._invoke(fake)
        self.assertEqual(result['status'], 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW')
        self.assertTrue(result['all_commands_drained'])
        self.assertTrue(result['all_leases_closed'])
        self.assertTrue(result['inputs_revalidated_after_drain'])
        self.assertFalse(result['ordinary_provider_qualified'])
        self.assertEqual(len(result['commands']), 1)
        self.assertEqual(result['commands'][0]['actual_exit'], 0)

    def test_netbsd_controller_retains_failed_command_and_does_not_retry(self):
        def fail(argv, log, **kwargs):
            Path(log).write_bytes(b'planned command failed\n')
            raise CommandError(9, argv, log=log)
        result = self._invoke(fail)
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(result['commands'][0]['actual_exit'], 9)
        self.assertTrue(result['all_commands_drained'])
        self.assertEqual(len(result['commands']), 1)

    def test_netbsd_controller_revalidates_seed_after_drain(self):
        class Selection:
            def as_dict(inner):
                return {'plan': self.plan}
        def fake(argv, log, **kwargs):
            Path(log).write_bytes(b'route log\n')
            (self.output / 'bin').mkdir(parents=True, exist_ok=True)
            (self.output / 'bin/gas').write_bytes(b'evidence\n')
            return subprocess.CompletedProcess(argv, 0)
        calls = []
        def load(*args, **kwargs):
            calls.append(True)
            if len(calls) == 2:
                raise SDKError('accepted input changed after child drainage')
            return Selection()
        with patch('emberbsd_sdk.bootstrap_seed.load_bootstrap_seed', side_effect=load), \
             patch('emberbsd_sdk.bootstrap_netbsd_recipe.prepare_netbsd_recipe_work',
                   side_effect=self._prepare), \
             patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=fake):
            result = bootstrap_pilot.run_bootstrap(self.descriptor_ref['path'],
                descriptor_sha256=self.descriptor_ref['sha256'],
                accepted_seed_receipt=self.acceptance_ref, bootstrap_plan=self.plan_ref,
                required_roles=('make',))
        self.assertEqual(len(calls), 2)
        self.assertEqual(result['status'], 'FAILED')
        self.assertFalse(result['inputs_revalidated_after_drain'])
        self.assertTrue(result['all_commands_drained'])


if __name__ == '__main__':
    unittest.main()
