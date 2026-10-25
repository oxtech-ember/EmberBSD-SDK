# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD (AI-assisted).
"""One initial pkg_install build. The existing program runner owns children."""

import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import tarfile

from .bootstrap_bsdinstall import _compat
from .bootstrap_gnu_program import _tree
from .bootstrap_make import _safe_path
from .inventory import sha256_file, snapshot_tree
from .native_toolset import _bytes, _envelope, _json, _native_image, _require
from .paths import entry

STAGE = 'pkg-install-bootstrap'
MANIFEST_RELATIVE = 'profiles/common-build-tools/bootstrap/pkg-install.json'
MANIFEST_SHA256 = '9a0d854e0b476e6b0d308cc5dee5e1b86dc69431e4c290f3192cb2030ef0608a'
_PATCH = 'profiles/common-build-tools/patches/pkgsrc-pkg-install-dyld-cache.patch'
_PREFIXES = ('archivers/libarchive/', 'pkgtools/pkg_install/')
_DRIVERS = {'cc', 'cxx', 'cpp', 'as', 'ld', 'ar', 'ranlib', 'nm', 'strip', 'make'}
_OUTPUTS = {name: 'pkg_install/' + directory + '/' + name for name, directory in
            (('pkg_add', 'add'), ('pkg_admin', 'admin'), ('pkg_create', 'create'), ('pkg_info', 'info'))}


def pkg_install_manifest(plan):
    value = plan['program']
    _require(value['manifest'] == dict(path=os.path.join(value['owner']['root'], MANIFEST_RELATIVE),
                                     sha256=MANIFEST_SHA256), 'unsupported initial pkg_install manifest')
    manifest = _json(value['manifest'])
    _require(manifest['format'] == 'emberbsd-native-bootstrap-program' and
        type(manifest['version']) is int and manifest['version'] == 1 and
        manifest['name'] == 'pkg-install' and manifest['initial_only'] is True and
        manifest['build_system'] == 'pkgsrc-bootstrap' and
        manifest['compiler'] == dict(family='gcc', major=16, abi='native') and
        manifest['output_programs'] == _OUTPUTS and
        manifest['infrastructure_patches'] == [_PATCH] and
        [r['name'] for r in manifest['components']] == ['libarchive', 'pkg_install'] and
        len(manifest['runtime_cases']) == 16 and
        all(r['make_targets'] == ['all'] for r in manifest['components']),
        'unsupported fixed initial pkg_install contract')
    return manifest


def _token(value, work):
    from .bootstrap_producer import _current_host_facts
    return value.replace('@WORK@', work).replace('@HOST_KERNEL_VERSION@',
                                               _current_host_facts()['kernel_version'])


def pkg_install_sources(plan):
    return {name: _token(text, plan['work_root']).encode()
            for name, text in pkg_install_manifest(plan)['files'].items()}


def _environment(plan, commands, flags, *, package=False):
    work = _safe_path(plan['work_root'])
    env = dict(PATH=work + '/bin', LC_ALL='C', TZ='UTC', TMPDIR=work, CONFIG_SITE='/dev/null',
               PKG_DBDIR=work + '/pkgdb', PKG_PATH='')
    for variable, role in (('CC', 'cc'), ('CXX', 'cxx'), ('CPP', 'cpp'), ('AS', 'as'), ('LD', 'ld'),
            ('AR', 'ar'), ('RANLIB', 'ranlib'), ('NM', 'nm'), ('STRIP', 'strip'),
            ('SHELL', 'sh'), ('CONFIG_SHELL', 'sh'), ('MAKE', 'make'), ('INSTALL', 'install')):
        env[variable] = shlex.join(commands[role])
    cpp, ld = list(flags['cppflags']), [*flags['ldflags'], '-Wl,--threads=1']
    if package:
        cpp += ['-I' + plan['program']['compat']['root'] + '/include',
                '-I' + work + '/source/libarchive/libarchive']
        ld += ['-L' + plan['program']['compat']['root'] + '/lib']
    env.update(CPPFLAGS=shlex.join(cpp), CFLAGS=shlex.join(flags['cflags']),
               CXXFLAGS=shlex.join(flags['cxxflags']), LDFLAGS=shlex.join(ld),
               LIBS='-lnbcompat' if package else '')
    return env


