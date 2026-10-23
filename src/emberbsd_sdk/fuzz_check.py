# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD; AI-assisted diagnostic fuzz evidence tooling.
"""Run bounded host fuzzing and check portable, unsigned evidence."""

import argparse
import hashlib
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import sys

from .errors import CommandError, SDKError
from .jsonio import json_bytes, read_json, write_json
from .paths import open_regular, verify_stable, reject_overlap
from .stream import capture_streamed

FUZZ_CHECK_API = 1
FUZZ_MANIFEST_VERSION = 1
HEX = re.compile(r'[0-9a-f]{64}')


def file_sha256(path):
    handle, before = open_regular(path)
    digest = hashlib.sha256()
    with handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    verify_stable(path, before)
    return digest.hexdigest()


def text(value):
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        raise SDKError('expected nonempty text without control characters')
    return value


def relative(value):
    text(value)
    path = Path(value)
    if path.is_absolute() or '..' in path.parts or str(path) != value or value == '.':
        raise SDKError(f'invalid relative artifact path: {value}')
    return value


def keys(value, required):
    if not isinstance(value, dict) or set(value) != set(required.split()):
        raise SDKError(f'expected fields: {required}')


def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise SDKError(f'expected integer in {low}..{high}')
    return value


def tree_files(root):
    root = Path(root)
    if not root.is_dir() or root.is_symlink():
        raise SDKError(f'expected real directory: {root}')
    result = []
    for base, dirs, files in os.walk(root):
        for name in dirs + files:
            path = Path(base) / name
            mode = path.lstat().st_mode
            if not stat.S_ISDIR(mode) and not stat.S_ISREG(mode):
                raise SDKError(f'unsupported tree entry: {path}')
        for name in files:
            path = Path(base) / name
            result.append({'path': relative(path.relative_to(root).as_posix()),
                           'sha256': file_sha256(path)})
    return sorted(result, key=lambda item: item['path'])


def artifact(value, current=False, absolute=True):
    path = text(value['path'])
    digest = value['sha256']
    if not isinstance(digest, str) or not HEX.fullmatch(digest):
        raise SDKError('invalid SHA256')
    if absolute:
        if not Path(path).is_absolute():
            raise SDKError('artifact path must be absolute')
    else:
        relative(path)
    if current and file_sha256(path) != digest:
        raise SDKError(f'artifact hash mismatch: {path}')


