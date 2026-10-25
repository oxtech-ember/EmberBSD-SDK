# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Run the current Ports-owned pkgsrc exporter under SDK process and lock control."""

import hashlib
import importlib.util
import json
import os
import platform
import re
import stat
import sys
import threading

from . import process, provenance
from .errors import SDKError, CommandError, ProcessOwnershipError
from .inventory import sha256_file, snapshot_tree, transfer_inventory_equal
from .jsonio import json_bytes, read_json, write_json
from .native_toolset import _absolute, _require, _sha, _source
from .paths import (canonical_existing, canonical_new, entry, reject_overlap,
                    stable_identity, verify_stable)

BOOTSTRAP_PKGSRC_EXPORT_API = 1
SOURCE_FORMAT = 'emberbsd-bootstrap-pkgsrc-export-source'
PLAN_FORMAT = 'emberbsd-bootstrap-pkgsrc-export-plan'
RESULT_FORMAT = 'emberbsd-bootstrap-pkgsrc-export-result'
ACCEPTANCE_FORMAT = 'emberbsd-bootstrap-pkgsrc-export-acceptance'
TRANSFER_ACCEPTANCE_FORMAT = 'emberbsd-bootstrap-pkgsrc-transfer-acceptance'
SCOPE = 'pkgsrc-source-export'
TRANSFER_SCOPE = 'pkgsrc-source-transfer'
TRANSFER_NORMALIZATION = 'symlink-mode-ignored-v1'
PORTS_REPOSITORY = 'https://github.com/oxtech-ember/EmberBSD-Ports'
PKGSRC_REPOSITORY = 'https://github.com/NetBSD/pkgsrc'
MANIFEST_LIMIT = 64 * 1024 * 1024
RUNTIME_LIMIT = 3600
_SDK_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
_EXPORTER = 'scripts/prepare-pkgsrc.py'
_CONFIG = 'pkgsrc/export.conf'
_SDK_MODULES = ('bootstrap_pilot.py', 'bootstrap_pkgsrc_export.py', 'elf.py', 'errors.py',
                'bootstrap_netbsd_gcc16.py', 'bootstrap_netbsd_recipe.py',
                'bootstrap_pkgsrc_provider.py', 'inventory.py', 'jsonio.py',
                'native_toolset.py', 'paths.py', 'process.py', 'provenance.py')
_FORBIDDEN_TOOLS = {'cc', 'c++', 'gcc', 'g++', 'clang', 'clang++', 'make', 'bmake', 'gmake'}


def _hash_ref(value, label):
    _require(isinstance(value, dict) and set(value) == {'path', 'sha256'},
             f'{label} needs an exact path and SHA256 reference')
    return dict(path=_absolute(value['path']), sha256=_sha(value['sha256']))


def _file_ref(path):
    path = _absolute(path)
    info = os.lstat(path)
    _require(stat.S_ISREG(info.st_mode), f'expected a regular source file: {path}')
    return dict(path=path, sha256=sha256_file(path))


def _git_env(git_path):
    return dict(PATH=os.path.dirname(git_path), LC_ALL='C', GIT_OPTIONAL_LOCKS='0',
                GIT_NO_LAZY_FETCH='1', GIT_TERMINAL_PROMPT='0')


def _git(git_path, root, *args):
    return process.capture([git_path, '-C', root, *args], env=_git_env(git_path)).stdout


def _require_repository_remote(root, git_path, expected):
    """Bind a source root to its one configured canonical upstream URL."""
    try:
        urls = _git(git_path, root, 'remote', 'get-url', '--all', 'origin').decode('utf-8').splitlines()
    except (CommandError, UnicodeError) as exc:
        raise SDKError('pkgsrc exporter source has no readable canonical Git origin') from exc
    normalized = [url[:-4] if url.endswith('.git') else url for url in urls]
    _require(len(normalized) == 1 and normalized[0] == expected,
             'pkgsrc exporter source Git origin does not match its approved repository')


def _python_record(value, *, inspect_inputs):
    _require(isinstance(value, dict) and set(value) in ({'path', 'sha256'},
             {'path', 'sha256', 'version'}), 'selected SDK Python needs an exact path and SHA256')
    path, digest = _absolute(value['path']), _sha(value['sha256'])
    _require(os.path.basename(path).startswith('python'),
             'selected SDK interpreter path must identify Python explicitly')
    if inspect_inputs:
        _require(os.path.realpath(path) == path and os.access(path, os.X_OK) and
                 sha256_file(path) == digest, 'selected SDK Python changed')
        query = process.capture([path, '-B', '-I', '-S', '-c',
                                 'import json,sys;print(json.dumps(list(sys.version_info[:3])))'],
                                env=dict(PATH=os.path.dirname(path), LC_ALL='C', TZ='UTC'))
        try:
            version = json.loads(query.stdout.decode('ascii'))
        except (UnicodeError, ValueError) as exc:
            raise SDKError('selected SDK Python returned an invalid version query') from exc
        _require(isinstance(version, list) and len(version) == 3 and
                 all(type(part) is int and part >= 0 for part in version) and
                 version >= [3, 15, 0], 'pkgsrc SDK exporter requires Python 3.15 or later')
        observed = dict(path=path, sha256=digest, version=version)
        _require('version' not in value or value['version'] == version,
                 'selected SDK Python version changed')
        return observed
    version = value.get('version')
    if version is not None:
        _require(isinstance(version, list) and len(version) == 3 and
                 all(type(part) is int and part >= 0 for part in version) and
                 version >= [3, 15, 0], 'pkgsrc SDK exporter requires Python 3.15 or later')
    return dict(path=path, sha256=digest, **({'version': version} if version is not None else {}))


