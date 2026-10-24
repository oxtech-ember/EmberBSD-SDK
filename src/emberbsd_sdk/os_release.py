# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD ember/release/{kit,sysroot-kit}.rb.
# AI-assisted Python migration. See docs/os-release.md for the source baseline.
"""OS release composition and immutable receipt validation; no build on import."""

import argparse
import gzip
import hashlib
import os
from pathlib import Path
import re
import shutil
import sys

from .errors import SDKError
from .inventory import sha256_file
from .jsonio import read_json, write_json
from .paths import regular_file, real_directory
from .process import capture, sanitized_environment

REPOSITORY = 'oxtech-ember/EmberBSD'
SETS = 'base comp etc games gpufw kern-GENERIC man manhtml misc modules rescue tests text'.split()
OPTIONAL_SETS = 'base32 comp32 debug debug32 debug64'.split()
VM_CHECKS = ('local_install https_install first_boot_without_media reboot_persistence '
             'tls_reject_unknown_ca tls_reject_wrong_name tls_reject_missing_anchors '
             'http_404_rejected github_redirect').split()
SYSROOT_SETS = ['base.tar.xz', 'comp.tar.xz']
KERNEL = 'netbsd-EMBER64.gz'
EVIDENCE = 'target-acceptance.log'
SYSROOT_REQUIRED = SYSROOT_SETS + [KERNEL, 'build-profile.json', 'source-export.sha256', 'release-notes.md']
SYSROOT_CHECKS = 'libc_aliases cross_c11 cross_cxx20 target_c11 target_cxx20 base_loader_smoke fork_cas'.split()
# Historical schema 1 is an immutable receipt format, not a current build profile.
LEGACY_FLAGS = ('USETOOLS=yes HAVE_GCC=12 TOOLCHAIN_MISSING=no MKGCC=yes MKGCCCMDS=no MKCXX=yes '
    'MKLIBSTDCXX=yes MKLINKLIB=yes MKSTATICLIB=yes MKPIC=yes MKPICLIB=yes MKDYNAMICROOT=yes '
    'MKFIRMWARE=yes MKKMOD=no MKX11=no MKRUMP=no MKCOMPAT=no MKLLVM=no MKLLVMRT=no '
    'MKDEBUG=no MKDEBUGLIB=no MKDEBUGKERNEL=no MKATF=no MKKYUA=no MKMAN=no MKDOC=no '
    'MKINFO=no MKHTML=no MKLINT=no MKPROFILE=no MKCVS=no MKGROFF=no MKGDB=no MKBINUTILS=no '
    'MKREPRO=yes RELEASEMACHINEDIR=evbarm-aarch64 USE_XZ_SETS=yes').split() + ['MAKETARSETS=base comp']


def check(condition, message):
    if not condition:
        raise SDKError(message)


def full_hash(value, length=64):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{' + str(length) + '}', value) is not None


def command(*argv):
    return capture([str(arg) for arg in argv], env=sanitized_environment()).stdout


def tag(sha, profile):
    return f'build-{sha}-' + ('amd64-no-x11' if profile == 'amd64' else 'aarch64-sysroot')


def product_version(value):
    check(isinstance(value, str) and re.fullmatch(r'[0-9][A-Za-z0-9]*(?:[.\-+][A-Za-z0-9]+)*', value) and
          not value.endswith('.lock'), 'Invalid product version')
    return value


def source_version(text):
    lines = [line for line in text.splitlines() if line.startswith('EMBER_VERSION?=')]
    check(len(lines) == 1, 'Source must contain exactly one canonical EMBER_VERSION')
    match = re.fullmatch(r'EMBER_VERSION\?=[ \t]*([^\s#]+)[ \t]*(?:#.*)?', lines[0])
    check(match is not None, 'Invalid canonical source version assignment')
    return product_version(match[1])


def archive_version(archive):
    return source_version(extract_bytes(archive, 'share/mk/bsd.own.mk').decode('utf-8'))


def images(version):
    version = product_version(version)
    return [f'EmberBSD-{version}-amd64.iso', f'EmberBSD-{version}-amd64-install.img.gz']


