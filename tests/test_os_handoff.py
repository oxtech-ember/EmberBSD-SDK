# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
import hashlib
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/prepare-os-handoff.py'
RC = './etc/rc.d/ember_os_update'
PROGRAM = './usr/libexec/ember-os-update'
PROFILE = './usr/share/ember-update/handoff/test-profile.json'


class HandoffSets(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.slots = {'profile': 'test-profile', 'storage': 'ffs', 'adapter': 'uboot-ffs',
                      'roots': {'A': 'netbsd-root', 'B': 'ember-root-b'}, 'esp': 'EFI'}
        (self.root / 'description').write_text(json.dumps({'slots': self.slots}))
        (self.root / 'METALOG').write_bytes(b'. type=dir uname=root gname=wheel mode=0755\n')
        self.stock('etc')
        self.stock('base')

    def stock(self, role, extra=None):
        binaries = ['./etc/rc.d/ember_update'] if role == 'etc' else [
            './usr/sbin/ember-update', './usr/libexec/ember-os-slot', './usr/libexec/ember-install-slot',
            './usr/libexec/ember-stage-ffs']
        paths = {name: b'native' for name in binaries}
        paths[f'./etc/mtree/set.{role}'] = b'# original ordering\n\n. type=dir uname=root gname=wheel mode=0755\n'
        paths[f'./var/db/obsolete/{role}'] = (RC + '\n./etc/rc.d/ember_pkg_update\n' if role == 'etc'
                                            else PROGRAM + '\n').encode()
        paths.update(extra or {})
        with tarfile.open(self.root / f'{role}.tar.xz', 'w:xz') as archive:
            for name, data in paths.items():
                member = tarfile.TarInfo(name)
                member.mode, member.mtime, member.size = 0o555, 23, len(data)
                archive.addfile(member, io.BytesIO(data))
            if role == 'base':
                link = tarfile.TarInfo('./usr/sbin/existing-link')
                link.type, link.linkname = tarfile.LNKTYPE, binaries[0]
                archive.addfile(link)

    def run_tool(self, dest):
        return subprocess.run([sys.executable, SCRIPT, '--base', self.root / 'base.tar.xz',
            '--etc', self.root / 'etc.tar.xz', '--metalog', self.root / 'METALOG', '--generation', '5',
            '--description', self.root / 'description', '--output', self.root / dest], capture_output=True)

    def test_seal_lists_and_reproducibility(self):
        for directory in ('first', 'second'):
            result = self.run_tool(directory)
            self.assertEqual(result.returncode, 0, result.stderr)
        output = self.root / 'first'
        for role in ('base', 'etc'):
            name = role + '.tar.xz'
            self.assertEqual((output / name).read_bytes(), (self.root / 'second' / name).read_bytes())
            with tarfile.open(output / name) as archive:
                self.assertEqual(sorted(archive.getnames()), (output / f'{role}.list').read_text().splitlines())
                added = [RC] if role == 'etc' else [PROGRAM, PROFILE]
                for path in added:
                    member = archive.getmember(path)
                    data = archive.extractfile(member).read()
                    digest = hashlib.sha256(data).hexdigest()
                    line = path + ' type=file uname=root gname=wheel mode='
                    line += f'0{member.mode:o} size={member.size} sha256={digest}\n'
                    self.assertEqual((member.uid, member.gid, member.mtime), (0, 0, 23))
                    manifest = archive.extractfile(f'./etc/mtree/set.{role}').read()
                    self.assertTrue(manifest.startswith(b'# original ordering\n\n. type=dir '))
                    self.assertIn(line.encode(), manifest)
                    self.assertIn(line.encode(), (output / 'METALOG').read_bytes())
                    if path == PROFILE:
                        self.assertEqual(json.loads(data), {'profile': self.slots, 'generation': 5,
                            'etc_sha256': hashlib.sha256((output / 'etc.tar.xz').read_bytes()).hexdigest()})
                        self.assertEqual(member.mode, 0o444)
                    else:
                        self.assertEqual((data, member.mode), (b'#!/bin/sh\nexit 0\n', 0o555))
                if role == 'base':
                    self.assertTrue(archive.getmember('./usr/sbin/existing-link').islnk())
        self.assertNotEqual(self.run_tool('first').returncode, 0)

    def test_measured_generated_manifests_are_rejected(self):
        entry = b'./etc/mtree/set.etc type=file mode=0444 size=100 sha256=' + b'0' * 64 + b'\n'
        self.stock('etc', {'./etc/mtree/set.etc': entry})
        self.assertIn(b'measured generated manifest', self.run_tool('final').stderr)
        self.stock('etc')
        (self.root / 'METALOG').write_bytes(entry)
        self.assertIn(b'measured generated manifest', self.run_tool('final').stderr)
        self.assertFalse((self.root / 'final').exists())

    def test_fixed_legacy_wedge_labels(self):
        self.slots['roots']['A'] = 'custom-root'
        (self.root / 'description').write_text(json.dumps({'slots': self.slots}))
        self.assertNotEqual(self.run_tool('final').returncode, 0)
        self.assertFalse((self.root / 'final').exists())

    def test_missing_retirement_or_live_service_leaves_no_output(self):
        for role, extra in [('base', {'./var/db/obsolete/base': b''}), ('etc', {RC: b'active'})]:
            with self.subTest(role=role):
                self.stock(role, extra)
                result = self.run_tool('final')
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.root / 'final').exists())
                self.stock(role)