def _manifest_rows(root, git_path):
    """Hash every tracked Ports path, while treating pkgsrc only as its gitlink."""
    root = canonical_existing(root)
    _require_repository_remote(root, git_path, PORTS_REPOSITORY)
    source = provenance.git_source(root, kind='ports', git=git_path, env=_git_env(git_path))
    _require(len(source['external_gitlinks']) == 1 and
             source['external_gitlinks'][0]['path'] == 'upstream/pkgsrc',
             'current Ports source must have only the pinned upstream/pkgsrc gitlink')
    submodule = canonical_existing(os.path.join(root, 'upstream/pkgsrc'))
    _require_repository_remote(submodule, git_path, PKGSRC_REPOSITORY)
    pkgsrc_source = provenance.git_source(submodule, kind='pkgsrc', git=git_path,
                                          env=_git_env(git_path))
    gitlink = source['external_gitlinks'][0]
    _require(gitlink['commit'] == pkgsrc_source['commit'],
             'Ports pkgsrc gitlink and checked-out upstream commit differ')
    raw = _git(git_path, root, 'ls-files', '--stage', '-z')
    entries = {}
    for item in raw.split(b'\0'):
        if not item:
            continue
        try:
            header, raw_name = item.split(b'\t', 1)
            mode, blob, stage = header.decode('ascii').split(' ')
            name = raw_name.decode('utf-8', 'strict')
        except (ValueError, UnicodeError) as exc:
            raise SDKError('unsupported Ports source index entry') from exc
        _require(stage == '0' and re.fullmatch('[0-9a-f]{40}', blob) is not None and
                 name not in entries, 'Ports source index is unmerged, duplicated, or malformed')
        entries[name] = (mode, blob)
    _require(entries.get('upstream/pkgsrc') == ('160000', gitlink['commit']),
             'Ports source index does not pin the checked-out pkgsrc commit')

    expected = {'upstream/pkgsrc'}
    for name in entries:
        expected.add(name)
        parent = name.rpartition('/')[0]
        while parent:
            expected.add(parent)
            parent = parent.rpartition('/')[0]
    rows = {}
    for name, (mode, blob) in entries.items():
        path = os.path.join(root, name)
        info = os.lstat(path)
        if mode == '160000':
            _require(name == 'upstream/pkgsrc' and stat.S_ISDIR(info.st_mode),
                     'unsupported Ports source gitlink path')
            rows[name] = dict(kind='gitlink', commit=blob)
            continue
        if mode in ('100644', '100755'):
            _require(stat.S_ISREG(info.st_mode), f'tracked Ports file changed type: {name}')
            digest = hashlib.sha1(b'blob ' + str(info.st_size).encode('ascii') + b'\0')
            size = 0
            with open(path, 'rb') as handle:
                before = os.fstat(handle.fileno())
                while block := handle.read(1024 * 1024):
                    digest.update(block)
                    size += len(block)
                after = os.fstat(handle.fileno())
            verify_stable(path, info)
            _require(stable_identity(before) == stable_identity(after) and
                     digest.hexdigest() == blob, f'tracked Ports file bytes differ from Git: {name}')
            rows[name] = dict(kind='file', mode=mode, size=size,
                              sha256=sha256_file(path), git_blob=blob)
            continue
        if mode == '120000':
            _require(stat.S_ISLNK(info.st_mode), f'tracked Ports link changed type: {name}')
            target = os.readlink(path)
            data = os.fsencode(target)
            blob_sha = hashlib.sha1(b'blob ' + str(len(data)).encode('ascii') + b'\0' + data).hexdigest()
            verify_stable(path, info)
            _require(blob_sha == blob, f'tracked Ports symlink differs from Git: {name}')
            rows[name] = dict(kind='link', target=target, git_blob=blob)
            continue
        raise SDKError(f'unsupported Ports source Git mode: {name}')

    root_info = os.lstat(root)
    actual = set()

    def walk(directory, prefix=''):
        before = os.lstat(directory)
        with os.scandir(directory) as scan:
            children = sorted(scan, key=lambda item: item.name)
        for child in children:
            relative = prefix + child.name
            if not prefix and child.name == '.git':
                continue
            if relative == 'upstream/pkgsrc':
                actual.add(relative)
                continue
            path = os.path.join(directory, child.name)
            info = os.lstat(path)
            actual.add(relative)
            if stat.S_ISDIR(info.st_mode):
                rows[relative] = dict(kind='directory')
                walk(path, relative + '/')
            elif stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                _require(relative in entries, f'untracked or ignored Ports input: {relative}')
            else:
                raise SDKError(f'special file in Ports source: {relative}')
            verify_stable(path, info)
        verify_stable(directory, before)

    walk(root)
    verify_stable(root, root_info)
    _require(actual == expected, 'Ports source has missing, extra, or untracked paths')
    return source, pkgsrc_source, dict(sorted(rows.items()))


def export_source_manifest(ports_root, git_path, output_path):
    """Write the full tracked Ports manifest outside its source checkout."""
    git_path = canonical_existing(git_path)
    ports_root = canonical_existing(ports_root)
    output_path = canonical_new(output_path)
    reject_overlap(output_path, ports_root)
    rows = _manifest_rows(ports_root, git_path)[2]
    payload = json_bytes(rows)
    _require(len(payload) <= MANIFEST_LIMIT, 'complete Ports source manifest exceeds 64 MiB')
    write_json(output_path, rows, exclusive=True)
    return _file_ref(output_path)


