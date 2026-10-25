# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Prepare the canonical Ports libelf source subset for native OS builds."""

import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile

from . import inventory, jsonio, paths, process, provenance, sourceprep
from .errors import SDKError
from .publication import publish_directory_noreplace, require_directory_publication

LIBELF_SOURCE_API = 1
RECIPE = 'pkgsrc/devel/elftoolchain'
MANIFEST = RECIPE + '/files/base-source.json'
RECEIPT = 'libelf-source.json'
UPSTREAM_NAME = 'elftoolchain-0.7.1'
UPSTREAM_URL = ('https://downloads.sourceforge.net/project/elftoolchain/'
                'Sources/elftoolchain-0.7.1/elftoolchain-0.7.1.tar.bz2')
UPSTREAM_SVN_REVISION = 3417
BASE_REPOSITORY = 'https://github.com/oxtech-ember/EmberBSD'
BASE_PREFIX = 'external/bsd/elftoolchain/dist'
BASE_TREE_IDS = {
    'common': '598ff3ac00acdcec17cda0ec326681b3ee24ab8c',
    'libelf': 'cfd8db242562d5f3e716aabe5499539bd25d52d8',
}
PACKAGING_OVERRIDES = {'libelf/Makefile', 'libelf/Version.map'}
MAX_ARCHIVE_SIZE = 64 * 1024 * 1024


def _require(condition, message):
    if not condition:
        raise SDKError(message)


def _read(path):
    handle, before = paths.open_regular(path)
    with handle:
        data = handle.read()
    paths.verify_stable(path, before)
    return data


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _hash(value, length):
    return isinstance(value, str) and re.fullmatch('[a-f0-9]{' + str(length) + '}', value) is not None


def _source_path(value):
    if not isinstance(value, str) or not value.isascii() or \
            re.fullmatch(r'(?:common|libelf)/(?:[A-Za-z0-9_.+-]+/)*[A-Za-z0-9_.+-]+', value) is None:
        return False
    return not any(part in ('', '.', '..') for part in value.split('/'))


def _manifest(root):
    path = Path(root, MANIFEST)
    raw = _read(path)
    value = jsonio.loads_json(raw)
    _require(isinstance(value, dict) and set(value) ==
             {'format', 'version', 'upstream', 'base', 'files', 'packaging_overrides'},
             'invalid elftoolchain base-source manifest fields')
    _require(value['format'] == 'emberbsd-elftoolchain-base-source' and
             type(value['version']) is int and value['version'] == 1,
             'unsupported elftoolchain base-source manifest')

    upstream = value['upstream']
    _require(isinstance(upstream, dict) and set(upstream) ==
             {'distname', 'url', 'svn_revision', 'archive'},
             'invalid elftoolchain upstream manifest')
    _require(upstream['distname'] == UPSTREAM_NAME and upstream['url'] == UPSTREAM_URL and
             type(upstream['svn_revision']) is int and
             upstream['svn_revision'] == UPSTREAM_SVN_REVISION,
             'unsupported elftoolchain upstream identity')
    archive = upstream['archive']
    _require(isinstance(archive, dict) and set(archive) ==
             {'size', 'sha256', 'sha512', 'blake2s'} and
             type(archive['size']) is int and 0 < archive['size'] <= MAX_ARCHIVE_SIZE and
             _hash(archive['sha256'], 64) and _hash(archive['sha512'], 128) and
             _hash(archive['blake2s'], 64), 'invalid elftoolchain archive identity')

    base = value['base']
    _require(isinstance(base, dict) and set(base) ==
             {'repository', 'commit', 'prefix', 'trees'} and
             base['repository'] == BASE_REPOSITORY and _hash(base['commit'], 40) and
             base['prefix'] == BASE_PREFIX and isinstance(base['trees'], dict) and
             set(base['trees']) == {'common', 'libelf'} and
             all(_hash(tree, 40) for tree in base['trees'].values()) and
             base['trees'] == BASE_TREE_IDS,
             'invalid EmberBSD base-source identity')

    files = value['files']
    _require(isinstance(files, dict) and files, 'empty elftoolchain base-source file set')
    folded = set()
    for name, row in files.items():
        _require(_source_path(name), f'unsafe elftoolchain source path: {name!r}')
        _require(name.casefold() not in folded, 'case-colliding elftoolchain source paths')
        folded.add(name.casefold())
        _require(isinstance(row, dict) and set(row) == {'size', 'mode', 'sha256'} and
                 type(row['size']) is int and row['size'] >= 0 and
                 type(row['mode']) is int and row['mode'] in (0o644, 0o755) and
                 _hash(row['sha256'], 64), f'invalid elftoolchain source identity: {name}')
    _require(any(name.startswith('common/') for name in files) and
             any(name.startswith('libelf/') for name in files),
             'base-source manifest must cover common and libelf')

    overrides = value['packaging_overrides']
    _require(isinstance(overrides, dict) and set(overrides) == PACKAGING_OVERRIDES,
             'unsupported elftoolchain packaging override set')
    expected = dict(files)
    for name, row in overrides.items():
        _require(name in files and isinstance(row, dict) and set(row) ==
                 {'size', 'mode', 'sha256', 'base_sha256', 'reason'} and
                 type(row['size']) is int and row['size'] >= 0 and
                 type(row['mode']) is int and row['mode'] in (0o644, 0o755) and
                 _hash(row['sha256'], 64) and _hash(row['base_sha256'], 64) and
                 isinstance(row['reason'], str) and row['reason'].strip() and
                 row['base_sha256'] == files[name]['sha256'],
                 f'invalid elftoolchain packaging override: {name}')
        expected[name] = {key: row[key] for key in ('size', 'mode', 'sha256')}
    return raw, value, expected


