# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""Source-wave receipt merge must drop failed and blocked rows that later succeed."""

from pathlib import Path
import tempfile
import unittest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.source_wave import _merge_receipts, drop_resolved_receipts


def _write(state, name, text):
    path = state / name
    path.write_text(text)
    return text


class ReceiptMergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.real = Path(self.tmp.name) / 'real'
        self.private = Path(self.tmp.name) / 'private'
        self.real.mkdir()
        self.private.mkdir()

    def _seed(self, *, done='', failed='', blocked='', skipped='', progress=''):
        before = {
            'done': _write(self.real, 'done', done),
            'failed': _write(self.real, 'failed', failed),
            'blocked': _write(self.real, 'blocked', blocked),
            'skipped': _write(self.real, 'skipped', skipped),
            'progress': _write(self.real, 'progress', progress),
        }
        for name, text in before.items():
            (self.private / name).write_text(text)
        return before

    def test_success_this_wave_drops_previous_failed_row(self):
        before = self._seed(
            failed='x11/plasma6-layer-shell-qt incompatible-workdir\n'
                   'x11/plasma6-plasma-desktop build\n',
        )
        (self.private / 'done').write_text(
            'x11/plasma6-layer-shell-qt plasma6-layer-shell-qt-6.8.2nb1 abc\n'
        )
        planned = {'x11/plasma6-layer-shell-qt': 'plasma6-layer-shell-qt-6.8.2nb1'}
        receipts = _merge_receipts(self.real, self.private, before, planned=planned)
        self.assertEqual(
            (self.real / 'done').read_text(),
            'x11/plasma6-layer-shell-qt plasma6-layer-shell-qt-6.8.2nb1 abc\n',
        )
        self.assertEqual(
            (self.real / 'failed').read_text(),
            'x11/plasma6-plasma-desktop build\n',
        )
        self.assertEqual(receipts['done'][0][0], 'x11/plasma6-layer-shell-qt')
        self.assertEqual(receipts['failed'], [])

    def test_native_success_does_not_clear_target_failure(self):
        before = self._seed(
            failed='x11/libfoo build\n'
                   'x11/libfoo@native build\n',
        )
        (self.private / 'done').write_text(
            'x11/libfoo@native libfoo-1.0nb2 abc\n'
        )
        planned = {
            'x11/libfoo': 'libfoo-1.0nb2',
            'x11/libfoo@native': 'libfoo-1.0nb2',
        }
        _merge_receipts(self.real, self.private, before, planned=planned)
        self.assertEqual((self.real / 'failed').read_text(), 'x11/libfoo build\n')
        self.assertIn('x11/libfoo@native libfoo-1.0nb2 abc\n', (self.real / 'done').read_text())

    def test_target_success_does_not_clear_native_failure(self):
        before = self._seed(
            failed='x11/libfoo build\n'
                   'x11/libfoo@native build\n',
        )
        (self.private / 'done').write_text(
            'x11/libfoo libfoo-1.0nb2 abc\n'
        )
        planned = {
            'x11/libfoo': 'libfoo-1.0nb2',
            'x11/libfoo@native': 'libfoo-1.0nb2',
        }
        _merge_receipts(self.real, self.private, before, planned=planned)
        self.assertEqual((self.real / 'failed').read_text(), 'x11/libfoo@native build\n')

    def test_old_done_row_does_not_suppress_new_planned_failure(self):
        before = self._seed(
            done='x11/qt6-qtbase-devel qt6-qtbase-devel-6.12.0nb23 old\n',
        )
        (self.private / 'failed').write_text('x11/qt6-qtbase-devel build\n')
        planned = {'x11/qt6-qtbase-devel': 'qt6-qtbase-devel-6.12.0nb24'}
        receipts = _merge_receipts(self.real, self.private, before, planned=planned)
        self.assertEqual((self.real / 'failed').read_text(), 'x11/qt6-qtbase-devel build\n')
        self.assertEqual(receipts['failed'][0][0], 'x11/qt6-qtbase-devel')
        self.assertEqual((self.real / 'done').read_text(), before['done'])

    def test_current_done_row_resolves_reason_only_failure_when_it_is_planned(self):
        before = self._seed(
            done='x11/plasma6-layer-shell-qt plasma6-layer-shell-qt-6.8.2nb1 abc\n',
            failed='x11/plasma6-layer-shell-qt incompatible-workdir\n'
                   'x11/plasma6-plasma-desktop build\n',
        )
        (self.private / 'done').write_text('')
        (self.private / 'failed').write_text('x11/plasma6-plasma-desktop build\n')
        planned = {
            'x11/plasma6-layer-shell-qt': 'plasma6-layer-shell-qt-6.8.2nb1',
            'x11/plasma6-plasma-desktop': 'plasma6-plasma-desktop-6.8.2nb4',
        }
        receipts = _merge_receipts(self.real, self.private, before, planned=planned)
        self.assertEqual((self.real / 'failed').read_text(), 'x11/plasma6-plasma-desktop build\n')
        self.assertEqual(receipts['failed'], [])

    def test_named_failure_resolves_only_when_done_pkgname_matches(self):
        before = self._seed(
            done='x11/app app-1.0nb1 old\n',
            failed='x11/app app-1.0nb2 build\n',
        )
        planned = {'x11/app': 'app-1.0nb2'}
        _merge_receipts(self.real, self.private, before, planned=planned)
        self.assertEqual((self.real / 'failed').read_text(), 'x11/app app-1.0nb2 build\n')
        (self.private / 'done').write_text(before['done'] + 'x11/app app-1.0nb2 new\n')
        (self.private / 'failed').write_text('x11/app app-1.0nb2 build\n')
        receipts = _merge_receipts(self.real, self.private, before, planned=planned)
        self.assertEqual((self.real / 'failed').read_text(), '')
        self.assertEqual(receipts['failed'], [])

    def test_new_failure_is_still_recorded(self):
        before = self._seed(failed='devel/old failed\n')
        (self.private / 'failed').write_text(
            'devel/old failed\n'
            'x11/plasma6-plasma-desktop CONF_FILES\n'
        )
        receipts = _merge_receipts(self.real, self.private, before)
        self.assertEqual(
            (self.real / 'failed').read_text(),
            'devel/old failed\n'
            'x11/plasma6-plasma-desktop CONF_FILES\n',
        )
        self.assertEqual(receipts['failed'][0][0], 'x11/plasma6-plasma-desktop')

    def test_done_path_is_not_reappended_as_failed(self):
        before = self._seed()
        (self.private / 'done').write_text('x11/app app-1.0nb1 hash\n')
        (self.private / 'failed').write_text('x11/app build\n')
        planned = {'x11/app': 'app-1.0nb1'}
        receipts = _merge_receipts(self.real, self.private, before, planned=planned)
        self.assertEqual((self.real / 'failed').read_text(), '')
        self.assertEqual(receipts['failed'], [])
        self.assertEqual((self.real / 'done').read_text(), 'x11/app app-1.0nb1 hash\n')

    def test_drop_resolved_receipts_clears_failed_and_blocked_for_current_roots(self):
        self._seed(
            failed='x11/qt6-qtbase-devel build\n'
                   'x11/plasma6-plasma-desktop build\n',
            blocked='x11/qt6-qtbase-devel blocked-dep\n'
                    'www/other blocked-dep\n',
        )
        planned = {
            'x11/qt6-qtbase-devel': 'qt6-qtbase-devel-6.12.0nb24',
            'graphics/unused@native': 'unused-1.0nb1',
        }
        drop_resolved_receipts(
            self.real,
            (
                ('x11/qt6-qtbase-devel', 'qt6-qtbase-devel-6.12.0nb24'),
                ('graphics/unused@native', 'unused-1.0nb1'),
            ),
            planned=planned,
        )
        self.assertEqual(
            (self.real / 'failed').read_text(),
            'x11/plasma6-plasma-desktop build\n',
        )
        self.assertEqual((self.real / 'blocked').read_text(), 'www/other blocked-dep\n')

    def test_retain_current_does_not_drop_failure_for_an_older_pkgname(self):
        self._seed(failed='x11/qt6-qtbase-devel qt6-qtbase-devel-6.12.0nb23 build\n')
        drop_resolved_receipts(
            self.real,
            (('x11/qt6-qtbase-devel', 'qt6-qtbase-devel-6.12.0nb24'),),
            planned={'x11/qt6-qtbase-devel': 'qt6-qtbase-devel-6.12.0nb24'},
        )
        self.assertEqual(
            (self.real / 'failed').read_text(),
            'x11/qt6-qtbase-devel qt6-qtbase-devel-6.12.0nb23 build\n',
        )

    def test_drop_resolved_receipts_rejects_a_symlink(self):
        self._seed()
        (self.real / 'failed').unlink()
        (self.real / 'failed').symlink_to('/tmp/other')
        with self.assertRaises(SDKError):
            drop_resolved_receipts(self.real, (('x11/app', 'app-1.0nb1'),), planned={'x11/app': 'app-1.0nb1'})


if __name__ == '__main__':
    unittest.main()