def describe_export_source(ports_root, manifest_ref, *, git_path):
    """Create data-only selectors for the exact current Python exporter."""
    manifest_ref = _hash_ref(manifest_ref, 'complete Ports source manifest')
    source, pkgsrc, rows = _manifest_rows(ports_root, canonical_existing(git_path))
    stored = read_json(manifest_ref['path'], max_bytes=MANIFEST_LIMIT)
    _require(sha256_file(manifest_ref['path']) == manifest_ref['sha256'] and stored == rows,
             'complete Ports source manifest is stale')
    exporter = _file_ref(os.path.join(source['path'], _EXPORTER))
    config = _file_ref(os.path.join(source['path'], _CONFIG))
    _require(rows.get(_EXPORTER, {}).get('sha256') == exporter['sha256'] and
             rows.get(_CONFIG, {}).get('sha256') == config['sha256'],
             'current exporter or export configuration is not source-bound')
    result = dict(format=SOURCE_FORMAT, version=1, scope=SCOPE,
                  ports_source=dict(repository=PORTS_REPOSITORY, commit=source['commit']),
                  pkgsrc_source=dict(repository=PKGSRC_REPOSITORY, commit=pkgsrc['commit']),
                  ports_root=source['path'], pkgsrc_root=pkgsrc['path'], ports_manifest=manifest_ref,
                  exporter=dict(relative=_EXPORTER, file=exporter),
                  export_config=dict(relative=_CONFIG, file=config))
    validate_export_source(result, git_path=git_path, inspect_inputs=True)
    return result


def validate_export_source(source, *, git_path, inspect_inputs=True):
    fields = {'format', 'version', 'scope', 'ports_source', 'pkgsrc_source', 'ports_root', 'pkgsrc_root',
              'ports_manifest', 'exporter', 'export_config'}
    _require(isinstance(source, dict) and set(source) == fields and
             source.get('format') == SOURCE_FORMAT and type(source.get('version')) is int and
             source['version'] == 1 and source.get('scope') == SCOPE,
             'unsupported current Ports export source descriptor')
    _source(source['ports_source'])
    _source(source['pkgsrc_source'])
    _require(source['ports_source']['repository'] == PORTS_REPOSITORY and
             source['pkgsrc_source']['repository'] == PKGSRC_REPOSITORY,
             'pkgsrc exporter source descriptor names an unapproved repository')
    ports_root, pkgsrc_root = _absolute(source['ports_root']), _absolute(source['pkgsrc_root'])
    manifest_ref = _hash_ref(source['ports_manifest'], 'complete Ports source manifest')
    for field, relative in (('exporter', _EXPORTER), ('export_config', _CONFIG)):
        row = source[field]
        _require(isinstance(row, dict) and set(row) == {'relative', 'file'} and
                 row['relative'] == relative, 'unsupported Ports source exporter path')
        _hash_ref(row['file'], 'Ports source file')
    if inspect_inputs:
        git_path = canonical_existing(git_path)
        observed, pkgsrc, rows = _manifest_rows(ports_root, git_path)
        _require(observed['path'] == ports_root and observed['commit'] == source['ports_source']['commit'] and
                 pkgsrc['path'] == pkgsrc_root and pkgsrc['commit'] == source['pkgsrc_source']['commit'],
                 'current Ports or pkgsrc source revision changed')
        manifest = read_json(manifest_ref['path'], max_bytes=MANIFEST_LIMIT)
        _require(sha256_file(manifest_ref['path']) == manifest_ref['sha256'] and manifest == rows,
                 'complete Ports source manifest changed')
        for field in ('exporter', 'export_config'):
            row = source[field]
            path = os.path.join(ports_root, row['relative'])
            _require(_file_ref(path) == row['file'] and
                     rows[row['relative']]['sha256'] == row['file']['sha256'],
                     'current exporter or configuration changed')
    return source


def _sdk_identity(git_path, *, require_clean):
    if require_clean:
        sdk = provenance.git_source(_SDK_ROOT, kind='sdk', git=git_path, env=_git_env(git_path))
    else:
        commit = _git(git_path, _SDK_ROOT, 'rev-parse', '--verify', 'HEAD^{commit}').decode('ascii').strip()
        _require(re.fullmatch('[0-9a-f]{40}', commit) is not None,
                 'SDK exporter source needs a full Git commit')
        sdk = dict(path=_SDK_ROOT, commit=commit, external_gitlinks=[])
    modules = {}
    for name in _SDK_MODULES:
        path = os.path.join(_SDK_ROOT, 'src/emberbsd_sdk', name)
        modules[name] = _file_ref(path)['sha256']
    worker = _file_ref(os.path.join(_SDK_ROOT, 'scripts/bootstrap-pkgsrc-export-worker.py'))
    cli = _file_ref(os.path.join(_SDK_ROOT, 'scripts/bootstrap-pkgsrc.py'))
    return dict(root=_SDK_ROOT, commit=sdk['commit'], modules=modules,
                worker=dict(relative='scripts/bootstrap-pkgsrc-export-worker.py', file=worker),
                cli=dict(relative='scripts/bootstrap-pkgsrc.py', file=cli))


def _tools(value, *, inspect_inputs):
    _require(isinstance(value, dict) and set(value) == {'git', 'patch'},
             'pkgsrc source export requires exactly selected Git and patch utilities')
    for role, row in value.items():
        _require(isinstance(row, dict) and set(row) == {'path', 'sha256'},
                 'source export utility must have an exact path and hash')
        path = _absolute(row['path'])
        _sha(row['sha256'])
        names = ('patch', 'gpatch', 'nbpatch') if role == 'patch' else ('git',)
        _require(os.path.basename(path) in names and role not in _FORBIDDEN_TOOLS,
                 'source export utility path cannot substitute for a compiler or make')
        if inspect_inputs:
            info = os.lstat(path)
            _require(stat.S_ISREG(info.st_mode) and os.access(path, os.X_OK) and
                     sha256_file(path) == row['sha256'],
                     f'selected source export utility changed: {role}')
    return value