def _distinfo(data, archive_name, patch_names):
    entries = {}
    for line in data.decode('utf-8', 'strict').splitlines():
        if not line or line.startswith('$NetBSD'):
            continue
        match = re.fullmatch(r'(BLAKE2s|SHA512|Size|SHA1) \(([^/()]+)\) = ([a-f0-9]+)( bytes)?', line)
        _require(match is not None, 'unsupported elftoolchain distinfo entry')
        kind, name, value, suffix = match.groups()
        _require((kind == 'Size') == (suffix == ' bytes'), 'invalid elftoolchain distinfo size suffix')
        _require((kind, name) not in entries, 'duplicate elftoolchain distinfo entry')
        entries[kind, name] = value
    expected = {('BLAKE2s', archive_name), ('SHA512', archive_name), ('Size', archive_name)}
    expected.update(('SHA1', name) for name in patch_names)
    _require(set(entries) == expected,
             'elftoolchain distinfo must cover the archive and every patch')
    return entries


def _makefile(makefile, manifest):
    try:
        text = makefile.decode('utf-8', 'strict')
    except UnicodeError as exc:
        raise SDKError('elftoolchain Makefile is not UTF-8') from exc
    text = re.sub(r'\\\r?\n[ \t]*', ' ', text)
    allowed_variables = {
        'DISTNAME', 'PKGREVISION', 'CATEGORIES', 'MASTER_SITES', 'EXTRACT_SUFX',
        'MAINTAINER', 'HOMEPAGE', 'COMMENT', 'LICENSE', 'CONFLICTS',
        'NOT_FOR_PLATFORM', 'MAKE_JOBS_SAFE', 'USE_BSD_MAKEFILE', 'USE_TOOLS',
        'GCC_REQD', 'MAKE_ENV', 'MAKE_FLAGS', 'INSTALLATION_DIRS',
    }
    allowed_includes = {'../../mk/bsd.prefs.mk', '../../mk/bsd.pkg.mk'}
    expected_condition = ('(${OPSYS} == "NetBSD" && (${EMBERBSD_PORTS:Uno:tl} == "yes" || '
                          'exists(${.PARSEDIR}/../../EMBERBSD-PKGSRC-REVISION))) || '
                          'exists(${TOOLS_CROSS_DESTDIR}/usr/share/emberbsd/ports/elftoolchain.distinfo)')
    expected_condition = re.sub(r'\s+', ' ', expected_condition)
    variables = {}
    includes = []
    in_condition = False
    saw_condition = False
    target = None

    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith('#'):
            continue
        if target is not None and not raw_line.startswith('\t'):
            target = None
        line = raw_line.strip()
        if line.startswith('.include'):
            match = re.fullmatch(r'\.include\s+"([^"]+)"', line)
            _require(match is not None and match[1] in allowed_includes,
                     'unsupported elftoolchain source include')
            includes.append(match[1])
            continue
        if line.startswith('.if '):
            _require(not in_condition and not saw_condition and
                     re.sub(r'\s+', ' ', line[4:]) == expected_condition,
                     'unsupported elftoolchain conditional')
            in_condition = saw_condition = True
            continue
        if line == '.endif':
            _require(in_condition, 'unmatched elftoolchain .endif')
            in_condition = False
            continue
        _require(not line.startswith(('.else', '.elif', '.for', '.endfor', '.undef', '.export')),
                 'unsupported elftoolchain source directive')
        if raw_line.startswith('\t'):
            _require(target == 'post-install', 'unsupported elftoolchain source command')
            continue
        target_match = re.fullmatch(r'([A-Za-z0-9_.+-]+):\s*', line)
        if target_match is not None:
            _require(target is None and not in_condition and target_match[1] == 'post-install',
                     f'unsupported elftoolchain source hook: {target_match[1]}')
            target = target_match[1]
            continue
        assignment = re.fullmatch(r'([A-Za-z0-9_.-]+)\s*([+?:!]?=)(.*)', line)
        _require(assignment is not None, 'unsupported elftoolchain Makefile source syntax')
        key, operator, value = assignment.groups()
        if key == 'PKG_FAIL_REASON':
            _require(in_condition and operator == '+=' and
                     value.startswith(' "EmberBSD base provides libelf.') and value.endswith('"'),
                     'unsupported elftoolchain package guard')
            continue
        _require(not in_condition and key in allowed_variables and operator != '!=',
                 f'unsupported elftoolchain source override: {key}')
        variables.setdefault(key, []).append((operator, value.strip()))

    _require(not in_condition and sorted(includes) ==
             ['../../mk/bsd.pkg.mk', '../../mk/bsd.prefs.mk'],
             'elftoolchain Makefile must use the standard pkgsrc includes')
    upstream = manifest['upstream']
    expected_values = {'DISTNAME': upstream['distname'], 'MASTER_SITES':
                       upstream['url'][:-len(upstream['distname'] + '.tar.bz2')],
                       'EXTRACT_SUFX': '.tar.bz2'}
    for key, value in expected_values.items():
        definitions = variables.get(key, [])
        _require(len(definitions) == 1 and definitions[0][0] == '=' and
                 definitions[0][1] == value and '$' not in value,
                 f'elftoolchain requires one literal {key} assignment')
    revisions = variables.get('PKGREVISION', [('=', '0')])
    _require(len(revisions) == 1 and revisions[0][0] == '=' and
             re.fullmatch(r'[0-9]+', revisions[0][1]) is not None,
             'unsupported elftoolchain PKGREVISION')
    return expected_values


