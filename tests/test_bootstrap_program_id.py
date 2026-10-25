# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Independent identity data oracles and the finite ID runner. No C runs."""
import importlib
import copy
import hashlib
import json
import os
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import test_bootstrap_program_native as fixtures
import test_bootstrap_program as runner_fixtures
from emberbsd_sdk import bootstrap_program as program
from emberbsd_sdk.errors import SDKError


class IdentityOracleTests(unittest.TestCase):
    def module(self):
        return importlib.import_module('emberbsd_sdk.bootstrap_identity')

    def test_process_groups_exceed_the_old_sixteen_group_hint(self):
        module = self.module()
        with patch('os.getgid', return_value=100), patch('os.getgroups', return_value=list(range(18))), \
             patch('os.sysconf', side_effect=AssertionError('do not use a group limit hint')):
            self.assertEqual(module.identity_output('groups'),
                             b'100 ' + b' '.join(str(n).encode() for n in range(18)) + b'\n')

    def test_real_and_effective_ids_and_unknown_name_fallback(self):
        module = self.module()
        with patch('os.getuid', return_value=100), patch('os.geteuid', return_value=200), \
             patch('pwd.getpwuid', side_effect=KeyError):
            self.assertEqual(module.identity_output('uid'), b'200\n')
            self.assertEqual(module.identity_output('ruid'), b'100\n')
            self.assertEqual(module.identity_output('uname'), b'200\n')
        with self.assertRaises(SDKError):
            module.identity_output('unsupported')

    def test_named_subject_uses_password_and_group_APIs(self):
        module = self.module()
        account = SimpleNamespace(pw_uid=0, pw_gid=0, pw_name='root')
        with patch('pwd.getpwnam', return_value=account), \
             patch('os.getgrouplist', return_value=[0, 7, 8]), \
             patch('grp.getgrgid', side_effect=lambda gid: SimpleNamespace(gr_name={0:'wheel',7:'seven',8:'eight'}[gid])):
            self.assertEqual(module.identity_output('root-group-names'), b'wheel seven eight\n')
            self.assertEqual(module.identity_output('root-pretty'), b'uid\troot\ngroups\twheel seven eight\n')

    def test_summary_preserves_real_effective_order_and_optional_names(self):
        module = self.module()
        users = {100: SimpleNamespace(pw_name='user'), 200: SimpleNamespace(pw_name='effective')}
        groups = {100: SimpleNamespace(gr_name='users'), 0: SimpleNamespace(gr_name='wheel')}
        with patch('os.getuid', return_value=100), patch('os.geteuid', return_value=200), \
             patch('os.getgid', return_value=100), patch('os.getegid', return_value=201), \
             patch('os.getgroups', return_value=[100, 0, 0, 7]), \
             patch('pwd.getpwuid', side_effect=lambda uid: users[uid]), \
             patch('grp.getgrgid', side_effect=lambda gid: groups[gid]), \
             patch.dict(os.environ, {'USER':'ambient-wrong', 'LOGNAME':'ambient-wrong'}):
            self.assertEqual(module.identity_output('summary'),
                b'uid=100(user) gid=100(users) euid=200(effective) egid=201 groups=100(users),0(wheel),7\n')


class IdProgramTests(unittest.TestCase):
    def fixture(self):
        native = fixtures.NativeProgramTests(methodName='runTest')
        native.setUp()
        self.addCleanup(native.doCleanups)
        native.configure('id')
        return native

    def test_shared_runner_retains_all_aliases_and_observations(self):
        native = self.fixture()
        case = native.case
        self.assertIs(case.validate(), case.plan)
        self.assertEqual(len(case.plan['commands']), 34)
        self.assertIn(str(case.source / 'common/include'), case.plan['commands'][0]['argv'])
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged', side_effect=native.fake_command), \
             patch.object(program, 'identity_output', return_value=b'synthetic identity\n'):
            result = case.invoke()
        self.assertEqual(result['status'], 'COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW')
        self.assertFalse(result['ordinary_provider_qualified'])
        self.assertEqual(result['commands'][1]['invocation_aliases'], {'groups':'id', 'whoami':'id'})
        self.assertEqual(os.readlink(case.work / 'groups'), 'id')
        self.assertEqual(os.readlink(case.work / 'whoami'), 'id')

    def test_changed_adapter_and_command_refuse_before_child(self):
        native = self.fixture()
        case = native.case
        original = copy.deepcopy(case.plan)
        case.plan['commands'][0]['argv'].remove(str(case.source / 'common/include'))
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged') as child, self.assertRaises(SDKError):
            case.invoke()
        child.assert_not_called()
        case.plan.clear()
        case.plan.update(original)
        (case.source / program._ID_GROUP_HEADER).write_text('changed typed adapter\n')
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged') as child, self.assertRaises(SDKError):
            case.invoke()
        child.assert_not_called()

    def test_changed_semantic_mode_refuses(self):
        native = self.fixture()
        case = native.case
        path = Path(case.plan['program']['manifest']['path'])
        case.manifest['runtime_cases'][0]['stdout_identity'] = 'unsupported'
        path.write_text(json.dumps(case.manifest))
        case.refresh_source()
        with patch('emberbsd_sdk.bootstrap_pilot.run_logged') as child, self.assertRaises(SDKError):
            case.invoke()
        child.assert_not_called()

    def test_changed_observed_identity_is_refused(self):
        native = self.fixture()
        case = native.case
        case.work.mkdir()
        (case.work / 'case0.stdout').write_bytes(b'wrong identity\n')
        (case.work / 'case0.stderr').write_bytes(b'')
        with patch.object(program, 'identity_output', return_value=b'expected identity\n'), self.assertRaises(SDKError):
            program.verify_program_step(case.plan['commands'][2], str(case.work), case.plan)

    @unittest.skipUnless(os.environ.get('EMBERBSD_TEST_OS_SOURCE'), 'set EMBERBSD_TEST_OS_SOURCE')
    def test_complete_real_owner_contract_builds_every_step(self):
        root = Path(os.environ['EMBERBSD_TEST_OS_SOURCE'])
        relative, digest, sources, provenance = program._profile({'stage':'id-bootstrap'})
        raw = (root / relative).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), digest)
        manifest = json.loads(raw)
        program._native_program_contract(manifest, 'id')
        members = {relative, *sources, *manifest['headers'], *provenance}
        self.assertEqual(len(members), 8)
        for member in members:
            self.assertTrue((root / member).is_file(), member)
            self.assertFalse((root / member).is_symlink(), member)
        self.assertEqual(manifest['invocations'], ['id','groups','whoami'])
        runner = runner_fixtures.ProgramTests(methodName='runTest')
        runner.setUp()
        self.addCleanup(runner.doCleanups)
        plan = dict(runner.plan, stage='id-bootstrap', program=dict(source=dict(root=str(root)),
            manifest=dict(path=str(root / relative), sha256=digest)))
        with patch('subprocess.Popen', side_effect=AssertionError('no native child')):
            steps = program.program_steps(plan, runner.descriptor, runner.commands, runner.flags)
        self.assertEqual(len(steps), 34)
        self.assertEqual([row['id'] for row in steps if row['phase'] == 'program-case'],
                         ['case-' + case['name'] for case in manifest['runtime_cases']])
        self.assertTrue(all('\0' not in word for row in steps for word in row['argv']))
        self.assertIn(str(root / 'common/include'), steps[0]['argv'])


if __name__ == '__main__':
    unittest.main()
