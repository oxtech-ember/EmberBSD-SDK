# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Prepare the canonical Ports libedit source for native OS build rules."""

import hashlib
import os
from pathlib import Path
import re
import shutil
import tempfile

from . import elf, inventory, jsonio, paths, process, provenance, sourceprep
from .errors import SDKError
from .publication import publish_directory_noreplace, require_directory_publication

LIBEDIT_SOURCE_API = 1
RECIPE = 'pkgsrc/devel/editline'
RECEIPT = 'libedit-source.json'


def _require(condition, message):
    if not condition:
        raise SDKError(message)


def _read(path):
    handle, before = paths.open_regular(path)
    with handle:
        data = handle.read()
    paths.verify_stable(path, before)
    return data


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _recipe(ports, *, git='git'):
    source = provenance.git_source(ports, kind='ports', git=git)
    root = Path(source['path'], RECIPE)
    makefile = _read(root / 'Makefile')
    # Version 1 supports the normal distfile + patches pipeline only. Custom
    # extraction, patch hooks and SUBST transforms require a new shared contract.
    text = makefile.decode()
    includes = {'../../mk/bsd.prefs.mk', '../../devel/ncurses/buildlink3.mk', '../../mk/bsd.pkg.mk'}
    configure_commands = {'${CP} ${FILESDIR}/err.h ${BUILDLINK_DIR}/include', 'cd ${WRKSRC} && autoreconf -fi'}
    variables = {'DISTNAME', 'PKGNAME', 'CATEGORIES', 'MASTER_SITES', 'MAINTAINER', 'HOMEPAGE',
                 'COMMENT', 'LICENSE', 'PKGCONFIG_OVERRIDE', 'USE_LIBTOOL', 'GNU_CONFIGURE',
                 'USE_LANGUAGES', 'USE_TOOLS', 'GCC_REQD', 'BUILDLINK_API_DEPENDS.ncurses', 'PKG_FAIL_REASON'}
    stage = None
    for line in text.splitlines():
        if not line or line.lstrip().startswith('#'):
            continue
        if line.startswith('\t'):
            if stage == 'pre-configure':
                _require(line.strip() in configure_commands, 'unsupported libedit source command before configure')
            continue
        if re.match(r'\s*\.(?:s?include|-include)', line):
            include = re.fullmatch(r'\s*\.include \"([^\"]+)\"\s*', line)
            _require(include is not None and include[1] in includes, 'unsupported libedit source include')
            continue
        assignment = re.match(r'\s*([A-Za-z0-9_.+${}:-]+)\s*[+?:!]?=', line)
        if assignment:
            key = assignment[1].rstrip('+')
            _require(key in variables,
                     f'unsupported libedit source override: {key}')
            continue
        target = re.match(r'\s*([^\s:]+)\s*:(?![=])', line)
        if target:
            stage = target[1]
            _require(target[1] in ('pre-configure', 'post-install'),
                     f'unsupported libedit source hook: {target[1]}')
    values = {}
    for key, pattern in (('DISTNAME', r'libedit-[0-9]{8}-[0-9]+\.[0-9]+'),
                         ('MASTER_SITES', r'https://thrysoee\.dk/editline/')):
        definitions = re.findall(r'^' + key + r'\s*([+?:!]?=)(.*)$', makefile.decode(), re.M)
        _require(len(definitions) == 1 and definitions[0][0] == '=' and
                 re.fullmatch(pattern, definitions[0][1].strip()) is not None,
                 f'libedit requires one literal {key} assignment')
        values[key] = definitions[0][1].strip()
    distinfo = _read(root / 'distinfo')
    entries = {}
    for line in distinfo.decode().splitlines():
        if not line or line.startswith('$NetBSD'):
            continue
        match = re.fullmatch(r'(BLAKE2s|SHA512|Size|SHA1) \(([^/()]+)\) = ([a-f0-9]+)( bytes)?', line)
        _require(match is not None, 'unsupported libedit distinfo entry')
        kind, name, value, suffix = match.groups()
        _require((kind == 'Size') == (suffix == ' bytes'), 'invalid distinfo size suffix')
        _require((kind, name) not in entries, 'duplicate libedit distinfo entry')
        entries[kind, name] = value
    archive_name = values['DISTNAME'] + '.tar.gz'
    patches = sorted(root.joinpath('patches').iterdir())
    _require(patches and all(p.name.startswith('patch-') for p in patches), 'unexpected libedit patch entry')
    process.capture(['git', '-C', source['path'], 'ls-files', '--error-unmatch',
                     RECIPE + '/Makefile', RECIPE + '/distinfo',
                     *[RECIPE + '/patches/' + p.name for p in patches]],
                    env=process.git_environment())
    expected = {('BLAKE2s', archive_name), ('SHA512', archive_name), ('Size', archive_name)}
    expected.update(('SHA1', p.name) for p in patches)
    _require(set(entries) == expected, 'libedit distinfo must cover the archive and every patch')
    patch_rows = []
    for patch in patches:
        data = _read(patch)
        # pkgsrc mk/checksum/checksum filters complete RCS ID lines.
        sha1 = hashlib.sha1(b''.join(line for line in data.splitlines(keepends=True) if b'$NetBSD' not in line)).hexdigest()
        _require(sha1 == entries['SHA1', patch.name], f'libedit patch checksum mismatch: {patch.name}')
        patch_rows.append(dict(name=patch.name, sha1=sha1, sha256=_digest(data)))
    metadata = dict(ports_commit=source['commit'], recipe=RECIPE, distname=values['DISTNAME'],
                    url=values['MASTER_SITES'] + archive_name,
                    makefile_sha256=_digest(makefile), distinfo_sha256=_digest(distinfo), patches=patch_rows)
    return source, metadata, entries, distinfo


