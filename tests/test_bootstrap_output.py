#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
# Origin: EmberBSD contributors (AI-assisted).
"""Exercise output assembly data contracts without producer programs."""

import copy
import json
import tempfile
import os
from pathlib import Path
import unittest
from unittest.mock import patch


import test_core  # Adds the SDK source to sys.path.
from emberbsd_sdk import bootstrap_output
from emberbsd_sdk.bootstrap_output import (_planned_links as planned,
    _contents_changes as contents, _equal_except_links as compare,
    _completed_sequence as completed)
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.inventory import sha256_file, snapshot_installed_tree, snapshot_tree
from emberbsd_sdk.jsonio import write_json
from emberbsd_sdk.process import ContextLocks

SOURCE = Path(bootstrap_output.__file__)


class AssemblyDataTests(unittest.TestCase):
    def setUp(self):
        self.root = '/fixture/prefix'
        self.file = dict(kind='file', mode=0o755, size=3, sha256='a' * 64)
        self.old = {'bin/perllink': dict(kind='link', mode=0o755,
                                       target=self.root + '/lib/perl5/bin/perllink'),
                    'lib/perl5/bin/perllink': self.file}
        self.changes = planned(self.root, self.old)
        self.data = (f'@name perl-fixture\n@cwd {self.root}\nbin/perllink\n'
                     f'@comment Symlink:{self.root}/lib/perl5/bin/perllink\n'
                     'lib/perl5/bin/perllink\n@comment MD5:unchanged\n').encode()

    def test_exact_relative_target(self):
        self.assertEqual(self.changes, [dict(path='bin/perllink',
            original_target=self.root + '/lib/perl5/bin/perllink', target='../lib/perl5/bin/perllink')])

    def test_existing_relative_links_stay_unchanged(self):
        self.old['bin/perllink']['target'] = '../lib/perl5/bin/perllink'
        self.assertEqual(planned(self.root, self.old), [])

    def test_external_or_similar_prefix_refuses(self):
        for target in ['/usr/bin/perl', '/fixture/prefix-other/bin/perl', self.root]:
            with self.subTest(target=target), self.assertRaises(SDKError):
                self.old['bin/perllink']['target'] = target
                planned(self.root, self.old)

    def test_non_normalized_absolute_target_refuses(self):
        self.old['bin/perllink']['target'] = self.root + '/lib/../lib/perl5/bin/perllink'
        with self.assertRaises(SDKError):
            planned(self.root, self.old)

    def test_missing_member_refuses(self):
        del self.old['lib/perl5/bin/perllink']
        with self.assertRaises(SDKError):
            planned(self.root, self.old)

    def test_only_paired_comment_changes(self):
        after, updates = contents(self.data, self.root, self.changes)
        self.assertEqual(after, self.data.replace(
            b'@comment Symlink:/fixture/prefix/lib/perl5/bin/perllink\n',
            b'@comment Symlink:../lib/perl5/bin/perllink\n'))
        self.assertEqual(updates[0]['line'], 4)
        self.assertEqual(len(updates), 1)

    def test_wrong_recorded_target_refuses(self):
        data = self.data.replace(b'Symlink:/fixture/prefix/lib/', b'Symlink:/other/lib/')
        with self.assertRaises(SDKError):
            contents(data, self.root, self.changes)

    def test_missing_comment_refuses(self):
        data = self.data.replace(b'@comment Symlink:/fixture/prefix/lib/perl5/bin/perllink\n', b'')
        with self.assertRaises(SDKError):
            contents(data, self.root, self.changes)

    def test_unrelated_contents_byte_exact(self):
        data = b'@name other\n@cwd /fixture/prefix\nbin/other\n@comment Symlink:literal\n'
        self.assertEqual(contents(data, self.root, self.changes), (data, []))

    def test_ignored_member_is_not_an_owner(self):
        data = self.data.replace(b'bin/perllink\n@comment', b'@ignore\nbin/perllink\n@comment', 1)
        self.assertEqual(contents(data, self.root, self.changes), (data, []))

    def test_multi_cwd_matches_same_absolute_member(self):
        data = self.data.replace(b'@cwd /fixture/prefix\nbin/perllink', b'@cwd /fixture/prefix/bin\nperllink')
        after, updates = contents(data, self.root, self.changes)
        self.assertEqual(updates[0]['path'], 'bin/perllink')
        self.assertIn(b'@comment Symlink:../lib/perl5/bin/perllink\n', after)

    def test_strict_delta_allows_only_link_text_and_link_mode(self):
        after = {**self.old, 'bin/perllink': dict(kind='link', mode=0o777, target=self.changes[0]['target'])}
        compare(self.old, after, self.changes)

    def test_changed_regular_file_refuses(self):
        after = {**self.old, 'lib/perl5/bin/perllink': {**self.file, 'sha256': 'b' * 64}}
        with self.assertRaises(SDKError):
            compare(self.old, after, self.changes)

    def test_extra_or_missing_member_refuses(self):
        for after in [{}, {**self.old, 'extra': self.file}]:
            with self.subTest(after=after), self.assertRaises(SDKError):
                compare(self.old, after, self.changes)

    def test_wrong_normalized_target_refuses(self):
        after = {**self.old, 'bin/perllink': dict(kind='link', mode=0o755, target='../other')}
        with self.assertRaises(SDKError):
            compare(self.old, after, self.changes)

    def test_path_order_is_global_and_not_directory_dfs(self):
        rows = {'a/x': dict(kind='link', mode=0o777, target=self.root + '/target'),
                'a.b': dict(kind='link', mode=0o777, target=self.root + '/target'),
                'target': self.file}
        self.assertEqual([row['path'] for row in planned(self.root, rows)], ['a.b', 'a/x'])

    def test_source_keeps_unique_owner_gate_and_original_metadata_backups(self):
        source = SOURCE.read_text()
        self.assertIn("member['path'] not in ownership", source)
        self.assertIn("set(ownership) == {row['path'] for row in changes}", source)
        self.assertLess(source.index("'package-{index:03d}-before.txt'"),
                        source.index('observed = _normalize_internal_absolute_links'))
        self.assertNotIn('run_logged(', source)
        self.assertNotIn('load_bootstrap_seed(', source)