def validate_manifest(value, current=False):
    keys(value, 'schema name scope host sources tools binaries corpus build_logs limitations sanitizer_options')
    if value['schema'] != FUZZ_MANIFEST_VERSION or type(value['schema']) is not int:
        raise SDKError('unsupported fuzz manifest schema')
    text(value['name'])
    if value['scope'] != 'diagnostic-host':
        raise SDKError('only diagnostic-host builds are accepted')
    keys(value['host'], 'system machine')
    for part in value['host'].values():
        text(part)
    if current and value['host'] != {'system': platform.system(), 'machine': platform.machine()}:
        raise SDKError('manifest host differs from execution host')
    for field in ('sources', 'build_logs', 'limitations'):
        if not isinstance(value[field], list) or not value[field]:
            raise SDKError(f'{field} must be a nonempty list')
    names = set()
    paths = set()
    for source in value['sources']:
        keys(source, 'name revision path sha256')
        text(source['name'])
        text(source['revision'])
        if source['name'] in names or source['path'] in paths:
            raise SDKError('duplicate source name or path')
        names.add(source['name'])
        paths.add(source['path'])
        artifact(source, current)
    keys(value['tools'], 'clang gcc')
    for name, tool in value['tools'].items():
        keys(tool, 'path sha256 version flags')
        artifact(tool, current)
        if not isinstance(tool['version'], str) or not tool['version'].strip():
            raise SDKError('missing compiler version')
        if name == 'gcc' and not re.search(r'\b16\.\d', tool['version']):
            raise SDKError('replay requires GCC16')
        if name == 'clang' and 'clang' not in tool['version'].lower():
            raise SDKError('fuzzer requires Clang')
        if not isinstance(tool['flags'], list) or not tool['flags']:
            raise SDKError('missing compiler flags')
        for flag in tool['flags']:
            text(flag)
    keys(value['binaries'], 'fuzzer replay')
    for binary in value['binaries'].values():
        keys(binary, 'path sha256')
        artifact(binary, current)
    keys(value['corpus'], 'path files')
    if not Path(text(value['corpus']['path'])).is_absolute():
        raise SDKError('corpus path must be absolute')
    entries = value['corpus']['files']
    if not isinstance(entries, list) or not entries:
        raise SDKError('initial corpus must not be empty')
    for item in entries:
        keys(item, 'path sha256')
        artifact(item, absolute=False)
    if entries != sorted(entries, key=lambda item: item['path']) or len({i['path'] for i in entries}) != len(entries):
        raise SDKError('corpus paths must be unique and sorted')
    if current and tree_files(value['corpus']['path']) != entries:
        raise SDKError('initial corpus differs from manifest')
    for item in value['build_logs']:
        keys(item, 'path sha256')
        artifact(item, current)
    for limitation in value['limitations']:
        text(limitation)
    options = value['sanitizer_options']
    keys(options, 'ASAN_OPTIONS UBSAN_OPTIONS')
    if options['ASAN_OPTIONS'] not in ('abort_on_error=1:detect_leaks=1', 'abort_on_error=1:detect_leaks=0'):
        raise SDKError('ASan must abort on error and declare leak detection')
    if options['UBSAN_OPTIONS'] != 'halt_on_error=1:print_stacktrace=1':
        raise SDKError('UBSan must halt on error')
    if options['ASAN_OPTIONS'].endswith('=0') and not any('leak' in s.lower() for s in value['limitations']):
        raise SDKError('disabled leak detection requires an explicit limitation')
    return value


def _command(argv, destination, env, timeout):
    record = {'argv': argv, 'status': None, 'timeout_seconds': timeout}
    try:
        result = capture_streamed(argv, env=env, cwd=str(destination.parent),
                                  timeout=timeout, stdout_limit=1024 * 1024,
                                  empty_snapshot_timeout=10)
        if result.stderr_truncated:
            raise SDKError('diagnostic output exceeded capture limit')
        stdout, stderr = result.value, result.stderr
        record['status'] = 0
    except CommandError as exc:
        stdout, stderr = exc.stdout, exc.stderr
        record['status'] = exc.returncode
    except BaseException as exc:
        record['error'] = str(exc)
        write_json(destination.with_suffix('.json'), record)
        raise
    destination.with_suffix('.stdout').write_bytes(stdout)
    destination.with_suffix('.stderr').write_bytes(stderr)
    write_json(destination.with_suffix('.json'), record)
    if record['status'] != 0:
        raise SDKError(f'producer failed: {record["status"]}; {destination}')
    return stderr


