# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Produce a narrowly scoped pkgsrc bootstrap prefix from accepted inputs."""

import os
import platform
import stat
import threading
import time

from .bootstrap_bmake_seed import (
    _compiler_seed_references, _guard_lock_root, validate_compiler_seed,
)
from .bootstrap_pkgsrc_export import _hash_ref, validate_export_transfer_acceptance
from .errors import SDKError, ProcessOwnershipError
from .inventory import sha256_file, snapshot_tree
from .jsonio import read_json, write_json
from .native_toolset import _absolute, _require, _sha
from .paths import (
    canonical_existing, canonical_new, entry, inside, real_directory, reject_overlap,
    verify_stable,
)
from .process import ContextLocks, run_logged

BOOTSTRAP_PKGSRC_PROVIDER_API = 1
PLAN_FORMAT = 'emberbsd-bootstrap-pkgsrc-provider-plan'
RESULT_FORMAT = 'emberbsd-bootstrap-pkgsrc-provider-result'
SCOPE = 'pkgsrc-bootstrap-tools'
SCOPE_BOUNDARY = ('source-bound pkgsrc bootstrap tools only; NetBSD lorder uses transient host /tmp files; '
                  'prefix-local absolute output links are rewritten relative; external links are rejected; '
                  'no NativeToolset qualification or laptop installation')
_MAX_RUNTIME = 6 * 60 * 60
_HOST_TOOLS = (
    'ar', 'as', 'awk', 'basename', 'cat', 'chgrp', 'chmod', 'chown', 'cmp', 'cp', 'cut',
    'date', 'diff', 'dirname', 'echo', 'egrep', 'env', 'expr', 'fgrep', 'find',
    'file', 'ftp', 'grep', 'groups', 'gzip', 'head', 'hostname', 'id', 'install', 'join',
    'ld', 'lorder', 'ln', 'ls', 'mkdir', 'mktemp', 'mv', 'nm', 'patch', 'printf', 'pwd',
    'ranlib', 'rm', 'rmdir', 'sed', 'sh', 'sleep', 'sort', 'strip', 'tail', 'tar',
    'touch', 'tr', 'tsort', 'uname', 'uniq', 'wc',
    'whoami', 'xargs',
)
# NetBSD maps mktemp and tsort as platform tools. Keep ar, groups, join,
# lorder, nm, ranlib, uname, and whoami as private PATH aliases only; as and
# ld are compiler helpers.
_PKGSRC_TOOL_ROUTE_ROLES = tuple(
    role for role in _HOST_TOOLS
    if role not in ('ar', 'as', 'groups', 'join', 'ld', 'lorder', 'nm', 'ranlib',
                    'uname', 'whoami')
)
_FORBIDDEN_TOOLS = {
    'cc', 'c++', 'gcc', 'g++', 'clang', 'clang++', 'make', 'bmake', 'gmake',
}
_PKGSRC_BOOTSTRAP_TOOL_PATHS = {
    'awk': 'bin/nawk',
    'install': 'bin/install-sh',
    'sed': 'bin/sed',
    'sh': 'bin/mksh',
}
_PKGSRC_TOOL_ROUTE_MARKER = '# EmberBSD SDK private pkgsrc tool routes v1'
_REQUIRED_SOURCE_FILES = (
    'bootstrap/bootstrap',
    'devel/bmake/Makefile',
    'devel/gmake/Makefile',
    'pkgtools/bootstrap-mk-files/Makefile',
    'pkgtools/pkg_install/Makefile',
    'shells/mksh/Makefile',
)


def _file_hash_ref(value, label):
    ref = _hash_ref(value, label)
    _require(sha256_file(ref['path']) == ref['sha256'], f'{label} changed')
    return ref


def _host_tool_map(value, *, inspect_inputs):
    _require(isinstance(value, dict) and set(value) == set(_HOST_TOOLS),
             'pkgsrc bootstrap requires its complete fixed host utility roster')
    result = {}
    for role in _HOST_TOOLS:
        row = value[role]
        _require(isinstance(row, dict) and set(row) == {'path', 'sha256'},
                 f'pkgsrc bootstrap host utility needs an exact path and hash: {role}')
        path, digest = _absolute(row['path']), _sha(row['sha256'])
        allowed_paths = ('/bin/sh',) if role == 'sh' else (
            ('/bin/' + role, '/usr/bin/' + role, '/sbin/chown')
            if role in ('chgrp', 'chown') else
            ('/bin/' + role, '/usr/bin/' + role))
        _require(path in allowed_paths and
                 role not in _FORBIDDEN_TOOLS,
                 f'pkgsrc bootstrap PATH contains an unsupported host utility: {role}')
        if inspect_inputs:
            info = os.stat(path)
            resolved = os.path.realpath(path)
            _require((resolved.startswith('/bin/') or resolved.startswith('/usr/bin/') or
                      (path == '/sbin/chown' and resolved == '/sbin/chown')) and
                     stat.S_ISREG(info.st_mode) and os.access(path, os.X_OK) and
                     sha256_file(path) == digest,
                     f'pkgsrc bootstrap host utility changed or is not executable: {role}')
        result[role] = dict(path=path, sha256=digest)
    return result