def _protected(source_ref, source, plan=None):
    paths = [source['ports_root'], source['pkgsrc_root'],
             source['ports_manifest']['path'], source['exporter']['file']['path'],
             source['export_config']['file']['path'], source_ref['path'], _SDK_ROOT]
    if plan is not None:
        paths.extend([plan['python']['path'],
                      plan['plan_path'],
                      *(row['path'] for row in plan['tools'].values()),
                      plan['sdk_source']['root'], plan['sdk_source']['worker']['file']['path'],
                      plan['sdk_source']['cli']['file']['path']])
    return paths


def _guard_lock_root(lock_root, protected):
    lock_root = _absolute(lock_root)
    for path in protected:
        try:
            reject_overlap(lock_root, _absolute(path))
        except SDKError as exc:
            raise SDKError('source export lock root overlaps a protected path') from exc
    return lock_root


def create_export_plan(source_ref, work_root, lock_root, python, tools, *, plan_path,
                       runtime_seconds=900):
    source_ref = _hash_ref(source_ref, 'source descriptor')
    _require(sha256_file(source_ref['path']) == source_ref['sha256'],
             'pkgsrc export source selector changed')
    work = _absolute(work_root)
    plan_path = _absolute(plan_path)
    python = _python_record(python, inspect_inputs=True)
    _tools(tools, inspect_inputs=False)
    plan = dict(format=PLAN_FORMAT, version=1, scope=SCOPE, purpose='export-current-pkgsrc',
                source_descriptor=source_ref, work_root=work,
                export_root=os.path.join(work, 'export'), scratch_root=os.path.join(work, 'scratch'),
                result_path=os.path.join(work, 'result.json'),
                inventory_path=os.path.join(work, 'export-inventory.json'),
                controller_log=os.path.join(work, 'controller.log'), plan_path=plan_path, python=python,
                tools=tools, host=dict(os=platform.system(), cpu=platform.machine()),
                resources=dict(lock_root=_absolute(lock_root), runtime_seconds=runtime_seconds),
                sdk_source=None, commands=[])
    sdk_identity = _sdk_identity(tools['git']['path'], require_clean=False)
    plan['sdk_source'] = sdk_identity
    plan['commands'] = [dict(id='export-current-pkgsrc', argv=[python['path'], '-B', '-I', '-S',
        os.path.join(_SDK_ROOT, 'scripts/bootstrap-pkgsrc-export-worker.py'),
        source_ref['path'], source_ref['sha256'], plan_path], cwd=work,
        env=dict(PATH=os.pathsep.join(sorted({os.path.dirname(python['path']),
                                              *(os.path.dirname(row['path']) for row in tools.values())})),
                 LC_ALL='C', TZ='UTC', TMPDIR=work, PATCH_VERSION_CONTROL='none',
                 PYTHONDONTWRITEBYTECODE='1'),
        expected_exit=0)]
    return validate_export_plan(plan, read_json(source_ref['path']), source_ref=source_ref,
                                inspect_inputs=False)


def validate_export_plan(plan, source, *, source_ref=None, inspect_inputs=True,
                         allow_claimed_work=False):
    fields = {'format', 'version', 'scope', 'purpose', 'source_descriptor', 'work_root',
              'export_root', 'scratch_root', 'result_path', 'inventory_path', 'controller_log',
              'plan_path', 'python', 'tools', 'host', 'resources', 'sdk_source', 'commands'}
    _require(isinstance(plan, dict) and set(plan) == fields and
             plan.get('format') == PLAN_FORMAT and type(plan.get('version')) is int and
             plan['version'] == 1 and plan.get('scope') == SCOPE and
             plan.get('purpose') == 'export-current-pkgsrc',
             'unsupported current pkgsrc export plan')
    ref = _hash_ref(plan['source_descriptor'], 'source descriptor')
    if source_ref is not None:
        _require(ref == _hash_ref(source_ref, 'source descriptor'),
                 'pkgsrc export plan source descriptor reference changed')
    work = _absolute(plan['work_root'])
    plan_path = _absolute(plan['plan_path'])
    _require(plan['export_root'] == os.path.join(work, 'export') and
             plan['scratch_root'] == os.path.join(work, 'scratch') and
             plan['result_path'] == os.path.join(work, 'result.json') and
             plan['inventory_path'] == os.path.join(work, 'export-inventory.json') and
             plan['controller_log'] == os.path.join(work, 'controller.log'),
             'pkgsrc export outputs differ from the fixed work layout')
    _require(isinstance(plan['host'], dict) and set(plan['host']) == {'os', 'cpu'} and
             all(isinstance(plan['host'][key], str) and plan['host'][key]
                 for key in ('os', 'cpu')), 'pkgsrc export host identity is malformed')
    python = _python_record(plan['python'], inspect_inputs=inspect_inputs)
    _tools(plan['tools'], inspect_inputs=inspect_inputs)
    resources = plan['resources']
    _require(isinstance(resources, dict) and set(resources) == {'lock_root', 'runtime_seconds'} and
             type(resources['runtime_seconds']) is int and 0 < resources['runtime_seconds'] <= RUNTIME_LIMIT,
             'pkgsrc export resource bounds are invalid')
    protected = _protected(ref, source, plan)
    for path in protected:
        reject_overlap(work, path)
    _guard_lock_root(resources['lock_root'], [work, *protected])
    _require(not (_FORBIDDEN_TOOLS & set(os.path.basename(row['path']) for row in plan['tools'].values())),
             'system compiler or make may not substitute in the source exporter')
    expected_command = [python['path'], '-B', '-I', '-S',
        os.path.join(_SDK_ROOT, 'scripts/bootstrap-pkgsrc-export-worker.py'),
        ref['path'], ref['sha256'], plan_path]
    _require(plan['commands'] == [dict(id='export-current-pkgsrc', argv=expected_command,
        cwd=work, env=dict(PATH=os.pathsep.join(sorted({os.path.dirname(python['path']),
             *(os.path.dirname(row['path']) for row in plan['tools'].values())})),
             LC_ALL='C', TZ='UTC', TMPDIR=work, PATCH_VERSION_CONTROL='none',
             PYTHONDONTWRITEBYTECODE='1'), expected_exit=0)],
        'pkgsrc export command differs from its fixed controller route')
    if inspect_inputs:
        if allow_claimed_work:
            _require(canonical_existing(work) == work and os.path.isdir(work),
                     'claimed pkgsrc export work must remain a real directory')
        else:
            _require(canonical_new(work) == work and entry(work) is None,
                     'pkgsrc export work directory must be fresh with real ancestors')
        identity = _sdk_identity(plan['tools']['git']['path'], require_clean=True)
        _require(identity == plan['sdk_source'] and
                 plan['host'] == dict(os=platform.system(), cpu=platform.machine()),
                 'SDK exporter implementation or host changed')
        validate_export_source(source, git_path=plan['tools']['git']['path'], inspect_inputs=True)
    return plan