class CompletedSequenceTests(unittest.TestCase):
    def setUp(self):
        self.reference = dict(path='/fixture/plan.json', sha256='a' * 64)
        self.plan = dict(format='emberbsd-native-gcc16-bootstrap-plan', version=1,
                         scope='gcc16-bootstrap', seed_descriptor={'pinned': 'fixture'},
                         commands=[{'id': 'first'}])
        self.prior = dict(format='emberbsd-native-gcc16-producer-result', version=1,
                          scope='gcc16-bootstrap', api=1, status='FAILED',
                          stage='full-recipe-bootstrap', ordinary_provider_qualified=False,
                          evidence_level='command-reported-routes-and-actual-exits',
                          all_commands_drained=True, inputs_revalidated_after_drain=True,
                          all_leases_closed=True, bootstrap_plan=self.reference,
                          seed_descriptor=self.plan['seed_descriptor'], commands=[{'id': 'first'}],
                          error='absolute or escaped payload link: /fixture/prefix/bin/link')

    def test_only_closed_post_command_failure_is_admitted(self):
        completed(self.plan, self.prior, self.reference)

    def test_scope_version_api_and_qualification_are_strict(self):
        for key, value in [('scope', 'native-build'), ('version', True), ('version', 2),
                           ('api', True), ('api', 2), ('ordinary_provider_qualified', 0),
                           ('ordinary_provider_qualified', True)]:
            with self.subTest(key=key, value=value), self.assertRaises(SDKError):
                completed(self.plan, dict(self.prior, **{key: value}), self.reference)

    def test_drain_revalidation_and_close_must_be_true_booleans(self):
        for key in ('all_commands_drained', 'inputs_revalidated_after_drain', 'all_leases_closed'):
            for value in (False, 1, None):
                with self.subTest(key=key, value=value), self.assertRaises(SDKError):
                    completed(self.plan, dict(self.prior, **{key: value}), self.reference)

    def test_completed_output_and_wrong_failure_refuse(self):
        for delta in ({'output_inventory': {}}, {'output_assembly': {}}, {'status': 'PASS'},
                      {'error': 'command failed'}, {'stage': 'initial-darwin-tools'},
                      {'bootstrap_plan': {}}, {'seed_descriptor': {}}):
            with self.subTest(delta=delta), self.assertRaises(SDKError):
                completed(self.plan, dict(self.prior, **delta), self.reference)

    def test_missing_extra_or_empty_command_sequence_refuses(self):
        for commands in ([], [{'id': 'a'}, {'id': 'b'}], None):
            with self.subTest(commands=commands), self.assertRaises(SDKError):
                completed(self.plan, dict(self.prior, commands=commands), self.reference)
        plan = copy.deepcopy(self.plan)
        plan['commands'] = []
        with self.assertRaises(SDKError):
            completed(plan, dict(self.prior, commands=[]), self.reference)