def build_version(build, profile):
    archive = build / 'source.tar.gz'
    if archive.exists():
        check(sha256_file(archive) == profile['source_sha256'], 'Source export differs')
        return archive_version(archive)
    # Old receipts can outlive their source export. Only their exact, agreeing
    # command-line version overrides provide this historical fallback.
    commands = profile['commands']
    values = [[a.split('=', 1)[1] for i, a in enumerate(argv) if i > 0 and argv[i - 1] == '-V' and
               a.startswith('EMBER_VERSION=')] for argv in commands]
    check(profile.get('schema') == 1 and len(values) == 2 and len(values[0]) == 1 and values[0] == values[1],
          'Source archive is required to read the product version')
    return product_version(values[0][0])


def checksum_text(directory, names):
    return ''.join(f'SHA256 ({name}) = {sha256_file(Path(directory) / name)}\n' for name in names)


def asset(directory, name):
    path = Path(directory) / name
    regular_file(path)
    return dict(name=name, size=path.stat().st_size, sha256=sha256_file(path))


def seal(directory, manifest):
    write_json(Path(directory) / 'manifest.json', manifest)
    (Path(directory) / 'SHA256').write_text(checksum_text(directory, [f['name'] for f in manifest['files']] + ['manifest.json']))


def archive_names(path):
    # bsdtar also reads the ISO filesystem; no files are extracted into a tree.
    names = command('tar', '-tf', path).decode('utf-8').splitlines()
    check(names and all(not n.startswith('/') and '..' not in n.split('/') for n in names), 'Unsafe or empty release-set paths')
    return names


def extract_bytes(archive, member):
    return command('tar', '-xOf', archive, member)


def extract_file(archive, member, destination):
    # Stream large set archives rather than holding an entire image in memory.
    with open(destination, 'xb') as output:
        import subprocess
        result = subprocess.run(['tar', '-xOf', str(archive), member], stdout=output,
                                stderr=subprocess.PIPE, env=sanitized_environment(), check=False)
    check(result.returncode == 0, f'Cannot extract {member}: {result.stderr.decode("utf-8", "replace")}')


def check_native_compiler(archive):
    names = [n.removeprefix('./') for n in archive_names(archive)]
    forbidden = {'usr/bin/cc', 'usr/bin/c++', 'usr/bin/gcc', 'usr/bin/g++', 'usr/bin/cpp'}
    check(not forbidden.intersection(names) and not any(re.fullmatch(r'usr/libexec/(?:gcc/.*?/)?cc1(?:plus|obj)?', n) for n in names),
          'comp contains native bootstrap compiler commands')


def elf_aarch64(data):
    return data[:6] == b'\x7fELF\x02\x01' and data[18:20] == b'\xb7\x00'


def set_content_check(directory):
    base, comp = (Path(directory) / name for name in SYSROOT_SETS)
    base_names, comp_names = archive_names(base), archive_names(comp)
    required = ('usr/include/stdio.h usr/include/stdlib.h usr/include/pthread.h usr/lib/crt0.o '
                'usr/lib/crti.o usr/lib/crtn.o usr/lib/libc.a usr/lib/libm.a usr/lib/libpthread.a').split()
    normalized = {n.removeprefix('./'): n for n in comp_names}
    check(set(required).issubset(normalized), 'comp lacks required headers, startup objects or link libraries')
    check_native_compiler(comp)
    check(elf_aarch64(extract_bytes(comp, normalized['usr/lib/crt0.o'])), 'Startup object is not AArch64 ELF64 little-endian')
    loader = next((n for n in base_names if n.removeprefix('./') == 'libexec/ld.elf_so'), None)
    check(loader and elf_aarch64(extract_bytes(base, loader)), 'Base lacks the AArch64 ELF loader')
    libc = [n for n in base_names if re.fullmatch(r'(?:\./)?(?:usr/)?lib/libc\.so\.12\.\d+', n)]
    check(libc and any(elf_aarch64(extract_bytes(base, n)) for n in libc), 'Base lacks an AArch64 libc provider')


def kernel_check(path, sha):
    with gzip.open(path, 'rb') as handle:
        data = handle.read()
    check(elf_aarch64(data), 'Test kernel is not AArch64 ELF64 little-endian')
    check(f'EmberBSD:{sha}\n\0'.encode() in data, 'Test kernel lacks the full source build identity')