def _copy(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def run(manifest_path, output, *, runs=1000, seed=1, max_len=65536,
        timeout=300, input_timeout=10, rss_limit_mb=2048):
    limits = {'runs': integer(runs, 1, 10**9), 'seed': integer(seed, 1, 2**31-1),
              'max_len': integer(max_len, 1, 64*1024*1024),
              'timeout': integer(timeout, 1, 86400),
              'input_timeout': integer(input_timeout, 1, 3600),
              'rss_limit_mb': integer(rss_limit_mb, 64, 1048576)}
    manifest = validate_manifest(read_json(manifest_path), current=True)
    output = Path(output).absolute()
    for item in manifest['sources'] + manifest['build_logs'] + list(manifest['binaries'].values()) + list(manifest['tools'].values()):
        reject_overlap(output, item['path'])
    reject_overlap(output, manifest['corpus']['path'])
    output.mkdir(parents=False, exist_ok=False)
    (output / 'INCOMPLETE').write_text('No successful acceptance yet.\n', encoding='utf-8')
    write_json(output / 'manifest.json', manifest)
    for name, item in manifest['binaries'].items():
        target = output / 'binaries' / name
        _copy(item['path'], target)
        target.chmod(0o755)
    for index, item in enumerate(manifest['build_logs']):
        _copy(item['path'], output / 'build-logs' / str(index))
    for item in manifest['corpus']['files']:
        for role in ('initial-corpus', 'corpus'):
            _copy(Path(manifest['corpus']['path']) / item['path'], output / role / item['path'])
    (output / 'logs').mkdir()
    (output / 'crashes').mkdir()
    # Do not inherit sanitizer settings or dynamic-loader overrides from a shell.
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'TMPDIR', 'LANG') if key in os.environ}
    env.update(manifest['sanitizer_options'])
    argv = [str(output / 'binaries/fuzzer'), str(output / 'corpus'),
            f'-runs={runs}', f'-seed={seed}', f'-max_len={max_len}',
            f'-timeout={input_timeout}', f'-rss_limit_mb={rss_limit_mb}',
            '-print_final_stats=1', f'-artifact_prefix={output / "crashes"}/']
    stderr = _command(argv, output / 'logs/fuzz', env, timeout)
    completions = re.findall(rb'Done (\d+) runs in', stderr)
    if not completions or int(completions[-1]) < runs:
        raise SDKError('libFuzzer did not confirm the requested run count')
    corpus = tree_files(output / 'corpus')
    # Replay the original seed set even if libFuzzer removes or rewrites a seed.
    replay = []
    for role in ('initial-corpus', 'corpus'):
        for item in tree_files(output / role):
            index = len(replay)
            relative_path = f'{role}/{item["path"]}'
            _command([str(output / 'binaries/replay'), str(output / relative_path)],
                     output / f'logs/replay-{index}', env, input_timeout)
            replay.append({'path': relative_path, 'sha256': item['sha256']})
    validate_manifest(read_json(manifest_path), current=True)
    if read_json(manifest_path) != manifest:
        raise SDKError('manifest changed during execution')
    if tree_files(output / 'crashes'):
        raise SDKError('crash artifacts remain')
    # Keep a failed run visibly incomplete until every result has been saved.
    (output / 'INCOMPLETE').unlink()
    receipt = {'schema': 1, 'scope': 'diagnostic-host', 'complete': True,
               'manifest_sha256': file_sha256(output / 'manifest.json'),
               'limits': limits, 'environment': env, 'replay': replay,
               'corpus': corpus, 'files': tree_files(output)}
    write_json(output / 'evidence.json', receipt)
    verify(output)
    return file_sha256(output / 'evidence.json')


