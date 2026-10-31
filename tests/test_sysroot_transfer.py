# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Transfer contracts use build fixtures. They do not qualify target execution."""

import hashlib
import copy
import io
import json
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

from test_core import SDK_SOURCE, TemporaryCase
from test_builder import fixture, write
from emberbsd_sdk import inventory, publication, sysroot_transfer as transfer
from emberbsd_sdk.errors import SDKError
from test_builder import B as Builder
from emberbsd_sdk.targets import TARGETS


# Transfer fixtures are complete SDKs; riscv32 currently has only C foundation.
COMPLETE_SDK_TARGETS = ('aarch64', 'amd64')


class SysrootTransferTests(TemporaryCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = tempfile.TemporaryDirectory(prefix='sdk-transfer-fixtures-')
        cls.addClassCleanup(cls.fixtures.cleanup)
        cls.accepted = {}
        for target in COMPLETE_SDK_TARGETS:
            root = Path(cls.fixtures.name).resolve() / target
            root.mkdir()
            options = fixture(root, target)
            Builder(options).run()
            source = Path(options['sdk']) / 'sysroot'
            write(source / 'usr/include/name with spaces.h', '// Unicode: λ\n', 0o640)
            write(source / 'usr/include/empty.h', b'')
            write(source / 'usr/lib/executable-fixture', '#!/bin/sh\nexit 0\n', 0o751)
            (source / 'usr/include/alias.h').symlink_to('name with spaces.h')
            (source / 'include-alias').symlink_to('usr/include')
            (source / 'usr/include').chmod(0o750)
            cls.refresh_receipt(source, target)
            cls.accepted[target] = source

    @staticmethod
    def refresh_receipt(source, target):
        receipt_path = source / Builder.RECEIPT
        receipt = json.loads(receipt_path.read_text())
        receipt['outputs'] = inventory.sysroot(
            source, TARGETS[target]['elf'],
            exclude=(Builder.RECEIPT, Builder.LOG, Builder.MARKER))
        receipt_path.write_text(json.dumps(receipt))

    def setUp(self):
        super().setUp()
        self.source = self.root / 'source'
        shutil.copytree(self.accepted['aarch64'], self.source, symlinks=True)
        self.bundle = self.root / 'bundle'
        self.destination = self.root / 'destination'

    def export(self, target='aarch64'):
        return transfer.export_sysroot(self.source, self.bundle, target=target)

    def restore(self, pin, target='aarch64', **options):
        return transfer.import_sysroot(
            self.bundle, self.destination, target=target,
            manifest_sha256=pin, **options)

    def receipt_change(self, mutate):
        path = self.source / Builder.RECEIPT
        receipt = json.loads(path.read_text())
        mutate(receipt)
        path.write_text(json.dumps(receipt))

    def test_explicit_native_v2_and_historical_v1_build_receipts(self):
        original = json.loads((self.source / Builder.RECEIPT).read_text())
        self.assertEqual(original['version'], 2)
        for mutation in ('identity', 'host-prefix', 'host-cc', 'host-cxx', 'native-routing', 'flags'):
            changed = copy.deepcopy(original)
            if mutation == 'identity': del changed['signature']['native_toolset']
            elif mutation == 'flags': changed['signature']['flags'].append('HOST_CC=/usr/bin/cc')
            else: changed['paths'][mutation] = '/different/provider'
            with self.subTest(mutation=mutation), self.assertRaises(SDKError):
                transfer._provenance(changed, 'aarch64')
        historical = copy.deepcopy(original); historical['version'] = 1
        del historical['signature']['native_toolset']
        del historical['signature']['native_inputs']
        for name in ('host-cc', 'host-cxx', 'host-prefix', 'native-routing'): del historical['paths'][name]
        transfer._provenance(historical, 'aarch64')
        (self.source / Builder.RECEIPT).write_text(json.dumps(historical))
        pin = self.export()['manifest_sha256']
        self.restore(pin)

    def test_gcc_build_receipt_cannot_claim_libcxx_base_aliases(self):
        changed = json.loads((self.source / Builder.RECEIPT).read_text())
        flags = changed['signature']['flags']
        flags[flags.index('MKLIBCXX=no')] = 'MKLIBCXX=yes'
        with self.assertRaisesRegex(SDKError, 'GCC sysroot cannot select'):
            transfer._provenance(changed, 'aarch64')

    def repin(self, mutate):
        path = self.bundle / 'manifest.json'
        manifest = json.loads(path.read_text())
        mutate(manifest)
        path.write_text(json.dumps(manifest, sort_keys=True))
        pin = hashlib.sha256(path.read_bytes()).hexdigest()
        (self.bundle / 'READY').write_text(pin + '\n')
        return pin

    def assert_restore_refuses(self, pin, target='aarch64', **options):
        before = sorted(path.name for path in self.root.iterdir())
        with self.assertRaises((SDKError, OSError, ValueError)):
            self.restore(pin, target=target, **options)
        self.assertFalse(self.destination.exists())
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), before)

    def test_riscv32_requires_c_foundation_instead_of_a_complete_sdk(self):
        self.assertEqual(set(self.accepted), set(COMPLETE_SDK_TARGETS))
        root = self.root / 'riscv32'
        root.mkdir()
        with self.assertRaisesRegex(SDKError, 'only the intermediate C foundation'):
            Builder(fixture(root, 'riscv32')).run()

    def test_round_trip_preserves_bytes_modes_and_relative_links_for_both_targets(self):
        self.assertEqual(transfer.SYSROOT_TRANSFER_API, 1)
        for target in COMPLETE_SDK_TARGETS:
            with self.subTest(target=target):
                source = self.accepted[target]
                bundle = self.root / ('bundle-' + target)
                destination = self.root / ('destination-' + target)
                before = inventory.snapshot_tree(source)
                record = transfer.export_sysroot(source, bundle, target=target)
                pin = record['manifest_sha256']
                self.assertEqual((bundle / 'READY').read_text(), pin + '\n')
                self.assertEqual(hashlib.sha256((bundle / 'manifest.json').read_bytes()).hexdigest(), pin)
                with tarfile.open(bundle / 'payload.tar.gz') as payload:
                    members = payload.getmembers()
                    self.assertTrue(members)
                    self.assertTrue(all(member.isfile() for member in members))
                    self.assertEqual([m.name for m in members],
                                     [f'files/{i:08d}' for i in range(len(members))])
                old_umask = os.umask(0o077)
                try:
                    transfer.import_sysroot(bundle, destination, target=target,
                                            manifest_sha256=pin)
                finally:
                    os.umask(old_umask)
                self.assertEqual(inventory.snapshot_tree(source), before)
                expected = json.loads((source / Builder.RECEIPT).read_text())['outputs']
                actual = inventory.sysroot(destination, TARGETS[target]['elf'])
                self.assertEqual({path: actual[path] for path in expected}, expected)
                for excluded in (Builder.RECEIPT, Builder.LOG, Builder.MARKER):
                    self.assertFalse((destination / excluded).exists())
                self.assertEqual((destination / 'usr/include/alias.h').read_bytes(),
                                 (source / 'usr/include/alias.h').read_bytes())

    def test_manifest_does_not_publish_private_build_paths_or_environment(self):
        sentinel = 'private-auth-token-never-export'
        self.receipt_change(lambda record: record['signature']['environment'].update(SECRET=sentinel))
        self.export()
        text = (self.bundle / 'manifest.json').read_text()
        self.assertNotIn(sentinel, text)
        self.assertNotIn(self.fixtures.name, text)

    def test_export_refuses_incomplete_unqualified_missing_receipt_and_changed_outputs(self):
        mutations = {
            'incomplete': lambda: (self.source / Builder.MARKER).write_text('active'),
            'unqualified': lambda: self.receipt_change(lambda r: r.update(qualification='not-built')),
            'missing-receipt': lambda: (self.source / Builder.RECEIPT).unlink(),
            'changed-output': lambda: (self.source / 'usr/include/stdio.h').write_text('changed'),
            'extra-output': lambda: (self.source / 'unrecorded').write_text('new'),
            'wrong-source-pin': lambda: self.receipt_change(lambda r: r['sources']['os'].update(commit='abc')),
            'unknown-workspace-target': lambda: self.receipt_change(lambda r: r['workspace'].update(layout_version=1)),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                shutil.rmtree(self.source)
                shutil.copytree(self.accepted['aarch64'], self.source, symlinks=True)
                mutate()
                with self.assertRaises((SDKError, OSError, ValueError)):
                    self.export()
                self.assertFalse((self.bundle / 'READY').exists())
                if self.bundle.exists():
                    shutil.rmtree(self.bundle)

    def test_export_does_not_overwrite_existing_bundle(self):
        self.bundle.mkdir()
        sentinel = self.bundle / 'keep'
        sentinel.write_bytes(b'keep')
        before = sentinel.stat()
        with self.assertRaises((SDKError, OSError)):
            self.export()
        self.assertEqual(sentinel.read_bytes(), b'keep')
        self.assertEqual(sentinel.stat().st_ino, before.st_ino)
        self.assertEqual(list(self.bundle.iterdir()), [sentinel])

    def test_export_refuses_wrong_target(self):
        with self.assertRaises(SDKError):
            self.export(target='amd64')
        self.assertFalse((self.bundle / 'READY').exists())

    def test_import_requires_ready_pin_and_matching_target(self):
        pin = self.export()['manifest_sha256']
        for bad_pin in ('', '0' * 64, pin.upper(), '../manifest.json'):
            with self.subTest(pin=bad_pin):
                self.assert_restore_refuses(bad_pin)
        self.assert_restore_refuses(pin, target='amd64')
        (self.bundle / 'READY').unlink()
        self.assert_restore_refuses(pin)

    def test_import_rejects_corrupt_manifest_without_touching_destination(self):
        pin = self.export()['manifest_sha256']
        path = self.bundle / 'manifest.json'
        path.write_bytes(path.read_bytes() + b' ')
        self.assert_restore_refuses(pin)

    def test_import_rejects_corrupt_payload_and_cleans_stage(self):
        pin = self.export()['manifest_sha256']
        path = self.bundle / 'payload.tar.gz'
        data = bytearray(path.read_bytes())
        data[len(data) // 2] ^= 1
        path.write_bytes(data)
        self.assert_restore_refuses(pin)

    def test_import_limits_declared_output_bytes(self):
        pin = self.export()['manifest_sha256']
        self.assert_restore_refuses(pin, max_bytes=1)
        self.assert_restore_refuses(pin, max_bytes=-1)

    def test_import_does_not_replace_existing_destination_entries(self):
        pin = self.export()['manifest_sha256']
        for kind in ('file', 'directory', 'link', 'dangling'):
            with self.subTest(kind=kind):
                if kind == 'file':
                    self.destination.write_bytes(b'keep')
                elif kind == 'directory':
                    self.destination.mkdir()
                    (self.destination / 'sentinel').write_bytes(b'keep')
                else:
                    self.destination.symlink_to(self.source if kind == 'link' else self.root / 'absent')
                before = self.destination.lstat()
                with self.assertRaises((SDKError, OSError)):
                    self.restore(pin)
                self.assertEqual(self.destination.lstat().st_ino, before.st_ino)
                if kind == 'directory':
                    self.assertEqual((self.destination / 'sentinel').read_bytes(), b'keep')
                    shutil.rmtree(self.destination)
                else:
                    if kind == 'file':
                        self.assertEqual(self.destination.read_bytes(), b'keep')
                    self.destination.unlink()

    def test_interrupted_import_cleans_owned_stage(self):
        pin = self.export()['manifest_sha256']
        before = sorted(path.name for path in self.root.iterdir())
        with patch.object(publication, 'publish_directory_noreplace', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.restore(pin)
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), before)
        self.assertFalse(self.destination.exists())

    def rewrite_payload(self, mutate):
        payload = self.bundle / 'payload.tar.gz'
        with tarfile.open(payload, 'r:gz') as archive:
            records = [(member, archive.extractfile(member).read())
                       for member in archive.getmembers()]
        records = mutate(records)
        with tarfile.open(payload, 'w:gz', format=tarfile.USTAR_FORMAT) as archive:
            for member, data in records:
                archive.addfile(member, io.BytesIO(data) if member.isfile() else None)
        def metadata(manifest):
            manifest['payload'].update(size=payload.stat().st_size,
                                       sha256=hashlib.sha256(payload.read_bytes()).hexdigest())
        return self.repin(metadata)

    def test_unsafe_inventory_paths_are_rejected_before_restore(self):
        self.export()
        original = (self.bundle / 'manifest.json').read_bytes()
        for name in ('../outside', '/absolute', 'usr/../outside', 'usr//file',
                     'usr/./file', 'usr\\file', 'usr/control\nfile',
                     '.prebuild-sdk-import.json'):
            with self.subTest(name=name):
                (self.bundle / 'manifest.json').write_bytes(original)
                pin = self.repin(lambda manifest: manifest['inventory'].update(
                    {name: dict(kind='file', mode=0o600, size=0,
                                sha256=hashlib.sha256(b'').hexdigest())}))
                self.assert_restore_refuses(pin)

    def test_inventory_ancestors_must_be_real_listed_directories(self):
        self.export()
        original = (self.bundle / 'manifest.json').read_bytes()
        for ancestor in ('absent', 'usr/include/alias.h', 'usr/include/stdio.h'):
            with self.subTest(ancestor=ancestor):
                (self.bundle / 'manifest.json').write_bytes(original)
                pin = self.repin(lambda manifest: manifest['inventory'].update(
                    {ancestor + '/child': dict(kind='file', mode=0o600, size=0,
                                               sha256=hashlib.sha256(b'').hexdigest())}))
                self.assert_restore_refuses(pin)

    def test_link_escape_dangling_cycle_and_file_traversal_are_rejected(self):
        self.export()
        original = (self.bundle / 'manifest.json').read_bytes()
        targets = ('/outside', '../../../outside', 'missing', 'alias.h',
                   'stdio.h/child', '../../..', '..\\outside')
        for target in targets:
            with self.subTest(target=target):
                (self.bundle / 'manifest.json').write_bytes(original)
                def mutate(manifest):
                    manifest['inventory']['usr/include/alias.h']['target'] = target
                self.assert_restore_refuses(self.repin(mutate))

    def test_archive_record_types_names_counts_and_bytes_are_verified(self):
        self.export()
        original_manifest = (self.bundle / 'manifest.json').read_bytes()
        original_payload = (self.bundle / 'payload.tar.gz').read_bytes()
        def bad_type(kind):
            def mutate(records):
                record, _ = records[0]
                record.type = kind
                record.size = 0
                record.linkname = 'outside' if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE) else ''
                records[0] = (record, b'')
                return records
            return mutate
        def bad_name(records):
            records[0][0].name = '../outside'
            return records
        def wrong_data(records):
            for index, (record, data) in enumerate(records):
                if data:
                    records[index] = (record, bytes([data[0] ^ 1]) + data[1:])
                    break
            return records
        mutations = {
            'symlink': bad_type(tarfile.SYMTYPE),
            'hardlink': bad_type(tarfile.LNKTYPE),
            'fifo': bad_type(tarfile.FIFOTYPE),
            'directory': bad_type(tarfile.DIRTYPE),
            'unexpected-name': bad_name,
            'missing-record': lambda records: records[:-1],
            'extra-record': lambda records: records + [records[-1]],
            'duplicate-record': lambda records: [records[0], *records],
            'wrong-content': wrong_data,
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                (self.bundle / 'manifest.json').write_bytes(original_manifest)
                (self.bundle / 'payload.tar.gz').write_bytes(original_payload)
                self.assert_restore_refuses(self.rewrite_payload(mutate))
        self.assertFalse((self.root / 'outside').exists())

    def test_case_collisions_are_not_silently_merged_on_case_folding_filesystem(self):
        probe = self.write('case-probe', 'x')
        if not (self.root / 'CASE-PROBE').exists():
            self.skipTest('this filesystem preserves distinct case names')
        probe.unlink()
        self.export()
        manifest = json.loads((self.bundle / 'manifest.json').read_text())
        manifest['inventory']['USR'] = dict(kind='directory', mode=0o755)
        pin = self.repin(lambda record: record.update(inventory=manifest['inventory']))
        self.assert_restore_refuses(pin)

    def test_interrupted_export_never_publishes_ready(self):
        original_addfile = tarfile.TarFile.addfile
        count = 0
        def interrupt(archive, member, fileobj=None):
            nonlocal count
            original_addfile(archive, member, fileobj)
            count += 1
            if count == 2:
                raise KeyboardInterrupt
        with patch.object(tarfile.TarFile, 'addfile', interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.export()
        self.assertTrue(self.bundle.is_dir())
        self.assertFalse((self.bundle / 'READY').exists())
        self.assert_restore_refuses('0' * 64)

    def test_source_changed_during_export_leaves_bundle_unready(self):
        original_addfile = tarfile.TarFile.addfile
        changed = False
        def change(archive, member, fileobj=None):
            nonlocal changed
            original_addfile(archive, member, fileobj)
            if not changed:
                (self.source / Builder.MARKER).write_text('producer became active')
                changed = True
        with patch.object(tarfile.TarFile, 'addfile', change):
            with self.assertRaises(SDKError):
                self.export()
        self.assertFalse((self.bundle / 'READY').exists())

    def test_export_hashes_the_actual_streamed_file_bytes(self):
        original_open = transfer.paths.open_regular
        changed = False
        def change(path):
            nonlocal changed
            handle, before = original_open(path)
            if self.bundle.exists() and Path(path) == self.source / 'usr/include/stdio.h':
                data = handle.read()
                handle.close()
                changed = True
                return io.BytesIO(bytes([data[0] ^ 1]) + data[1:]), before
            return handle, before
        with patch.object(transfer.paths, 'open_regular', change):
            with self.assertRaisesRegex(SDKError, 'changed'):
                self.export()
        self.assertTrue(changed)
        self.assertFalse((self.bundle / 'READY').exists())

    def test_late_destination_race_preserves_other_writer_and_cleans_stage(self):
        pin = self.export()['manifest_sha256']
        original_publish = publication.publish_directory_noreplace
        def race(source, destination):
            Path(destination).mkdir()
            (Path(destination) / 'sentinel').write_bytes(b'other writer')
            return original_publish(source, destination)
        with patch.object(publication, 'publish_directory_noreplace', race):
            with self.assertRaises(OSError):
                self.restore(pin)
        self.assertEqual((self.destination / 'sentinel').read_bytes(), b'other writer')
        self.assertEqual(sorted(path.name for path in self.root.iterdir()),
                         ['bundle', 'destination', 'source'])

    def test_cli_help_round_trip_and_duplicate_option_refusal(self):
        commands = {name: [sys.executable, '-B', str(SDK_SOURCE / 'scripts' / (name + '-sysroot.py'))]
                    for name in ('export', 'import')}
        for command in commands.values():
            result = subprocess.run([*command, '--help'], cwd=self.root, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(b'--target', result.stdout)
        export_arguments = ['--sysroot', str(self.source), '--output', str(self.bundle), '--target', 'aarch64']
        result = subprocess.run([*commands['export'], *export_arguments, '--target', 'aarch64'],
                                cwd=self.root, capture_output=True)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(b'duplicate', result.stderr)
        self.assertFalse(self.bundle.exists())
        result = subprocess.run([*commands['export'], *export_arguments], cwd=self.root, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        pin = json.loads(result.stdout)['manifest_sha256']
        arguments = ['--bundle', str(self.bundle), '--destination', str(self.destination),
                     '--manifest-sha256', pin, '--target', 'aarch64']
        result = subprocess.run([*commands['import'], *arguments], cwd=self.root, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['manifest_sha256'], pin)
        self.assertEqual((self.destination / 'usr/include/stdio.h').read_bytes(),
                         (self.source / 'usr/include/stdio.h').read_bytes())
        result = subprocess.run([*commands['import'], *arguments], cwd=self.root, capture_output=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertNotIn(b'Traceback', result.stderr)

    def test_import_rejects_repinned_malformed_host_and_compiler_provenance(self):
        self.export()
        original = (self.bundle / 'manifest.json').read_bytes()
        mutations = {
            'host-type': lambda p: p.update(build_host=[]),
            'host-path': lambda p: p.update(build_host='/private/build-host'),
            'host-control': lambda p: p.update(build_host='Darwin\nhost'),
            'host-empty': lambda p: p.update(build_host=''),
            'missing-compiler-binary': lambda p: p['compiler']['binaries'].pop('gcc'),
            'unexpected-compiler-binary': lambda p: p['compiler']['binaries'].update(unknown='a' * 64),
            'non-mapping-compiler-binaries': lambda p: p['compiler'].update(binaries=['a' * 64]),
            'unexpected-compiler-field': lambda p: p['compiler'].update(extra='not-supported'),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                (self.bundle / 'manifest.json').write_bytes(original)
                pin = self.repin(lambda manifest: mutate(manifest['provenance']))
                self.assert_restore_refuses(pin)

    def test_failed_import_cleans_mode_zero_directories_without_touching_user_data(self):
        self.export()
        original_manifest = (self.bundle / 'manifest.json').read_bytes()
        user_data = self.root / 'user-data'
        user_data.mkdir(mode=0o750)
        write(user_data / 'keep', b'user data must survive', 0o640)
        before_user = inventory.snapshot_tree(user_data)
        before_mode = user_data.stat().st_mode
        before_source = inventory.snapshot_tree(self.source)
        before_names = sorted(path.name for path in self.root.iterdir())
        for failure in ('inspection', 'publication'):
            with self.subTest(failure=failure):
                (self.bundle / 'manifest.json').write_bytes(original_manifest)
                if failure == 'inspection':
                    def restrictive_modes(manifest):
                        for name in ('usr', 'usr/include', 'usr/include/sys'):
                            manifest['inventory'][name]['mode'] = 0
                    pin = self.repin(restrictive_modes)
                    with patch.object(inventory, 'sysroot', side_effect=SDKError('injected inspection failure')):
                        self.assert_restore_refuses(pin)
                else:
                    pin = self.repin(lambda manifest: None)
                    def fail_publication(source, destination):
                        tree = Path(source)
                        (tree / 'outside-alias').symlink_to(user_data, target_is_directory=True)
                        for name in ('usr/include/sys', 'usr/include', 'usr'):
                            (tree / name).chmod(0)
                        raise OSError('injected publication failure')
                    with patch.object(publication, 'publish_directory_noreplace', fail_publication):
                        with self.assertRaisesRegex(OSError, 'injected publication failure'):
                            self.restore(pin)
                self.assertFalse(self.destination.exists())
                self.assertEqual(sorted(path.name for path in self.root.iterdir()), before_names)
                self.assertEqual(inventory.snapshot_tree(user_data), before_user)
                self.assertEqual(user_data.stat().st_mode, before_mode)
                self.assertEqual(inventory.snapshot_tree(self.source), before_source)

    def test_transport_timestamp_drift_does_not_weaken_local_source_checks(self):
        original_regular = transfer.paths.regular_file
        original_verify = transfer.paths.verify_stable
        transport_reads = set()
        source_checks = []
        def drifting_stat(path):
            info = original_regular(path)
            if Path(path).parent != self.bundle:
                return info
            transport_reads.add(Path(path).name)
            return SimpleNamespace(st_dev=info.st_dev, st_ino=info.st_ino,
                                   st_mode=info.st_mode, st_size=info.st_size,
                                   st_mtime_ns=info.st_mtime_ns + 1_000_000_000,
                                   st_ctime_ns=info.st_ctime_ns + 2_000_000_000)
        def strict_local_check(path, before):
            if Path(path).parent == self.bundle:
                raise AssertionError('transport timestamps must not define byte integrity')
            if self.source in Path(path).parents:
                source_checks.append(Path(path))
            return original_verify(path, before)
        with patch.object(transfer.paths, 'regular_file', drifting_stat), \
                patch.object(transfer.paths, 'verify_stable', strict_local_check):
            pin = self.export()['manifest_sha256']
            self.restore(pin)
        self.assertEqual(transport_reads, {'payload.tar.gz', 'manifest.json', 'READY'})
        self.assertIn(self.source / Builder.RECEIPT, source_checks)
        self.assertIn(self.source / 'usr/include/stdio.h', source_checks)
        self.assertEqual((self.destination / 'usr/include/stdio.h').read_bytes(),
                         (self.source / 'usr/include/stdio.h').read_bytes())

    def test_changed_transport_readback_never_publishes_ready(self):
        original_open = transfer._open_transport
        for name in ('manifest.json', 'payload.tar.gz'):
            with self.subTest(name=name):
                if self.bundle.exists():
                    shutil.rmtree(self.bundle)
                changed = False
                def change(path):
                    nonlocal changed
                    if Path(path) == self.bundle / name and not changed:
                        data = bytearray(Path(path).read_bytes())
                        data[len(data) // 2] ^= 1
                        Path(path).write_bytes(data)
                        changed = True
                    return original_open(path)
                with patch.object(transfer, '_open_transport', change):
                    with self.assertRaises(SDKError):
                        self.export()
                self.assertTrue(changed)
                self.assertFalse((self.bundle / 'READY').exists())