def _source_proof(source, inventory_ref):
    return dict(format='emberbsd-ports-export-source-proof', version=1,
                ports_source=source['ports_source'], pkgsrc_source=source['pkgsrc_source'],
                ports_manifest=source['ports_manifest'], exporter=source['exporter'],
                export_config=source['export_config'], inventory=inventory_ref)


def _write_result(path, value):
    write_json(path, value, exclusive=entry(path) is None)


def worker_main(source_path, source_sha256, plan_path):
    """Worker entry; the outer controller owns this entire process session."""
    try:
        plan_path = canonical_existing(plan_path)
        plan = read_json(plan_path)
        source_ref = _hash_ref(dict(path=source_path, sha256=source_sha256), 'source descriptor')
        source = read_json(source_ref['path'])
        validate_export_source(source, git_path=plan['tools']['git']['path'], inspect_inputs=True)
        validate_export_plan(plan, source, source_ref=source_ref, inspect_inputs=False)
        _require(_tools(plan['tools'], inspect_inputs=True) is plan['tools'] and
                 _sdk_identity(plan['tools']['git']['path'], require_clean=True) == plan['sdk_source'],
                 'SDK exporter or selected utility changed before child dispatch')
        _require(_python_record(plan['python'], inspect_inputs=True) == plan['python'],
                 'selected SDK Python changed before child dispatch')
        _require(sha256_file(source_ref['path']) == source_ref['sha256'] and
                 plan['plan_path'] == plan_path and os.path.realpath(sys.executable) == plan['python']['path'],
                 'pkgsrc export worker inputs differ from the controller plan')
        result_path = plan['result_path']
        result = read_json(result_path)
        _require(result.get('status') == 'RUNNING' and result.get('source_descriptor') == source_ref and
                 result.get('export_plan') == dict(path=plan_path, sha256=sha256_file(plan_path)),
                 'pkgsrc export work record differs from its locked selectors')

        class Selection:
            def __init__(self):
                self.routes = {role: (row['path'],) for role, row in plan['tools'].items()}

            def command(self, role):
                _require(role in self.routes, 'Ports exporter requested an unselected utility: ' + str(role))
                return self.routes[role]

        environment = dict(PATH=plan['commands'][0]['env']['PATH'], LC_ALL='C', TZ='UTC',
                           TMPDIR=plan['work_root'], PATCH_VERSION_CONTROL='none',
                           GIT_OPTIONAL_LOCKS='0',
                           GIT_NO_LAZY_FETCH='1', GIT_TERMINAL_PROMPT='0',
                           PYTHONDONTWRITEBYTECODE='1')
        routes = {row['path']: role for role, row in plan['tools'].items()}
        command_number = 0

        def runner(argv, cwd=None, input=None):
            nonlocal command_number
            _require(isinstance(argv, (list, tuple)) and argv and argv[0] in routes,
                     'Ports exporter attempted an unselected executable')
            role = routes[argv[0]]
            stdout_path = os.path.join(plan['work_root'], 'logs', f'command-{command_number:04d}.stdout')
            stderr_path = os.path.join(plan['work_root'], 'logs', f'command-{command_number:04d}.stderr')
            row = dict(id=f'command-{command_number:04d}', role=role, argv=list(argv), cwd=cwd,
                       input=None if input is None else dict(size=len(input), sha256=hashlib.sha256(input).hexdigest()))
            command_number += 1
            try:
                returned = process.capture(list(argv), cwd=cwd, env=environment, input=input)
            except CommandError as exc:
                stdout, stderr, code = exc.stdout, exc.stderr, exc.returncode
                _store_command_output(stdout_path, stdout)
                _store_command_output(stderr_path, stderr)
                row.update(actual_exit=code, stdout=dict(path=stdout_path, sha256=sha256_file(stdout_path)),
                           stderr=dict(path=stderr_path, sha256=sha256_file(stderr_path)))
                result['commands'].append(row)
                _write_result(result_path, result)
                raise
            _store_command_output(stdout_path, returned.stdout)
            _store_command_output(stderr_path, returned.stderr)
            row.update(actual_exit=0, stdout=dict(path=stdout_path, sha256=sha256_file(stdout_path)),
                       stderr=dict(path=stderr_path, sha256=sha256_file(stderr_path)))
            result['commands'].append(row)
            _write_result(result_path, result)
            return returned.stdout

        exporter_path = os.path.join(source['ports_root'], _EXPORTER)
        spec = importlib.util.spec_from_file_location('emberbsd_current_ports_pkgsrc_exporter', exporter_path)
        _require(spec is not None and spec.loader is not None,
                 'cannot load the current Ports pkgsrc exporter')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        export = getattr(module, 'export_pkgsrc', None)
        _require(callable(export), 'current Ports exporter has no callable export_pkgsrc API')
        revision = export(source['ports_root'], plan['export_root'],
                          selection=Selection(), runner=runner, scratch=plan['scratch_root'])
        _require(isinstance(revision, str) and revision == source['pkgsrc_source']['commit'],
                 'Ports exporter returned a different upstream pkgsrc revision')
        inventory = snapshot_tree(plan['export_root'])
        inventory_ref = dict(path=plan['inventory_path'], sha256='')
        write_json(inventory_ref['path'], inventory, exclusive=True)
        inventory_ref['sha256'] = sha256_file(inventory_ref['path'])
        result.update(status='EXPORT_COMPLETED_PENDING_INDEPENDENT_REVIEW',
                      exported_pkgsrc_commit=revision, output_inventory=inventory_ref,
                      source_proof=_source_proof(source, inventory_ref),
                      all_commands_drained=False, inputs_revalidated_after_drain=False)
        _write_result(result_path, result)
        return 0
    except BaseException as exc:
        try:
            plan = read_json(plan_path)
            result_path = plan['result_path']
            result = read_json(result_path)
            result.update(status='FAILED', error=str(exc), all_commands_drained=False,
                          inputs_revalidated_after_drain=False)
            _write_result(result_path, result)
        except Exception:
            pass
        print('pkgsrc export worker failed: ' + str(exc), file=sys.stderr, flush=True)
        return 1