def legacy_profile_check(profile, sha, version):
    product_version(version)
    check(type(profile['schema']) is int and [profile[k] for k in ('schema', 'status', 'machine', 'architecture', 'kernel_config')] ==
          [1, 'built', 'evbarm', 'aarch64', 'EMBER64'], 'Incomplete or wrong AArch64 build profile')
    check(profile['source_repository'] == 'https://github.com/' + REPOSITORY and profile['source_commit'] == sha,
          'Build receipt belongs to another source')
    check(full_hash(profile['source_sha256']), 'Missing source export hash')
    check(type(profile['source_timestamp']) is int and profile['source_timestamp'] > 0, 'Invalid source timestamp')
    check(isinstance(profile['target_cc_version'], str) and '12.5.0' in profile['target_cc_version'], 'Unrecorded bootstrap compiler')
    commands = profile['commands']
    check(isinstance(commands, list) and len(commands) == 2, 'Missing full build commands')
    for index, argv in enumerate(commands):
        check(isinstance(argv, list) and all(isinstance(a, str) for a in argv), 'Invalid build argv')
        environment = profile['build_environment']
        check(set(environment) == {'PATH', 'HOME', 'TMPDIR', 'LC_ALL', 'MAKECONF'} and environment['LC_ALL'] == 'C' and
              environment['MAKECONF'] == '/dev/null', 'Unexpected build environment')
        prefix = ['env', '-i'] + [f'{n}={environment[n]}' for n in ('PATH', 'HOME', 'TMPDIR', 'LC_ALL', 'MAKECONF')] + ['sh', './build.sh']
        check(argv[:len(prefix)] == prefix, 'Build command contains extra inherited controls')
        args = argv[len(prefix):]
        check(args[:6] == ['-U', '-u', '-m', 'evbarm', '-a', 'aarch64'] and len(args) > 16 and
              re.fullmatch('-j[1-9][0-9]*', args[6]), 'Unexpected build options')
        check([args[i] for i in (7, 9, 11, 13)] == ['-O', '-T', '-D', '-R'], 'Unexpected output-directory options')
        dirs = [args[i] for i in (8, 10, 12, 14)]
        check(all(d.startswith('/') and not re.search(r'\s', d) for d in dirs), 'Invalid output directories')
        output = str(Path(dirs[0]).parent)
        check(dirs == [output + '/' + n for n in ('obj', 'tools', 'dest', 'release')], 'Mixed output directories')
        controls = args[15:-2]
        overrides = [v for v in controls[1::2] if v.startswith('EMBER_VERSION=')]
        check(overrides in ([], ['EMBER_VERSION=' + version]), 'Build command selects another product version')
        expected = LEGACY_FLAGS + overrides + [f'MKREPRO_TIMESTAMP={profile["source_timestamp"]}', f'EMBER_BUILD_ID={sha}', f'BUILDINFO=EmberBSD:{sha}',
                                  f'HOST_CC={profile["host"]["cc"]}', f'HOST_CXX={profile["host"]["cxx"]}', f'KERNOBJDIR={dirs[0]}/kernels']
        check(len(controls) == 2 * len(expected) and controls[::2] == ['-V'] * len(expected), 'Unexpected build operations or controls')
        check(sorted(controls[1::2]) == sorted(expected), 'Build command does not select the exact required profile')
        check(args[-2:] == (['tools', 'kernel=EMBER64'] if index == 0 else ['distribution', 'sets']), 'Incomplete build stages')


def profile_check(profile, sha, version, target='aarch64'):
    if profile.get('schema') == 1 and target == 'aarch64':
        legacy_profile_check(profile, sha, version)
    else:
        from .os_release_builder import validate_profile
        validate_profile(profile, sha, target)
        check(profile['product_version'] == version, 'Manifest/source and build product versions differ')


def require_qualification(manifest, profile):
    q = manifest['qualification']
    check(isinstance(q, dict) and q.get('source_commit') == manifest['source_commit'], 'A matching qualification is required')
    files = {f['name']: f for f in manifest['files']}
    if profile == 'amd64':
        image_names = images(manifest['product_version'])
        bindings = {'image_sha256': image_names[0], 'usb_image_sha256': image_names[1]}
        check(q.get('installed_kernel_sha256') == manifest['components']['kernel']['sha256'], 'Qualification has another kernel')
        checks = VM_CHECKS
    else:
        bindings = {'base_sha256': SYSROOT_SETS[0], 'comp_sha256': SYSROOT_SETS[1], 'kernel_gzip_sha256': KERNEL}
        checks = SYSROOT_CHECKS
        check(isinstance(q.get('consumer_compiler'), str) and '16.2.0' in q['consumer_compiler'], 'Qualification must identify actual GCC16 consumers')
        check(q.get('execution') == 'EmberBSD aarch64 full-system QEMU TCG', 'Unrecorded target execution platform')
        check(EVIDENCE in files and q.get('evidence_sha256', {}).get(EVIDENCE) == files[EVIDENCE]['sha256'], 'Qualification lacks bound target evidence')
    for field, name in bindings.items():
        check(q.get(field) == files[name]['sha256'], 'Qualification has another ' + name)
    check(all(q.get('checks', {}).get(n) is True for n in checks), 'Incomplete target qualification')


