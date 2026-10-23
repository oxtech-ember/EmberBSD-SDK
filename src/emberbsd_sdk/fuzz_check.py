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
import xml.etree.ElementTree as ET

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


def validate_manifest(value, current=False, *, legacy_lsan=False):
    keys(value, 'schema name scope host sources tools binaries corpus build_logs limitations sanitizer_options')
    if legacy_lsan and 'lsan_replay' in value.get('binaries', {}):
        raise SDKError('legacy LeakSanitizer evidence cannot attest per-input replay')
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
    if not isinstance(value['tools'], dict) or not {'clang', 'gcc'} <= set(value['tools']):
        raise SDKError('tools must include clang and gcc')
    if set(value['tools']) - {'clang', 'gcc', 'clang_lsan', 'gcc_memcheck'}:
        raise SDKError('unsupported fuzz compiler tool')
    for name, tool in value['tools'].items():
        keys(tool, 'path sha256 version flags')
        artifact(tool, current)
        if not isinstance(tool['version'], str) or not tool['version'].strip():
            raise SDKError('missing compiler version')
        if name.startswith('gcc') and not re.search(r'\b16\.\d', tool['version']):
            raise SDKError('replay requires GCC16')
        if name.startswith('clang') and 'clang' not in tool['version'].lower():
            raise SDKError('fuzzer requires Clang')
        if not isinstance(tool['flags'], list) or not tool['flags']:
            raise SDKError('missing compiler flags')
        for flag in tool['flags']:
            text(flag)
        if name == 'gcc_memcheck' and any('-fsanitize=' in flag for flag in tool['flags']):
            raise SDKError('Memcheck replay must not use compiler sanitizers')
    if not isinstance(value['binaries'], dict) or not {'fuzzer', 'replay'} <= set(value['binaries']):
        raise SDKError('binaries must include fuzzer and replay')
    if set(value['binaries']) - {'fuzzer', 'replay', 'lsan_probe', 'lsan_replay', 'memcheck'}:
        raise SDKError('unsupported fuzz binary')
    for binary in value['binaries'].values():
        keys(binary, 'path sha256')
        artifact(binary, current)
    if ('memcheck' in value['binaries'] and
            value['binaries']['memcheck']['sha256'] == value['binaries']['replay']['sha256']):
        raise SDKError('Memcheck requires a separate replay binary without sanitizers')
    if ('lsan_replay' in value['binaries'] and
            value['binaries']['lsan_replay']['sha256'] in {
                value['binaries']['fuzzer']['sha256'],
                value['binaries']['replay']['sha256'],
                value['binaries'].get('lsan_probe', {}).get('sha256')}):
        raise SDKError('LeakSanitizer requires a separate non-fuzzer replay binary')
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
    lsan_binaries = {'lsan_probe'} if legacy_lsan else {'lsan_probe', 'lsan_replay'}
    if options['ASAN_OPTIONS'] == 'abort_on_error=1:detect_leaks=1' and not lsan_binaries <= set(value['binaries']):
        raise SDKError('LeakSanitizer requires positive-control lsan_probe and lsan_replay binaries')
    if 'lsan_probe' in value['binaries'] and 'clang_lsan' not in value['tools']:
        raise SDKError('LeakSanitizer probe requires recorded Clang build flags')
    if 'lsan_replay' in value['binaries'] and 'clang_lsan' not in value['tools']:
        raise SDKError('LeakSanitizer replay requires recorded Clang build flags')
    clang_lsan_binaries = {'lsan_probe'} if legacy_lsan else {'lsan_probe', 'lsan_replay'}
    if ('clang_lsan' in value['tools'] and not clang_lsan_binaries <= set(value['binaries'])):
        raise SDKError('clang_lsan tool record requires lsan_probe and lsan_replay binaries')
    if ({'lsan_probe', 'lsan_replay'} & set(value['binaries']) and
            options['ASAN_OPTIONS'] != 'abort_on_error=1:detect_leaks=1'):
        raise SDKError('LeakSanitizer binaries require leak detection to be enabled')
    if ('lsan_replay' in value['binaries'] and
            not any(flag.startswith('-fsanitize=address')
                    for flag in value['tools']['clang_lsan']['flags'])):
        raise SDKError('LeakSanitizer requires an address-sanitized Clang build')
    if 'memcheck' in value['binaries'] and 'gcc_memcheck' not in value['tools']:
        raise SDKError('Memcheck replay requires recorded GCC16 build flags')
    if 'gcc_memcheck' in value['tools'] and 'memcheck' not in value['binaries']:
        raise SDKError('gcc_memcheck tool record requires a memcheck replay binary')
    if options['ASAN_OPTIONS'].endswith('=0') and not any('leak' in s.lower() for s in value['limitations']):
        raise SDKError('disabled leak detection requires an explicit limitation')
    return value