def _recipe(ports):
    source = provenance.git_source(ports, kind='ports')
    recipe_root = Path(source['path'], RECIPE)
    makefile = _read(recipe_root / 'Makefile')
    manifest_bytes, manifest, expected_files = _manifest(source['path'])
    values = _makefile(makefile, manifest)
    distinfo = _read(recipe_root / 'distinfo')
    patches_dir = recipe_root / 'patches'
    patch_paths = sorted(patches_dir.iterdir(), key=lambda item: item.name)
    _require(patch_paths and all(re.fullmatch(r'patch-[A-Za-z0-9_.+-]+', item.name) and
                                 stat.S_ISREG(item.lstat().st_mode) for item in patch_paths),
             'unexpected elftoolchain patch entry')
    patch_names = [item.name for item in patch_paths]
    tracked = [RECIPE + '/Makefile', RECIPE + '/distinfo', MANIFEST]
    tracked.extend(RECIPE + '/patches/' + name for name in patch_names)
    process.capture(['git', '-C', source['path'], 'ls-files', '--error-unmatch', *tracked],
                    env=process.sanitized_environment())
    archive_name = manifest['upstream']['distname'] + '.tar.bz2'
    entries = _distinfo(distinfo, archive_name, patch_names)
    archive_identity = manifest['upstream']['archive']
    _require(entries['BLAKE2s', archive_name] == archive_identity['blake2s'] and
             entries['SHA512', archive_name] == archive_identity['sha512'] and
             entries['Size', archive_name] == str(archive_identity['size']),
             'elftoolchain archive manifest does not match distinfo')
    patch_rows = []
    for path in patch_paths:
        data = _read(path)
        sha1 = hashlib.sha1(b''.join(line for line in data.splitlines(keepends=True)
                                     if b'$NetBSD' not in line)).hexdigest()
        _require(sha1 == entries['SHA1', path.name],
                 f'elftoolchain patch checksum mismatch: {path.name}')
        patch_rows.append(dict(name=path.name, sha1=sha1, sha256=_sha256(data)))
    metadata = dict(
        ports_commit=source['commit'], recipe=RECIPE,
        distname=values['DISTNAME'], url=manifest['upstream']['url'],
        makefile_sha256=_sha256(makefile), distinfo_sha256=_sha256(distinfo),
        base_source_sha256=_sha256(manifest_bytes),
        upstream_svn_revision=manifest['upstream']['svn_revision'],
        upstream_archive=archive_identity, base=manifest['base'], patches=patch_rows)
    return source, metadata, entries, distinfo, expected_files