def _store_command_output(path, data):
    _require(isinstance(data, bytes) and len(data) <= MANIFEST_LIMIT,
             'source exporter command output exceeds 64 MiB')
    with open(path, 'xb') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _cleanup_export_scratch(scratch_root, work_root):
    """Remove only the exporter-owned archive after the caller proves drain."""
    scratch_root, work_root = _absolute(scratch_root), _absolute(work_root)
    _require(scratch_root == os.path.join(work_root, 'scratch') and
             canonical_existing(scratch_root) == scratch_root,
             'pkgsrc exporter scratch is not the fixed child of its producer work root')
    rows = snapshot_tree(scratch_root)
    _require(set(rows) == {'pkgsrc.tar'} and rows['pkgsrc.tar']['kind'] == 'file',
             'pkgsrc exporter scratch contains unexpected data; preserve it for inspection')
    os.unlink(os.path.join(scratch_root, 'pkgsrc.tar'))
    os.rmdir(scratch_root)
    _require(entry(scratch_root) is None,
             'pkgsrc exporter scratch cleanup did not remove the exact archive')


def run_export(source_ref, plan_ref):
    """Execute current Ports export under shared source and exclusive work leases."""
    _require(threading.current_thread() is threading.main_thread(),
             'pkgsrc source export must run on the main thread')
    source_ref, plan_ref = _hash_ref(source_ref, 'source descriptor'), _hash_ref(plan_ref, 'export plan')
    source = read_json(source_ref['path'])
    plan = read_json(plan_ref['path'])
    _require(sha256_file(source_ref['path']) == source_ref['sha256'] and
             sha256_file(plan_ref['path']) == plan_ref['sha256'],
             'pkgsrc export selector changed')
    validate_export_source(source, git_path=plan['tools']['git']['path'], inspect_inputs=True)
    validate_export_plan(plan, source, source_ref=source_ref, inspect_inputs=True)
    _require(plan['plan_path'] == plan_ref['path'],
             'selected plan file path differs from its source-bound plan')
    work = plan['work_root']
    lock_root = canonical_existing(plan['resources']['lock_root'])
    _require(entry(work) is None and canonical_new(work) == work,
             'pkgsrc export requires a fresh work directory')
    protected = _protected(source_ref, source, plan)
    for path in protected:
        reject_overlap(work, path)
    _guard_lock_root(lock_root, [work, *protected])
    shared = set(protected)
    shared.discard(work)
    with process.ContextLocks(sorted(shared), lock_root, shared=True), process.ContextLocks([work], lock_root):
        validate_export_source(read_json(source_ref['path']),
                               git_path=plan['tools']['git']['path'], inspect_inputs=True)
        validate_export_plan(read_json(plan_ref['path']), source, source_ref=source_ref,
                             inspect_inputs=True)
        _require(entry(work) is None, 'pkgsrc export work was claimed before dispatch')
        os.mkdir(work, 0o700)
        os.mkdir(os.path.join(work, 'logs'), 0o700)
        result = dict(format=RESULT_FORMAT, version=1, scope=SCOPE, status='RUNNING',
                      source_descriptor=source_ref, export_plan=plan_ref, commands=[],
                      export_root=plan['export_root'], output_inventory=None, source_proof=None,
                      all_commands_drained=False, inputs_revalidated_after_drain=False,
                      controller_command=None,
                      acceptance='PENDING_INDEPENDENT_REVIEW', scope_boundary=
                      'current source-bound Ports pkgsrc export only; no package build or NativeToolset qualification')
        _write_result(plan['result_path'], result)
        try:
            process.run_logged(plan['commands'][0]['argv'], plan['controller_log'],
                               cwd=plan['commands'][0]['cwd'], env=plan['commands'][0]['env'],
                               timeout_seconds=plan['resources']['runtime_seconds'])
        except ProcessOwnershipError as exc:
            result.update(status='HOLD_UNPROVEN_DRAIN', error=str(exc), owned_sid=exc.sid)
            _write_result(plan['result_path'], result)
            from .bootstrap_pilot import _hold
            _hold()
        except Exception as exc:
            if entry(plan['result_path']) is not None:
                try:
                    result = read_json(plan['result_path'])
                except SDKError:
                    pass
            result.update(status='FAILED', error=str(exc), all_commands_drained=True)
        else:
            try:
                result = read_json(plan['result_path'])
                _require(result.get('status') == 'EXPORT_COMPLETED_PENDING_INDEPENDENT_REVIEW',
                         'pkgsrc export worker did not complete its fixed source route')
                result['all_commands_drained'] = True
                validate_export_source(read_json(source_ref['path']),
                                       git_path=plan['tools']['git']['path'], inspect_inputs=True)
                validate_export_plan(read_json(plan_ref['path']), source, source_ref=source_ref,
                                     inspect_inputs=True, allow_claimed_work=True)
                observed = snapshot_tree(plan['export_root'])
                inventory = result['output_inventory']
                _require(inventory == dict(path=plan['inventory_path'],
                                           sha256=sha256_file(plan['inventory_path'])) and
                         observed == read_json(plan['inventory_path'], max_bytes=MANIFEST_LIMIT),
                         'pkgsrc export inventory changed before independent review')
                _cleanup_export_scratch(plan['scratch_root'], work)
                result.update(status='EXPORT_COMPLETED_PENDING_INDEPENDENT_REVIEW',
                              output_inventory=inventory, source_proof=_source_proof(source, inventory),
                              inputs_revalidated_after_drain=True,
                              controller_log=_file_ref(plan['controller_log']),
                              controller_command=dict(plan['commands'][0], actual_exit=0,
                                                      log=_file_ref(plan['controller_log'])))
            except Exception as exc:
                result.update(status='FAILED', error=str(exc), inputs_revalidated_after_drain=False)
        if result['status'] != 'HOLD_UNPROVEN_DRAIN' and entry(plan['controller_log']) is not None:
            result['controller_log'] = _file_ref(plan['controller_log'])
        _write_result(plan['result_path'], result)
    result['all_leases_closed'] = True
    return result