def _verify(directory, profile):
    directory = Path(directory)
    real_directory(directory)
    for name in ('manifest.json', 'SHA256'):
        regular_file(directory / name)
    m = read_json(directory / 'manifest.json')
    sha = m['source_commit']
    check(full_hash(sha, 40), 'Invalid full source commit')
    check(m['repository'] == REPOSITORY and m['release_tag'] == tag(sha, profile), 'Wrong repository or release tag')
    version = product_version(m['product_version'])
    check(m['base_version'] == '11.0', 'Wrong base version')
    modern = m['schema'] in ('emberbsd.aarch64.sysroot.v2', 'emberbsd.amd64.release.v2')
    if profile == 'amd64':
        check(((type(m['schema']) is int and m['schema'] == 1) or m['schema'] == 'emberbsd.amd64.release.v2') and m['architecture'] == 'amd64', 'Wrong installation identity')
        check([m['profile'][n] for n in ('MKX11', 'MKRUMP', 'MKGCCCMDS', 'KERNEL_SETS')] == ['no', 'no', 'no', 'GENERIC'], 'Wrong release profile')
        check(full_hash(m['source_archive_sha256']), 'Missing source archive hash')
        for name in ('image', 'kernel', 'modules', 'base'):
            check(m['components'][name]['source_commit'] == sha, f'Mixed {name} source')
        if 'image_checker' in m:
            checker = m['image_checker']
            check(checker['repository'] == REPOSITORY and full_hash(checker['source_commit'], 40) and
                  checker['path'] == 'ember/tests/installer-image.py' and full_hash(checker['sha256']), 'Invalid image checker provenance')
        kernel = m['components']['kernel']
        check(kernel['buildinfo'] == f'EmberBSD:{sha}' and full_hash(kernel['sha256']), 'Invalid kernel identity')
        required = images(version) + ['release-notes.md'] + [n + '.tar.xz' for n in SETS]
        if modern:
            required += ['build-profile.json', 'source-export.sha256']
        allowed = required + [n + '.tar.xz' for n in OPTIONAL_SETS] + ['SHA512', 'qualification.json']
    else:
        check(m['schema'] in ('emberbsd.aarch64.sysroot.v1', 'emberbsd.aarch64.sysroot.v2') and
              m['architecture'] == 'aarch64' and m['machine'] == 'evbarm', 'Wrong sysroot identity')
        required = SYSROOT_REQUIRED
        allowed = required + ['qualification.json', 'build-run.json', EVIDENCE]
    files = m['files']
    check(isinstance(files, list) and all(isinstance(f, dict) for f in files), 'Invalid asset list')
    names = [f['name'] for f in files]
    check(all(isinstance(n, str) and re.fullmatch('[A-Za-z0-9][A-Za-z0-9._+-]*', n) for n in names), 'Unsafe asset name')
    check(len(names) == len(set(names)) and not {'manifest.json', 'SHA256'}.intersection(names), 'Duplicate or reserved asset')
    check(set(required).issubset(names) and set(names).issubset(allowed), 'Incomplete or foreign asset set')
    check(set(os.listdir(directory)) == set(names + ['manifest.json', 'SHA256']), 'Missing or extra kit file')
    for f in files:
        regular_file(directory / f['name'])
        check(type(f['size']) is int and 0 < f['size'] < 2**31, 'Invalid GitHub asset size: ' + f['name'])
        check(asset(directory, f['name']) == f, 'Changed asset: ' + f['name'])
    check((directory / 'SHA256').read_text() == checksum_text(directory, names + ['manifest.json']), 'Checksum manifest changed')
    if profile != 'amd64' or modern:
        p = read_json(directory / 'build-profile.json')
        check(type(p['schema']) is int and p['schema'] in ((2, 3) if modern else (1,)), 'Manifest and build profile schema differ')
        profile_check(p, sha, version, 'amd64' if profile == 'amd64' else 'aarch64')
        check(m['source_archive_sha256'] == p['source_sha256'] and m['build_profile'] == p, 'Source/build receipt changed')
        check((directory / 'source-export.sha256').read_text() == p['source_sha256'] + '  source.tar.gz\n', 'Source export hash receipt changed')
        if profile != 'amd64':
            for name in SYSROOT_SETS:
                check(p['output_sha256']['release/evbarm-aarch64/binary/sets/' + name] == sha256_file(directory / name), 'Set belongs to another build: ' + name)
            check(p['output_sha256'][KERNEL] == sha256_file(directory / KERNEL), 'Kernel belongs to another build')
        else:
            for name in images(version):
                check(p['output_sha256']['release/images/' + name] == sha256_file(directory / name), 'Image belongs to another build: ' + name)
    if m['qualification'] is not None:
        check('qualification.json' in names and read_json(directory / 'qualification.json') == m['qualification'], 'Qualification receipt changed')
        require_qualification(m, profile)
    else:
        check('qualification.json' not in names and EVIDENCE not in names, 'Unbound qualification receipt or target evidence')
    if 'build-run.json' in names:
        run = read_json(directory / 'build-run.json')
        check(run['headSha'] == sha and run['conclusion'] == 'success' and isinstance(run['url'], str) and
              re.fullmatch(r'https://github\.com/oxtech-ember/(?:EmberBSD|EmberBSD-SDK)/actions/runs/[0-9]+', run['url']), 'CI receipt belongs to another build')
    if profile != 'amd64':
        kernel_check(directory / KERNEL, sha)
        set_content_check(directory)
    return m


