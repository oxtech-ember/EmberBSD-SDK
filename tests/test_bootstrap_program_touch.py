# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Touch source contracts and synthetic filesystem effects; no native children."""
import copy
import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import test_bootstrap_program as fixtures
from emberbsd_sdk import bootstrap_program as program, bootstrap_program_fs as fs
from emberbsd_sdk import bootstrap_program_components as components
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.bootstrap_seed import _plan


@unittest.skipUnless(os.environ.get('EMBERBSD_TOUCH_SOURCE'), 'select canonical touch source')
class TouchProgramTests(unittest.TestCase):
    def setUp(self):
        owner = Path(os.environ['EMBERBSD_TOUCH_SOURCE'])
        self.case = c = fixtures.ProgramTests(methodName='runTest')
        c.setUp()
        self.addCleanup(c.doCleanups)
        c.source = c.base / 'touch-source'
        parent = c.base / 'touch.noindex'
        parent.mkdir()
        c.work = parent / 'touch-work'
        c.plan.update(stage='touch-bootstrap', work_root=str(c.work))
        relative, digest, sources, provenance = program._profile(c.plan)
        c.manifest = json.loads((owner/relative).read_text())
        for name in {relative, *sources, *provenance}:
            path = c.source/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((owner/name).read_bytes())
        self.assertEqual(hashlib.sha256((c.source/relative).read_bytes()).hexdigest(), digest)
        c.inputs['root'] = str(c.source)
        c.plan['program']['manifest'] = dict(path=str(c.source/relative), sha256=digest)
        c.refresh_source()
        self.exports = {}
        self.component_roots = []
        c.plan['program']['components'] = {}
        # Synthetic receipts let this consumer test exercise the real lease
        # discovery. Component acceptance itself has separate causal tests.
        for name in ('filetime', 'parsedate'):
            root = c.base/name
            root.mkdir()
            self.component_roots += [str(root), str(root/'source')]
            (root/'source').mkdir()
            names = ['libnbcompat.a', 'nbtool_config.h', 'bootstrap_cdefs.h', 'compat_'+name+'.h']
            if name == 'parsedate':
                names.append('compat_parsedate_features.h')
            exports = {}
            for member in names:
                raw = b'common declarations\n' if member == 'bootstrap_cdefs.h' else (name+member).encode()
                path = root/member
                path.write_bytes(raw)
                path.chmod(0o444)
                exports[member] = dict(path=str(path), sha256=hashlib.sha256(raw).hexdigest())
            self.exports[name] = exports
            original = dict(work_root=str(root))
            saved = dict(bootstrap_plan=c.f.document(original), source=dict(root=str(root/'source'),
                         source_proof=c.f.document(dict(members=c.f.document({})))),
                         producer_result=c.f.document({}), seed_descriptor=c.f.document(c.descriptor))
            c.plan['program']['components'][name] = c.f.document(dict(candidate=c.f.document(saved),
                                                      independent_review=c.f.document({})))
        self.validator = patch.object(components.component, 'validate_input', side_effect=self.selected)
        self.validator.start()
        self.addCleanup(self.validator.stop)
        c.plan['commands'] = program.program_steps(c.plan, c.descriptor, c.commands, c.flags)

    def selected(self, reference, descriptor, *, component):
        self.assertEqual(reference, self.case.plan['program']['components'][component])
        self.assertEqual(descriptor, self.case.descriptor)
        for row in self.exports[component].values():
            if hashlib.sha256(Path(row['path']).read_bytes()).hexdigest() != row['sha256']:
                raise SDKError('synthetic retained component changed')
        return self.exports[component]

    def test_exact_plan_archives_configs_and_seven_source_members(self):
        c = self.case
        self.assertIs(c.validate(), c.plan)
        self.assertEqual(_plan(c.f.document(c.plan), c.descriptor, c.commands, c.flags), c.plan)
        self.assertEqual(len(c.proof and json.loads(Path(c.proof['members']['path']).read_text())), 7)
        self.assertEqual(len(c.plan['commands']), 39)
        self.assertEqual(len(c.manifest['runtime_cases']), 36)
        compile_args = c.plan['commands'][0]['argv']
        include_paths = [compile_args[i+1] for i, word in enumerate(compile_args) if word == '-include']
        self.assertEqual(include_paths, [self.exports['parsedate']['compat_parsedate_features.h']['path'],
            self.exports['filetime']['nbtool_config.h']['path'], self.exports['parsedate']['nbtool_config.h']['path']])
        link = c.plan['commands'][1]['argv']
        self.assertEqual([word for word in link if word.endswith('.a')],
                         [self.exports[name]['libnbcompat.a']['path'] for name in ('filetime', 'parsedate')])
        self.assertEqual(link.count('-lm'), 1)
        self.assertTrue(all('-DHAVE_' not in word for word in compile_args))
        self.assertEqual(components.read_inputs(c.plan)[0], self.component_roots)
        self.assertEqual(len([p for p in program.program_outputs(c.plan) if p.endswith('.filesystem.json')]), 37)

    def test_live_atime_path_refusal_preserves_retained_exact_plan(self):
        c = self.case
        c.work = c.base/'plain-touch-work'
        c.plan['work_root'] = str(c.work)
        c.plan['commands'] = program.program_steps(c.plan, c.descriptor, c.commands, c.flags)
        reference = c.f.document(c.plan)
        # The real owner contract, source membership, components, and commands
        # all validate. Only new live admission refuses the indexed work path.
        self.assertIs(c.validate(), c.plan)
        self.assertEqual(_plan(reference, c.descriptor, c.commands, c.flags,
                               inspect_inputs=False), c.plan)
        with self.assertRaisesRegex(SDKError, 'atime_ns.*[.]noindex'):
            _plan(reference, c.descriptor, c.commands, c.flags)
        self.assertFalse(c.work.exists())

    def test_source_only_export_does_not_admit_missing_component_receipts(self):
        c = self.case
        del c.plan['program']['components']
        program.validate_program_source(c.plan, c.descriptor['sources']['os'])
        with self.assertRaises(SDKError):
            c.validate()

    def test_successful_noop_refuses_before_next_case(self):
        c = self.case
        with patch('subprocess.Popen', side_effect=AssertionError('no native children')), \
             patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=c.fake_command):
            result = c.invoke()
        self.assertEqual(result['status'], 'FAILED')
        self.assertIn('filesystem actual state', result['error'])
        self.assertEqual(len(result['commands']), 3)

    def test_component_drift_after_child_is_checked_before_semantic_success(self):
        c = self.case
        def changed(argv, log, **kwargs):
            value = c.fake_command(argv, log, **kwargs)
            path = Path(self.exports['parsedate']['compat_parsedate.h']['path'])
            path.chmod(0o644)
            path.write_bytes(b'changed after owned child\n')
            return value
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=changed):
            result = c.invoke()
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(result['commands'][0]['actual_exit'], 0)
        self.assertIn('component changed', result['error'])
        self.assertEqual(len(result['commands']), 1)
        self.assertTrue(result['all_commands_drained'])

    def test_source_mutation_and_unexpected_header_are_refused(self):
        c = self.case
        (c.source/'private.h').write_bytes(b'private replacement')
        c.refresh_source()
        with self.assertRaises(SDKError):
            c.validate()

    def test_independent_fixed_timestamp_effect_and_retained_report(self):
        c = self.case
        row = next(row for row in c.manifest['runtime_cases'] if row['name'] == 'numeric-minus-one')
        c.work.mkdir()
        directory = fs.prepare(str(c.work), row, key='numeric-minus-one')
        # Python writes one known timestamp. This checks the observer and its
        # retained bytes, not touch's date parser or a native C result.
        os.utime(directory/'file', ns=(-1000000000, -1000000000), follow_symlinks=False)
        reference = fs.observe(str(c.work), row, key='numeric-minus-one')
        fs.verify_report(reference, row, key='numeric-minus-one')
        changed = copy.deepcopy(row)
        changed['filesystem']['expect']['file']['mtime_ns'] = 0
        with self.assertRaises(SDKError):
            fs.verify_report(reference, changed, key='numeric-minus-one')

    def test_exact_positive_and_error_contracts_cannot_be_repinned(self):
        c = self.case
        path = c.source/'tools/touch/bootstrap.json'
        value = copy.deepcopy(c.manifest)
        value['runtime_cases'][0]['filesystem']['expect']['file']['mtime_ns'] = 1
        path.write_text(json.dumps(value))
        c.plan['program']['manifest']['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaises(SDKError):
            program.program_manifest(c.plan)


if __name__ == '__main__':
    unittest.main()
