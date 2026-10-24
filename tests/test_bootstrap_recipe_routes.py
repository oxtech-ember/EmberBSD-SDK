# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""AI-assisted metadata regressions for the fixed Darwin recipe tool routes."""

import copy
import json
import os
from pathlib import Path
import shlex
import struct
import tempfile
import unittest
from unittest.mock import patch

from test_bootstrap_seed import SeedFixture
from test_native_toolset import digest
from emberbsd_sdk import bootstrap_binding
from emberbsd_sdk.bootstrap_seed import _environment
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.inventory import snapshot_tree


class DarwinRecipeRoutesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.seed = SeedFixture(Path(self.temp.name))
        f = self.seed.native
        d = self.seed.descriptor
        host = dict(os='Darwin', cpu='arm64')
        compiler = dict(family='gcc', version='16.2.0', triplet='aarch64-apple-darwin25')
        d.update(host=host, compiler=compiler)
        self.seed.plan.update(host=host, output_compiler=compiler)
        linker = self.seed.prefix / 'ld64.lld'
        Path(d['tools']['ld']['path']).rename(linker)
        d['tools']['ld']['path'] = str(linker)
        (self.seed.prefix / 'ld').symlink_to('ld64.lld')
        dsymutil = f.bundle / 'dsymutil'
        dsymutil.write_bytes(f.image)
        dsymutil.chmod(0o755)
        d['tools']['dsymutil'] = dict(d['tools']['strip'], path=str(dsymutil))
        image = bytearray(64)
        struct.pack_into('<IIII', image, 0, 0xfeedfacf, 0x0100000c, 0, 2)
        for row in d['tools'].values():
            Path(row['path']).write_bytes(image)
            row['sha256'] = digest(Path(row['path']))
        d['dynamic_dependencies'] = self.seed.dynamic(d)
        for role in ('cc', 'cxx'):
            f.receipt['checks']['identity'][role]['-dumpmachine']['stdout'] = f.raw(
                b'aarch64-apple-darwin25\n')
        # Synthetic source bytes keep the Ports template at its owner.
        template = f.bundle / 'template.specs.in'
        specs = f.bundle / 'seed.specs'
        template.write_bytes(b'fixed @DEPLOYMENT@ @SDK_VERSION@ fixture\n')
        specs.write_bytes(b'fixed 26.0 26.5 fixture\n')
        self.sdk = Path(d['host_abi']['headers'][0]['root'])
        metadata = self.sdk / 'SDKSettings.json'
        metadata.write_text(json.dumps(dict(Version='26.5', CanonicalName='macosx26.5',
            DefaultVariant='macos', SupportedTargets=dict(macosx=dict(BuildVersionPlatformID='1')))))
        d['host_abi']['headers'][0]['inventory'] = f.document(snapshot_tree(str(self.sdk)))
        ref = lambda p: dict(path=str(p), sha256=digest(p))
        self.binding = dict(kind=bootstrap_binding.DARWIN_LLVM_MC_LLD_SEED,
            ports_source=copy.deepcopy(d['sources']['ports']),
            template_relative=bootstrap_binding._LLD_TEMPLATE_RELATIVE,
            template=ref(template), instantiated=ref(specs), deployment='26.0',
            sdk_version='26.5', sdk_metadata=ref(metadata),
            reported_programs=dict(ld=str(self.seed.prefix / 'ld')))
        d['driver_binding'] = self.binding
        for name, value in (('_LLD_TEMPLATE_SHA256', digest(template)),
                            ('_LLD_INSTANTIATED_SHA256', {'26.5': digest(specs)})):
            mock = patch.object(bootstrap_binding, name, value)
            mock.start()
            self.addCleanup(mock.stop)
        self.seed.commands = {
            role: (row['path'], '-B' + str(self.seed.prefix) + os.sep,
                   '-specs=' + str(specs), '-mmacosx-version-min=26.0')
            if role in ('cc', 'cxx', 'cpp') else (row['path'],)
            for role, row in d['tools'].items()}
        self.legacy_environment = _environment(
            {role: row['path'] for role, row in d['tools'].items()},
            self.seed.commands, d['abi_flags'])
        self.routes = {
            'EMBERBSD_GCC16_SEED_CC': shlex.join(self.seed.commands['cc']),
            'EMBERBSD_GCC16_SEED_CXX': shlex.join(self.seed.commands['cxx']),
            'EMBERBSD_GCC16_SEED_CPP': shlex.join(self.seed.commands['cpp']),
            'EMBERBSD_GCC16_LLVM_MC': d['tools']['as']['path'],
            'EMBERBSD_GCC16_LD64_LLD': str(linker),
            'EMBERBSD_GCC16_DSYMUTIL': str(dsymutil),
            'EMBERBSD_GCC16_AR': d['tools']['ar']['path'],
            'EMBERBSD_GCC16_RANLIB': d['tools']['ranlib']['path'],
            'EMBERBSD_GCC16_NM': d['tools']['nm']['path'],
            'EMBERBSD_GCC16_STRIP': d['tools']['strip']['path'],
            'OSX_SDK_PATH': str(self.sdk),
            'EMBERBSD_GCC16_DARWIN_SDK_VERSION': '26.5'}
        for step in self.seed.plan['commands']:
            step['env'] = dict(self.legacy_environment)
        self.seed.plan['required_roles'] += ['dsymutil', 'as', 'ld', 'ar', 'ranlib', 'nm', 'strip']
        self.seal()
        for name, value in (('system', 'Darwin'), ('machine', 'arm64')):
            mock = patch('emberbsd_sdk.native_toolset.platform.' + name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)

    def seal(self):
        self.seed.seal()
        self.seed.acceptance['driver_binding'] = copy.deepcopy(self.binding)
        query = self.seed.acceptance['checks']['driver_components']['ld']
        query['stdout'] = self.seed.native.raw((self.binding['reported_programs']['ld'] + '\n').encode())
        self.seed.acceptance_ref = self.seed.native.document(self.seed.acceptance)

    def test_legacy_environment_cannot_hide_a_recipe_compiler_override(self):
        self.seed.plan['commands'][1]['argv'].append('EMBERBSD_GCC16_SEED_CC=/usr/bin/clang')
        self.seal()
        with self.assertRaises(SDKError):
            self.seed.load()

    def test_recipe_binds_the_accepted_darwin_sdk_version(self):
        descriptor = self.seed.descriptor
        env = _environment({role: row['path'] for role, row in descriptor['tools'].items()},
                           self.seed.commands, descriptor['abi_flags'], descriptor)
        self.assertEqual(env.get('EMBERBSD_GCC16_DARWIN_SDK_VERSION'), '26.5')

    def valid_plan(self):
        for step in self.seed.plan['commands']:
            step['env'] = dict(self.legacy_environment, **self.routes)
        self.seal()

    def test_full_plan_binds_the_complete_ports_recipe_tool_routes(self):
        self.valid_plan()
        selected = self.seed.load()
        for step in selected.as_dict()['plan']['commands']:
            for name, expected in self.routes.items():
                self.assertEqual(step['env'][name], expected)
        for role in ('cc', 'cxx', 'cpp'):
            self.assertEqual(selected.command(role), self.seed.commands[role])
        self.assertEqual(selected.abi_flags['cflags'], ())

    def test_every_recipe_environment_selector_is_required_and_exact(self):
        self.valid_plan()
        original = copy.deepcopy(self.seed.plan['commands'][1]['env'])
        for name in self.routes:
            for change in ('missing', 'conflicting'):
                with self.subTest(name=name, change=change):
                    env = self.seed.plan['commands'][1]['env'] = dict(original)
                    if change == 'missing':
                        env.pop(name)
                    else:
                        env[name] = '/usr/bin/clang'
                    self.seal()
                    with self.assertRaisesRegex(SDKError, 'accepted native routes'):
                        self.seed.load()

    def test_make_assignments_cannot_replace_the_bound_ports_tool_selectors(self):
        self.valid_plan()
        original = list(self.seed.plan['commands'][1]['argv'])
        for name in self.routes:
            for operator in ('=', '?=', '+=', ':=', '!='):
                with self.subTest(name=name, operator=operator):
                    self.seed.plan['commands'][1]['argv'] = [*original,
                        name + operator + '/usr/bin/clang']
                    self.seal()
                    with self.assertRaisesRegex(SDKError, 'overrides an accepted native route|assignment operator'):
                        self.seed.load()

    def test_identical_direct_make_selector_is_permitted(self):
        self.valid_plan()
        step = self.seed.plan['commands'][1]
        step['argv'] += [name + '=' + value for name, value in self.routes.items()]
        self.seal()
        self.seed.load()

    def test_recipe_requires_an_explicit_dsymutil_member(self):
        self.valid_plan()
        self.seed.descriptor['tools'].pop('dsymutil')
        self.seed.commands.pop('dsymutil')
        self.seed.plan['required_roles'].remove('dsymutil')
        self.seed.descriptor['dynamic_dependencies'] = self.seed.dynamic(self.seed.descriptor)
        self.seal()
        with self.assertRaisesRegex(SDKError, 'missing fixed Darwin recipe tool role: dsymutil'):
            self.seed.load()

    def test_extra_environment_and_another_abi_sdk_are_refused(self):
        self.valid_plan()
        original = copy.deepcopy(self.seed.plan['commands'][1]['env'])
        for name, value, reason in (('SDKROOT', str(self.sdk), 'environment override'),
                                    ('OSX_SDK_PATH', '/another/SDK', 'accepted native routes')):
            with self.subTest(name=name):
                self.seed.plan['commands'][1]['env'] = dict(original, **{name: value})
                self.seal()
                with self.assertRaisesRegex(SDKError, reason):
                    self.seed.load()

    def test_recipe_does_not_infer_a_header_root_for_the_old_binding(self):
        descriptor = copy.deepcopy(self.seed.descriptor)
        descriptor['driver_binding']['kind'] = bootstrap_binding.DARWIN_LLVM_MC_SEED
        descriptor['driver_binding'].pop('sdk_metadata')
        with self.assertRaisesRegex(SDKError, 'fixed v2 SDK metadata binding'):
            _environment({role: row['path'] for role, row in descriptor['tools'].items()},
                         self.seed.commands, descriptor['abi_flags'], descriptor)


    def test_make_assignment_names_cannot_hide_a_selector_with_space_or_expansion(self):
        self.valid_plan()
        original = list(self.seed.plan['commands'][1]['argv'])
        names = (' EMBERBSD_GCC16_SEED_CC', 'EMBERBSD_GCC16_SEED_CC ',
                 'EMBERBSD_GCC16_SEED_CC ?', 'EMBERBSD_GCC16_SEED_CC+ ',
                 'EMBERBSD_GCC16_SEED_CC :', 'EMBERBSD_GCC16_SEED_CC ! ',
                 '${ROUTE}', '$(ROUTE)', '${:UEMBERBSD_GCC16_SEED_CC}')
        for name in names:
            with self.subTest(name=name):
                self.seed.plan['commands'][1]['argv'] = [*original, name + '=/usr/bin/clang']
                self.seal()
                with self.assertRaises(SDKError):
                    self.seed.load()

    def test_make_eval_cannot_inject_a_compiler_assignment(self):
        self.valid_plan()
        original = list(self.seed.plan['commands'][1]['argv'])
        assignment = 'EMBERBSD_GCC16_SEED_CC=/usr/bin/clang'
        for tail in (['--eval=' + assignment], ['--eval', assignment],
                     ['--ev=' + assignment], ['--eva', assignment],
                     ['-E' + assignment], ['-E', assignment]):
            with self.subTest(tail=tail):
                self.seed.plan['commands'][1]['argv'] = [*original, *tail]
                self.seal()
                with self.assertRaisesRegex(SDKError, 'replaces its declared recipe'):
                    self.seed.load()

    def test_literal_package_names_and_long_make_options_remain_supported(self):
        self.valid_plan()
        self.seed.plan['commands'][1]['argv'] += ['PKG_OPTIONS.gcc16=-nls',
                                                'package-policy=bootstrap', '--jobs=2']
        self.seal()
        self.seed.load()


    def test_short_option_clusters_cannot_replace_the_recipe_or_evaluate_make_code(self):
        self.valid_plan()
        original = list(self.seed.plan['commands'][1]['argv'])
        for word in ('-sf/unpinned.mk', '-ksf/unpinned.mk', '-nEuntrusted-body',
                     '-sC/unpinned-directory'):
            with self.subTest(word=word):
                self.seed.plan['commands'][1]['argv'] = [*original, word]
                self.seal()
                with self.assertRaisesRegex(SDKError, 'replaces its declared recipe'):
                    self.seed.load()


if __name__ == '__main__':
    unittest.main()