class AssemblyFilesystemTests(unittest.TestCase):
    """Use real SDK leases, inventories, hashes and writes on disposable trees.

    Producer commands and their closed records are fixture data. No producer
    runs here. Original seed admission remains the caller's separate contract.
    No safety validator or filesystem operation is patched for success cases.
    """

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='sdk-output-assembly-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.work = self.root / 'work'
        self.prefix, self.pkgdb = self.work / 'prefix', self.work / 'pkgdb'
        self.inputs, self.export = self.root / 'inputs', self.root / 'export'
        self.inputs.mkdir()
        self.export.mkdir()
        self.evidence = self.root / 'assembly'
        self.write(self.prefix / 'lib/perl5/bin/perllink', b'fixture Perl program\n', 0o755)
        self.write(self.prefix / 'bin/gmake', b'fixture make program\n', 0o755)
        (self.prefix / 'bin/perllink').symlink_to(self.prefix / 'lib/perl5/bin/perllink')
        (self.prefix / 'gnu/bin').mkdir(parents=True)
        (self.prefix / 'gnu/bin/make').symlink_to(self.prefix / 'bin/gmake')
        (self.prefix / 'bin/make').symlink_to('gmake')
        self.perl_contents = self.pkgdb / 'perl-fixture-1/+CONTENTS'
        self.make_contents = self.pkgdb / 'make-fixture-1/+CONTENTS'
        self.write(self.perl_contents, (
            f'@name perl-fixture-1\n@cwd {self.prefix}\nbin/perllink\n'
            f'@comment Symlink:{self.prefix}/lib/perl5/bin/perllink\n'
            'lib/perl5/bin/perllink\n@comment MD5:fixture-unchanged\n').encode())
        self.write(self.make_contents, (
            f'@name make-fixture-1\n@cwd {self.prefix}\ngnu/bin/make\n'
            f'@comment Symlink:{self.prefix}/bin/gmake\n'
            'bin/gmake\n@comment MD5:fixture-unchanged\n'
            'bin/make\n@comment Symlink:gmake\n').encode())
        self.write(self.pkgdb / 'make-fixture-1/+BUILD_INFO', b'unchanged build metadata\n')
        self.write(self.work / 'packages/All/perl-fixture-1.tgz', b'canonical archive fixture\x00perl\n')
        self.write(self.work / 'packages/All/make-fixture-1.tgz', b'canonical archive fixture\x00make\n')
        self.plan = dict(format='emberbsd-native-gcc16-bootstrap-plan', version=1,
                         scope='gcc16-bootstrap', purpose='first-native-gcc16',
                         host=dict(os='Darwin', cpu='arm64'), work_root=str(self.work),
                         output_bundle_root=str(self.prefix),
                         ports_export=dict(root=str(self.export)),
                         resources=dict(lock_root=str(self.root)),
                         seed_descriptor=self.document('fixture-seed.json', {'fixture': 'seed reference'}))
        self.plan['commands'] = [dict(id='fixture-' + phase, recipe='lang/gcc16', phase=phase,
                                     role='make', argv=['/fixture-not-executed/bmake', phase],
                                     cwd=str(self.export), env={'PATH': ''})
                                 for phase in ('build', 'compare', 'install')]
        self.plan_ref = self.document('plan.json', self.plan)
        actual = []
        for expected in self.plan['commands']:
            log = self.work / (expected['id'] + '.log')
            self.write(log, b'synthetic closed command record; no child executed\n')
            actual.append(dict(expected, actual_exit=0, log=self.reference(log)))
        self.prior = dict(format='emberbsd-native-gcc16-producer-result', version=1,
                          scope='gcc16-bootstrap', api=1, status='FAILED',
                          stage='full-recipe-bootstrap', ordinary_provider_qualified=False,
                          evidence_level='command-reported-routes-and-actual-exits',
                          all_commands_drained=True, inputs_revalidated_after_drain=True,
                          all_leases_closed=True, bootstrap_plan=self.plan_ref,
                          seed_descriptor=self.plan['seed_descriptor'], commands=actual,
                          error='absolute or escaped payload link: ' + str(self.prefix / 'bin/perllink'))
        self.failed_ref = self.document('failed-result.json', self.prior)
        persisted = dict(self.prior)
        del persisted['all_leases_closed']
        write_json(str(self.work / 'result.json'), persisted)
        self.source = dict(repository='https://example.invalid/fixture-sdk', commit='a' * 40)
        # Fail any accidental process launch. Filesystem and validation gates stay real.
        no_child = patch('subprocess.Popen', side_effect=AssertionError('assembly must not launch a child'))
        no_child.start()
        self.addCleanup(no_child.stop)

    @staticmethod
    def write(path, data, mode=0o644):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)

    @staticmethod
    def reference(path):
        return dict(path=str(path), sha256=sha256_file(str(path)))

    def document(self, name, value):
        path = self.inputs / name
        write_json(str(path), value)
        return self.reference(path)

    def assembled(self):
        with ContextLocks([str(self.work), str(self.evidence)], str(self.root)):
            return bootstrap_output._assemble(self.plan, str(self.evidence))

    def completed(self):
        return bootstrap_output.assemble_completed_bootstrap_output(
            self.plan_ref, self.failed_ref, evidence_root=str(self.evidence),
            finalizer_source=self.source)

    def inventories(self):
        return (snapshot_installed_tree(str(self.prefix)),
                snapshot_installed_tree(str(self.pkgdb)),
                snapshot_tree(str(self.work / 'packages')))

    def test_two_packages_exact_link_and_comment_delta_archives_unchanged(self):
        before, packages_before, archives_before = self.inventories()
        perl_before, make_before = self.perl_contents.read_bytes(), self.make_contents.read_bytes()
        result = self.assembled()
        changes = planned(str(self.prefix), before)
        after = snapshot_tree(str(self.prefix))
        compare(before, after, changes)
        self.assertEqual(after['bin/perllink']['target'], '../lib/perl5/bin/perllink')
        self.assertEqual(after['gnu/bin/make']['target'], '../../bin/gmake')
        self.assertEqual(after['bin/make'], before['bin/make'])
        self.assertEqual(self.perl_contents.read_bytes(), perl_before.replace(
            f'@comment Symlink:{self.prefix}/lib/perl5/bin/perllink\n'.encode(),
            b'@comment Symlink:../lib/perl5/bin/perllink\n'))
        self.assertEqual(self.make_contents.read_bytes(), make_before.replace(
            f'@comment Symlink:{self.prefix}/bin/gmake\n'.encode(),
            b'@comment Symlink:../../bin/gmake\n'))
        package_changes = {str(p.relative_to(self.pkgdb)) for p in (self.perl_contents, self.make_contents)}
        packages_after = snapshot_installed_tree(str(self.pkgdb))
        self.assertEqual(set(packages_before), set(packages_after))
        for name, row in packages_before.items():
            if name not in package_changes:
                self.assertEqual(packages_after[name], row)
            else:
                self.assertEqual(packages_after[name], dict(row,
                    sha256=sha256_file(str(self.pkgdb / name)), size=(self.pkgdb / name).stat().st_size))
        self.assertEqual(snapshot_tree(str(self.work / 'packages')), archives_before)
        self.assertEqual(result['normalized_link_count'], 2)
        self.assertEqual(result['registered_link_count'], 2)
        self.assertIs(result['canonical_package_archives_modified'], False)
        self.assertIs(result['ordinary_provider_qualified'], False)
        ledger = json.loads(Path(result['assembly_plan']['path']).read_bytes())
        self.assertEqual(ledger['status'], 'PLANNED')
        backups = {row['path']: Path(row['original']['path']).read_bytes()
                   for row in ledger['package_metadata_changes']}
        self.assertEqual(backups['perl-fixture-1/+CONTENTS'], perl_before)
        self.assertEqual(backups['make-fixture-1/+CONTENTS'], make_before)
        for row in ledger['package_metadata_changes']:
            for key in ('original', 'assembled'):
                self.assertEqual(sha256_file(row[key]['path']), row[key]['sha256'])
        self.assertEqual(json.loads(Path(result['output_inventory']['path']).read_bytes()), after)

    def test_public_helper_preserves_original_failed_results(self):
        failed_before = Path(self.failed_ref['path']).read_bytes()
        persisted_before = (self.work / 'result.json').read_bytes()
        archive_before = snapshot_tree(str(self.work / 'packages'))
        result = self.completed()
        self.assertEqual(result['status'], 'OUTPUT_ASSEMBLED_PENDING_INDEPENDENT_REVIEW')
        self.assertIs(result['commands_reexecuted'], False)
        self.assertIs(result['original_producer_inputs_revalidated_here'], False)
        self.assertIs(result['ordinary_provider_qualified'], False)
        self.assertIs(result['all_leases_closed'], True)
        receipt = json.loads((self.evidence / 'result.json').read_bytes())
        self.assertEqual(dict(receipt, all_leases_closed=True), result)
        self.assertEqual(Path(self.failed_ref['path']).read_bytes(), failed_before)
        self.assertEqual((self.work / 'result.json').read_bytes(), persisted_before)
        self.assertEqual(snapshot_tree(str(self.work / 'packages')), archive_before)
        self.assertEqual(result['original_failed_result'], self.failed_ref)
        self.assertEqual(result['original_persisted_result'], self.reference(self.work / 'result.json'))
        snapshot_tree(str(self.prefix))

    def assert_preflight_refuses_without_payload_changes(self, message):
        before = self.inventories()
        with self.assertRaisesRegex(SDKError, message):
            self.assembled()
        self.assertEqual(self.inventories(), before)
        self.assertFalse((self.evidence / 'assembly-plan.json').exists())

    def test_wrong_paired_target_refuses_before_mutation(self):
        data = self.make_contents.read_bytes().replace(b'Symlink:', b'Symlink:wrong-')
        self.make_contents.write_bytes(data)
        self.assert_preflight_refuses_without_payload_changes('comment differs')

    def test_duplicate_package_owner_refuses_before_mutation(self):
        self.write(self.pkgdb / 'duplicate-fixture-1/+CONTENTS', self.make_contents.read_bytes())
        self.assert_preflight_refuses_without_payload_changes('multiple package owners')

    def test_missing_package_owner_refuses_before_mutation(self):
        self.make_contents.write_bytes(f'@name make-fixture-1\n@cwd {self.prefix}\nbin/gmake\n'.encode())
        self.assert_preflight_refuses_without_payload_changes('one exact registered package owner')

    def test_external_absolute_target_refuses_before_mutation(self):
        outside = self.root / 'outside'
        self.write(outside, b'outside prefix\n')
        (self.prefix / 'bin/outside').symlink_to(outside)
        self.assert_preflight_refuses_without_payload_changes('not normalized inside the prefix')

    def test_changed_command_log_refuses_before_mutation(self):
        Path(self.prior['commands'][0]['log']['path']).write_bytes(b'changed command evidence\n')
        before = self.inventories()
        with self.assertRaisesRegex(SDKError, 'referenced input changed'):
            self.completed()
        self.assertEqual(self.inventories(), before)
        self.assertFalse(self.evidence.exists())

    def test_metadata_write_failure_preserves_ledger_backups_and_failed_receipt(self):
        # Inject an I/O failure after complete prevalidation. Do not bypass a gate.
        with patch.object(bootstrap_output, '_replace_metadata', side_effect=OSError('fixture disk failure')):
            result = self.completed()
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(result['error'], 'fixture disk failure')
        self.assertIs(result['all_leases_closed'], True)
        self.assertEqual(json.loads((self.evidence / 'result.json').read_bytes())['status'], 'FAILED')
        ledger = json.loads((self.evidence / 'assembly-plan.json').read_bytes())
        for row in ledger['package_metadata_changes']:
            self.assertEqual((self.pkgdb / row['path']).read_bytes(), Path(row['original']['path']).read_bytes())

    def test_keyboard_interrupt_is_saved_and_remains_visible(self):
        self.assert_visible_interrupt(KeyboardInterrupt('fixture interrupt'))

    def test_system_exit_is_saved_and_keeps_exit_code(self):
        self.assert_visible_interrupt(SystemExit(17))

    def assert_visible_interrupt(self, interruption):
        # This is fault injection, not acceptance of a fake successful operation.
        failed_before = Path(self.failed_ref['path']).read_bytes()
        with patch.object(bootstrap_output, '_replace_metadata', side_effect=interruption):
            with self.assertRaises(type(interruption)) as caught:
                self.completed()
        self.assertIs(caught.exception, interruption)
        receipt = json.loads((self.evidence / 'result.json').read_bytes())
        self.assertEqual(receipt['status'], 'FAILED')
        self.assertIn(type(interruption).__name__, receipt['error'])
        self.assertEqual(Path(self.failed_ref['path']).read_bytes(), failed_before)
        # The real locks can be acquired after the propagated interruption.
        with ContextLocks([str(self.work), str(self.evidence)], str(self.root)):
            pass


if __name__ == '__main__':
    unittest.main(verbosity=2)