def verify(directory, profile):
    check(profile in ('amd64', 'aarch64-sysroot'), 'Unknown release profile')
    try:
        return _verify(directory, profile)
    except (KeyError, TypeError, ValueError, IndexError, AttributeError, OSError, EOFError) as exc:
        raise SDKError(f'Invalid kit: {exc}') from exc


def prepare_sysroot(build, directory, qualification):
    sha = (build / 'build-success').read_text().strip()
    check(full_hash(sha, 40), 'No complete pinned build')
    p = read_json(build / 'build-profile.json')
    version = build_version(build, p)
    profile_check(p, sha, version)
    directory.mkdir(mode=0o700)
    for name in SYSROOT_SETS:
        shutil.copyfile(build / 'release/evbarm-aarch64/binary/sets' / name, directory / name)
    for name in (KERNEL, 'build-profile.json'):
        shutil.copyfile(build / name, directory / name)
    (directory / 'source-export.sha256').write_text(p['source_sha256'] + '  source.tar.gz\n')
    names = list(SYSROOT_REQUIRED)
    if (build / 'build-run.json').is_file():
        shutil.copyfile(build / 'build-run.json', directory / 'build-run.json')
        names.append('build-run.json')
    q = None
    if qualification:
        raw = read_json(qualification)
        fields = ('source_commit base_sha256 comp_sha256 kernel_gzip_sha256 consumer_compiler execution checks runtime_libraries evidence_sha256').split()
        q = {k: raw[k] for k in fields if k in raw}
        q['checks'] = {k: raw['checks'][k] for k in SYSROOT_CHECKS if k in raw['checks']}
        write_json(directory / 'qualification.json', q)
        shutil.copyfile(build / EVIDENCE, directory / EVIDENCE)
        names += ['qualification.json', EVIDENCE]
    modern = p.get('schema') in (2, 3)
    compiler = 'the verified common GCC16 and runtime' if modern else 'the historical in-tree GCC12.5 bootstrap'
    (directory / 'release-notes.md').write_text(
        f'# EmberBSD {version} AArch64 development sysroot\n\nSource: `{sha}`. Compatibility base: NetBSD 11.0.\n'
        f'The complete base and comp sets use {compiler}.\n'
        'Native bootstrap compiler commands are excluded. Build commands and hashes are in build-profile.json.\n'
        'source-export.sha256 identifies the git archive, not the GitHub generated source archive.\n'
        f'Source: https://github.com/{REPOSITORY}/tree/{sha}.\n\n'
        + ('The bound target receipt records finite GCC16 C/C++ and libc checks.\n' if q else 'Target qualification is pending. Do not publish this candidate.\n')
        + 'This kit does not qualify installation media or physical hardware. Firmware and source licences remain in the sets.\n'
        'Verify SHA256 and provenance before extracting into an empty sysroot. Do not install over a live system.\n')
    m = dict(schema='emberbsd.aarch64.sysroot.v2' if modern else 'emberbsd.aarch64.sysroot.v1', repository=REPOSITORY,
             source_commit=sha, release_tag=tag(sha, 'aarch64-sysroot'), source_archive_sha256=p['source_sha256'],
             architecture='aarch64', machine='evbarm', product_version=version, base_version='11.0', build_profile=p,
             files=[asset(directory, n) for n in names], qualification=q, hardware_qualification='not-tested')
    seal(directory, m)
    return verify(directory, 'aarch64-sysroot')