def verify(evidence, manifest_path=None, sha256=None):
    evidence = Path(evidence)
    if (evidence / 'INCOMPLETE').exists():
        raise SDKError('incomplete run')
    if sha256 is not None and file_sha256(evidence / 'evidence.json') != sha256:
        raise SDKError('trusted evidence digest differs')
    receipt = read_json(evidence / 'evidence.json')
    keys(receipt, 'schema scope complete manifest_sha256 limits environment replay corpus files')
    if receipt['schema'] != 1 or receipt['scope'] != 'diagnostic-host' or receipt['complete'] is not True:
        raise SDKError('unsupported or incomplete evidence')
    files = [item for item in tree_files(evidence) if item['path'] != 'evidence.json']
    if files != receipt['files']:
        raise SDKError('evidence inventory differs')
    if file_sha256(evidence / 'manifest.json') != receipt['manifest_sha256']:
        raise SDKError('manifest snapshot differs')
    manifest = validate_manifest(read_json(evidence / 'manifest.json'))
    if manifest_path is not None:
        current = validate_manifest(read_json(manifest_path), current=True)
        if current != manifest:
            raise SDKError('current manifest differs from evidence')
    for name, item in manifest['binaries'].items():
        if file_sha256(evidence / 'binaries' / name) != item['sha256']:
            raise SDKError('binary snapshot differs')
    for index, item in enumerate(manifest['build_logs']):
        if file_sha256(evidence / 'build-logs' / str(index)) != item['sha256']:
            raise SDKError('build log snapshot differs')
    if tree_files(evidence / 'initial-corpus') != manifest['corpus']['files']:
        raise SDKError('seed snapshot differs')
    if tree_files(evidence / 'corpus') != receipt['corpus']:
        raise SDKError('corpus snapshot differs')
    expected = [{'path': f'{role}/{item["path"]}', 'sha256': item['sha256']}
                for role in ('initial-corpus', 'corpus')
                for item in tree_files(evidence / role)]
    if expected != receipt['replay']:
        raise SDKError('replay coverage is incomplete')
    limits = receipt['limits']
    keys(limits, 'runs seed max_len timeout input_timeout rss_limit_mb')
    integer(limits['runs'], 1, 10**9)
    for name in ('seed', 'max_len', 'timeout', 'input_timeout', 'rss_limit_mb'):
        integer(limits[name], 1, 2**31-1)
    for name in ['fuzz'] + [f'replay-{i}' for i in range(len(expected))]:
        command = read_json(evidence / 'logs' / f'{name}.json')
        keys(command, 'argv status timeout_seconds')
        if command['status'] != 0 or type(command['status']) is not int:
            raise SDKError(f'failed command: {name}')
        for suffix in ('stdout', 'stderr'):
            file_sha256(evidence / 'logs' / f'{name}.{suffix}')
        argv = command['argv']
        if not isinstance(argv, list) or not argv:
            raise SDKError('missing command arguments')
        # Stored commands retain their original paths after evidence relocation.
        root = Path(argv[0]).parent.parent
        if name == 'fuzz':
            wanted = [str(root / 'binaries/fuzzer'), str(root / 'corpus'),
                      f'-runs={limits["runs"]}', f'-seed={limits["seed"]}',
                      f'-max_len={limits["max_len"]}', f'-timeout={limits["input_timeout"]}',
                      f'-rss_limit_mb={limits["rss_limit_mb"]}', '-print_final_stats=1',
                      f'-artifact_prefix={root / "crashes"}/']
            budget = limits['timeout']
        else:
            index = int(name.split('-')[1])
            wanted = [str(root / 'binaries/replay'), str(root / expected[index]['path'])]
            budget = limits['input_timeout']
        if argv != wanted or command['timeout_seconds'] != budget:
            raise SDKError(f'command differs from the run contract: {name}')
    stderr = (evidence / 'logs/fuzz.stderr').read_bytes()
    done = re.findall(rb'Done (\d+) runs in', stderr)
    if not done or int(done[-1]) < limits['runs'] or tree_files(evidence / 'crashes'):
        raise SDKError('fuzz completion is absent or crash artifacts remain')
    for key, value in manifest['sanitizer_options'].items():
        if receipt['environment'].get(key) != value:
            raise SDKError('sanitizer environment differs')
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='operation', required=True)
    start = sub.add_parser('run')
    start.add_argument('--manifest', required=True)
    start.add_argument('--output', required=True)
    for flag, default in (('runs', 1000), ('seed', 1), ('max-len', 65536),
                          ('timeout', 300), ('input-timeout', 10), ('rss-limit-mb', 2048)):
        start.add_argument('--' + flag, type=int, default=default)
    check = sub.add_parser('verify')
    check.add_argument('--evidence', required=True)
    check.add_argument('--manifest')
    check.add_argument('--sha256')
    args = vars(parser.parse_args(argv))
    operation = args.pop('operation')
    try:
        if operation == 'run':
            args['manifest_path'] = args.pop('manifest')
            print(run(**args))
        else:
            verify(args['evidence'], args['manifest'], args['sha256'])
            print('Evidence is internally consistent; no origin or formal-proof claim.')
        return 0
    except (SDKError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f'fuzz-check: {exc}', file=sys.stderr)
        return 1