def _command(argv, destination, env, timeout, *, allow_failure=False):
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
    if record['status'] != 0 and not allow_failure:
        raise SDKError(f'producer failed: {record["status"]}; {destination}')
    return stderr


def _memcheck_platform():
    system = platform.system()
    machine = platform.machine().lower()
    if system == 'Linux':
        supported = {
            'x86_64', 'amd64', 'i386', 'i486', 'i586', 'i686',
            'ppc', 'powerpc', 'ppc64', 'powerpc64', 'ppc64le',
            's390x', 'armv7l', 'armv8l', 'aarch64', 'arm64',
            'mips', 'mipsel', 'mips64', 'mips64el', 'riscv64'
        }
        if machine in supported:
            return None
    elif system == 'FreeBSD':
        if machine in {'i386', 'i486', 'i586', 'i686', 'x86', 'x86_64', 'amd64'}:
            return None
        if machine in {'aarch64', 'arm64'}:
            try:
                if int(platform.release().split('.')[0]) >= 14:
                    return None
            except ValueError:
                pass
    elif system == 'Darwin':
        version = platform.mac_ver()[0]
        try:
            major_minor = tuple(int(part) for part in version.split('.')[:2])
        except ValueError:
            major_minor = ()
        if machine in {'x86_64', 'amd64'} and major_minor and major_minor <= (11, 0):
            return None
        if machine in {'i386', 'i686', 'x86'} and major_minor and major_minor <= (10, 13):
            return None
    return f'{system}/{machine} is not supported by the current Valgrind platform matrix'


def _memcheck_preflight(mode, env, binaries):
    if mode not in {'off', 'optional', 'required'}:
        raise SDKError('memcheck mode must be off, optional, or required')
    if mode == 'off':
        return {'requested': mode, 'status': 'disabled', 'reason': None,
                'path': None, 'sha256': None}
    reason = _memcheck_platform()
    if reason is not None:
        status = 'unsupported'
    elif 'memcheck' not in binaries:
        status, reason = 'unavailable', 'manifest has no uninstrumented GCC16 memcheck binary'
    else:
        found = shutil.which('valgrind', path=env.get('PATH'))
        if found is None:
            status, reason = 'unavailable', 'valgrind executable was not found in PATH'
        else:
            path = Path(found).resolve(strict=True)
            if not path.is_file() or not os.access(path, os.X_OK):
                raise SDKError(f'valgrind is not an executable regular file: {path}')
            return {'requested': mode, 'status': 'ready', 'reason': None,
                    'path': str(path), 'sha256': file_sha256(path)}
    if mode == 'required':
        raise SDKError(f'Memcheck is required but {status}: {reason}')
    return {'requested': mode, 'status': status, 'reason': reason,
            'path': None, 'sha256': None}


def _memcheck_argv(tool_path, root, relative_path, xml_path):
    return [tool_path, '--tool=memcheck',
            '--error-exitcode=97', '--leak-check=full',
            '--show-leak-kinds=all',
            '--errors-for-leak-kinds=definite,indirect,possible',
            '--track-origins=yes', '--xml=yes', f'--xml-file={xml_path}',
            str(root / 'binaries/memcheck'), str(root / relative_path)]