def validate_export_acceptance(receipt, receipt_sha256, *, result_ref, plan_ref, source_ref):
    """Validate a gate-issued receipt; this module never creates one."""
    receipt_ref = _hash_ref(dict(path=receipt, sha256=receipt_sha256), 'independent export acceptance')
    _require(sha256_file(receipt_ref['path']) == receipt_ref['sha256'],
             'independent export acceptance receipt changed')
    value = read_json(receipt_ref['path'])
    fields = {'format', 'version', 'scope', 'status', 'evidence_level', 'producer_result',
              'export_plan', 'source_descriptor', 'output_root', 'output_inventory',
              'source_proof', 'commands', 'controller_command', 'all_commands_drained',
              'inputs_revalidated_after_drain', 'scope_boundary'}
    _require(isinstance(value, dict) and set(value) == fields and
             value.get('format') == ACCEPTANCE_FORMAT and type(value.get('version')) is int and
             value['version'] == 1 and value.get('scope') == SCOPE and value.get('status') == 'PASS' and
             value.get('evidence_level') == 'independent-source-export-review',
             'independent pkgsrc export acceptance receipt is missing or invalid')
    result_ref = _hash_ref(result_ref, 'export result')
    plan_ref = _hash_ref(plan_ref, 'export plan')
    source_ref = _hash_ref(source_ref, 'source descriptor')
    _require(_hash_ref(value['producer_result'], 'export result') == result_ref and
             _hash_ref(value['export_plan'], 'export plan') == plan_ref and
             _hash_ref(value['source_descriptor'], 'source descriptor') == source_ref and
             sha256_file(result_ref['path']) == result_ref['sha256'] and
             sha256_file(plan_ref['path']) == plan_ref['sha256'] and
             sha256_file(source_ref['path']) == source_ref['sha256'],
             'independent pkgsrc export receipt does not bind these exact inputs')
    result_data = read_json(result_ref['path'])
    plan_data = read_json(plan_ref['path'])
    source_data = read_json(source_ref['path'])
    _require(result_data.get('format') == RESULT_FORMAT and result_data.get('scope') == SCOPE and
             result_data.get('status') == 'EXPORT_COMPLETED_PENDING_INDEPENDENT_REVIEW' and
             result_data.get('acceptance') == 'PENDING_INDEPENDENT_REVIEW' and
             result_data.get('source_descriptor') == source_ref and result_data.get('export_plan') == plan_ref and
             result_data.get('all_commands_drained') is True and
             result_data.get('inputs_revalidated_after_drain') is True,
             'export result is not a drained, source-bound pending result')
    validate_export_source(source_data, git_path=plan_data['tools']['git']['path'], inspect_inputs=True)
    validate_export_plan(plan_data, source_data, source_ref=source_ref,
                         inspect_inputs=True, allow_claimed_work=True)
    output = result_data['export_root']
    inventory_ref = _hash_ref(result_data['output_inventory'], 'export output inventory')
    _require(output == plan_data['export_root'] and inventory_ref['path'] == plan_data['inventory_path'] and
             snapshot_tree(output) == read_json(inventory_ref['path'], max_bytes=MANIFEST_LIMIT) and
             sha256_file(inventory_ref['path']) == inventory_ref['sha256'] and
             result_data['source_proof'] == _source_proof(source_data, inventory_ref),
             'source export output or complete inventory changed')
    _require(value['all_commands_drained'] is True and value['inputs_revalidated_after_drain'] is True and
             value['output_root'] == result_data['export_root'] and
             value['output_inventory'] == result_data['output_inventory'] and
             value['source_proof'] == result_data['source_proof'] and
             value['commands'] == result_data['commands'] and
             value['controller_command'] == result_data['controller_command'] and
             value['scope_boundary'] ==
             'independently reviewed current Ports source export only; no pkgsrc build or NativeToolset qualification',
             'independent pkgsrc export receipt differs from the pending export evidence')
    return receipt_ref, value