def validate_identity(record):
    """Check the portable receipt schema without accessing its original host."""
    _require(isinstance(record, dict) and set(record) == {'format', 'version', 'recipe', 'archive', 'tree_sha256'},
             'invalid libedit source receipt fields')
    _require(record['format'] == 'emberbsd-libedit-source' and type(record['version']) is int and record['version'] == 1,
             'unsupported libedit source receipt')
    recipe = record['recipe']
    _require(isinstance(recipe, dict) and set(recipe) == {'ports_commit', 'recipe', 'distname', 'url', 'makefile_sha256', 'distinfo_sha256', 'patches'}, 'invalid libedit recipe receipt')
    def hashed(value, size):
        return isinstance(value, str) and re.fullmatch('[a-f0-9]{' + str(size) + '}', value) is not None
    _require(hashed(recipe['ports_commit'], 40) and recipe['recipe'] == RECIPE and
             isinstance(recipe['distname'], str) and
             re.fullmatch(r'libedit-[0-9]{8}-[0-9]+\.[0-9]+', recipe['distname']) is not None and
             recipe['url'] == 'https://thrysoee.dk/editline/' + recipe['distname'] + '.tar.gz', 'invalid libedit recipe identity')
    _require(all(hashed(recipe[k], 64) for k in ('makefile_sha256', 'distinfo_sha256')) and hashed(record['tree_sha256'], 64), 'invalid libedit identity hash')
    archive = record['archive']
    _require(isinstance(archive, dict) and set(archive) == {'sha256', 'sha512', 'blake2s', 'size'} and
             all(hashed(archive[k], n) for k, n in (('sha256', 64), ('sha512', 128), ('blake2s', 64))) and
             type(archive['size']) is int and archive['size'] > 0, 'invalid libedit archive identity')
    patches = recipe['patches']
    _require(isinstance(patches, list) and patches, 'missing libedit patches')
    names = []
    for patch in patches:
        _require(isinstance(patch, dict) and set(patch) == {'name', 'sha1', 'sha256'} and
                 isinstance(patch['name'], str) and
                 re.fullmatch(r'patch-[A-Za-z0-9_.+-]+', patch['name']) is not None and
                 hashed(patch['sha1'], 40) and hashed(patch['sha256'], 64), 'invalid libedit patch identity')
        names.append(patch['name'])
    _require(names == sorted(set(names)), 'duplicate or unsorted libedit patch receipt')
    return record


def _tree_hash(root):
    return _digest(jsonio.json_bytes(inventory.snapshot_tree(str(root), exclude=(RECEIPT,))))


def validate_source(root, *, ports=None, git='git', content=True):
    """Verify the receipt, the source contents and, when supplied, the current clean recipe.

    With content false the tree is identified by its preparation receipt and
    not read again; the recipe is still compared with the Ports checkout."""
    root = Path(paths.canonical_existing(root))
    record = validate_identity(jsonio.read_json(root / RECEIPT))
    for name in ('src', 'doc'):
        paths.real_directory(root / name)
    _require(inventory.sha256_file(root / 'ports.distinfo') == record['recipe']['distinfo_sha256'], 'libedit distinfo changed')
    _require(not content or _tree_hash(root) == record['tree_sha256'], 'libedit source tree changed')
    if ports is not None:
        _, metadata, entries, _ = _recipe(ports, git=git)
        # Retain the preparation commit as provenance. Unrelated Ports commits
        # do not change the recipe, archive or patched source inputs.
        _require(all(record['recipe'][key] == value for key, value in metadata.items()
                     if key != 'ports_commit'),
                 'libedit source does not match selected clean Ports recipe')
        name = metadata['distname'] + '.tar.gz'
        _require(record['archive']['sha512'] == entries['SHA512', name] and
                 record['archive']['blake2s'] == entries['BLAKE2s', name] and
                 str(record['archive']['size']) == entries['Size', name],
                 'libedit archive receipt does not match Ports distinfo')
    return record


