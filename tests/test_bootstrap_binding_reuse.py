# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Binding reuse metadata controls. No native or historical payload reads."""
import copy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_bootstrap_pilot as fixtures
from emberbsd_sdk import bootstrap_binding as binding
from emberbsd_sdk import bootstrap_compat_component as component
from emberbsd_sdk.errors import SDKError


class BindingReuseTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.FixedLLDBindingTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.old = copy.deepcopy(self.fixture.descriptor)
        self.old.update(compiler=dict(family='gcc', major=16),
                        abi_flags=dict(cflags=['-isysroot', '/selected/abi']),
                        driver_components=dict(ld='ld'))
        self.current = copy.deepcopy(self.old)
        self.current['sources']['ports']['commit'] = '2' * 40
        self.current['driver_binding']['ports_source'] = copy.deepcopy(self.current['sources']['ports'])
        self.current['driver_binding']['template']['path'] = str(self.fixture.root / 'new-checkout/specs.in')
        self.count = 0

    def document(self, value):
        self.count += 1
        path = self.fixture.root / ('record-' + str(self.count) + '.json')
        path.write_text(json.dumps(value))
        return dict(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest())

    def test_rebound_commit_and_template_path_keep_effective_binding(self):
        original = copy.deepcopy(self.old)
        self.assertTrue(binding.same_driver_binding(self.old, self.current))
        self.assertEqual(self.old, original)

    def test_retired_template_specs_and_sdk_payload_are_not_opened(self):
        for key in ('template', 'instantiated', 'sdk_metadata'):
            Path(self.old['driver_binding'][key]['path']).unlink()
        with patch.object(binding, '_bytes', side_effect=AssertionError('retired payload read')), \
             patch('subprocess.Popen', side_effect=AssertionError('child execution')):
            self.assertTrue(binding.same_driver_binding(self.old, self.current))

    def test_changed_effective_fields_or_provenance_refuse(self):
        mutations = {
            'kind': lambda d: d['driver_binding'].update(kind='unknown'),
            'deployment': lambda d: d['driver_binding'].update(deployment='27.0'),
            'sdk-version': lambda d: d['driver_binding'].update(sdk_version='27.0'),
            'template-hash': lambda d: d['driver_binding']['template'].update(sha256='0' * 64),
            'specs-hash': lambda d: d['driver_binding']['instantiated'].update(sha256='0' * 64),
            'specs-path': lambda d: d['driver_binding']['instantiated'].update(path=str(self.fixture.root / 'different.specs')),
            'sdk-metadata': lambda d: d['driver_binding']['sdk_metadata'].update(sha256='0' * 64),
            'relative-template': lambda d: d['driver_binding'].update(template_relative='other/specs.in'),
            'reported-program': lambda d: d['driver_binding']['reported_programs'].update(ld='/unselected/ld'),
            'selected-linker': lambda d: d['tools']['ld'].update(path='/unselected/ld'),
            'source-mismatch': lambda d: d['driver_binding']['ports_source'].update(commit='3' * 40),
            'template-relative-path': lambda d: d['driver_binding']['template'].update(path='relative/specs.in'),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                changed = copy.deepcopy(self.current)
                mutate(changed)
                self.assertFalse(binding.same_driver_binding(self.old, changed))
        changed = copy.deepcopy(self.current)
        for source in (changed['sources']['ports'], changed['driver_binding']['ports_source']):
            source['repository'] = 'https://example.invalid/another-owner'
        self.assertFalse(binding.same_driver_binding(self.old, changed))

    def test_equal_but_unrecognized_bindings_do_not_pass(self):
        changed = copy.deepcopy(self.old)
        changed['driver_binding']['template']['sha256'] = '0' * 64
        self.assertFalse(binding.same_driver_binding(changed, copy.deepcopy(changed)))

    def test_absent_binding_remains_exact(self):
        empty = copy.deepcopy(self.old)
        empty.pop('driver_binding')
        self.assertTrue(binding.same_driver_binding(empty, copy.deepcopy(empty)))
        self.assertFalse(binding.same_driver_binding(empty, self.current))
        self.assertFalse(binding.same_driver_binding(self.old, empty))

    def accepted(self, name):
        # Candidate production/outputs have their own full owner-fixture tests.
        # Isolate only this consumer's compiler/ABI/binding reuse boundary here.
        output = self.fixture.root / ('sealed-' + name)
        output.write_bytes(b'synthetic accepted component export\n')
        output.chmod(0o444)
        saved = dict(component=name, bootstrap_plan=self.document({'synthetic': 'plan'}),
                     producer_result=self.document({'synthetic': 'result'}),
                     seed_descriptor=self.document(self.old),
                     exports={'libnbcompat.a': dict(path=str(output), sha256=hashlib.sha256(output.read_bytes()).hexdigest())})
        candidate_ref = self.document(saved)
        review = dict(status='REVIEWED_INITIAL_OS_COMPAT_COMPONENT', candidate=candidate_ref,
                      findings=[], all_leases_closed=True, ordinary_provider_qualified=False)
        receipt = dict(format='emberbsd-os-compat-component-acceptance', version=1,
                       scope='gcc16-bootstrap', status='ACCEPTED_INITIAL_OS_COMPAT_COMPONENT',
                       component=name, candidate=candidate_ref, independent_review=self.document(review),
                       initial_only=True, ordinary_provider_qualified=False)
        return self.document(receipt), saved

    def test_both_os_components_reuse_the_binding_without_rewriting_receipts(self):
        for name in ('filetime', 'parsedate'):
            with self.subTest(component=name):
                receipt, saved = self.accepted(name)
                original = Path(receipt['path']).read_bytes()
                with patch.object(component, 'candidate', return_value=saved):
                    self.assertEqual(component.validate_input(receipt, self.current, component=name), saved['exports'])
                self.assertEqual(Path(receipt['path']).read_bytes(), original)

    def test_os_component_compiler_abi_and_driver_tools_stay_exact(self):
        receipt, saved = self.accepted('filetime')
        for field in ('host', 'compiler', 'host_abi', 'abi_flags', 'driver_components', 'driver_prefix'):
            with self.subTest(field=field), patch.object(component, 'candidate', return_value=saved):
                changed = copy.deepcopy(self.current)
                changed[field] = {'changed': True}
                with self.assertRaisesRegex(SDKError, 'compiler or ABI differs'):
                    component.validate_input(receipt, changed)
        changed = copy.deepcopy(self.current)
        changed['tools']['cc']['sha256'] = '0' * 64
        with patch.object(component, 'candidate', return_value=saved), \
             self.assertRaisesRegex(SDKError, 'compiler or ABI differs'):
            component.validate_input(receipt, changed)
        changed = copy.deepcopy(self.current)
        changed['driver_binding']['deployment'] = '27.0'
        with patch.object(component, 'candidate', return_value=saved), \
             self.assertRaisesRegex(SDKError, 'compiler or ABI differs'):
            component.validate_input(receipt, changed)