def _verify_lsan_canary(evidence, root, manifest, timeout):
    if manifest['sanitizer_options']['ASAN_OPTIONS'] == 'abort_on_error=1:detect_leaks=0':
        return {'status': 'disabled', 'canary': None}
    name = 'lsan-canary'
    command = read_json(evidence / 'logs' / f'{name}.json')
    keys(command, 'argv status timeout_seconds')
    if command['argv'] != [str(root / 'binaries/lsan_probe')]:
        raise SDKError('LeakSanitizer canary command differs')
    if command['timeout_seconds'] != timeout or type(command['status']) is not int or command['status'] == 0:
        raise SDKError('LeakSanitizer canary did not fail as expected')
    stderr = (evidence / 'logs' / f'{name}.stderr').read_bytes()
    if b'LeakSanitizer: detected memory leaks' not in stderr or b'Direct leak of' not in stderr:
        raise SDKError('LeakSanitizer canary lacks a leak report')
    return {'status': 'passed', 'canary': f'logs/{name}'}


def _xml_error_count(path):
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise SDKError(f'invalid Valgrind XML report: {path}: {exc}') from exc
    return len(root.findall('.//error'))


def _copy(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def run(manifest_path, output, *, runs=1000, seed=1, max_len=65536,
        timeout=300, input_timeout=10, rss_limit_mb=2048, memcheck='off'):
    limits = {'runs': integer(runs, 1, 10**9), 'seed': integer(seed, 1, 2**31-1),
              'max_len': integer(max_len, 1, 64*1024*1024),
              'timeout': integer(timeout, 1, 86400),
              'input_timeout': integer(input_timeout, 1, 3600),
              'rss_limit_mb': integer(rss_limit_mb, 64, 1048576)}
    manifest = validate_manifest(read_json(manifest_path), current=True)
    base_env = {key: os.environ[key] for key in ('PATH', 'HOME', 'TMPDIR', 'LANG') if key in os.environ}
    memcheck_plan = _memcheck_preflight(memcheck, base_env, manifest['binaries'])
    output = Path(output).absolute()
    for item in manifest['sources'] + manifest['build_logs'] + list(manifest['binaries'].values()) + list(manifest['tools'].values()):
        reject_overlap(output, item['path'])
    if memcheck_plan['path'] is not None:
        reject_overlap(output, memcheck_plan['path'])
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
    if memcheck_plan['status'] == 'ready':
        _copy(memcheck_plan['path'], output / 'tools/valgrind')
        (output / 'tools/valgrind').chmod(0o755)
    # Keep RSS-limited libFuzzer and GCC replay free of its process-lifetime
    # RSS-monitor allocations. Check each retained input in a separate Clang
    # LSan replay process, which has no libFuzzer worker thread.
    lsan_enabled = manifest['sanitizer_options']['ASAN_OPTIONS'] == 'abort_on_error=1:detect_leaks=1'
    env = dict(base_env)
    env.update(manifest['sanitizer_options'])
    lsan_env = dict(env)
    if lsan_enabled:
        env['ASAN_OPTIONS'] = 'abort_on_error=1:detect_leaks=0'
    lsan = {'status': 'disabled', 'canary': None, 'replay': [], 'environment': None}
    if lsan_enabled:
        canary_stderr = _command([str(output / 'binaries/lsan_probe')],
                                 output / 'logs/lsan-canary', lsan_env, input_timeout,
                                 allow_failure=True)
        canary = read_json(output / 'logs/lsan-canary.json')
        if (canary['status'] == 0 or
                b'LeakSanitizer: detected memory leaks' not in canary_stderr or
                b'Direct leak of' not in canary_stderr):
            raise SDKError('LeakSanitizer positive control did not report its intentional leak')
        lsan = {'status': 'passed', 'canary': 'logs/lsan-canary', 'replay': [],
                'environment': {'ASAN_OPTIONS': lsan_env['ASAN_OPTIONS'],
                                'UBSAN_OPTIONS': lsan_env['UBSAN_OPTIONS']}}

    memcheck_result = {'requested': memcheck, 'status': memcheck_plan['status'],
                       'reason': memcheck_plan['reason'], 'tool': None, 'replay': []}
    if memcheck_plan['status'] == 'ready':
        _command([memcheck_plan['path'], '--version'],
                 output / 'logs/memcheck-version', base_env, 30)
        version = (output / 'logs/memcheck-version.stdout').read_text(
            encoding='utf-8', errors='replace').strip()
        if not version:
            version = (output / 'logs/memcheck-version.stderr').read_text(
                encoding='utf-8', errors='replace').strip()
        if not version:
            raise SDKError('Valgrind did not report its version')
        memcheck_result['tool'] = {
            'path': memcheck_plan['path'], 'sha256': memcheck_plan['sha256'],
            'version': version, 'snapshot': 'tools/valgrind'}
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
    if lsan_enabled:
        for index, item in enumerate(replay):
            _command([str(output / 'binaries/lsan_replay'), str(output / item['path'])],
                     output / f'logs/lsan-replay-{index}', lsan_env, input_timeout)
            lsan['replay'].append(item)
    if memcheck_plan['status'] == 'ready':
        for index, item in enumerate(replay):
            xml_path = output / f'logs/memcheck-{index}.xml'
            _command(_memcheck_argv(memcheck_plan['path'], output, item['path'], xml_path),
                     output / f'logs/memcheck-{index}', base_env, input_timeout)
            if _xml_error_count(xml_path):
                raise SDKError(f'Valgrind Memcheck reported errors for {item["path"]}')
            memcheck_result['replay'].append(item)
        memcheck_result['status'] = 'passed'
    validate_manifest(read_json(manifest_path), current=True)
    if read_json(manifest_path) != manifest:
        raise SDKError('manifest changed during execution')
    if tree_files(output / 'crashes'):
        raise SDKError('crash artifacts remain')
    # Keep a failed run visibly incomplete until every result has been saved.
    (output / 'INCOMPLETE').unlink()
    receipt = {'schema': 3, 'scope': 'diagnostic-host', 'complete': True,
               'manifest_sha256': file_sha256(output / 'manifest.json'),
               'limits': limits, 'environment': env, 'replay': replay,
               'corpus': corpus, 'files': tree_files(output),
               'memory_checks': {'lsan': lsan, 'memcheck': memcheck_result}}
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
    base_fields = 'schema scope complete manifest_sha256 limits environment replay corpus files'
    if receipt.get('schema') == 1:
        keys(receipt, base_fields)
    elif receipt.get('schema') == 2:
        keys(receipt, base_fields + ' memory_checks')
    elif receipt.get('schema') == 3:
        keys(receipt, base_fields + ' memory_checks')
    else:
        raise SDKError('unsupported evidence schema')
    if receipt['scope'] != 'diagnostic-host' or receipt['complete'] is not True:
        raise SDKError('unsupported or incomplete evidence')
    files = [item for item in tree_files(evidence) if item['path'] != 'evidence.json']
    if files != receipt['files']:
        raise SDKError('evidence inventory differs')
    if file_sha256(evidence / 'manifest.json') != receipt['manifest_sha256']:
        raise SDKError('manifest snapshot differs')
    manifest = validate_manifest(read_json(evidence / 'manifest.json'),
                                 legacy_lsan=receipt.get('schema') == 2)
    if receipt['schema'] == 1 and (
            manifest['sanitizer_options']['ASAN_OPTIONS'] == 'abort_on_error=1:detect_leaks=1' or
            {'lsan_probe', 'lsan_replay', 'memcheck'} & set(manifest['binaries'])):
        raise SDKError('evidence schema 1 cannot attest LSan or Memcheck results')
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
    command_names = ['fuzz'] + [f'replay-{i}' for i in range(len(expected))]
    if receipt['schema'] == 2 and manifest['sanitizer_options']['ASAN_OPTIONS'] == 'abort_on_error=1:detect_leaks=1':
        command_names.append('lsan-canary')
    if receipt['schema'] == 3 and manifest['sanitizer_options']['ASAN_OPTIONS'] == 'abort_on_error=1:detect_leaks=1':
        command_names += ['lsan-canary'] + [f'lsan-replay-{i}' for i in range(len(expected))]
    root = None
    for name in command_names:
        command = read_json(evidence / 'logs' / f'{name}.json')
        keys(command, 'argv status timeout_seconds')
        if type(command['status']) is not int:
            raise SDKError(f'invalid command status: {name}')
        if name != 'lsan-canary' and command['status'] != 0:
            raise SDKError(f'failed command: {name}')
        for suffix in ('stdout', 'stderr'):
            file_sha256(evidence / 'logs' / f'{name}.{suffix}')
        argv = command['argv']
        if not isinstance(argv, list) or not argv:
            raise SDKError('missing command arguments')
        # Stored commands retain their original paths after evidence relocation.
        command_root = Path(argv[0]).parent.parent
        if name == 'fuzz':
            root = command_root
        elif command_root != root:
            raise SDKError('command paths do not share one evidence root')
        if name == 'fuzz':
            wanted = [str(command_root / 'binaries/fuzzer'), str(command_root / 'corpus'),
                      f'-runs={limits["runs"]}', f'-seed={limits["seed"]}',
                      f'-max_len={limits["max_len"]}', f'-timeout={limits["input_timeout"]}',
                      f'-rss_limit_mb={limits["rss_limit_mb"]}', '-print_final_stats=1',
                      f'-artifact_prefix={command_root / "crashes"}/']
            budget = limits['timeout']
        elif name.startswith('replay-'):
            index = int(name.split('-')[1])
            wanted = [str(command_root / 'binaries/replay'), str(command_root / expected[index]['path'])]
            budget = limits['input_timeout']
        elif name.startswith('lsan-replay-'):
            index = int(name.rsplit('-', 1)[1])
            wanted = [str(command_root / 'binaries/lsan_replay'),
                      str(command_root / expected[index]['path'])]
            budget = limits['input_timeout']
        else:
            wanted = [str(command_root / 'binaries/lsan_probe')]
            budget = limits['input_timeout']
        if argv != wanted or command['timeout_seconds'] != budget:
            raise SDKError(f'command differs from the run contract: {name}')
    stderr = (evidence / 'logs/fuzz.stderr').read_bytes()
    done = re.findall(rb'Done (\d+) runs in', stderr)
    if not done or int(done[-1]) < limits['runs'] or tree_files(evidence / 'crashes'):
        raise SDKError('fuzz completion is absent or crash artifacts remain')
    for key, value in manifest['sanitizer_options'].items():
        expected_value = value
        if (receipt['schema'] == 3 and key == 'ASAN_OPTIONS' and
                value == 'abort_on_error=1:detect_leaks=1'):
            expected_value = 'abort_on_error=1:detect_leaks=0'
        if receipt['environment'].get(key) != expected_value:
            raise SDKError('sanitizer environment differs')
    if receipt['schema'] in {2, 3}:
        checks = receipt['memory_checks']
        keys(checks, 'lsan memcheck')
        lsan = checks['lsan']
        if receipt['schema'] == 2:
            keys(lsan, 'status canary')
            if lsan != _verify_lsan_canary(evidence, root, manifest, limits['input_timeout']):
                raise SDKError('LeakSanitizer result differs from its positive control')
        else:
            keys(lsan, 'status canary replay environment')
            if manifest['sanitizer_options']['ASAN_OPTIONS'] == 'abort_on_error=1:detect_leaks=1':
                canary = _verify_lsan_canary(evidence, root, manifest, limits['input_timeout'])
                expected_lsan = {'status': 'passed', 'canary': canary['canary'],
                                 'replay': expected,
                                 'environment': {'ASAN_OPTIONS': 'abort_on_error=1:detect_leaks=1',
                                                 'UBSAN_OPTIONS': manifest['sanitizer_options']['UBSAN_OPTIONS']}}
                if lsan != expected_lsan:
                    raise SDKError('LeakSanitizer replay evidence differs')
            elif lsan != {'status': 'disabled', 'canary': None, 'replay': [], 'environment': None}:
                raise SDKError('disabled LeakSanitizer result differs')

        memcheck = checks['memcheck']
        keys(memcheck, 'requested status reason tool replay')
        mode = memcheck['requested']
        if mode not in {'off', 'optional', 'required'}:
            raise SDKError('invalid Memcheck request mode')
        if mode == 'off':
            if memcheck != {'requested': 'off', 'status': 'disabled', 'reason': None,
                            'tool': None, 'replay': []}:
                raise SDKError('disabled Memcheck fields differ')
        elif memcheck['status'] in {'unsupported', 'unavailable'}:
            if (mode != 'optional' or not isinstance(memcheck['reason'], str) or
                    not memcheck['reason'] or memcheck['tool'] is not None or
                    memcheck['replay']):
                raise SDKError('optional Memcheck refusal is malformed')
        elif memcheck['status'] == 'passed':
            tool = memcheck['tool']
            keys(tool, 'path sha256 version snapshot')
            text(tool['path'])
            text(tool['version'])
            artifact({'path': str(evidence / relative(tool['snapshot'])),
                      'sha256': tool['sha256']}, current=True)
            if tool['snapshot'] != 'tools/valgrind':
                raise SDKError('unexpected Valgrind tool snapshot path')
            version_command = read_json(evidence / 'logs/memcheck-version.json')
            keys(version_command, 'argv status timeout_seconds')
            if (version_command['argv'] != [tool['path'], '--version'] or
                    version_command['status'] != 0 or type(version_command['status']) is not int or
                    version_command['timeout_seconds'] != 30):
                raise SDKError('Valgrind version command differs')
            version_output = (evidence / 'logs/memcheck-version.stdout').read_text(
                encoding='utf-8', errors='replace').strip()
            if not version_output:
                version_output = (evidence / 'logs/memcheck-version.stderr').read_text(
                    encoding='utf-8', errors='replace').strip()
            if version_output != tool['version'] or tool['sha256'] != file_sha256(evidence / tool['snapshot']):
                raise SDKError('Valgrind tool identity differs from its snapshot')
            if memcheck['reason'] is not None:
                raise SDKError('successful Memcheck must not have a refusal reason')
            if memcheck['replay'] != expected:
                raise SDKError('Memcheck replay coverage is incomplete')
            for index, item in enumerate(expected):
                log_name = f'memcheck-{index}'
                xml_relative = f'logs/{log_name}.xml'
                command = read_json(evidence / 'logs' / f'{log_name}.json')
                keys(command, 'argv status timeout_seconds')
                wanted = _memcheck_argv(tool['path'], root, item['path'], root / xml_relative)
                if (command['argv'] != wanted or command['status'] != 0 or
                        type(command['status']) is not int or
                        command['timeout_seconds'] != limits['input_timeout']):
                    raise SDKError(f'Memcheck command differs: {log_name}')
                if _xml_error_count(evidence / xml_relative):
                    raise SDKError(f'Memcheck XML contains errors: {xml_relative}')
        else:
            raise SDKError('unsupported or incomplete Memcheck result')
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='operation', required=True)
    start = sub.add_parser('run')
    start.add_argument('--manifest', required=True)
    start.add_argument('--output', required=True)
    start.add_argument('--memcheck', choices=('off', 'optional', 'required'), default='off')
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