def prepare_source(ports, archive, output):
    """Validate, unpack, patch and publish a new prepared root. Never replace it."""
    require_directory_publication()
    source, metadata, entries, distinfo = _recipe(ports)
    archive = paths.canonical_existing(archive)
    name = metadata['distname'] + '.tar.gz'
    data = _read(archive)
    hashes = sourceprep.archive_hashes(data)
    _require(hashes['sha512'] == entries['SHA512', name] and hashes['blake2s'] == entries['BLAKE2s', name] and
             str(hashes['size']) == entries['Size', name], 'libedit archive checksum mismatch')
    output = Path(paths.path_string(output))
    output = Path(paths.canonical_existing(output.parent), output.name)
    _require(not os.path.lexists(output), 'libedit output already exists')
    paths.reject_overlap(str(output), source['path'])
    patch_tool = shutil.which('patch')
    _require(patch_tool is not None, 'libedit preparation requires patch')
    with tempfile.TemporaryDirectory(prefix='.libedit-', dir=output.parent) as temporary:
        temporary = Path(temporary)
        # Extract the verified bytes, not a reopened mutable input archive.
        copied = temporary / 'source.tar.gz'
        copied.write_bytes(data)
        sourceprep.extract_archive(copied, temporary, metadata['distname'], 'libedit')
        root = temporary / metadata['distname']
        for row in metadata['patches']:
            patch = Path(source['path'], RECIPE, 'patches', row['name'])
            patch_data = _read(patch)
            _require(_digest(patch_data) == row['sha256'], 'libedit patch changed before application')
            staged_patch = temporary / row['name']
            staged_patch.write_bytes(patch_data)
            sourceprep.apply_patch(
                patch_tool, staged_patch, patch_data, root, 'libedit',
                patch_args=('-p0', '-F0', '--posix', '--batch', '--forward',
                            '--no-backup-if-mismatch'))
        _require(not any(p.name.endswith(('.orig', '.rej')) for p in root.rglob('*')), 'libedit patch left backup or reject files')
        (root / 'ports.distinfo').write_bytes(distinfo)
        record = dict(format='emberbsd-libedit-source', version=1, recipe=metadata,
                      archive=hashes, tree_sha256=_tree_hash(root))
        jsonio.write_json(root / RECEIPT, record, mode=0o444)
        validate_source(root, ports=source['path'])
        _require(inventory.sha256_file(archive) == hashes['sha256'], 'libedit archive changed during preparation')
        publish_directory_noreplace(root, output)
    return record


def validate_installation(root, record, machine):
    """Require one native SONAME-3 provider and the selected Ports identity."""
    root = Path(paths.canonical_existing(root))
    validate_identity(record)
    marker = inventory.required_file(str(root), 'usr/share/emberbsd/ports/editline.distinfo')
    _require(inventory.sha256_file(marker) == record['recipe']['distinfo_sha256'],
             'installed libedit distinfo does not match prepared source')
    for name in ('histedit.h', 'readline/readline.h', 'readline/history.h'):
        inventory.required_file(str(root), 'usr/include/' + name)
    _require(paths.canonical_existing(root / 'usr/include/readline/history.h') ==
             paths.canonical_existing(root / 'usr/include/readline/readline.h'),
             'installed libedit history header must select the readline header')
    library = Path(inventory.required_file(str(root), 'usr/lib/libedit.so'))
    _require(library.parent in (root / 'lib', root / 'usr/lib') and
             re.fullmatch(r'libedit\.so\.3(?:\.[0-9]+)?', library.name) is not None,
             'installed libedit linker name does not select the native ABI-3 library')
    runtime = inventory.required_file(str(root), str(library.parent.relative_to(root) / 'libedit.so.3'))
    _require(runtime == str(library), 'installed libedit runtime link selects a different provider')
    facts = elf.read_elf_dynamic(library, machine=machine)
    _require(facts['type'] == 3 and facts['soname'] == 'libedit.so.3', 'installed libedit must have native SONAME libedit.so.3')
    for directory in (root / 'lib', root / 'usr/lib', root / 'usr/pkg/lib'):
        for candidate in directory.glob('libedit.*'):
            if directory == root / 'usr/pkg/lib' or candidate.name.startswith('libedit.so'):
                _require(directory != root / 'usr/pkg/lib' and
                         candidate.name in ('libedit.so', 'libedit.so.3', library.name) and
                         paths.canonical_existing(candidate) == str(library),
                         'installed libedit has a second runtime provider')
    return dict(distinfo_sha256=inventory.sha256_file(marker),
                library=str(library.relative_to(root)), library_sha256=inventory.sha256_file(library),
                machine=machine, soname=facts['soname'])