def prepare_amd64(build, directory, qualification, os_source):
    sha = (build / 'build-success').read_text().strip()
    check(full_hash(sha, 40), 'No complete pinned build')
    p = read_json(build / 'build-profile.json') if (build / 'build-profile.json').exists() else None
    version = build_version(build, p) if p else archive_version(build / 'source.tar')
    source = Path(os_source) if os_source else Path(p['sdk_receipt']['paths']['os']) if p else build / 'source'
    checker_path = 'ember/tests/installer-image.py'
    checker_commit = command('git', '-C', source, 'rev-parse', 'HEAD').decode().strip()
    check(full_hash(checker_commit, 40), 'Installer checker requires an explicit OS source checkout')
    checker_bytes = command('git', '-C', source, 'cat-file', 'blob', 'HEAD:' + checker_path)
    regular_file(source / checker_path)
    check((source / checker_path).read_bytes() == checker_bytes, 'Installer checker differs from its recorded source')
    checker = dict(repository=REPOSITORY, source_commit=checker_commit, path=checker_path,
                   sha256=hashlib.sha256(checker_bytes).hexdigest())
    iso, usb = (build / 'release/images' / n for n in images(version))
    # The OS owns the installer image contract. SDK calls that source's test.
    command(sys.executable, source / 'ember/tests/installer-image.py', iso, sha, version, 'github.com')
    prefix = 'amd64/binary/sets/'
    paths = [n for n in archive_names(iso) if n.startswith(prefix) and n.endswith('.tar.xz')]
    names = [n.removeprefix(prefix) for n in paths]
    required = [n + '.tar.xz' for n in SETS]
    allowed = required + [n + '.tar.xz' for n in OPTIONAL_SETS]
    check(set(required).issubset(names) and set(names).issubset(allowed) and len(names) == len(set(names)), 'ISO has incomplete or foreign sets')
    sums = extract_bytes(iso, prefix + 'SHA512').decode()
    sha512 = dict(re.findall(r'^SHA512 \(([^)]+)\)\s*=\s*([0-9a-f]{128})$', sums, re.M))
    check(set(names).issubset(sha512), 'ISO lacks set checksums')
    directory.mkdir(mode=0o700)
    for member, name in zip(paths, names):
        extract_file(iso, member, directory / name)
        with open(directory / name, 'rb') as handle:
            check(hashlib.file_digest(handle, 'sha512').hexdigest() == sha512[name], 'ISO set checksum failed: ' + member)
    kernel = extract_bytes(directory / 'kern-GENERIC.tar.xz', './netbsd')
    check_native_compiler(directory / 'comp.tar.xz')
    buildinfo = f'EmberBSD:{sha}'
    check((buildinfo + '\n\0').encode() in kernel, 'Installed kernel does not report the full build source')
    boot = extract_bytes(iso, 'netbsd')
    if boot.startswith(b'\x1f\x8b'):
        boot = gzip.decompress(boot)
    kernel_sha = hashlib.sha256(kernel).hexdigest()
    check(hashlib.sha256(boot).hexdigest() == kernel_sha, 'ISO boot kernel differs from its installation set')
    with gzip.open(usb, 'rb') as handle:
        while handle.read(1024 * 1024):
            pass
    for image in images(version):
        shutil.copyfile(build / 'release/images' / image, directory / image)
    (directory / 'SHA512').write_text(sums)
    q = None
    if qualification:
        raw = read_json(qualification)
        q = {k: raw[k] for k in ('source_commit', 'image_sha256', 'usb_image_sha256', 'installed_kernel_sha256', 'checks') if k in raw}
        q['checks'] = {k: raw['checks'][k] for k in VM_CHECKS if k in raw['checks']}
        write_json(directory / 'qualification.json', q)
    if p:
        profile_check(p, sha, version, 'amd64')
        check(sha256_file(build / 'source.tar.gz') == p['source_sha256'], 'Source export differs')
        shutil.copyfile(build / 'build-profile.json', directory / 'build-profile.json')
        (directory / 'source-export.sha256').write_text(p['source_sha256'] + '  source.tar.gz\n')
    (directory / 'release-notes.md').write_text(
        f'# EmberBSD {version} amd64 UEFI installation candidate\n\nSource: `{sha}`. Compatibility base: NetBSD 11.0.\n'
        'Profile: no X11, no rump, GENERIC kernel and matching modules/base. Native bootstrap compiler commands are excluded.\n'
        + ('The build uses the common verified GCC16 compiler and runtime.\n' if p else 'This historical build uses the in-tree bootstrap compiler; it does not qualify a GCC16 userland.\n')
        + ('The bound VM receipt records installation, persistence and TLS rejection checks.\n' if q else 'VM qualification is pending. Do not publish this candidate.\n')
        + 'Physical machines are not qualified. GitHub-native installation requires a separate post-publication check.\n'
        'Verify SHA256 before use. Select the 13 required sets; skip only an absent optional set after its explicit HTTP 404.\n'
        'Never skip a required set or a certificate error. Back up the destination before installation.\n'
        f'Binary channel: https://github.com/{REPOSITORY}/releases/download/{tag(sha, "amd64")}/\n'
        'Source archives are not in this binary channel.\n')
    components = {n: {'source_commit': sha} for n in ('image', 'kernel', 'modules', 'base')}
    components['kernel'].update(buildinfo=buildinfo, sha256=kernel_sha)
    m = dict(schema='emberbsd.amd64.release.v2' if p else 1, repository=REPOSITORY, source_commit=sha,
             source_archive_sha256=p['source_sha256'] if p else sha256_file(build / 'source.tar'),
             release_tag=tag(sha, 'amd64'), architecture='amd64', product_version=version, base_version='11.0',
             profile=dict(MKX11='no', MKRUMP='no', MKGCCCMDS='no', KERNEL_SETS='GENERIC', BUILD_KERNELS='INSTALL'),
             components=components, files=[asset(directory, n) for n in sorted(os.listdir(directory))], qualification=q,
             omitted_sets=sorted(set(sha512) - set(names)), hardware_qualification='not-tested')
    m['image_checker'] = checker
    if p:
        m['build_profile'] = p
    seal(directory, m)
    return verify(directory, 'amd64')


