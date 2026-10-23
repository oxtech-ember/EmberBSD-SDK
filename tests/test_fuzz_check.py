# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Host process contracts; fixtures are not a real library fuzz campaign."""
import copy
import os
from pathlib import Path
import platform
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk import fuzz_check as fuzz
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.jsonio import read_json, write_json


class FuzzChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.corpus = self.root / 'seeds'
        self.corpus.mkdir()
        (self.corpus / 'empty').write_bytes(b'')
        (self.corpus / 'data').write_bytes(b'seed')
        self.fuzzer = self.script('fuzzer', "import sys\nprint('Done 10 runs in 0 second(s)',file=sys.stderr)\n")
        self.replay = self.script('replay', 'import pathlib,sys\npathlib.Path(sys.argv[1]).read_bytes()\n')
        self.source = self.root / 'source.c'
        self.source.write_text('int x;\n')
        self.log = self.root / 'build.log'
        self.log.write_text('fixture compiler invocations\n')
        self.manifest = {'schema': 1, 'name': 'test', 'scope': 'diagnostic-host',
                         'host': {'system': platform.system(), 'machine': platform.machine()},
                         'sources': [dict(self.artifact(self.source), name='fixture', revision='fixture-1')],
                         'tools': {'clang': dict(self.artifact(Path(sys.executable).resolve()), version='clang version 23.1.4', flags=['-fsanitize=fuzzer,address,undefined']),
                                   'gcc': dict(self.artifact(Path(sys.executable).resolve()), version='gcc 16.2.0', flags=['-fsanitize=address,undefined'])},
                         'binaries': {'fuzzer': self.artifact(self.fuzzer), 'replay': self.artifact(self.replay)},
                         'corpus': {'path': str(self.corpus), 'files': fuzz.tree_files(self.corpus)},
                         'build_logs': [self.artifact(self.log)],
                         'limitations': ['Command fixtures, not a real fuzz engine. Leak detection is unavailable in this fixture.'],
                         'sanitizer_options': {'ASAN_OPTIONS': 'abort_on_error=1:detect_leaks=0', 'UBSAN_OPTIONS': 'halt_on_error=1:print_stacktrace=1'}}
        self.path = self.root / 'build.json'
        self.output = self.root / 'evidence'
        self.save()

    def artifact(self, path):
        return {'path': str(path), 'sha256': fuzz.file_sha256(path)}

    def script(self, name, body):
        result = self.root / name
        result.write_text(f'#!{sys.executable}\n{body}')
        result.chmod(0o755)
        return result

    def save(self):
        write_json(self.path, self.manifest, exclusive=not self.path.exists())

    def run_fixture(self, **kwargs):
        return fuzz.run(self.path, self.output, runs=10, **kwargs)

    def rewrite_receipt(self, edit):
        receipt = read_json(self.output / 'evidence.json')
        edit(receipt)
        write_json(self.output / 'evidence.json', receipt, exclusive=False)

    def test_success_offline_relocation_and_trusted_digest(self):
        digest = self.run_fixture()
        relocated = self.root / 'moved'
        self.output.rename(relocated)
        self.source.unlink()
        self.fuzzer.unlink()
        self.replay.unlink()
        fuzz.verify(relocated, sha256=digest)
        with self.assertRaises(SDKError):
            fuzz.verify(relocated, sha256='0' * 64)

    def test_changed_source_refuses_current_validation(self):
        self.run_fixture()
        self.source.write_text('int changed;\n')
        fuzz.verify(self.output)
        with self.assertRaises(SDKError):
            fuzz.verify(self.output, self.path)

    def test_crashing_fuzzer_keeps_incomplete(self):
        self.script('fuzzer', 'raise SystemExit(77)\n')
        self.manifest['binaries']['fuzzer'] = self.artifact(self.fuzzer)
        self.save()
        with self.assertRaises(SDKError):
            self.run_fixture()
        self.assertTrue((self.output / 'INCOMPLETE').exists())
        self.assertEqual(read_json(self.output / 'logs/fuzz.json')['status'], 77)
        with self.assertRaises(SDKError):
            fuzz.verify(self.output)

    def test_crashing_replay_keeps_incomplete(self):
        self.script('replay', 'raise SystemExit(78)\n')
        self.manifest['binaries']['replay'] = self.artifact(self.replay)
        self.save()
        with self.assertRaises(SDKError):
            self.run_fixture()
        self.assertTrue((self.output / 'INCOMPLETE').exists())
        self.assertEqual(read_json(self.output / 'logs/replay-0.json')['status'], 78)

    def test_timeout_refuses_and_records_failure(self):
        self.script('fuzzer', 'import time\ntime.sleep(60)\n')
        self.manifest['binaries']['fuzzer'] = self.artifact(self.fuzzer)
        self.save()
        with self.assertRaises(SDKError):
            self.run_fixture(timeout=1)
        self.assertTrue((self.output / 'INCOMPLETE').exists())
        self.assertIn('deadline', read_json(self.output / 'logs/fuzz.json')['error'])

    def test_exit_zero_without_completion_is_not_success(self):
        self.script('fuzzer', 'pass\n')
        self.manifest['binaries']['fuzzer'] = self.artifact(self.fuzzer)
        self.save()
        with self.assertRaises(SDKError):
            self.run_fixture()

    def test_tampered_binary_refused_before_run(self):
        self.fuzzer.write_text('changed')
        with self.assertRaises(SDKError):
            self.run_fixture()
        self.assertFalse(self.output.exists())

    def test_missing_initial_seed_refused(self):
        (self.corpus / 'empty').unlink()
        with self.assertRaises(SDKError):
            self.run_fixture()

    def test_corpus_symlink_refused(self):
        (self.corpus / 'alias').symlink_to(self.source)
        with self.assertRaises(SDKError):
            self.run_fixture()

    def test_invalid_scope_and_gcc_version_refused(self):
        bad = copy.deepcopy(self.manifest)
        bad['scope'] = 'target'
        with self.assertRaises(SDKError):
            fuzz.validate_manifest(bad)
        bad = copy.deepcopy(self.manifest)
        bad['tools']['gcc']['version'] = 'gcc 12.1'
        with self.assertRaises(SDKError):
            fuzz.validate_manifest(bad)

    def test_duplicate_source_and_unknown_fields_refused(self):
        bad = copy.deepcopy(self.manifest)
        bad['sources'] *= 2
        with self.assertRaises(SDKError):
            fuzz.validate_manifest(bad)
        bad = copy.deepcopy(self.manifest)
        bad['ignore_errors'] = True
        with self.assertRaises(SDKError):
            fuzz.validate_manifest(bad)

    def test_sanitizer_suppression_refused(self):
        bad = copy.deepcopy(self.manifest)
        bad['sanitizer_options']['UBSAN_OPTIONS'] = 'halt_on_error=0'
        with self.assertRaises(SDKError):
            fuzz.validate_manifest(bad)

    def test_tampered_evidence_log_refused(self):
        self.run_fixture()
        (self.output / 'logs/fuzz.stderr').write_bytes(b'changed')
        with self.assertRaises(SDKError):
            fuzz.verify(self.output)

    def test_missing_replay_record_refused_even_if_inventory_rewritten(self):
        self.run_fixture()
        (self.output / 'logs/replay-0.json').unlink()
        self.rewrite_receipt(lambda r: r.update(files=[i for i in fuzz.tree_files(self.output) if i['path'] != 'evidence.json']))
        with self.assertRaises((SDKError, FileNotFoundError)):
            fuzz.verify(self.output)

    def test_replay_coverage_omission_refused(self):
        self.run_fixture()
        self.rewrite_receipt(lambda r: r['replay'].pop())
        with self.assertRaises(SDKError):
            fuzz.verify(self.output)

    def test_existing_output_and_input_overlap_refused(self):
        self.output.mkdir()
        with self.assertRaises(FileExistsError):
            self.run_fixture()
        with self.assertRaises(SDKError):
            fuzz.run(self.path, self.corpus / 'inside', runs=10)

    def test_generated_input_is_replayed(self):
        self.script('fuzzer', "import pathlib,sys\n(pathlib.Path(sys.argv[1])/'generated').write_bytes(b'new')\nprint('Done 10 runs in 0 second(s)',file=sys.stderr)\n")
        self.script('replay', "import pathlib,sys\nraise SystemExit(79 if pathlib.Path(sys.argv[1]).read_bytes()==b'new' else 0)\n")
        for name, path in (('fuzzer', self.fuzzer), ('replay', self.replay)):
            self.manifest['binaries'][name] = self.artifact(path)
        self.save()
        with self.assertRaises(SDKError):
            self.run_fixture()
        self.assertEqual(read_json(self.output / 'logs/replay-4.json')['status'], 79)

    def test_changed_source_during_run_refused(self):
        self.script('fuzzer', f"import pathlib,sys\npathlib.Path({str(self.source)!r}).write_text('changed')\nprint('Done 10 runs in 0 second(s)',file=sys.stderr)\n")
        self.manifest['binaries']['fuzzer'] = self.artifact(self.fuzzer)
        self.save()
        with self.assertRaises(SDKError):
            self.run_fixture()
        self.assertTrue((self.output / 'INCOMPLETE').exists())

    def test_bool_run_limit_refused(self):
        with self.assertRaises(SDKError):
            fuzz.run(self.path, self.output, runs=True)

    def test_duplicate_json_key_refused(self):
        self.path.write_text('{"schema":1,"schema":1}')
        with self.assertRaises(SDKError):
            self.run_fixture()


if __name__ == '__main__':
    unittest.main()