def _pkgsrc_tool_routes(work, utility_bin, bootstrap_work):
    routes = {
        role: os.path.join(bootstrap_work, _PKGSRC_BOOTSTRAP_TOOL_PATHS[role])
        if role in _PKGSRC_BOOTSTRAP_TOOL_PATHS else
        os.path.join(utility_bin, role)
        for role in _PKGSRC_TOOL_ROUTE_ROLES
    }
    # NetBSD's pkgsrc map names the archive extractor "bsdtar" while this
    # provider pins the same executable under its stable "tar" role.
    routes['bsdtar'] = os.path.join(utility_bin, 'tar')
    # Preserve the command options from tools.NetBSD.mk when replacing its
    # absolute host paths with private aliases.
    routes['gzip'] += ' -nf ${GZIP}'
    routes['mkdir'] += ' -p'
    return {role: routes[role] for role in sorted(routes)}


def _append_pkgsrc_tool_routes(mkconf, routes):
    """Pin pkgsrc's selected absolute tool mappings inside its settings block."""
    mkconf = _absolute(mkconf)
    info = os.lstat(mkconf)
    _require(stat.S_ISREG(info.st_mode), 'pkgsrc bootstrap mk.conf must be a regular file')
    _require(isinstance(routes, dict) and routes and
             set(routes) == set(_PKGSRC_TOOL_ROUTE_ROLES) | {'bsdtar'},
             'pkgsrc bootstrap tool routes do not match the fixed platform-tool roster')
    for role, path in routes.items():
        _require(isinstance(role, str) and
                 (role.replace('-', '').replace('_', '').isalnum() or role == 'bsdtar'),
                 'pkgsrc bootstrap tool route has an invalid role')
        _require(isinstance(path, str),
                 f'pkgsrc bootstrap tool route is not a string: {role}')
        parts = path.split(' ', 1)
        command_path = parts[0]
        suffix = parts[1] if len(parts) == 2 else ''
        expected_suffix = {'gzip': '-nf ${GZIP}', 'mkdir': '-p'}.get(role, '')
        _require(_absolute(command_path) == command_path and
                 not any(ch.isspace() for ch in command_path) and
                 suffix == expected_suffix,
                 f'pkgsrc bootstrap tool route is not a safe fixed command: {role}')
    with open(mkconf, 'r', encoding='utf-8') as stream:
        content = stream.read()
    _require(_PKGSRC_TOOL_ROUTE_MARKER not in content,
             'pkgsrc bootstrap tool routes were already applied')
    lines = content.splitlines(keepends=True)
    closers = [index for index, line in enumerate(lines)
               if line.strip() == '.endif\t\t\t# end pkgsrc settings']
    _require(len(closers) == 1,
             'pkgsrc bootstrap mk.conf has no unique pkgsrc settings boundary')
    block = [_PKGSRC_TOOL_ROUTE_MARKER + '\n']
    block.extend(f'TOOLS_PLATFORM.{role}= {routes[role]}\n' for role in sorted(routes))
    lines[closers[0]:closers[0]] = block
    with open(mkconf, 'w', encoding='utf-8') as stream:
        stream.writelines(lines)
    return dict(path=mkconf, sha256=sha256_file(mkconf),
                roles=sorted(routes), marker=_PKGSRC_TOOL_ROUTE_MARKER)