def validate_export_transfer_acceptance(receipt, receipt_sha256, *, export_acceptance_ref,
                                        inspect_inputs=True):
    """Validate a gate-issued source transfer after target-side full revalidation.

    The original source-export receipt names the exporter host's path, which is
    not necessarily mounted on the target. This receipt carries the exact
    accepted inventory bytes to the target and independently binds its current
    tree snapshot. It does not accept a package build or qualify a toolset.
    """
    receipt_ref = _hash_ref(dict(path=receipt, sha256=receipt_sha256),
                            'independent target source-transfer acceptance')
    accepted_ref = _hash_ref(export_acceptance_ref, 'independent source-export acceptance')
    _require(sha256_file(receipt_ref['path']) == receipt_ref['sha256'] and
             sha256_file(accepted_ref['path']) == accepted_ref['sha256'],
             'source transfer acceptance or source export acceptance changed')
    value = read_json(receipt_ref['path'])
    fields = {'format', 'version', 'scope', 'status', 'evidence_level',
              'source_export_acceptance', 'source_inventory', 'output_root',
              'output_inventory', 'transfer_report', 'source_proof',
              'normalization_policy',
              'inputs_revalidated_after_transfer', 'scope_boundary'}
    _require(isinstance(value, dict) and set(value) == fields and
             value.get('format') == TRANSFER_ACCEPTANCE_FORMAT and
             type(value.get('version')) is int and value['version'] == 1 and
             value.get('scope') == TRANSFER_SCOPE and value.get('status') == 'PASS' and
             value.get('evidence_level') == 'independent-target-source-revalidation',
             'independent pkgsrc transfer acceptance receipt is missing or invalid')
    _require(_hash_ref(value['source_export_acceptance'], 'source-export acceptance') == accepted_ref,
             'target transfer receipt does not bind the selected source-export acceptance')
    accepted = read_json(accepted_ref['path'])
    accepted_fields = {'format', 'version', 'scope', 'status', 'evidence_level',
        'producer_result', 'export_plan', 'source_descriptor', 'output_root',
        'output_inventory', 'source_proof', 'commands', 'controller_command',
        'all_commands_drained', 'inputs_revalidated_after_drain', 'scope_boundary'}
    _require(isinstance(accepted, dict) and set(accepted) == accepted_fields and
             accepted.get('format') == ACCEPTANCE_FORMAT and type(accepted.get('version')) is int and
             accepted['version'] == 1 and accepted.get('scope') == SCOPE and
             accepted.get('status') == 'PASS' and
             accepted.get('evidence_level') == 'independent-source-export-review' and
             accepted.get('all_commands_drained') is True and
             accepted.get('inputs_revalidated_after_drain') is True and
             accepted.get('scope_boundary') ==
             'independently reviewed current Ports source export only; no pkgsrc build or NativeToolset qualification',
             'source transfer does not descend from a passing independent source-export receipt')
    for field in ('producer_result', 'export_plan', 'source_descriptor'):
        _hash_ref(accepted[field], f'accepted source export {field}')
    source_inventory_ref = _hash_ref(value['source_inventory'], 'transferred source inventory')
    accepted_inventory = _hash_ref(accepted['output_inventory'], 'accepted source inventory')
    _require(source_inventory_ref['sha256'] == accepted_inventory['sha256'] and
             (not inspect_inputs or sha256_file(source_inventory_ref['path']) == source_inventory_ref['sha256']),
             'transferred inventory bytes differ from the independently accepted export inventory')
    output_inventory_ref = _hash_ref(value['output_inventory'], 'target source inventory')
    _require((not inspect_inputs or sha256_file(output_inventory_ref['path']) == output_inventory_ref['sha256']) and
             value['normalization_policy'] == TRANSFER_NORMALIZATION,
             'target inventory or transfer normalization policy changed')
    output_inventory = read_json(output_inventory_ref['path'], max_bytes=MANIFEST_LIMIT)
    if inspect_inputs:
        source_inventory = read_json(source_inventory_ref['path'], max_bytes=MANIFEST_LIMIT)
    output_root = _absolute(value['output_root'])
    if inspect_inputs:
        _require(canonical_existing(output_root) == output_root and
                 snapshot_tree(output_root) == output_inventory and
                 transfer_inventory_equal(source_inventory, output_inventory),
                 'target pkgsrc tree differs from the complete accepted transfer inventory')
    report_ref = _hash_ref(value['transfer_report'], 'source transfer report')
    _require(sha256_file(report_ref['path']) == report_ref['sha256'],
             'source transfer report changed')
    _require(value['source_proof'] == accepted['source_proof'] and
             value['inputs_revalidated_after_transfer'] is True and
             value['scope_boundary'] ==
             'independently revalidated current pkgsrc source tree at target; no package build or NativeToolset qualification',
             'target source transfer receipt differs from the accepted export provenance')
    return receipt_ref, value
