# SPDX-License-Identifier: BSD-2-Clause
# Origin: EmberBSD contributors (AI-assisted).
"""Current and retained mkdir contracts. All files are data; no child runs."""
import copy
import hashlib
import inspect
import json
import os
from pathlib import Path
import tempfile
import unittest

import test_core  # Adds the SDK source to sys.path.
from emberbsd_sdk import bootstrap_program as program
from emberbsd_sdk import bootstrap_program_fs as filesystem
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.inventory import snapshot_tree

LEGACY = '111fe0d327e4ccdc7de008ac3844e49893edfbd43d62400ef2dbc4ceb76c5453'


@unittest.skipUnless(os.environ.get('EMBERBSD_TEST_FILEUTILS_SOURCE'), 'set EMBERBSD_TEST_FILEUTILS_SOURCE')
class MkdirVersionTests(unittest.TestCase):
    def setUp(self):
        self.source = Path(os.environ['EMBERBSD_TEST_FILEUTILS_SOURCE'])
        self.current_raw = (self.source / 'tools/mkdir/bootstrap.json').read_bytes()
        self.current = json.loads(self.current_raw)
        self.digest = hashlib.sha256(self.current_raw).hexdigest()
        self.legacy = copy.deepcopy(self.current)
        self.legacy['runtime_cases'] = [case for case in self.legacy['runtime_cases'] if case['name'] != 'invalid-mode']
        self.legacy_raw = (json.dumps(self.legacy, indent=2) + '\n').encode()
        self.assertEqual(hashlib.sha256(self.legacy_raw).hexdigest(), LEGACY)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def plan(self, raw, digest, name):
        root = self.root / name
        target = root / 'tools/mkdir/bootstrap.json'
        target.parent.mkdir(parents=True)
        target.write_bytes(raw)
        return dict(stage='mkdir-bootstrap', work_root=str(self.root / ('work-' + name)),
            program=dict(source=dict(root=str(root)), manifest=dict(path=str(target), sha256=digest)))

    def contract(self, manifest, digest):
        self.assertIn('manifest_digest', inspect.signature(program._native_program_contract).parameters)
        return program._native_program_contract(manifest, 'mkdir', manifest_digest=digest)

    def test_current_default_selects_eleven_case_owner(self):
        self.assertEqual(program._profile(dict(stage='mkdir-bootstrap'))[1], self.digest)
        self.assertEqual(len(program.program_manifest(self.plan(self.current_raw, self.digest, 'current'))['runtime_cases']), 11)

    def test_bound_legacy_selects_exact_ten_case_owner(self):
        plan = self.plan(self.legacy_raw, LEGACY, 'legacy')
        self.assertEqual(program._profile(plan)[1], LEGACY)
        self.assertEqual(program.program_manifest(plan), self.legacy)
        self.contract(self.legacy, LEGACY)

    def test_unknown_digest_refuses_before_file_read(self):
        for digest in ('0' * 64, None, [], False, 7):
            with self.subTest(digest=digest), self.assertRaises(SDKError):
                program._profile(dict(stage='mkdir-bootstrap', program=dict(manifest=dict(sha256=digest))))
        with self.assertRaises(SDKError):
            self.contract(self.current, '0' * 64)

    def test_current_and_legacy_counts_cannot_be_cross_paired(self):
        for manifest, digest in ((self.current, LEGACY), (self.legacy, self.digest)):
            with self.subTest(digest=digest), self.assertRaises(SDKError):
                self.contract(manifest, digest)
        for raw, digest, name in ((self.current_raw, LEGACY, 'wrong-legacy'),
                                  (self.legacy_raw, self.digest, 'wrong-current')):
            with self.subTest(name=name), self.assertRaises(SDKError):
                program.program_manifest(self.plan(raw, digest, name))

    def test_changed_diagnostic_cannot_rebind_its_own_new_digest(self):
        changed = copy.deepcopy(self.current)
        rows = [case for case in changed['runtime_cases'] if case['name'] == 'invalid-mode']
        self.assertEqual(len(rows), 1)
        rows[0]['stderr'] = "mkdir: Cannot set file mode `invalid': Undefined error: 0\n"
        raw = (json.dumps(changed, indent=2) + '\n').encode()
        with self.assertRaises(SDKError):
            program.program_manifest(self.plan(raw, hashlib.sha256(raw).hexdigest(), 'changed'))

    def test_source_proof_and_steps_support_both_bound_versions(self):
        for raw, digest, name, count in ((self.current_raw, self.digest, 'proof-current', 11),
                                         (self.legacy_raw, LEGACY, 'proof-legacy', 10)):
            with self.subTest(name=name):
                plan = self.plan(raw, digest, name)
                manifest = json.loads(raw)
                relative, selected, sources, provenance = program._profile(plan)
                self.assertEqual(selected, digest)
                root = Path(plan['program']['source']['root'])
                for member in {*sources, *manifest['headers'], *provenance}:
                    path = root / member
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes((self.source / member).read_bytes())
                def retained(filename, value):
                    path = self.root / (name + '-' + filename + '.json')
                    path.write_text(json.dumps(value))
                    return dict(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
                inventory = snapshot_tree(str(root))
                members = {}
                for member, item in inventory.items():
                    if item['kind'] == 'file':
                        data = (root / member).read_bytes()
                        members[member] = dict(sha256=item['sha256'], git_blob=hashlib.sha1(
                            b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest())
                source_pin = dict(repository='https://github.com/oxtech-ember/EmberBSD.git', commit='3' * 40)
                source = plan['program']['source']
                source.update(source=source_pin, inventory=retained('inventory', inventory))
                source['source_proof'] = retained('proof', dict(format='emberbsd-bootstrap-program-source',
                    version=1, source=source_pin, inventory=source['inventory'], members=retained('members', members)))
                self.assertEqual(program.validate_program_source(plan, source_pin), manifest)
                steps = program.program_steps(plan, dict(host=dict(os='Darwin', cpu='arm64')),
                    dict(cc=('/selected/gcc16',), sh=('/selected/mksh',)), dict(cppflags=[], cflags=[], ldflags=[]))
                self.assertEqual(len(steps), count + 3)

    def test_current_invalid_mode_requires_exact_stream_and_empty_tree(self):
        rows = [case for case in self.current['runtime_cases'] if case['name'] == 'invalid-mode']
        self.assertEqual(len(rows), 1)
        case = rows[0]
        self.assertEqual(case['stderr'], "mkdir: Cannot set file mode `invalid'\n")
        self.assertEqual(case['filesystem'], dict(umask='0022', fixtures={}, expect={}))
        plan = self.plan(self.current_raw, self.digest, 'observed')
        work = Path(plan['work_root'])
        work.mkdir()
        directory = filesystem.prepare(work, case)
        for stream in ('stdout', 'stderr'):
            (work / ('invalid-mode.' + stream)).write_text(case[stream])
        step = dict(id='case-invalid-mode', phase='program-case')
        program.verify_program_step(step, str(work), plan, actual_exit=1)
        for bad in ("mkdir: Cannot set file mode `invalid': Undefined error: 0\n", ''):
            (work / 'invalid-mode.stderr').write_text(bad)
            with self.assertRaises(SDKError):
                program.verify_program_step(step, str(work), plan, actual_exit=1)
        (work / 'invalid-mode.stderr').write_text(case['stderr'])
        (directory / 'dir').mkdir()
        with self.assertRaises(SDKError):
            program.verify_program_step(step, str(work), plan, actual_exit=1)