def _normalize_internal_absolute_links(root):
    """Make prefix-local absolute links relocatable and reject external targets."""
    root = canonical_existing(_absolute(root))
    real_directory(root)
    normalized = []

    def visit(directory):
        before = real_directory(directory)
        with os.scandir(directory) as scan:
            children = sorted(scan, key=lambda item: item.name)
        for child in children:
            path = os.path.join(directory, child.name)
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                target = os.readlink(path)
                verify_stable(path, info)
                if not os.path.isabs(target):
                    continue
                target_path = os.path.normpath(target)
                relative_to_root = os.path.relpath(target_path, root)
                _require(relative_to_root not in ('.', '..') and
                         not relative_to_root.startswith('..' + os.sep),
                         f'absolute payload link target is outside the output prefix: {path}')
                try:
                    resolved_target = canonical_existing(target_path)
                except SDKError as exc:
                    raise SDKError(
                        f'absolute payload link target is outside or missing from the output prefix: {path}'
                    ) from exc
                _require(inside(resolved_target, root),
                         f'absolute payload link target is outside the output prefix: {path}')
                relative_target = os.path.relpath(target_path, os.path.dirname(path))
                _require(not os.path.isabs(relative_target),
                         f'absolute payload link could not be made relative: {path}')
                temporary = path + '.emberbsd-link-normalize'
                _require(entry(temporary) is None,
                         f'pkgsrc output collides with link-normalization path: {path}')
                os.symlink(relative_target, temporary)
                try:
                    verify_stable(path, info)
                    _require(os.readlink(path) == target,
                             f'absolute payload link changed before normalization: {path}')
                    os.replace(temporary, path)
                finally:
                    if entry(temporary) is not None:
                        os.unlink(temporary)
                normalized.append(dict(path=os.path.relpath(path, root),
                                       original_target=target, target=relative_target))
                after = os.lstat(path)
                _require(stat.S_ISLNK(after.st_mode) and os.readlink(path) == relative_target,
                         f'normalized payload link changed: {path}')
                verify_stable(path, after)
                _require(inside(canonical_existing(path), root),
                         f'normalized payload link escapes the output prefix: {path}')
            elif stat.S_ISDIR(info.st_mode):
                visit(path)
            elif not stat.S_ISREG(info.st_mode):
                raise SDKError(f'special file in pkgsrc output prefix: {path}')
        after = os.lstat(directory)
        _require(stat.S_ISDIR(after.st_mode) and
                 (after.st_dev, after.st_ino, after.st_mode) ==
                 (before.st_dev, before.st_ino, before.st_mode),
                 f'pkgsrc output directory changed during link normalization: {directory}')

    visit(root)
    return normalized