def _validate_files(root, expected):
    expected_files = set(expected)
    expected_dirs = {'common', 'libelf'}
    for name in expected_files:
        parts = name.split('/')
        expected_dirs.update('/'.join(parts[:index]) for index in range(2, len(parts)))
    actual_files = set()
    actual_dirs = set()
    for top in ('common', 'libelf'):
        top_path = Path(root, top)
        paths.real_directory(top_path)
        for directory, names, files in os.walk(top_path, followlinks=False):
            relative_dir = Path(directory).relative_to(root).as_posix()
            actual_dirs.add(relative_dir)
            for name in list(names):
                path = Path(directory, name)
                info = path.lstat()
                _require(stat.S_ISDIR(info.st_mode),
                         f'non-directory in elftoolchain source tree: {path}')
                _require(stat.S_IMODE(info.st_mode) == 0o755,
                         f'wrong elftoolchain source directory mode: {path}')
            for name in files:
                path = Path(directory, name)
                info = path.lstat()
                _require(stat.S_ISREG(info.st_mode),
                         f'non-regular elftoolchain source file: {path}')
                relative = path.relative_to(root).as_posix()
                actual_files.add(relative)
                row = expected.get(relative)
                _require(row is not None, f'unexpected elftoolchain source file: {relative}')
                _require(stat.S_IMODE(info.st_mode) == row['mode'] and
                         info.st_size == row['size'] and
                         inventory.sha256_file(path) == row['sha256'],
                         f'elftoolchain source content mismatch: {relative}')
    _require(actual_files == expected_files and actual_dirs == expected_dirs,
             'elftoolchain source file set does not match the base-source manifest')


def _tree_hash(root):
    return _sha256(jsonio.json_bytes(inventory.snapshot_tree(str(root), exclude=(RECEIPT,))))