def prepare(build, directory, profile, qualification=None, os_source=None):
    check(profile in ('amd64', 'aarch64-sysroot'), 'Unknown release profile')
    try:
        if profile == 'amd64':
            return prepare_amd64(Path(build), Path(directory), qualification, os_source)
        return prepare_sysroot(Path(build), Path(directory), qualification)
    except (KeyError, TypeError, ValueError, IndexError, AttributeError, OSError, EOFError) as exc:
        raise SDKError(f'Preparation stopped; do not publish partial output: {exc}') from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', required=True, choices=('amd64', 'aarch64-sysroot'))
    commands = parser.add_subparsers(dest='action', required=True)
    p = commands.add_parser('prepare')
    p.add_argument('build')
    p.add_argument('output')
    p.add_argument('--qualification')
    p.add_argument('--os-source', help='explicit OS source for the image checker; recorded separately from build provenance')
    for name in ('verify', 'draft', 'publish', 'verify-remote'):
        commands.add_parser(name).add_argument('kit')
    args = parser.parse_args(argv)
    try:
        if args.action == 'prepare':
            result = prepare(args.build, args.output, args.profile, args.qualification, args.os_source)
            print(f'Prepared {result["release_tag"]}; qualification: ' + ('accepted' if result['qualification'] else 'pending'))
        elif args.action == 'verify':
            result = verify(args.kit, args.profile)
            print(f'PASS: {result["release_tag"]} composition, source and hashes')
        else:
            from .os_release_github import GitHub
            publisher = GitHub(args.kit, args.profile)
            print(publisher.verify_remote()['html_url'] if args.action == 'verify-remote' else getattr(publisher, args.action)())
        return 0
    except (SDKError, OSError, ValueError) as exc:
        print(f'OS release stopped: {exc}', file=sys.stderr)
        return 1