def pkg_install_steps(plan, descriptor, commands, flags):
    manifest, work = pkg_install_manifest(plan), _safe_path(plan['work_root'])
    triplet = descriptor['compiler']['triplet']
    result = []
    for component in manifest['components']:
        name = component['name']
        source = work + '/source/' + name
        env = _environment(plan, commands, flags, package=name == 'pkg_install')
        args = ['--build=' + triplet, '--host=' + triplet, '--prefix=' + work + '/unused-prefix']
        if name == 'pkg_install':
            args += ['--sysconfdir=' + work + '/config', '--with-pkgdbdir=' + work + '/pkgdb']
        result.append(dict(id=name + '-configure', phase='pkg-configure', cwd=source, env=env,
            expected_exit=0, argv=[*commands['sh'], source + '/configure', *args,
                                  *component['configure_args']]))
        args = ['-j' + str(plan['resources']['cpu_jobs']), 'MAKE=' + shlex.join(commands['make'])]
        if name == 'pkg_install':
            args += ['-f', 'Makefile', '-f', work + '/libarchive-private.mk',
                     'STATIC_LIBARCHIVE=' + work + '/source/libarchive/.libs/libarchive.a',
                     'PKGSRC_MACHINE_ARCH=aarch64', 'OPSYS=Darwin']
        result.append(dict(id=name + '-build', phase='pkg-build', cwd=source, env=dict(env),
            expected_exit=0, argv=[*commands['make'], *args, 'all']))
    env = _environment(plan, commands, flags, package=True)
    for case in manifest['runtime_cases']:
        argv = [work + '/source/' + manifest['output_programs'][case['invocation']],
                *(_token(word, work) for word in case['args'])]
        command = "printf '%s' " + shlex.quote(case['stdin']) + ' | ' + shlex.join(argv)
        command += ' >' + shlex.quote(work + '/' + case['name'] + '.stdout')
        command += ' 2>' + shlex.quote(work + '/' + case['name'] + '.stderr')
        result.append(dict(id='case-' + case['name'], phase='program-case', cwd=work,
            env=dict(env), expected_exit=case['exit'], argv=[*commands['sh'], '-c', command]))
    for name, relative in manifest['output_programs'].items():
        result.append(dict(id='runtime-' + name, phase='run', cwd=work, expected_exit=0,
            env=dict(env, DYLD_PRINT_LIBRARIES='1'), argv=[work + '/source/' + relative, '-V']))
    return result


def pkg_install_outputs(plan):
    manifest = pkg_install_manifest(plan)
    return [*manifest['files'], 'libarchive-private.mk',
        'source/libarchive/build/pkgconfig/libarchive.pc', 'source/libarchive/.libs/libarchive.a',
        *('source/' + p for p in manifest['output_programs'].values()),
        *(case['name'] + suffix for case in manifest['runtime_cases'] for suffix in ('.stdout', '.stderr'))]


def prepare_pkg_install_work(plan, commands):
    manifest, work = pkg_install_manifest(plan), plan['work_root']
    os.mkdir(work + '/source')
    for row in manifest['components']:
        source = Path(plan['program']['source']['root'], row['source_directory'])
        destination = Path(work, 'source', row['name'])
        shutil.copytree(source, destination, copy_function=shutil.copy2, symlinks=False)
        before = snapshot_tree(str(source))
        for name, entry_row in before.items():
            mode = entry_row['mode'] | (0o700 if entry_row['kind'] == 'directory' else 0o200)
            os.chmod(destination / name, mode)
        destination.chmod(destination.stat().st_mode | 0o700)
        expected = {n: dict(r, mode=r['mode'] | (0o700 if r['kind'] == 'directory' else 0o200))
                    for n, r in before.items()}
        _require(snapshot_tree(str(destination)) == expected, 'staged pkg_install source differs')
    os.mkdir(work + '/bin')
    for role in sorted(set(manifest['required_tool_roles']) - _DRIVERS):
        os.symlink(commands[role][0], work + '/bin/' + role)
    for directory in ('pkgdb', 'target', 'config'):
        os.mkdir(work + '/' + directory)
    for name in manifest['files']:
        Path(work, name).parent.mkdir(parents=True, exist_ok=True)