def validate_identity(record):
    """Validate the portable source receipt without its original host paths."""
    _require(isinstance(record, dict) and set(record) ==
             {'format', 'version', 'recipe', 'archive', 'files', 'tree_sha256'} and
             record['format'] == 'emberbsd-libelf-source' and
             type(record['version']) is int and record['version'] == 1,
             'invalid or unsupported libelf source receipt')
    recipe = record['recipe']
    fields = {'ports_commit', 'recipe', 'distname', 'url', 'makefile_sha256',
              'distinfo_sha256', 'base_source_sha256', 'upstream_svn_revision',
              'upstream_archive', 'base', 'patches'}
    _require(isinstance(recipe, dict) and set(recipe) == fields and
             _hash(recipe['ports_commit'], 40) and recipe['recipe'] == RECIPE and
             recipe['distname'] == UPSTREAM_NAME and recipe['url'] == UPSTREAM_URL and
             type(recipe['upstream_svn_revision']) is int and
             recipe['upstream_svn_revision'] == UPSTREAM_SVN_REVISION and
             all(_hash(recipe[key], 64) for key in
                 ('makefile_sha256', 'distinfo_sha256', 'base_source_sha256')) and
             _hash(record['tree_sha256'], 64), 'invalid libelf recipe identity')
    base = recipe['base']
    _require(isinstance(base, dict) and set(base) ==
             {'repository', 'commit', 'prefix', 'trees'} and
             base['repository'] == BASE_REPOSITORY and _hash(base['commit'], 40) and
             base['prefix'] == BASE_PREFIX and base['trees'] == BASE_TREE_IDS,
             'invalid libelf base-source receipt')
    archive = recipe['upstream_archive']
    _require(isinstance(archive, dict) and set(archive) ==
             {'size', 'sha256', 'sha512', 'blake2s'} and
             type(archive['size']) is int and 0 < archive['size'] <= MAX_ARCHIVE_SIZE and
             _hash(archive['sha256'], 64) and _hash(archive['sha512'], 128) and
             _hash(archive['blake2s'], 64) and record['archive'] == archive,
             'invalid libelf upstream archive receipt')
    patches = recipe['patches']
    _require(isinstance(patches, list) and patches,
             'missing libelf patch receipt')
    names = []
    for row in patches:
        _require(isinstance(row, dict) and set(row) == {'name', 'sha1', 'sha256'} and
                 isinstance(row['name'], str) and
                 re.fullmatch(r'patch-[A-Za-z0-9_.+-]+', row['name']) is not None and
                 _hash(row['sha1'], 40) and _hash(row['sha256'], 64),
                 'invalid libelf patch identity')
        names.append(row['name'])
    _require(names == sorted(set(names)), 'duplicate or unsorted libelf patches')
    files = record['files']
    _require(isinstance(files, dict) and files,
             'missing libelf prepared file inventory')
    folded = set()
    for name, row in files.items():
        _require(_source_path(name) and name.casefold() not in folded,
                 f'unsafe or colliding libelf receipt path: {name!r}')
        folded.add(name.casefold())
        _require(isinstance(row, dict) and set(row) == {'size', 'mode', 'sha256'} and
                 type(row['size']) is int and row['size'] >= 0 and
                 type(row['mode']) is int and row['mode'] in (0o644, 0o755) and
                 _hash(row['sha256'], 64), f'invalid libelf file identity: {name}')
    _require(set(files) >= PACKAGING_OVERRIDES and
             any(name.startswith('common/') for name in files) and
             any(name.startswith('libelf/') for name in files),
             'libelf receipt omits required source paths')
    return record


def validate_source(root, *, ports=None):
    """Validate the sibling common/libelf snapshot and optional clean recipe pin."""
    root = Path(paths.canonical_existing(root))
    record = validate_identity(jsonio.read_json(root / RECEIPT))
    _validate_files(root, record['files'])
    distinfo = Path(root, 'ports.distinfo')
    _require(inventory.sha256_file(distinfo) == record['recipe']['distinfo_sha256'],
             'libelf ports.distinfo changed')
    _require(_tree_hash(root) == record['tree_sha256'], 'libelf prepared source tree changed')
    receipt_info = paths.regular_file(root / RECEIPT)
    _require(stat.S_IMODE(receipt_info.st_mode) in (0o444, 0o644),
             'libelf source receipt mode is not read-only or Git-normalized')
    if ports is not None:
        _, metadata, _, _, expected = _recipe(ports)
        _require(metadata == record['recipe'] and expected == record['files'],
                 'libelf source does not match selected clean Ports recipe')
    return record


