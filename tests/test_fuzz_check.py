# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Host process contracts; fixtures are not a real library fuzz campaign."""
import copy
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk import fuzz_check as fuzz
from emberbsd_sdk.errors import CommandError, SDKError
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
        self.capture_patch = patch.object(fuzz, 'capture_streamed', self.capture_fixture)
        self.capture_patch.start()
        self.addCleanup(self.capture_patch.stop)

    @staticmethod
    def capture_fixture(argv, *, env, cwd, timeout, **_options):
        """Keep runner-contract tests independent of host process inspection."""
        try:
            result = subprocess.run(argv, env=env, cwd=cwd, timeout=timeout,
                                    stdin=subprocess.DEVNULL, capture_output=True,
                                    check=False)
        except subprocess.TimeoutExpired as exc:
            raise SDKError('fixture producer deadline expired') from exc
        if result.returncode:
            raise CommandError(result.returncode, argv, result.stdout, result.stderr)
        return SimpleNamespace(value=result.stdout, stderr=result.stderr,
                               stderr_truncated=False)

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

    def add_lsan_binaries(self):
        probe = self.script('lsan_probe',
                            "import sys\nprint('ERROR: LeakSanitizer: detected memory leaks',file=sys.stderr)\nprint('Direct leak of 37 byte',file=sys.stderr)\nraise SystemExit(1)\n")
        replay = self.script('lsan_replay',
                             "import os,pathlib,sys\nprint(os.environ['ASAN_OPTIONS'])\npathlib.Path(sys.argv[1]).read_bytes()\n")
        self.manifest['binaries']['lsan_probe'] = self.artifact(probe)
        self.manifest['binaries']['lsan_replay'] = self.artifact(replay)
        self.manifest['tools']['clang_lsan'] = {
            **self.artifact(Path(sys.executable).resolve()),
            'version': 'clang version 23.1.4', 'flags': ['-fsanitize=address']}
        self.manifest['sanitizer_options']['ASAN_OPTIONS'] = 'abort_on_error=1:detect_leaks=1'
        self.manifest['limitations'] = ['Fixture output validates the LSan process contract only.']
        self.save()

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

    def llvm_manifest(self):
        self.manifest['schema'] = 2
        self.manifest['tools']['clang']['family'] = 'clang'
        self.manifest['tools']['replay'] = {
            **self.manifest['tools'].pop('gcc'), 'family': 'clang',
            'version': 'clang version 23.1.4'}

    def test_clang_production_replay_does_not_require_gcc(self):
        self.llvm_manifest()
        self.save()
        self.run_fixture()
        fuzz.verify(self.output)
        self.assertEqual(read_json(self.output / 'manifest.json')['tools']['replay']['family'], 'clang')

    def test_new_manifest_can_select_explicit_gcc_replay(self):
        self.llvm_manifest()
        self.manifest['tools']['replay'].update(family='gcc', version='gcc 16.2.0')
        self.save()
        self.run_fixture()
        fuzz.verify(self.output)

    def test_replay_family_must_agree_with_compiler_version(self):
        self.llvm_manifest()
        self.manifest['tools']['replay']['version'] = 'gcc 16.2.0'
        with self.assertRaisesRegex(SDKError, 'Clang'):
            fuzz.validate_manifest(self.manifest)

    def test_new_manifest_rejects_ambiguous_gcc_alias(self):
        self.llvm_manifest()
        self.manifest['tools']['gcc'] = dict(self.manifest['tools']['replay'])
        with self.assertRaisesRegex(SDKError, 'unsupported fuzz compiler'):
            fuzz.validate_manifest(self.manifest)

    def test_new_memcheck_uses_the_production_family(self):
        self.llvm_manifest()
        binary = self.script('memcheck', 'print("uninstrumented replay fixture")\n')
        self.manifest['binaries']['memcheck'] = self.artifact(binary)
        self.manifest['tools']['memcheck'] = {
            **self.manifest['tools']['replay'], 'flags': ['-g']}
        fuzz.validate_manifest(self.manifest)
        self.manifest['tools']['memcheck'].update(family='gcc', version='gcc 16.2.0')
        with self.assertRaisesRegex(SDKError, 'production replay compilers differ'):
            fuzz.validate_manifest(self.manifest)

    def test_new_fuzzer_cannot_use_the_gcc_exception(self):
        self.llvm_manifest()
        self.manifest['tools']['clang'].update(family='gcc', version='gcc 16.2.0')
        with self.assertRaisesRegex(SDKError, 'fuzzer and LeakSanitizer require Clang'):
            fuzz.validate_manifest(self.manifest)

    def test_v1_evidence_remains_verifiable(self):
        self.run_fixture()
        self.rewrite_receipt(lambda receipt: (receipt.pop('memory_checks'),
                                              receipt.update(schema=1)))
        self.assertEqual(fuzz.verify(self.output)['schema'], 1)

    def test_lsan_requires_positive_control(self):
        self.manifest['sanitizer_options']['ASAN_OPTIONS'] = 'abort_on_error=1:detect_leaks=1'
        self.save()
        with self.assertRaisesRegex(SDKError, 'lsan_probe and lsan_replay'):
            fuzz.validate_manifest(read_json(self.path))

    def test_legacy_lsan_manifest_is_limited_to_canary_evidence(self):
        probe = self.script('legacy_lsan_probe', 'raise SystemExit(1)\n')
        self.manifest['binaries']['lsan_probe'] = self.artifact(probe)
        self.manifest['tools']['clang_lsan'] = {
            **self.artifact(Path(sys.executable).resolve()),
            'version': 'clang version 23.1.4', 'flags': ['-fsanitize=address']}
        self.manifest['sanitizer_options']['ASAN_OPTIONS'] = 'abort_on_error=1:detect_leaks=1'
        self.manifest['limitations'] = ['Legacy schema 2 can attest only its LSan positive control.']
        self.save()
        legacy_manifest = read_json(self.path)
        self.assertIs(fuzz.validate_manifest(legacy_manifest, legacy_lsan=True), legacy_manifest)
        with self.assertRaisesRegex(SDKError, 'lsan_probe and lsan_replay'):
            fuzz.validate_manifest(read_json(self.path))

        self.add_lsan_binaries()
        with self.assertRaisesRegex(SDKError, 'cannot attest per-input replay'):
            fuzz.validate_manifest(read_json(self.path), legacy_lsan=True)

    def test_lsan_positive_control_is_checked_and_recorded(self):
        self.add_lsan_binaries()
        self.run_fixture()
        receipt = read_json(self.output / 'evidence.json')
        lsan = receipt['memory_checks']['lsan']
        self.assertEqual(lsan['status'], 'passed')
        self.assertEqual(lsan['canary'], 'logs/lsan-canary')
        self.assertEqual(len(lsan['replay']), 4)
        self.assertEqual(lsan['environment']['ASAN_OPTIONS'], 'abort_on_error=1:detect_leaks=1')
        self.assertEqual(receipt['environment']['ASAN_OPTIONS'], 'abort_on_error=1:detect_leaks=0')
        self.assertIn('abort_on_error=1:detect_leaks=1',
                      (self.output / 'logs/lsan-replay-0.stdout').read_text())

    def test_lsan_replay_failure_keeps_run_incomplete(self):
        self.add_lsan_binaries()
        replay = self.script('lsan_replay',
                             "import pathlib,sys\nraise SystemExit(23 if pathlib.Path(sys.argv[1]).read_bytes()==b'seed' else 0)\n")
        self.manifest['binaries']['lsan_replay'] = self.artifact(replay)
        self.save()
        with self.assertRaisesRegex(SDKError, 'producer failed: 23'):
            self.run_fixture()
        self.assertTrue((self.output / 'INCOMPLETE').exists())
        self.assertEqual(read_json(self.output / 'logs/lsan-replay-0.json')['status'], 23)

    def test_v1_evidence_cannot_attest_lsan(self):
        self.add_lsan_binaries()
        self.run_fixture()
        self.rewrite_receipt(lambda receipt: (receipt.pop('memory_checks'),
                                              receipt.update(schema=1)))
        with self.assertRaisesRegex(SDKError, 'schema 1 cannot attest LSan'):
            fuzz.verify(self.output)

    def test_lsan_canary_without_leak_report_is_rejected(self):
        self.add_lsan_binaries()
        probe = self.script('bad_lsan_probe', "raise SystemExit(1)\n")
        self.manifest['binaries']['lsan_probe'] = self.artifact(probe)
        self.save()
        with self.assertRaisesRegex(SDKError, 'positive control'):
            self.run_fixture()
        self.assertTrue((self.output / 'INCOMPLETE').exists())

    def add_memcheck_binary(self):
        binary = self.script('memcheck',
                             '# distinct uninstrumented replay fixture\nimport pathlib,sys\npathlib.Path(sys.argv[1]).read_bytes()\n')
        self.manifest['binaries']['memcheck'] = self.artifact(binary)
        self.manifest['tools']['gcc_memcheck'] = {
            **self.artifact(Path(sys.executable).resolve()),
            'version': 'gcc 16.2.0', 'flags': ['-O1', '-g', '-fno-omit-frame-pointer']}
        self.save()

    def install_fake_valgrind(self, *, errors=False):
        bindir = self.root / 'fake-bin'
        bindir.mkdir()
        tool = bindir / 'valgrind'
        error_value = 'True' if errors else 'False'
        tool.write_text(
            f'#!{sys.executable}\n'
            'import pathlib,subprocess,sys\n'
            "args=sys.argv[1:]\n"
            "if args == ['--version']:\n print('valgrind-3.27.1')\n raise SystemExit(0)\n"
            "xml=next(x.split('=',1)[1] for x in args if x.startswith('--xml-file='))\n"
            "subprocess.run(args[-2:],check=True)\n"
            f"pathlib.Path(xml).write_text('<valgrindoutput><error/>') if {error_value} else pathlib.Path(xml).write_text('<valgrindoutput><errorcounts/></valgrindoutput>')\n"
            f"raise SystemExit(97 if {error_value} else 0)\n")
        tool.chmod(0o755)
        return bindir

    def test_optional_memcheck_records_unsupported_host(self):
        self.add_memcheck_binary()
        self.manifest['host'] = {'system': 'Darwin', 'machine': 'arm64'}
        self.save()
        with patch.object(fuzz.platform, 'system', return_value='Darwin'), \
             patch.object(fuzz.platform, 'machine', return_value='arm64'):
            self.run_fixture(memcheck='optional')
        memcheck = read_json(self.output / 'evidence.json')['memory_checks']['memcheck']
        self.assertEqual(memcheck['requested'], 'optional')
        self.assertEqual(memcheck['status'], 'unsupported')
        self.assertIsNone(memcheck['tool'])
        self.assertTrue(memcheck['reason'])

    def test_optional_memcheck_without_binary_is_unavailable(self):
        self.manifest['host'] = {'system': 'Linux', 'machine': 'x86_64'}
        self.save()
        with patch.object(fuzz.platform, 'system', return_value='Linux'), \
             patch.object(fuzz.platform, 'machine', return_value='x86_64'):
            self.run_fixture(memcheck='optional')
        memcheck = read_json(self.output / 'evidence.json')['memory_checks']['memcheck']
        self.assertEqual(memcheck['status'], 'unavailable')
        self.assertIn('no uninstrumented production', memcheck['reason'])

    def test_required_memcheck_refuses_unsupported_host_before_run(self):
        self.add_memcheck_binary()
        self.manifest['host'] = {'system': 'Darwin', 'machine': 'arm64'}
        self.save()
        with patch.object(fuzz.platform, 'system', return_value='Darwin'), \
             patch.object(fuzz.platform, 'machine', return_value='arm64'):
            with self.assertRaisesRegex(SDKError, 'required but unsupported'):
                self.run_fixture(memcheck='required')
        self.assertFalse(self.output.exists())

    def test_memcheck_executes_and_records_full_corpus(self):
        self.add_memcheck_binary()
        self.manifest['host'] = {'system': 'Linux', 'machine': 'x86_64'}
        self.save()
        bindir = self.install_fake_valgrind()
        path = str(bindir) + os.pathsep + os.environ.get('PATH', os.defpath)
        with patch.dict(os.environ, {'PATH': path}), \
             patch.object(fuzz.platform, 'system', return_value='Linux'), \
             patch.object(fuzz.platform, 'machine', return_value='x86_64'):
            self.run_fixture(memcheck='optional')
        memcheck = read_json(self.output / 'evidence.json')['memory_checks']['memcheck']
        self.assertEqual(memcheck['status'], 'passed')
        self.assertEqual(len(memcheck['replay']), 4)
        self.assertIn('valgrind-3.27.1', memcheck['tool']['version'])

    def test_memcheck_error_exit_keeps_run_incomplete(self):
        self.add_memcheck_binary()
        self.manifest['host'] = {'system': 'Linux', 'machine': 'x86_64'}
        self.save()
        bindir = self.install_fake_valgrind(errors=True)
        path = str(bindir) + os.pathsep + os.environ.get('PATH', os.defpath)
        with patch.dict(os.environ, {'PATH': path}), \
             patch.object(fuzz.platform, 'system', return_value='Linux'), \
             patch.object(fuzz.platform, 'machine', return_value='x86_64'):
            with self.assertRaisesRegex(SDKError, 'producer failed: 97'):
                self.run_fixture(memcheck='required')
        self.assertTrue((self.output / 'INCOMPLETE').exists())

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