def _private_libraries(work):
    path = Path(work, 'source/libarchive/build/pkgconfig/libarchive.pc')
    lines = [line.removeprefix('Libs.private:').strip() for line in path.read_text().splitlines()
             if line.startswith('Libs.private:')]
    _require(len(lines) == 1, 'libarchive.pc needs one actual private library line')
    words = shlex.split(lines[0])
    _require(all(re.fullmatch(r'-l[A-Za-z0-9_+.-]+', word) for word in words),
             'libarchive private libraries contain an unsupported search or command selector')
    # This file is data for the exact second bmake command, never shell code.
    fragment = Path(work, 'libarchive-private.mk')
    with fragment.open('x') as output:
        output.write('STATIC_LIBARCHIVE_LDADD= ' + ' '.join(words) + '\n.export STATIC_LIBARCHIVE_LDADD\n')
    return dict(libarchive_pc=dict(path=str(path), sha256=sha256_file(str(path))),
                private_libraries=words,
                make_fragment=dict(path=str(fragment), sha256=sha256_file(str(fragment))))


def verify_pkg_install_step(step, work, plan):
    if step['id'] == 'libarchive-configure':
        return _private_libraries(work)
    if step['id'] == 'libarchive-build':
        path = Path(work, 'source/libarchive/.libs/libarchive.a')
        with path.open('rb') as stream:
            _require(stream.read(8) == b'!<arch>\n', 'initial libarchive is not a static archive')
        return dict(static_libarchive=dict(path=str(path), sha256=sha256_file(str(path))))
    if step['id'] == 'pkg_install-build':
        outputs = {}
        for name, relative in _OUTPUTS.items():
            path = work + '/source/' + relative
            _native_image(path, plan['host'])
            outputs[name] = dict(path=path, sha256=sha256_file(path))
        return dict(program_outputs=outputs)
    if step['phase'] != 'program-case':
        return {}
    case = next(row for row in pkg_install_manifest(plan)['runtime_cases'] if step['id'] == 'case-' + row['name'])
    streams = {}
    for name in ('stdout', 'stderr'):
        path = work + '/' + case['name'] + '.' + name
        ref = dict(path=path, sha256=sha256_file(path))
        data = _bytes(ref)
        _require(bool(data) if name == 'stderr' and case.get('stderr_nonempty') is True else
                 data == case[name].encode(), 'initial pkg_install actual ' + name + ' differs')
        if name == 'stderr' and 'stderr_contains' in case:
            _require(case['stderr_contains'].encode() in data, 'missing required-library refusal diagnostic')
        streams[name] = ref
    checks = []
    for row in case.get('checks', []):
        path = os.path.join(work, row['path'])
        info = entry(path)
        _require(info is None if row['kind'] == 'absent' else info is not None and stat.S_ISREG(info.st_mode),
                 'pkg_install semantic filesystem observation differs')
        if info is not None and 'content' in row:
            _require(Path(path).read_bytes() == row['content'].encode(), 'installed pkg_install fixture differs')
        checks.append(dict(row, **({'sha256': sha256_file(path)} if info is not None else {})))
    if case['name'] in ('create-cache', 'create-missing', 'create-target'):
        archive = next(row for row in case['checks'] if row['kind'] == 'file')['path']
        with tarfile.open(os.path.join(work, archive), mode='r:') as package:
            names = package.getnames()
            _require('+BUILD_INFO' in names and '+CONTENTS' in names and
                     'share/initial-pkgtools.txt' in names, 'initial package omits required metadata or payload')
    return dict(streams, checks=checks)


