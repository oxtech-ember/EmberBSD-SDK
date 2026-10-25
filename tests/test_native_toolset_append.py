# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Pure append-only evidence fixtures. No native tool runs or qualifies here."""

import copy
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from test_bootstrap_seed import SeedFixture
from test_native_toolset import Fixture, digest
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.inventory import snapshot_tree
from emberbsd_sdk.native_toolset import NATIVE_TOOL_ROLES, load_native_toolset


class FoundationAppendTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name).resolve()
        for name in ('seed', 'output'):
            (base / name).mkdir()
        self.seed = SeedFixture(base / 'seed')
        self.fixture = f = Fixture(base / 'output')
        (f.bundle / 'support.dat').write_bytes(b'original provider data')
        (f.bundle / 'support-link').symlink_to('support.dat')
        f.providers['support'] = dict(source='ports', recipe='devel/gmp',
                                     recipe_sha256=digest(self.seed.export / 'devel/gmp/Makefile'))
        for name, value in (('system', 'Linux'), ('machine', 'x86_64')):
            guard = patch('emberbsd_sdk.native_toolset.platform.' + name, return_value=value)
            guard.start()
            self.addCleanup(guard.stop)
        self.seed.graduate(f)
        self.support_build = copy.deepcopy(f.build)
        self.support_build.update(recipe='devel/gmp', recipe_sha256=f.providers['support']['recipe_sha256'])
        f.receipt['provider_builds']['support'] = f.document(self.support_build)
        f.accept_receipt()
        f.load()
        self.original = copy.deepcopy(f.descriptor)
        self.accepted = copy.deepcopy(f.receipt)
        self.old_build = copy.deepcopy(f.build)
        self.original_ref = copy.deepcopy(f.descriptor_ref)
        self.accepted_ref = copy.deepcopy(f.receipt_ref)
        self.old_checks = copy.deepcopy(f.receipt['checks'])
        self.fresh_checks()
        path = f.bundle / 'gn'
        path.write_bytes(f.image)
        path.chmod(0o755)
        f.records['gn'] = dict(path=str(path), sha256=digest(path), version='fixture-2',
                               provider='gn', kind='native')
        f.sources['gn-source'] = dict(repository='https://example.invalid/gn', commit='4' * 40)
        f.providers['gn'] = dict(source='gn-source', recipe='devel/gn', recipe_sha256='3' * 64)
        self.new_build = dict(format='emberbsd-native-provider-build', version=1, scope='native-build',
                              status='PASS', host=f.host, source=f.sources['gn-source'],
                              recipe='devel/gn', recipe_sha256='3' * 64, compiler=f.compiler,
                              accepted_input_scope='qualified-native', build_log=f.raw(b'new synthetic build'))
        f.build['original_output'] = dict(descriptor=self.original_ref,
                                          qualification=self.accepted_ref, provider='ports')
        self.support_build['original_output'] = dict(descriptor=self.original_ref,
                                                      qualification=self.accepted_ref, provider='support')
        self.seal()

    def fresh_checks(self):
        f, replacements = self.fixture, {}

        def copy_files(value):
            if isinstance(value, dict):
                if set(value) == {'path', 'sha256'}:
                    old = Path(value['path'])
                    f.serial += 1
                    new = f.evidence / ('fresh-' + str(f.serial) + old.suffix)
                    shutil.copy2(old, new)
                    replacements[str(old)] = str(new)
                    return dict(path=str(new), sha256=digest(new))
                return {key: copy_files(row) for key, row in value.items()}
            if isinstance(value, list):
                return [copy_files(row) for row in value]
            return value

        def rebind(value):
            if isinstance(value, dict):
                return {key: rebind(row) for key, row in value.items()}
            if isinstance(value, list):
                return [rebind(row) for row in value]
            return replacements.get(value, value) if isinstance(value, str) else value

        f.receipt['checks'] = rebind(copy_files(f.receipt['checks']))

    def seal(self):
        f = self.fixture
        f.seal()
        f.receipt['provider_builds']['support'] = f.document(self.support_build)
        f.receipt['provider_builds']['gn'] = f.document(self.new_build)
        audit = json.loads(Path(f.receipt['execution_audit']['path']).read_bytes())
        audit['dynamic_dependencies'] = SeedFixture.dynamic(f.descriptor)
        f.receipt['execution_audit'] = f.document(audit)
        f.accept_receipt()

    def refuse(self, message=None):
        with self.assertRaisesRegex(SDKError, message or '.'):
            self.fixture.load(required_roles=(*NATIVE_TOOL_ROLES, 'gn'))

    def test_append_only_qualifies_new_selection_without_relabeling_original(self):
        selected = self.fixture.load(required_roles=(*NATIVE_TOOL_ROLES, 'gn'))
        self.assertEqual(selected.tool('gn'), self.fixture.records['gn']['path'])
        self.assertEqual(self.fixture.build['accepted_input_scope'], 'bootstrap-seed')
        self.assertEqual(self.fixture.build['bootstrap_graduation'], self.old_build['bootstrap_graduation'])
        self.assertEqual(json.loads(Path(self.original_ref['path']).read_bytes()), self.original)
        self.assertEqual(json.loads(Path(self.accepted_ref['path']).read_bytes()), self.accepted)

    def test_public_old_load_still_requires_exact_whole_inventory(self):
        with self.assertRaisesRegex(SDKError, 'native bundle inventory changed'):
            load_native_toolset(self.original_ref['path'], descriptor_sha256=self.original_ref['sha256'],
                                accepted_producer_receipt=self.accepted_ref, required_roles=NATIVE_TOOL_ROLES)

    def test_changed_old_non_tool_bytes_are_refused_even_with_new_inventory(self):
        (self.fixture.bundle / 'support.dat').write_bytes(b'changed provider data')
        self.seal()
        self.refuse('original native bundle entries changed')

    def test_changed_old_mode_and_symlink_target_are_refused(self):
        f = self.fixture
        for change in ('mode', 'link'):
            with self.subTest(change=change):
                if change == 'mode':
                    (f.bundle / 'support.dat').chmod(0o600)
                else:
                    (f.bundle / 'support.dat').chmod(0o644)
                    (f.bundle / 'support-link').unlink()
                    (f.bundle / 'support-link').symlink_to('gn')
                self.seal()
                self.refuse('original native bundle entries changed')

    def test_changed_source_and_abi_are_refused(self):
        f = self.fixture
        f.sources['sdk'] = dict(repository='https://example.invalid/sdk', commit='5' * 40)
        self.seal()
        self.refuse('original output sources changed')
        f.sources['sdk'] = copy.deepcopy(self.original['sources']['sdk'])
        f.descriptor['abi_flags']['cflags'] = ['-O2']
        f.bind_probe_flags()
        self.seal()
        self.refuse('original output abi_flags changed')

    def test_changed_provider_and_driver_mapping_are_refused(self):
        f = self.fixture
        f.providers['ports']['recipe_sha256'] = '6' * 64
        f.build['recipe_sha256'] = '6' * 64
        self.seal()
        self.refuse('original output providers changed')
        f.providers['ports']['recipe_sha256'] = self.original['providers']['ports']['recipe_sha256']
        f.build['recipe_sha256'] = self.old_build['recipe_sha256']
        f.descriptor['driver_components']['cc1'] = 'cc1plus'
        self.seal()
        self.refuse('original output driver_components changed')

    def test_changed_abi_inventory_cannot_be_rebound(self):
        f = self.fixture
        abi = copy.deepcopy(f.descriptor['host_abi'])
        root = Path(abi['headers'][0]['root'])
        (root / 'fixture-input').write_bytes(b'different ABI headers')
        abi['headers'][0]['inventory'] = f.document(snapshot_tree(str(root)))
        f.descriptor['host_abi'] = abi
        f.receipt['host_abi'] = abi
        self.seal()
        self.refuse('original output host_abi changed')

    def test_changed_inherited_tool_record_is_refused(self):
        self.fixture.records['make']['version'] = 'changed'
        self.seal()
        self.refuse('original output tools changed')

    def test_another_inherited_provider_cannot_drop_its_scope_or_graduation(self):
        self.support_build['accepted_input_scope'] = 'qualified-native'
        del self.support_build['bootstrap_graduation']
        del self.support_build['original_output']
        self.seal()
        self.refuse('original bootstrap build changed')

    def test_unknown_input_scope_and_scope_relabel_are_refused(self):
        f = self.fixture
        for value in ('unknown', 'qualified-native'):
            with self.subTest(value=value):
                f.build['accepted_input_scope'] = value
                self.seal()
                self.refuse('direct bootstrap provider reuse')

    def test_added_provider_needs_a_real_qualified_native_build(self):
        self.new_build['accepted_input_scope'] = 'unknown'
        self.seal()
        self.refuse('not an ordinary native provider')

    def test_original_accepted_receipt_is_required_and_validated(self):
        f = self.fixture
        del f.build['original_output']['qualification']
        self.seal()
        self.refuse('invalid original bootstrap output reference')
        failed = copy.deepcopy(self.accepted)
        failed['checks']['cc']['compile']['actual_exit'] = 1
        f.build['original_output']['qualification'] = f.document(failed)
        self.support_build['original_output']['qualification'] = f.build['original_output']['qualification']
        self.seal()
        self.refuse('successful actual command')

    def test_changed_original_graduation_is_not_hidden_by_reuse(self):
        self.fixture.build['bootstrap_graduation'] = self.fixture.raw(b'not the original graduation')
        self.seal()
        self.refuse('original bootstrap build changed')

    def test_old_checks_and_hardlinked_check_files_are_not_fresh(self):
        f = self.fixture
        f.receipt['checks'] = copy.deepcopy(self.old_checks)
        self.seal()
        self.refuse('fresh probes, queries and audit')
        self.fresh_checks()
        new = Path(f.receipt['checks']['cc']['source']['path'])
        new.unlink()
        os.link(self.old_checks['cc']['source']['path'], new)
        self.seal()
        self.refuse('fresh probes, queries and audit')

    def test_fresh_failed_probe_and_incomplete_runtime_closure_are_refused(self):
        f = self.fixture
        f.receipt['checks']['cxx']['link']['actual_exit'] = 1
        self.seal()
        self.refuse('successful actual command')
        f.receipt['checks']['cxx']['link']['actual_exit'] = 0
        self.seal()
        audit = json.loads(Path(f.receipt['execution_audit']['path']).read_bytes())
        del audit['dynamic_dependencies'][f.records['gn']['path']]
        f.receipt['execution_audit'] = f.document(audit)
        f.accept_receipt()
        self.refuse('dynamic closure omits native tools')

    def test_nested_reuse_and_self_reference_are_refused(self):
        f = self.fixture
        nested = copy.deepcopy(self.old_build)
        nested['original_output'] = copy.deepcopy(f.build['original_output'])
        accepted = copy.deepcopy(self.accepted)
        accepted['provider_builds']['ports'] = f.document(nested)
        f.build['original_output']['qualification'] = f.document(accepted)
        self.seal()
        self.refuse('nested reuse')
        f.build['original_output'] = dict(descriptor=f.descriptor_ref,
                                         qualification=f.receipt_ref, provider='ports')
        self.seal()
        self.refuse('nested reuse')

    def test_original_runtime_revalidation_failure_is_propagated(self):
        # Fault injection checks propagation. Darwin graph tests own rpath resolution.
        from emberbsd_sdk import bootstrap_seed
        validate = bootstrap_seed._dynamic_closure

        def reject_shadow(value, descriptor):
            validate(value, descriptor)
            if descriptor['inventory'] == self.original['inventory']:
                raise SDKError('original runtime acquired an rpath shadow')

        with patch('emberbsd_sdk.bootstrap_seed._dynamic_closure', side_effect=reject_shadow):
            self.refuse('rpath shadow')


if __name__ == '__main__':
    unittest.main()