def prepare_source(ports, archive, output, *, patch_tool=None):
    """Verify, patch and publish a new common/libelf root without replacement."""
    require_directory_publication()
    source, metadata, entries, distinfo, expected = _recipe(ports)
    archive_path = paths.canonical_existing(archive)
    archive_name = metadata['distname'] + '.tar.bz2'
    data = _read(archive_path)
    _require(len(data) <= MAX_ARCHIVE_SIZE, 'elftoolchain archive exceeds the size limit')
    hashes = sourceprep.archive_hashes(data)
    _require(hashes == metadata['upstream_archive'] and
             entries['SHA512', archive_name] == hashes['sha512'] and
             entries['BLAKE2s', archive_name] == hashes['blake2s'] and
             entries['Size', archive_name] == str(hashes['size']),
             'elftoolchain archive checksum mismatch')
    output = Path(paths.path_string(output))
    output = Path(paths.canonical_existing(output.parent), output.name)
    _require(not os.path.lexists(output), 'libelf output already exists')
    paths.reject_overlap(str(output), source['path'])
    paths.reject_overlap(str(output), archive_path)
    if patch_tool is None:
        patch_tool = shutil.which('patch')
        _require(patch_tool is not None, 'libelf preparation requires patch')
    else:
        try:
            selected = os.fspath(patch_tool)
        except TypeError as exc:
            raise SDKError('libelf patch tool must be an absolute executable path') from exc
        _require(isinstance(selected, str) and os.path.isabs(selected),
                 'libelf patch tool must be an absolute executable path')
        try:
            patch_tool = paths.canonical_existing(selected)
            paths.regular_file(patch_tool)
        except SDKError as exc:
            raise SDKError(f'invalid libelf patch tool: {exc}') from exc
        _require(os.access(patch_tool, os.X_OK), 'libelf patch tool is not executable')
    with tempfile.TemporaryDirectory(prefix='.libelf-', dir=output.parent) as temporary:
        temporary = Path(temporary)
        staged_archive = temporary / 'source.tar.bz2'
        staged_archive.write_bytes(data)
        sourceprep.extract_archive(staged_archive, temporary, metadata['distname'], 'elftoolchain')
        upstream_root = temporary / metadata['distname']
        for row in metadata['patches']:
            patch_path = Path(source['path'], RECIPE, 'patches', row['name'])
            patch_data = _read(patch_path)
            _require(_sha256(patch_data) == row['sha256'],
                     f'elftoolchain patch changed before application: {row["name"]}')
            staged_patch = temporary / row['name']
            staged_patch.write_bytes(patch_data)
            sourceprep.apply_patch(
                patch_tool, staged_patch, patch_data, upstream_root, 'elftoolchain',
                patch_args=('-p0', '-F0', '-f', '-E', '-V', 'none'),
                backup_directory=temporary / ('backup-' + row['name']),
                allow_dev_null=True)
        _validate_files(upstream_root, expected)
        prepared = temporary / 'prepared'
        prepared.mkdir()
        for name in ('common', 'libelf'):
            shutil.copytree(upstream_root / name, prepared / name)
        (prepared / 'ports.distinfo').write_bytes(distinfo)
        record = dict(format='emberbsd-libelf-source', version=1,
                      recipe=metadata, archive=hashes, files=expected,
                      tree_sha256=_tree_hash(prepared))
        jsonio.write_json(prepared / RECEIPT, record, mode=0o444)
        validate_source(prepared, ports=source['path'])
        _require(inventory.sha256_file(archive_path) == hashes['sha256'],
                 'elftoolchain archive changed during preparation')
        publish_directory_noreplace(prepared, output)
    return record
