# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Contained timestamp checks. No native child is executed."""

import copy
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from emberbsd_sdk import bootstrap_program_fs as fs
from emberbsd_sdk.errors import SDKError


ATIME = 1700000000123456789
MTIME = 1700000001987654321


class FilesystemTimes(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)

    def case(self, fixtures, expected=None, name='times'):
        return dict(name=name, filesystem=dict(umask='0022', fixtures=fixtures,
            expect=copy.deepcopy(fixtures if expected is None else expected)))

    def test_mismatch_error_keeps_pre_read_times_and_full_entry_facts(self):
        case = self.case({'file': dict(type='file', mode='0600', data='private fixture',
                                      atime_ns=ATIME, mtime_ns=MTIME)})
        fs.prepare(self.work, case)
        case['filesystem']['expect']['file']['atime_ns'] += 1
        real_fstat = os.fstat

        def later_metadata(fd):
            value = real_fstat(fd)
            fields = {name: getattr(value, name) for name in dir(value) if name.startswith('st_')}
            fields['st_atime_ns'] = ATIME + 999
            return SimpleNamespace(**fields)

        with patch.object(fs.os, 'fstat', side_effect=later_metadata):
            with self.assertRaises(SDKError) as caught:
                fs.observe(self.work, case)
        message = str(caught.exception)
        self.assertIn(': {', message)
        detail = json.loads(message.split(': ', 1)[1])
        expected = fs._expected(fs.validate(case['filesystem']))['file']
        observed = dict(expected, atime_ns=ATIME)
        self.assertEqual(detail, {'file': {'expected': expected, 'observed': observed}})
        self.assertNotIn('private fixture', message)
        self.assertFalse((self.work / 'times.filesystem.json').exists())
        self.assertEqual(json.loads(json.dumps({'error': message}))['error'], message)

    def test_mismatch_error_keeps_all_differing_fields_and_absent_entry(self):
        case = self.case({'file': dict(type='file', mode='0600', data='observed')})
        fs.prepare(self.work, case)
        case['filesystem']['expect'].update(
            file=dict(type='file', mode='0640', data='expected data'),
            missing=dict(type='file', mode='0600', data=''))
        with self.assertRaises(SDKError) as caught:
            fs.observe(self.work, case)
        message = str(caught.exception)
        self.assertIn(': {', message)
        detail = json.loads(message.split(': ', 1)[1])
        self.assertEqual(detail['file']['expected'], fs._expected(case['filesystem'])['file'])
        self.assertEqual(detail['file']['observed'], dict(type='file', mode='0600', size=8,
                         sha256=hashlib.sha256(b'observed').hexdigest()))
        self.assertEqual(detail['missing']['observed'], dict(type='absent'))
        self.assertFalse((self.work / 'times.filesystem.json').exists())

    def test_mismatch_error_is_bounded_by_the_existing_entry_budget(self):
        fixtures = {f'f{index}': dict(type='file', mode='0600', data='') for index in range(fs.MAX_NODES)}
        case = self.case(fixtures)
        fs.prepare(self.work, case)
        for value in case['filesystem']['expect'].values():
            value['mode'] = '0640'
        with self.assertRaises(SDKError) as caught:
            fs.observe(self.work, case)
        message = str(caught.exception)
        self.assertIn(': {', message)
        detail = json.loads(message.split(': ', 1)[1])
        self.assertEqual(set(detail), set(fixtures))
        self.assertLess(len(message), 65536)
        self.assertFalse((self.work / 'times.filesystem.json').exists())

    def test_mismatch_error_hashes_an_oversized_observed_link_target(self):
        case = self.case({'link': dict(type='symlink', target='missing')})
        root = fs.prepare(self.work, case)
        (root / 'link').unlink()
        target = 'x' * 600
        (root / 'link').symlink_to(target)
        with self.assertRaises(SDKError) as caught:
            fs.observe(self.work, case)
        detail = json.loads(str(caught.exception).split(': ', 1)[1])
        self.assertEqual(detail['link']['observed']['target'],
                         dict(size=600, sha256=hashlib.sha256(os.fsencode(target)).hexdigest()))
        self.assertEqual(detail['link']['expected']['target'], 'missing')
        self.assertFalse((self.work / 'times.filesystem.json').exists())

    def test_mismatch_error_hashes_an_undecodable_observed_link_target(self):
        case = self.case({'link': dict(type='symlink', target='missing')})
        root = fs.prepare(self.work, case)
        (root / 'link').unlink()
        target = b'\xff'
        os.symlink(target, os.fsencode(root / 'link'))
        self.assertEqual(os.readlink(os.fsencode(root / 'link')), target)
        with self.assertRaises(SDKError) as caught:
            fs.observe(self.work, case)
        message = str(caught.exception)
        self.assertTrue(message.startswith('filesystem actual state differs from its source contract: '))
        detail = json.loads(message.split(': ', 1)[1])
        self.assertEqual(detail['link']['observed']['target'],
                         dict(size=1, sha256=hashlib.sha256(target).hexdigest()))
        self.assertEqual(detail['link']['expected']['target'], 'missing')
        self.assertEqual(json.loads(fs.json_bytes({'error': message}))['error'], message)
        self.assertFalse((self.work / 'times.filesystem.json').exists())

    def test_file_directory_and_symlink_roundtrip_without_following_target(self):
        fixtures = {
            'dir': dict(type='directory', mode='0700', atime_ns=ATIME, mtime_ns=MTIME),
            'dir/file': dict(type='file', mode='0600', data='owned', atime_ns=MTIME, mtime_ns=ATIME),
            'link': dict(type='symlink', target='dir/file', atime_ns=ATIME, mtime_ns=MTIME)}
        case = self.case(fixtures)
        root = fs.prepare(self.work, case)
        for name, row in fixtures.items():
            actual = (root / name).lstat()
            self.assertEqual((actual.st_atime_ns, actual.st_mtime_ns), (row['atime_ns'], row['mtime_ns']))
        reference = fs.observe(self.work, case)
        fs.verify_report(reference, case)
        report = json.loads(Path(reference['path']).read_text())
        self.assertEqual(report['entries']['link']['mtime_ns'], MTIME)
        self.assertEqual(report['entries']['dir/file']['mtime_ns'], ATIME)

    def test_single_timestamp_is_optional_and_preserves_the_other(self):
        case = self.case({'file': dict(type='file', mode='0600', data='owned', mtime_ns=MTIME)})
        root = fs.prepare(self.work, case)
        self.assertEqual((root / 'file').stat().st_mtime_ns, MTIME)
        self.assertNotEqual((root / 'file').stat().st_atime_ns, MTIME)
        reference = fs.observe(self.work, case)
        self.assertNotIn('atime_ns', json.loads(Path(reference['path']).read_text())['entries']['file'])

    def test_schema_rejects_non_integer_out_of_range_and_unsupported_types(self):
        for value in (True, False, -(2**63) - 1, 2**63, 1.0, '1', None):
            for field in ('atime_ns', 'mtime_ns'):
                for group in ('fixtures', 'expect'):
                    with self.subTest(value=value, field=field, group=group):
                        case = self.case({'file': dict(type='file', mode='0600', data='')})
                        case['filesystem'][group]['file'][field] = value
                        with self.assertRaises(SDKError):
                            fs.validate(case['filesystem'])
        for kind in ('absent', 'hardlink'):
            case = self.case({'file': dict(type='file', mode='0600', data='')})
            group = 'expect' if kind == 'absent' else 'fixtures'
            case['filesystem'][group]['file'] = dict(type=kind, atime_ns=ATIME)
            if kind == 'hardlink':
                case['filesystem'][group]['file']['target'] = 'peer'
            with self.assertRaises(SDKError):
                fs.validate(case['filesystem'])
        for value in (-(2**63), -1, 0, 2**63 - 1):
            fs.validate(self.case({'file': dict(type='file', mode='0600', data='',
                                              atime_ns=value, mtime_ns=value)})['filesystem'])

    def test_setup_refuses_unrepresentable_timestamp_before_returning(self):
        case = self.case({'file': dict(type='file', mode='0600', data='', mtime_ns=MTIME)})
        with patch.object(fs.os, 'utime', return_value=None):
            with self.assertRaisesRegex(SDKError, 'timestamp'):
                fs.prepare(self.work, case)

    def test_negative_epoch_roundtrip(self):
        case = self.case({'file': dict(type='file', mode='0600', data='',
                                      atime_ns=-1000000000, mtime_ns=-2000000000)})
        fs.prepare(self.work, case)
        fs.verify_report(fs.observe(self.work, case), case)

    def test_changed_timestamp_and_rehashed_historical_report_refuse(self):
        case = self.case({'file': dict(type='file', mode='0600', data='', atime_ns=ATIME, mtime_ns=MTIME)})
        root = fs.prepare(self.work, case)
        os.utime(root / 'file', ns=(ATIME, MTIME + 1))
        with self.assertRaisesRegex(SDKError, 'actual state'):
            fs.observe(self.work, case)
        os.utime(root / 'file', ns=(ATIME, MTIME))
        reference = fs.observe(self.work, case)
        path = Path(reference['path'])
        value = json.loads(path.read_text())
        value['entries']['file']['mtime_ns'] += 1
        path.write_text(json.dumps(value))
        reference['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(SDKError, 'historical'):
            fs.verify_report(reference, case)

    def test_all_alias_stats_precede_content_reads_and_directory_enumeration(self):
        row = dict(type='file', mode='0600', data='owned')
        # No timestamp fields: this causally fails on the old one-pass observer.
        case = self.case({'first': row, 'second': dict(type='hardlink', target='first'),
                          'dir': dict(type='directory', mode='0700')},
                         {'first': row, 'second': dict(row, same_inode_as='first'),
                          'dir': dict(type='directory', mode='0700')})
        root = fs.prepare(self.work, case)
        directory_inode = (root / 'dir').stat().st_ino
        real_stat, real_open, real_scandir = os.stat, os.open, os.scandir
        observed = set()
        reads = []

        def record_stat(path, *args, **kwargs):
            result = real_stat(path, *args, **kwargs)
            if kwargs.get('follow_symlinks') is False:
                observed.add(path)
            return result

        def open_file(path, flags, *args, **kwargs):
            if path in ('first', 'second'):
                self.assertTrue({'first', 'second'} <= observed)
                reads.append(path)
            return real_open(path, flags, *args, **kwargs)

        def scan_directory(fd):
            if os.fstat(fd).st_ino == directory_inode:
                self.assertIn('dir', observed)
            return real_scandir(fd)

        with patch.object(fs.os, 'stat', side_effect=record_stat), \
             patch.object(fs.os, 'open', side_effect=open_file), \
             patch.object(fs.os, 'scandir', side_effect=scan_directory):
            fs.verify_report(fs.observe(self.work, case), case)
        self.assertEqual(reads, ['first', 'second'])

    def test_hardlink_alias_inherits_the_exact_fixture_timestamp(self):
        row = dict(type='file', mode='0600', data='owned', atime_ns=ATIME, mtime_ns=MTIME)
        case = self.case({'first': row, 'second': dict(type='hardlink', target='first')},
                         {'first': row, 'second': dict(row, same_inode_as='first')})
        fs.prepare(self.work, case)
        fs.verify_report(fs.observe(self.work, case), case)

    def test_directory_time_is_captured_before_scanning_its_children(self):
        case = self.case({'dir': dict(type='directory', mode='0700', atime_ns=ATIME, mtime_ns=MTIME)})
        root = fs.prepare(self.work, case)
        inode = (root / 'dir').stat().st_ino
        real_scandir = os.scandir

        def scan(fd):
            if os.fstat(fd).st_ino == inode:
                # Emulate the directory access effect on a mount with noatime.
                os.utime(fd, ns=(ATIME + 1, MTIME))
            return real_scandir(fd)

        with patch.object(fs.os, 'scandir', side_effect=scan):
            reference = fs.observe(self.work, case)
        fs.verify_report(reference, case)
        self.assertNotEqual((root / 'dir').stat().st_atime_ns, ATIME)

    def test_dangling_symlink_times_use_parent_descriptor_without_following(self):
        case = self.case({'link': dict(type='symlink', target='missing', atime_ns=ATIME, mtime_ns=MTIME)})
        real_utime = os.utime
        calls = []

        def timestamp(path, **kwargs):
            self.assertEqual(path, 'link')
            self.assertIs(kwargs['follow_symlinks'], False)
            self.assertIs(type(kwargs['dir_fd']), int)
            calls.append(path)
            return real_utime(path, **kwargs)

        with patch.object(fs.os, 'utime', side_effect=timestamp):
            root = fs.prepare(self.work, case)
        self.assertEqual(calls, ['link'])
        self.assertFalse((root / 'missing').exists())
        fs.verify_report(fs.observe(self.work, case), case)


if __name__ == '__main__':
    unittest.main()
