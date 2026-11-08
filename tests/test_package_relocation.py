# SPDX-License-Identifier: BSD-2-Clause
import hashlib
import importlib.util
import os
from pathlib import Path
import py_compile
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk import package_relocation as relocation
from emberbsd_sdk.bootstrap_bundle import _replace_file, relocate_text
from emberbsd_sdk.errors import SDKError


class RelocationTests(unittest.TestCase):
    def check_python_relocation(self, *, same_header=False, deferred=False, interrupted=False,
                                wrong_python=False):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'destination'
            root.mkdir()
            database = Path(temporary) / 'database'
            package = database / 'fixture-1'; package.mkdir(parents=True)
            old = '/' + 'a' * (len(str(root)) - 1) if same_header else '/producer'
            source = root / 'relocated_module.py'
            source.write_text('value = ' + repr(old + '/share') + '\n')
            stamp = int(source.stat().st_mtime)
            os.utime(source, (stamp, stamp))
            caches = []
            for optimization in ('', '1', '2'):
                cache = Path(importlib.util.cache_from_source(str(source), optimization=optimization))
                py_compile.compile(str(source), cfile=str(cache), optimize=int(optimization or '0'),
                                   invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP)
                if wrong_python:
                    foreign = cache.with_name(cache.name.replace(sys.implementation.cache_tag, 'cpython-0'))
                    cache.rename(foreign)
                    cache = foreign
                caches.append(cache)
            # Only the normal and -O caches belong to this package.
            unowned = (caches[2].read_bytes(), caches[2].stat().st_mtime_ns)
            unrelated = root / 'unrelated.py'
            unrelated.write_text('value = 1\n')
            unrelated_cache = Path(importlib.util.cache_from_source(str(unrelated)))
            unrelated_cache.write_bytes(b'unrelated stale cache')
            config = root / 'local-config.py'
            config.write_text('value = ' + repr(old) + '\n')
            config_before = (config.read_bytes(), config.stat().st_mtime_ns)
            files = [source, *caches[:2], unrelated, unrelated_cache]
            contents = package / '+CONTENTS'
            contents.write_text('@name fixture-1\n@cwd ' + str(root) + '\n' + relocation.MARKER + '\n' +
                relocation.SOURCE + old + '\n' + relocation.TEXT + source.name + '\n' +
                ''.join(path.relative_to(root).as_posix() + '\n@comment MD5:' +
                        hashlib.md5(path.read_bytes()).hexdigest() + '\n' for path in files) +
                '@ignore\n' + caches[2].relative_to(root).as_posix() + '\n')
            (package / relocation.READY).write_text(old + '\n')
            def replace(path, content):
                _replace_file(path, content)
                if same_header and path == source:
                    os.utime(path, (stamp, stamp))
            with patch.object(relocation, '_replace_file', side_effect=replace):
                if wrong_python:
                    before = [path.read_bytes() for path in caches]
                    with self.assertRaisesRegex(SDKError, 'different Python'):
                        relocation.finish(root, database, 'fixture-1')
                    self.assertEqual(before, [path.read_bytes() for path in caches])
                    self.assertEqual(list(relocation.pending(root, database)), ['fixture-1'])
                    return
                if deferred:
                    before = [path.read_bytes() for path in caches[:2]]
                    relocation.finish(root, database, 'fixture-1', configure=False)
                    self.assertEqual(before, [path.read_bytes() for path in caches[:2]])
                    self.assertEqual(list(relocation.pending(root, database)), ['fixture-1'])
                if interrupted:
                    with patch.object(relocation, 'refresh_checksums', side_effect=SDKError('interrupted')):
                        with self.assertRaisesRegex(SDKError, 'interrupted'):
                            relocation.finish(root, database, 'fixture-1')
                    self.assertEqual(list(relocation.pending(root, database)), ['fixture-1'])
                relocation.finish(root, database, 'fixture-1')
            recorded = {}
            member = None
            for line in contents.read_text().splitlines():
                if line and not line.startswith('@'):
                    member = line
                elif line.startswith('@comment MD5:'):
                    recorded[member] = line.removeprefix('@comment MD5:')
            for options, cache in zip(([], ['-O']), caches[:2]):
                result = subprocess.run([sys.executable, '-I', *options, '-c',
                    'import sys; sys.path.insert(0, ' + repr(str(root)) + '); '
                    'import relocated_module; print(relocated_module.value)'],
                    capture_output=True, text=True, check=True)
                self.assertEqual(result.stdout.strip(), str(root) + '/share')
                self.assertEqual(hashlib.md5(cache.read_bytes()).hexdigest(),
                                 recorded[cache.relative_to(root).as_posix()])
            self.assertEqual(unowned, (caches[2].read_bytes(), caches[2].stat().st_mtime_ns))
            self.assertEqual(unrelated_cache.read_bytes(), b'unrelated stale cache')
            self.assertEqual(config_before, (config.read_bytes(), config.stat().st_mtime_ns))
            before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in files]
            relocation.finish(root, database, 'fixture-1')
            self.assertEqual(before, [(path.read_bytes(), path.stat().st_mtime_ns) for path in files])

    def test_relocated_python_import_preserves_owned_cache_checksums(self):
        self.check_python_relocation()

    def test_same_timestamp_size_cache_uses_relocated_runtime_constant(self):
        self.check_python_relocation(same_header=True)

    def test_python_caches_wait_for_batch_configuration(self):
        self.check_python_relocation(deferred=True)

    def test_python_cache_checksums_recover_after_interruption(self):
        self.check_python_relocation(interrupted=True)

    def test_python_cache_requires_matching_sdk_interpreter(self):
        self.check_python_relocation(wrong_python=True)

    def test_deferred_finish_can_relocate_before_later_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / 'pkgdb/fixture-1'; package.mkdir(parents=True)
            contents = package / '+CONTENTS'
            contents.write_text('@name fixture-1\n@cwd ' + str(root) + '\n' + relocation.MARKER + '\n' +
                                relocation.SOURCE + str(root) + '\n' + relocation.HOOKS + '\n')
            (package / relocation.READY).write_text(str(root) + '\n')
            with patch.object(relocation, 'run_install_hooks') as run:
                relocation.finish(root, root / 'pkgdb', 'fixture-1', configure=False)
                run.assert_not_called()
                self.assertEqual(list(relocation.pending(root, root / 'pkgdb')), ['fixture-1'])
                self.assertTrue((package / relocation.READY).exists())
                self.assertIn(relocation.HOOKS, contents.read_text())

                relocation.finish(root, root / 'pkgdb', 'fixture-1')
                run.assert_called_once()
            self.assertEqual(list(relocation.pending(root, root / 'pkgdb')), [])
            self.assertFalse((package / relocation.READY).exists())

    def test_deferred_hook_at_original_prefix_needs_completion_and_retries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / 'pkgdb/fixture-1'; package.mkdir(parents=True)
            contents = package / '+CONTENTS'
            contents.write_text('@name fixture-1\n@cwd ' + str(root) + '\n' + relocation.MARKER + '\n' +
                                relocation.SOURCE + str(root) + '\n' + relocation.HOOKS + '\n')
            self.assertEqual(list(relocation.pending(root, root / 'pkgdb')), ['fixture-1'])
            with self.assertRaisesRegex(SDKError, 'pkg_add did not complete'):
                relocation.finish(root, root / 'pkgdb', 'fixture-1')
            (package / relocation.READY).write_text(str(root) + '\n')
            with patch.object(relocation, 'run_install_hooks', side_effect=SDKError('failed')):
                with self.assertRaisesRegex(SDKError, 'failed'):
                    relocation.finish(root, root / 'pkgdb', 'fixture-1')
            with patch.object(relocation, 'run_install_hooks') as run:
                relocation.finish(root, root / 'pkgdb', 'fixture-1')
                run.assert_called_once()
            self.assertEqual(list(relocation.pending(root, root / 'pkgdb')), [])
            # A crash after the final plist update leaves only harmless state.
            (package / relocation.READY).write_text('/old\n')
            relocation.finish(root, root / 'pkgdb', 'fixture-1')
            self.assertFalse((package / relocation.READY).exists())

    def test_repeated_producer_prefix_is_refused_before_text_changes(self):
        with self.assertRaisesRegex(SDKError, 'repeat its producer prefix'):
            relocate_text(b'#!/opt/sdk/bin/python', b'/opt/sdk', b'/alt/opt/sdk')
        for destination in (b'/opt', b'/alt/opt', b'/sdk', b'/sdk/other'):
            with self.subTest(destination=destination), self.assertRaisesRegex(SDKError, 'producer prefix'):
                relocate_text(b'#!/opt/sdk/sdk/bin/python', b'/opt/sdk', destination)
        with self.assertRaisesRegex(SDKError, 'producer prefix'):
            relocate_text(b'/a/a/b/a/a', b'/a/b/a', b'/b')

    def test_incomplete_pkg_add_cannot_bless_truncated_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / 'pkgdb'
            package = database / 'fixture-1'
            package.mkdir(parents=True)
            original_hash = hashlib.md5(b'complete payload').hexdigest()
            (root / 'payload').write_bytes(b'partial')
            text = ('@name fixture-1\n@cwd ' + str(root) + '\n' + relocation.MARKER + '\n' +
                    relocation.SOURCE + '/original\npayload\n@comment MD5:' + original_hash + '\n')
            contents = package / '+CONTENTS'
            contents.write_text(text)
            with self.assertRaisesRegex(SDKError, 'pkg_add did not complete'):
                relocation.finish(root, database, 'fixture-1')
            self.assertEqual(contents.read_text(), text)
            # Even after pkg_add completion, relocation must not replace MD5s
            # of files it does not change: later pkg_admin check can detect them.
            (package / relocation.READY).write_text('/original\n')
            relocation.finish(root, database, 'fixture-1')
            self.assertIn('@comment MD5:' + original_hash, contents.read_text())
            self.assertEqual((root / 'payload').read_bytes(), b'partial')