def validate_pkg_install_plan(plan, descriptor, commands, flags):
    from .bootstrap_pilot import _validate_resources
    from .bootstrap_producer import PRODUCER_FORMAT, PROFILE_EXPORT, _export_proof, _inventory
    _envelope(plan, PRODUCER_FORMAT, scope='gcc16-bootstrap')
    _require(set(plan) == {'format', 'version', 'scope', 'stage', 'seed_descriptor', 'host', 'sources',
        'work_root', 'required_roles', 'resources', 'program', 'commands'} and plan['stage'] == STAGE and
        plan['host'] == descriptor['host'] and plan['sources'] == descriptor['sources'] and
        descriptor['host']['os'] == 'Darwin' and descriptor['host']['cpu'] in ('arm64', 'aarch64'),
        'unsupported fixed initial pkg_install plan')
    _validate_resources(plan['resources'], recipe=True, parallel=True)
    _require(plan['resources']['runtime_seconds'] <= 1200, 'initial pkg_install runtime exceeds its finite budget')
    _safe_path(plan['work_root'])
    value = plan['program']
    _require(set(value) == {'source', 'owner', 'manifest', 'compat', 'export', 'profile'},
             'unsupported initial pkg_install input fields')
    manifest = pkg_install_manifest(plan)
    owner, _ = _tree(value['owner'], descriptor, owner=True)
    _require({n for n, row in owner.items() if row['kind'] == 'file'} ==
             {MANIFEST_RELATIVE, _PATCH, 'pkgsrc/export.conf'}, 'initial pkg_install owner roster differs')
    source, _ = _tree(value['source'], descriptor, owner=False)
    _require(entry(value['source']['root']).st_mode & 0o222 == 0 and
             all(row['mode'] & 0o222 == 0 for row in source.values()), 'initial pkg_install source must be read-only')
    _require(set(value['export']) == {'root', 'inventory', 'source_proof'} and
             value['profile'] == dict(path=os.path.join(value['export']['root'], PROFILE_EXPORT),
                                      sha256=value['profile']['sha256']),
             'initial pkg_install profile must retain its canonical export path')
    _bytes(value['profile'])
    exported = _inventory(value['export']['inventory'])
    _export_proof(value, descriptor, exported)
    wanted = {name for name in exported if name == 'bootstrap/bootstrap' or name.startswith(_PREFIXES)}
    wanted |= {str(parent) for name in tuple(wanted) for parent in Path(name).parents if str(parent) != '.'}
    _require(set(source) == wanted, 'initial pkg_install source subset omits or adds canonical members')
    for name, row in source.items():
        expected = exported[name]
        keys = ('kind', 'size', 'sha256') if row['kind'] == 'file' else ('kind',)
        _require(all(row.get(key) == expected.get(key) for key in keys), 'initial pkg_install source bytes differ')
    script = Path(value['source']['root'], 'bootstrap/bootstrap').read_text()
    flat = ' '.join(script.replace('\\\n', '').split())
    _require(' '.join(manifest['components'][0]['configure_args']) in flat and
             '--enable-bootstrap' in flat and 'STATIC_LIBARCHIVE=' in flat,
             'initial pkg_install configure sequence differs from canonical bootstrap')
    required = set(manifest['required_tool_roles']) | set(descriptor['driver_components'].values())
    _require(isinstance(plan['required_roles'], list) and len(set(plan['required_roles'])) == len(plan['required_roles']) and
             required <= set(plan['required_roles']) <= set(commands), 'initial pkg_install input roles are incomplete')
    for role in set(manifest['required_tool_roles']) - _DRIVERS:
        _require(len(commands[role]) == 1 and descriptor['tools'][role]['kind'] == 'native',
                 'initial pkg_install utility must retain its selected native spelling')
    _compat(value['compat'], descriptor)
    _require(plan['commands'] == pkg_install_steps(plan, descriptor, commands, flags),
             'initial pkg_install commands differ from the finite source-owned route')
    return plan