def _sdk_source_identity():
    root = os.path.realpath(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    module_root = os.path.join(root, 'src', 'emberbsd_sdk')
    paths = []
    for directory, dirs, files in os.walk(module_root, followlinks=False):
        dirs.sort()
        for name in sorted(files):
            if name.endswith('.py'):
                paths.append(os.path.join(directory, name))
    paths.append(os.path.join(root, 'scripts', 'bootstrap-pkgsrc.py'))
    files = []
    for path in sorted(set(paths)):
        info = os.lstat(path)
        _require(stat.S_ISREG(info.st_mode), f'SDK source must be a regular file: {path}')
        files.append(dict(path=path, sha256=sha256_file(path)))
    return dict(root=root, files=files)


def _validate_sdk_source(value, *, inspect_inputs):
    _require(isinstance(value, dict) and set(value) == {'root', 'files'} and
             isinstance(value['files'], list) and value['files'],
             'pkgsrc bootstrap plan has no pinned SDK source roster')
    root = _absolute(value['root'])
    expected_root = os.path.realpath(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    _require(root == expected_root, 'pkgsrc bootstrap SDK root changed')
    paths = [row.get('path') for row in value['files'] if isinstance(row, dict)]
    _require(len(paths) == len(value['files']) and len(set(paths)) == len(paths),
             'pkgsrc bootstrap SDK source roster is malformed')
    required_cli = os.path.join(root, 'scripts', 'bootstrap-pkgsrc.py')
    _require(required_cli in paths and any(path.endswith('/bootstrap_pkgsrc_provider.py') for path in paths),
             'pkgsrc bootstrap SDK plan omits its provider or CLI implementation')
    if inspect_inputs:
        observed = _sdk_source_identity()
        _require(observed == value, 'pkgsrc bootstrap SDK implementation changed')
    else:
        for row in value['files']:
            _require(isinstance(row, dict) and set(row) == {'path', 'sha256'} and
                     _absolute(row['path']).startswith(root + os.sep),
                     'pkgsrc bootstrap SDK source reference escaped its root')
            _sha(row['sha256'])
    return value


def _accepted_source(transfer_ref, export_acceptance_ref):
    transfer_ref = _file_hash_ref(transfer_ref, 'pkgsrc source-transfer acceptance')
    export_acceptance_ref = _file_hash_ref(export_acceptance_ref, 'pkgsrc source-export acceptance')
    receipt_path, receipt = validate_export_transfer_acceptance(
        transfer_ref['path'], transfer_ref['sha256'],
        export_acceptance_ref=export_acceptance_ref)
    source_root = _absolute(receipt['output_root'])
    _require(canonical_existing(source_root) == source_root,
             'accepted pkgsrc source root is not canonical')
    for relative in _REQUIRED_SOURCE_FILES:
        path = os.path.join(source_root, relative)
        info = entry(path)
        _require(info is not None and stat.S_ISREG(info.st_mode),
                 f'accepted pkgsrc source omits required package input: {relative}')
    bootstrap_path = os.path.join(source_root, 'bootstrap/bootstrap')
    _require(stat.S_IMODE(os.stat(bootstrap_path).st_mode) & 0o111 and
             os.access(bootstrap_path, os.X_OK),
             'accepted pkgsrc bootstrap entry point is not executable')
    return transfer_ref, export_acceptance_ref, receipt_path, receipt, source_root


def _provider_paths(work_root, source_root, compiler, jobs, utility_bin):
    bootstrap_work = os.path.join(work_root, 'bootstrap')
    bootstrap_pkgsrc_work = os.path.join(bootstrap_work, 'wrk')
    pkgsrc_work = os.path.join(work_root, 'pkgsrc-work')
    prefix = os.path.join(work_root, 'prefix')
    pkgdb = os.path.join(work_root, 'pkgdb')
    distfiles = os.path.join(work_root, 'distfiles')
    tmp = os.path.join(work_root, 'tmp')
    home = os.path.join(work_root, 'home')
    bmake = os.path.join(bootstrap_work, 'bin', 'bmake')
    bootstrap_script = os.path.join(source_root, 'bootstrap/bootstrap')
    mkconf = os.path.join(bootstrap_work, 'mk.conf')
    compiler_base = os.path.dirname(os.path.dirname(compiler))
    package_path = lambda relative: os.path.join(source_root, relative)
    bootstrap_env = dict(
        PATH=utility_bin, CC=compiler, CXX=compiler, CPP=compiler + ' -E',
        CFLAGS='-O2 -g', CPPFLAGS='', LDFLAGS='', SIZE=':', DISTDIR=distfiles,
        WRKOBJDIR=bootstrap_pkgsrc_work, USE_NATIVE_GCC='yes', GCCBASE=compiler_base,
        HOME=home, TMPDIR=tmp,
        LC_ALL='C', TZ='UTC',
    )
    package_path_value = os.pathsep.join((
        os.path.join(bootstrap_work, 'bin'), os.path.join(bootstrap_work, 'sbin'),
        os.path.join(prefix, 'bin'), os.path.join(prefix, 'sbin'), utility_bin,
    ))
    package_env = dict(
        PATH=package_path_value, CC=compiler, CXX=compiler, CPP=compiler + ' -E',
        MAKECONF=mkconf, PKG_DBDIR=pkgdb, LOCALBASE=prefix, MACHINE_ARCH='x86_64',
        DISTDIR=distfiles, WRKOBJDIR=pkgsrc_work, USE_NATIVE_GCC='yes',
        GCCBASE=compiler_base, OPSYS='NetBSD', HOME=home, TMPDIR=tmp, SIZE=':',
        LC_ALL='C', TZ='UTC',
    )
    common_make_args = [f'MAKE_JOBS={jobs}', f'CC={compiler}', f'CXX={compiler}',
                        f'CPP={compiler} -E', f'MAKECONF={mkconf}',
                        f'DISTDIR={distfiles}', f'WRKOBJDIR={pkgsrc_work}',
                        f'GCCBASE={compiler_base}', 'USE_NATIVE_GCC=yes', 'SIZE=:',
                        'PKG_COMPRESSION=none',
                        'PKGSRC_KEEP_BIN_PKGS=no']
    return dict(
        bootstrap_work=bootstrap_work, bootstrap_pkgsrc_work=bootstrap_pkgsrc_work,
        pkgsrc_work=pkgsrc_work,
        prefix_root=prefix, pkgdb=pkgdb,
        distfiles=distfiles,
        tmp=tmp, home=home, bmake_binary=bmake, bootstrap_script=bootstrap_script,
        mkconf=mkconf,
        pkgsrc_tool_routes=_pkgsrc_tool_routes(work_root, utility_bin, bootstrap_work),
        commands=[
            dict(id='bootstrap-full', phase='bootstrap', cwd=work_root,
                 argv=['/bin/sh', bootstrap_script, '--compiler', 'gcc', '--full',
                       '--cwrappers', 'no', '--preserve-path', '--prefix', prefix,
                       '--pkgdbdir', pkgdb, '--workdir', bootstrap_work,
                       '--make-jobs', str(jobs)], env=bootstrap_env, expected_exit=0),
            dict(id='install-mksh', phase='package', cwd=package_path('shells/mksh'),
                 argv=[bmake, '-C', package_path('shells/mksh'),
                       *common_make_args, 'install'], env=package_env, expected_exit=0),
            dict(id='install-gmake', phase='package', cwd=package_path('devel/gmake'),
                 argv=[bmake, '-C', package_path('devel/gmake'),
                       *common_make_args, 'install'], env=package_env, expected_exit=0),
        ],
    )


def _references(value):
    if isinstance(value, dict):
        if set(value) == {'path', 'sha256'}:
            yield _absolute(value['path'])
        for child in value.values():
            yield from _references(child)
    elif isinstance(value, list):
        for child in value:
            yield from _references(child)


def _validate_work_root(work, *, allow_claimed_work):
    work = _absolute(work)
    if allow_claimed_work:
        _require(canonical_existing(work) == work and os.path.isdir(work),
                 'claimed pkgsrc bootstrap work root must remain a real directory')
    else:
        _require(canonical_new(work) == work and entry(work) is None,
                 'pkgsrc bootstrap work root must be a fresh canonical path')


def _validate_mkconf_path_chars(path):
    _require(all((ch.isascii() and ch.isalnum()) or ch in '/._+-' for ch in path),
             'pkgsrc bootstrap work root contains characters unsafe for mk.conf assignments')


def _check_expected_exit(step, actual_exit):
    _require(type(actual_exit) is int and type(step.get('expected_exit')) is int and
             actual_exit == step['expected_exit'],
             f"pkgsrc bootstrap command {step.get('id')} exited {actual_exit}; "
             f"expected {step.get('expected_exit')}")


def _validate_host_identity(system, release, machine):
    _require(system == 'NetBSD' and release == '11.0' and
             machine in ('amd64', 'x86_64'),
             'pkgsrc bootstrap commands require NetBSD 11.0 amd64')


def _validate_plan(plan, *, inspect_inputs=True, allow_claimed_work=False):
    fields = {
        'format', 'version', 'scope', 'purpose', 'host', 'transfer_acceptance',
        'export_acceptance', 'compiler_seed', 'source_root', 'sdk_source',
        'work_root', 'utility_bin', 'bootstrap_work', 'bootstrap_pkgsrc_work',
        'pkgsrc_work',
        'prefix_root', 'pkgdb', 'distfiles', 'mkconf',
        'bmake_binary', 'host_tools', 'pkgsrc_tool_routes', 'resources', 'commands',
    }
    _require(isinstance(plan, dict) and set(plan) == fields and
             plan.get('format') == PLAN_FORMAT and type(plan.get('version')) is int and
             plan['version'] == 1 and plan.get('scope') == SCOPE and
             plan.get('purpose') == 'bootstrap-current-pkgsrc-tools',
             'unsupported pkgsrc bootstrap provider plan')
    _require(plan['host'] == 'NetBSD11.0-amd64',
             'pkgsrc bootstrap provider requires NetBSD 11.0 amd64')
    transfer_ref = _hash_ref(plan['transfer_acceptance'], 'target pkgsrc transfer acceptance')
    export_ref = _hash_ref(plan['export_acceptance'], 'source pkgsrc export acceptance')
    compiler_ref = _file_hash_ref(plan['compiler_seed'], 'accepted GCC16 compiler seed')
    seed = read_json(compiler_ref['path'])
    validate_compiler_seed(seed, inspect_inputs=inspect_inputs)
    _, _, _, _, source_root = _accepted_source(transfer_ref, export_ref)
    _require(plan['source_root'] == source_root,
             'pkgsrc bootstrap plan source root differs from the accepted transfer')
    sdk = _validate_sdk_source(plan['sdk_source'], inspect_inputs=inspect_inputs)
    work = _absolute(plan['work_root'])
    _validate_mkconf_path_chars(work)
    utility_bin = _absolute(plan['utility_bin'])
    _require(utility_bin == os.path.join(work, 'host-tools') and
             plan['bootstrap_work'] == os.path.join(work, 'bootstrap') and
             plan['bootstrap_pkgsrc_work'] == os.path.join(work, 'bootstrap/wrk') and
             plan['pkgsrc_work'] == os.path.join(work, 'pkgsrc-work') and
             plan['prefix_root'] == os.path.join(work, 'prefix') and
             plan['pkgdb'] == os.path.join(work, 'pkgdb') and
             plan['distfiles'] == os.path.join(work, 'distfiles') and
             plan['mkconf'] == os.path.join(work, 'bootstrap/mk.conf') and
             plan['bmake_binary'] == os.path.join(work, 'bootstrap/bin/bmake'),
             'pkgsrc bootstrap outputs differ from their fixed work layout')
    tools = _host_tool_map(plan['host_tools'], inspect_inputs=inspect_inputs)
    resources = plan['resources']
    _require(isinstance(resources, dict) and set(resources) == {
        'lock_root', 'runtime_seconds', 'cpu_jobs'} and
        type(resources['runtime_seconds']) is int and
        0 < resources['runtime_seconds'] <= _MAX_RUNTIME and
        type(resources['cpu_jobs']) is int and 0 < resources['cpu_jobs'] <= 64,
        'pkgsrc bootstrap resource bounds are invalid')
    paths = _provider_paths(work, source_root, seed['compiler']['path'],
                            resources['cpu_jobs'], utility_bin)
    _require(plan['commands'] == paths['commands'] and
             plan['pkgsrc_tool_routes'] == paths['pkgsrc_tool_routes'],
             'pkgsrc bootstrap commands or tool routes differ from the fixed source-owned route')
    lock_root = _absolute(resources['lock_root'])
    protected = [work, utility_bin, source_root, plan['sdk_source']['root'],
                 seed['prefix'], *list(_references(transfer_ref)),
                 *list(_references(export_ref)), *list(_references(compiler_ref)),
                 *[row['path'] for row in tools.values()],
                 *[row['path'] for row in sdk['files']]]
    _guard_lock_root(lock_root, protected)
    reject_overlap(work, source_root)
    reject_overlap(work, plan['sdk_source']['root'])
    reject_overlap(work, seed['prefix'])
    for path in protected[3:]:
        if path not in (seed['prefix'], plan['sdk_source']['root']):
            reject_overlap(work, path)
    if inspect_inputs:
        _validate_host_identity(platform.system(), platform.release(), platform.machine())
        _validate_work_root(work, allow_claimed_work=allow_claimed_work)
        _require(canonical_existing(lock_root) == lock_root and
                 os.path.isdir(lock_root), 'pkgsrc bootstrap lock root must be a real directory')
        _require(canonical_existing(source_root) == source_root,
                 'accepted pkgsrc source root changed')
    return plan


def create_bootstrap_plan(transfer_acceptance_ref, export_acceptance_ref,
                          compiler_seed_ref, work_root, lock_root, host_tools, *,
                          plan_path, cpu_jobs=1, runtime_seconds=3600):
    transfer_ref, export_ref, _, _, source_root = _accepted_source(
        transfer_acceptance_ref, export_acceptance_ref)
    compiler_ref = _file_hash_ref(compiler_seed_ref, 'accepted GCC16 compiler seed')
    seed = read_json(compiler_ref['path'])
    validate_compiler_seed(seed, inspect_inputs=False)
    work, plan_path = _absolute(work_root), _absolute(plan_path)
    _validate_mkconf_path_chars(work)
    _require(canonical_new(work) == work and entry(work) is None,
             'pkgsrc bootstrap work root must be a fresh canonical path')
    _require(type(cpu_jobs) is int and 0 < cpu_jobs <= 64,
             'pkgsrc bootstrap CPU jobs must be an integer from 1 through 64')
    _require(type(runtime_seconds) is int and 0 < runtime_seconds <= _MAX_RUNTIME,
             'pkgsrc bootstrap runtime must be an integer within its fixed budget')
    sdk = _sdk_source_identity()
    utility_bin = os.path.join(work, 'host-tools')
    plan = dict(
        format=PLAN_FORMAT, version=1, scope=SCOPE,
        purpose='bootstrap-current-pkgsrc-tools', host='NetBSD11.0-amd64',
        transfer_acceptance=transfer_ref, export_acceptance=export_ref,
        compiler_seed=compiler_ref, source_root=source_root, sdk_source=sdk,
        work_root=work, utility_bin=utility_bin,
        bootstrap_work=os.path.join(work, 'bootstrap'),
        bootstrap_pkgsrc_work=os.path.join(work, 'bootstrap', 'wrk'),
        pkgsrc_work=os.path.join(work, 'pkgsrc-work'),
        prefix_root=os.path.join(work, 'prefix'), pkgdb=os.path.join(work, 'pkgdb'),
        distfiles=os.path.join(work, 'distfiles'),
        mkconf=os.path.join(work, 'bootstrap', 'mk.conf'),
        bmake_binary=os.path.join(work, 'bootstrap/bin/bmake'), host_tools=host_tools,
        pkgsrc_tool_routes=_pkgsrc_tool_routes(
            work, utility_bin, os.path.join(work, 'bootstrap')),
        resources=dict(lock_root=_absolute(lock_root), runtime_seconds=runtime_seconds,
                       cpu_jobs=cpu_jobs), commands=[],
    )
    plan['commands'] = _provider_paths(work, source_root, seed['compiler']['path'],
                                       cpu_jobs, utility_bin)['commands']
    _validate_plan(plan, inspect_inputs=False)
    _require(entry(plan_path) is None,
             'pkgsrc bootstrap plan output must be a fresh file')
    reject_overlap(plan_path, work)
    reject_overlap(plan_path, _absolute(lock_root))
    write_json(plan_path, plan, exclusive=True)
    return plan


def run_bootstrap(plan_ref):
    """Run the finite bootstrap and package route while retaining all leases."""
    _require(threading.current_thread() is threading.main_thread(),
             'pkgsrc bootstrap provider must run on the main thread')
    plan_ref = _file_hash_ref(plan_ref, 'pkgsrc bootstrap plan')
    plan = read_json(plan_ref['path'])
    _validate_plan(plan, inspect_inputs=False)
    work = plan['work_root']
    lock_root = plan['resources']['lock_root']
    _require(entry(work) is None and canonical_new(work) == work,
             'pkgsrc bootstrap work directory must be fresh')
    if entry(lock_root) is None:
        _require(canonical_new(lock_root) == lock_root,
                 'pkgsrc bootstrap lock root path has aliased ancestors')
        try:
            os.mkdir(lock_root, 0o700)
        except FileExistsError:
            pass
    _require(canonical_existing(lock_root) == lock_root and os.path.isdir(lock_root),
             'pkgsrc bootstrap lock root must be a real directory')
    _validate_plan(plan, inspect_inputs=True)
    transfer_ref, export_ref, _, transfer, source_root = _accepted_source(
        plan['transfer_acceptance'], plan['export_acceptance'])
    compiler_seed = read_json(plan['compiler_seed']['path'])
    shared = set([plan_ref['path'], *list(_references(plan)), source_root,
                  plan['sdk_source']['root'], compiler_seed['prefix'],
                  *list(_references(transfer)),
                  *list(_references(read_json(export_ref['path']))),
                  *list(_compiler_seed_references(compiler_seed))])
    _guard_lock_root(lock_root, [work, *shared])
    for path in shared:
        reject_overlap(work, path)
    with ContextLocks(sorted(shared), lock_root, shared=True), ContextLocks([work], lock_root):
        _require(sha256_file(plan_ref['path']) == plan_ref['sha256'],
                 'pkgsrc bootstrap plan changed before dispatch')
        plan = read_json(plan_ref['path'])
        _validate_plan(plan, inspect_inputs=True)
        _require(entry(work) is None, 'pkgsrc bootstrap work was claimed before dispatch')
        os.mkdir(work, 0o700)
        os.mkdir(plan['utility_bin'], 0o700)
        os.mkdir(plan['pkgsrc_work'], 0o700)
        os.mkdir(os.path.join(work, 'tmp'), 0o700)
        os.mkdir(os.path.join(work, 'home'), 0o700)
        os.mkdir(plan['distfiles'], 0o700)
        for role, row in plan['host_tools'].items():
            os.symlink(row['path'], os.path.join(plan['utility_bin'], role))
        result_path = os.path.join(work, 'result.json')
        result = dict(
            format=RESULT_FORMAT, version=1, scope=SCOPE,
            status='RUNNING', bootstrap_plan=plan_ref,
            transfer_acceptance=plan['transfer_acceptance'],
            export_acceptance=plan['export_acceptance'],
            compiler_seed=plan['compiler_seed'], commands=[],
            all_commands_drained=False, inputs_revalidated_after_drain=False,
            ordinary_provider_qualified=False,
            evidence_level='accepted-source-and-GCC16-bootstrap-command-routes-with-relocatable-output-links',
            output_root=plan['prefix_root'], output_inventory=None, outputs=[],
            normalized_output_links=[],
            pkgsrc_tool_routes_applied=None,
            scope_boundary=SCOPE_BOUNDARY,
        )
        write_json(result_path, result, exclusive=True)
        deadline = time.monotonic() + plan['resources']['runtime_seconds']
        routes_applied = False
        try:
            for step in plan['commands']:
                if step['phase'] == 'package' and not routes_applied:
                    current_tools = _host_tool_map(plan['host_tools'], inspect_inputs=True)
                    for role, row in current_tools.items():
                        alias = os.path.join(plan['utility_bin'], role)
                        alias_info = os.lstat(alias)
                        _require(stat.S_ISLNK(alias_info.st_mode) and
                                 os.readlink(alias) == row['path'],
                                 f'pkgsrc bootstrap private host-tool alias changed: {role}')
                    for role in ('awk', 'install', 'sed', 'sh'):
                        path = plan['pkgsrc_tool_routes'][role]
                        info = os.lstat(path)
                        _require(stat.S_ISREG(info.st_mode) and os.access(path, os.X_OK),
                                 f'pkgsrc bootstrap tool was not produced: {role}')
                    result['pkgsrc_tool_routes_applied'] = _append_pkgsrc_tool_routes(
                        plan['mkconf'], plan['pkgsrc_tool_routes'])
                    routes_applied = True
                    write_json(result_path, result, exclusive=False)
                actual = dict(step)
                log = os.path.join(work, step['id'] + '.log')
                try:
                    remaining = deadline - time.monotonic()
                    _require(remaining > 0, 'pkgsrc bootstrap runtime budget expired')
                    returned = run_logged(step['argv'], log, cwd=step['cwd'], env=step['env'],
                                          timeout_seconds=remaining)
                    actual['actual_exit'] = returned.returncode
                    _check_expected_exit(step, returned.returncode)
                except ProcessOwnershipError as exc:
                    actual.update(actual_exit=None, error=str(exc), unfinalized_log_path=log)
                    result['commands'].append(actual)
                    result.update(status='HOLD_UNPROVEN_DRAIN', error=str(exc), owned_sid=exc.sid)
                    write_json(result_path, result, exclusive=False)
                    print(f'pkgsrc bootstrap HOLD: {exc}; source and work leases retained',
                          file=__import__('sys').stderr, flush=True)
                    from .bootstrap_pilot import _hold
                    _hold()
                except BaseException as exc:
                    actual.update(actual_exit=getattr(exc, 'returncode', None), error=str(exc))
                    raise
                finally:
                    if result['status'] != 'HOLD_UNPROVEN_DRAIN':
                        if entry(log) is not None:
                            actual['log'] = dict(path=log, sha256=sha256_file(log))
                        result['commands'].append(actual)
                        write_json(result_path, result, exclusive=False)
            result['normalized_output_links'] = _normalize_internal_absolute_links(
                plan['prefix_root'])
            write_json(result_path, result, exclusive=False)
            outputs = []
            for name, path in (
                ('bmake', plan['bmake_binary']),
                ('mksh', os.path.join(plan['prefix_root'], 'bin', 'mksh')),
                ('gmake', os.path.join(plan['prefix_root'], 'bin', 'gmake')),
            ):
                info = os.lstat(path)
                _require(stat.S_ISREG(info.st_mode) and os.access(path, os.X_OK),
                         f'pkgsrc bootstrap did not produce executable {name}')
                outputs.append(dict(name=name, path=path, sha256=sha256_file(path),
                                    mode=stat.S_IMODE(info.st_mode)))
            inventory_path = os.path.join(work, 'output-inventory.json')
            inventory = snapshot_tree(plan['prefix_root'])
            write_json(inventory_path, inventory, exclusive=True)
            result.update(status='COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW',
                          outputs=outputs,
                          output_inventory=dict(path=inventory_path,
                                                sha256=sha256_file(inventory_path)),
                          output_entries=len(inventory))
        except BaseException as exc:
            if result['status'] != 'HOLD_UNPROVEN_DRAIN':
                result.update(status='FAILED', error=str(exc))
        result['all_commands_drained'] = result['status'] != 'HOLD_UNPROVEN_DRAIN'
        if result['all_commands_drained']:
            try:
                _validate_plan(read_json(plan_ref['path']), inspect_inputs=True,
                               allow_claimed_work=True)
                _require(sha256_file(plan_ref['path']) == plan_ref['sha256'],
                         'pkgsrc bootstrap plan changed during production')
                result['inputs_revalidated_after_drain'] = True
            except SDKError as exc:
                result.update(status='FAILED', error=str(exc), inputs_revalidated_after_drain=False)
        write_json(result_path, result, exclusive=False)
    result['all_leases_closed'] = True
    write_json(result_path, result, exclusive=False)
    return result
