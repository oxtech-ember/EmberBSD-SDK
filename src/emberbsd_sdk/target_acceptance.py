# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Build and run the shared EmberBSD AArch64 target acceptance suite."""

import argparse
import gzip
import lzma
import os
from pathlib import Path
import re
import shutil
import sys

from . import jsonio, native_checks, native_toolset, os_release_builder, process, provenance
from . import paths
from .errors import SDKError
from .inventory import sha256_file

HOST_CHECKS = ('cross_c11', 'cross_cxx20')
GUEST_CHECKS = ('libc_aliases', 'target_c11', 'target_cxx20',
                'base_loader_smoke', 'fork_cas')
CHECKS = HOST_CHECKS + GUEST_CHECKS
V2_GUEST_CHECKS = GUEST_CHECKS + ('libm_valid', 'setjmp_valid')
MARKER = re.compile(r'EMBERBSD-CHECK ([A-Za-z0-9_-]+) ([a-z0-9_]+) (PASS|FAIL)\Z')


def _require(condition, message):
    if not condition:
        raise SDKError(message)


def _extract_base_archive(archive, root, work, tar, env, log):
    """Expand the verified xz stream before invoking the selected native tar."""
    temporary = work / 'base.uncompressed.tar'
    created = False
    try:
        try:
            with temporary.open('xb') as output:
                created = True
                with lzma.open(archive, 'rb') as source:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
        except (lzma.LZMAError, EOFError) as exc:
            raise SDKError(f'cannot decompress base set: {exc}') from exc
        process.run_logged([tar, '-xpf', str(temporary), '-C', str(root)], log, env=env)
    finally:
        if created:
            temporary.unlink(missing_ok=True)


def parse_guest_log(data, nonce, root_device, *, corpus="target-runtime-v1"):
    """Validate the complete nonce-bound guest protocol; reject noise as evidence."""
    _require(corpus in ('target-runtime-v1', 'target-runtime-v2'), 'unknown target runtime corpus')
    required_checks = V2_GUEST_CHECKS if corpus == 'target-runtime-v2' else GUEST_CHECKS
    try:
        lines = data.decode('utf-8', 'strict').splitlines()
    except UnicodeError as exc:
        raise SDKError('guest serial log is not UTF-8') from exc
    root = f'EMBERBSD-ACCEPTANCE {nonce} ROOT '
    complete = f'EMBERBSD-ACCEPTANCE {nonce} COMPLETE'
    for line in lines:
        if not line.startswith('EMBERBSD-ACCEPTANCE '):
            continue
        fields = line.split()
        _require(len(fields) >= 3 and fields[1] == nonce,
                 'guest acceptance event belongs to another run')
        _require(fields[2] in ('ROOT', 'COMPLETE', 'FAIL'),
                 'unknown guest acceptance event')
        _require(fields[2] != 'FAIL', 'guest reported acceptance failure')
    root_events = [line for line in lines if line.startswith('EMBERBSD-ACCEPTANCE ') and ' ROOT ' in line]
    _require(all(line.startswith(root) for line in root_events),
             'guest root event belongs to another run')
    roots = [line for line in root_events if line.startswith(root)]
    _require(len(roots) == 1, 'guest did not report exactly one selected read-only root')
    root_fields = roots[0][len(root):].split()
    _require(len(root_fields) == 3 and root_fields[1] == 'ffs' and
             root_fields[0] == '/dev/' + root_device and
             'ro' in root_fields[2].split(','), 'guest root is not the selected read-only FFS')
    _require(lines.count(complete) == 1, 'guest did not complete its target test run')
    seen = {}
    completion_index = lines.index(complete)
    root_index = lines.index(roots[0])
    _require(root_index < completion_index, 'guest completion precedes root validation')
    for index, line in enumerate(lines):
        if not line.startswith('EMBERBSD-CHECK '):
            continue
        match = MARKER.fullmatch(line)
        _require(match is not None, 'malformed guest check marker')
        marker_nonce, name, status = match.groups()
        _require(marker_nonce == nonce, 'guest check marker belongs to another run')
        _require(name in required_checks, 'unknown guest check marker: ' + name)
        _require(name not in seen, 'duplicate guest check marker: ' + name)
        _require(status == 'PASS', 'guest check failed: ' + name)
        _require(root_index < index < completion_index,
                 'guest check marker is outside the validated run')
        seen[name] = True
    _require(set(seen) == set(required_checks), 'guest did not report every required runtime check')
    return {name: True for name in required_checks}


def _git_revision(path):
    result = process.capture(['git', '-C', str(path), 'rev-parse', 'HEAD'], env=process.git_environment())
    revision = result.stdout.decode('ascii', 'strict').strip()
    _require(re.fullmatch(r'[0-9a-f]{40}', revision) is not None,
             'invalid Git source revision: ' + str(path))
    return revision


def _clean_source_revision(path, role, *, kind=None):
    """Return a complete, clean committed source snapshot for one input role."""
    try:
        identity = provenance.git_source(str(path), kind=kind)
    except SDKError as exc:
        raise SDKError(role + ' source snapshot is not a complete clean Git root: ' + str(path)) from exc
    return identity['commit']


def _select_os_sources(os_build_commit, os_source, os_test_source):
    """Bind build producers to the OS build and tests/helpers to a source snapshot."""
    build_revision = _clean_source_revision(os_source, 'OS build')
    _require(build_revision == os_build_commit,
             'OS source revision differs from the selected OS build')
    test_revision = _clean_source_revision(os_test_source, 'OS test')
    required = (os_test_source / 'ember/tests/target-acceptance/libc-aliases.c',
                os_test_source / 'ember/tests/target-acceptance/base-loader-smoke.c',
                os_test_source / 'ember/tests/target-acceptance/fork-cas.c',
                os_test_source / 'ember/tests/target-acceptance/libm-valid.c',
                os_test_source / 'ember/tests/target-acceptance/setjmp-valid.c',
                os_test_source / 'ember/image/target-acceptance-rc',
                os_test_source / 'ember/image/target-acceptance-root.sh')
    for source in required:
        _require(source.is_file() and not source.is_symlink(),
                 'OS test source snapshot is missing required input: ' + str(source))
    return build_revision, test_revision


def _install_diagnostic_runcom(os_test_source, root):
    """Install the acceptance-only init entrypoint into the disposable root."""
    source = os_test_source / 'ember/image/target-acceptance-rc'
    _require(source.is_file() and not source.is_symlink(),
             'OS test source is missing the diagnostic /etc/rc')
    destination = root / 'etc/rc'
    _require(not destination.exists() and not destination.is_symlink(),
             'base set unexpectedly contains /etc/rc')
    shutil.copy2(source, destination)
    os.chmod(destination, 0o755)


def _copy_runtime(sysroot, test_bundle, root, compiler_family):
    rows = (test_bundle / 'runtime-files.tsv').read_text().splitlines()
    expected_roles = {'clang': {'libcxx', 'libcxxabi', 'libunwind'},
                      'gcc': {'libstdc++', 'libgcc_s'}}
    _require(compiler_family in expected_roles and rows,
             'compiled target test bundle has no runtime manifest')
    parsed = []
    for row in rows:
        fields = row.split('\t')
        _require(len(fields) == 3 and re.fullmatch(r'[0-9a-f]{64}', fields[2]) is not None,
                 'invalid compiled runtime row')
        role, guest_path, expected = fields
        prefix = '/usr/pkg/llvm/' if compiler_family == 'clang' else '/usr/pkg/gcc16/'
        relative = Path(guest_path.lstrip('/'))
        _require(role in expected_roles[compiler_family] and guest_path.startswith(prefix) and
                 not relative.is_absolute() and all(part not in ('', '.', '..') for part in relative.parts) and
                 relative.as_posix() == guest_path.lstrip('/'),
                 'runtime path is outside selected package prefix')
        source = (sysroot / relative).resolve(strict=True)
        _require(paths.inside(source, sysroot) and source.is_file() and sha256_file(source) == expected,
                 'selected runtime changed after cross compilation: ' + role)
        parsed.append((role, guest_path, expected, source, relative))
    _require(len(parsed) == len(expected_roles[compiler_family]) and
             {row[0] for row in parsed} == expected_roles[compiler_family],
             'compiled target runtime manifest has missing or duplicate roles')
    for role, guest_path, expected, source, relative in parsed:
        destination = root / relative
        if compiler_family == 'clang':
            resolved = destination.resolve(strict=True)
            _require(paths.inside(resolved, root) and resolved.is_file() and
                     sha256_file(resolved) == expected,
                     'base set Clang runtime differs from selected SDK runtime: ' + role)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        _require(paths.inside(destination.parent.resolve(strict=True), root),
                 'runtime destination escapes disposable guest root')
        if destination.exists() or destination.is_symlink():
            resolved = destination.resolve(strict=True)
            _require(paths.inside(resolved, root) and resolved.is_file() and
                     sha256_file(resolved) == expected,
                     'base set contains a conflicting selected runtime: ' + role)
            continue
        shutil.copy2(source, destination)


def _copy_test_bundle(bundle, root):
    names = ('target.txt', 'compiler-family.txt', 'runtime-files.tsv', 'os-tests.txt',
             'atomic-tls', 'atomic-tls-lto', 'boundary', 'libboundary.so',
             'check-runtime.sh', 'run-target.sh', 'libc-aliases',
             'base-loader-smoke', 'fork-cas', 'fork-cas-static', 'libm-valid', 'setjmp-valid')
    for name in names:
        source = bundle / name
        if name == 'os-tests.txt' and not source.exists():
            continue
        _require(source.is_file() and not source.is_symlink(),
                 'compiled target bundle is missing ' + name)
        destination = root / 'tests' / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _require_os_makedev(destination):
    path = destination / 'dest/dev/MAKEDEV'
    _require(path.is_file(), 'same-build DESTDIR/MAKEDEV is missing')
    return path


def _compile_tests_command(native, options, script, os_source, target_prefix,
                           sysroot, tests, sdk_source, host_prefix):
    git = shutil.which('git')
    _require(git is not None, 'Git is required to record the OS test source')
    git = str(Path(git).resolve(strict=True))
    _require(os.path.isfile(git) and os.access(git, os.X_OK), 'Selected Git is not executable')
    command = [native.tool('python'), '-B', str(script), '--compiler-family',
               options.compiler_family, '--git', git, '--os-source', str(os_source),
               '--target', 'aarch64', str(target_prefix), str(sysroot), str(tests),
               '--sdk-source', str(sdk_source)]
    if options.compiler_family == 'clang':
        command[5:5] = ['--host-prefix', str(host_prefix)]
    return command


def run(options):
    sdk_source = Path(options.sdk_source).resolve(strict=True)
    running_sdk_source = Path(__file__).resolve().parents[2]
    _require(sdk_source == running_sdk_source,
             'selected SDK source differs from the running target acceptance controller')
    os_source = Path(options.os_source).resolve(strict=True)
    os_test_source = Path(options.os_test_source or options.os_source).resolve(strict=True)
    ports_source = Path(options.ports_source).resolve(strict=True)
    os_build = Path(options.os_build).resolve(strict=True)
    sysroot = Path(options.sysroot).resolve(strict=True)
    target_prefix = Path(options.target_prefix).resolve(strict=True)
    host_prefix = Path(options.host_prefix).resolve(strict=True)
    output = Path(paths.canonical_new(options.output))
    _require(output.is_absolute() and not output.exists(), 'output must be a new absolute directory')
    for selected_input in (sdk_source, os_source, os_test_source, ports_source, os_build, sysroot,
                           target_prefix, host_prefix):
        paths.reject_overlap(output, selected_input)

    profile_path = os_build / 'build-profile.json'
    profile = jsonio.read_json(profile_path)
    os_release_builder.validate_profile(profile, profile.get('source_commit'), 'aarch64')
    os_build_revision, os_test_revision = _select_os_sources(
        profile['source_commit'], os_source, os_test_source)
    set_base = os_build / 'release/evbarm-aarch64/binary/sets/base.tar.xz'
    set_comp = os_build / 'release/evbarm-aarch64/binary/sets/comp.tar.xz'
    kernel_gz = os_build / 'netbsd-EMBER64.gz'
    _require(sha256_file(set_base) == profile['output_sha256']['release/evbarm-aarch64/binary/sets/base.tar.xz'],
             'base set differs from the selected OS build')
    _require(sha256_file(kernel_gz) == profile['output_sha256']['netbsd-EMBER64.gz'],
             'kernel differs from the selected OS build')
    _require(sha256_file(set_comp) == profile['output_sha256']['release/evbarm-aarch64/binary/sets/comp.tar.xz'],
             'comp set differs from the selected OS build')
    receipt_paths = profile['sdk_receipt']['paths']
    tools = Path(receipt_paths['tools']).resolve(strict=True)
    destination = Path(receipt_paths['destination']).resolve(strict=True)
    _require_os_makedev(destination)

    native = native_toolset.load_host_prefix(str(host_prefix),
        required_roles={'cc', 'cxx', 'cpp', 'python', 'sh', 'tar', 'qemu-system-aarch64',
                        'awk', 'rm', 'sed', 'grep', 'cat', 'mv'},
        compiler_family='clang')
    _require(Path(sys.executable).resolve() == Path(native.tool('python')).resolve(),
             'controller Python differs from selected native SDK Python')
    qemu = native.tool('qemu-system-aarch64')
    result = process.capture([qemu, '--version'], env={'PATH': native.path, 'LC_ALL': 'C'})
    qemu_banner = (result.stdout + result.stderr).decode('utf-8', 'replace').splitlines()[0]
    _require('QEMU emulator version ' in qemu_banner, 'selected QEMU did not report its version')

    output.mkdir(parents=True)
    tests = output / 'tests'
    compile_script = ports_source / 'profiles/development-toolchain/cross/compile-tests.py'
    _require(compile_script.is_file(), 'shared Ports compile-tests.py is missing')
    ports_revision = _clean_source_revision(ports_source, 'Ports producer', kind='ports')
    sdk_revision = _clean_source_revision(sdk_source, 'SDK controller')
    compile_argv = _compile_tests_command(native, options, compile_script, os_test_source,
                                          target_prefix, sysroot, tests, sdk_source, host_prefix)
    process.run_logged(compile_argv, output / 'compile.log',
                       env=process.sanitized_environment({'PATH': native.path}))
    compile_checks = jsonio.read_json(tests / 'compile-checks.json')
    _require(compile_checks == {'cross_c11': True, 'cross_cxx20': True},
             'shared Ports cross compilation did not complete all required checks')
    _require((tests / 'compiler-family.txt').read_text().strip() == options.compiler_family,
             'compiled suite selected another compiler family')
    compiler_identity = jsonio.read_json(tests / 'compiler-identity.json')
    sysroot_identity_path = tests / 'sysroot-identity.json'
    _require(sysroot_identity_path.is_file() and not sysroot_identity_path.is_symlink(),
             'shared test compiler did not bind an SDK sysroot record')
    sysroot_identity = jsonio.read_json(sysroot_identity_path)
    _require(sysroot_identity.get('execution_acceptance') == 'not-run' and
             sysroot_identity.get('target') == 'aarch64',
             'invalid selected sysroot identity')
    build_signature = profile['sdk_receipt']['signature']
    expected_family = build_signature.get('compiler_family', 'gcc')
    if options.compiler_family == expected_family:
        _require(compiler_identity.get('family') == expected_family and
                 compiler_identity.get('compiler_version') == build_signature['compiler']['version'],
                 'target consumer compiler differs from the OS build compiler')
    _require((tests / 'os-source-commit.txt').read_text().strip() == os_test_revision,
             'compiled OS test source revision changed')

    work = output / 'root'
    work.mkdir()
    root = work / 'tree'
    root.mkdir()
    _extract_base_archive(set_base, root, work, native.tool('tar'),
                          process.sanitized_environment({'PATH': native.path}), output / 'extract-base.log')
    _copy_test_bundle(tests, root)
    _copy_runtime(sysroot, tests, root, options.compiler_family)
    _install_diagnostic_runcom(os_test_source, root)
    nonce = __import__('secrets').token_hex(16)
    (root / 'etc/emberbsd-target-acceptance.nonce').write_text(nonce + '\n')
    (root / 'etc/emberbsd-target-acceptance-root-device').write_text(options.root_device + '\n')
    (root / 'etc/fstab').write_text(f'/dev/{options.root_device} / ffs ro 1 1\n')
    image = work / 'root.ffs'
    image_helper = os_test_source / 'ember/image/target-acceptance-root.sh'
    image_bindings = work / 'native-bindings'
    image_env = native_checks.check_environment(native, bindings=image_bindings,
                                                 environment={'LC_ALL': 'C'})
    image_env['HOST_SH'] = native.tool('sh')
    process.run_logged([native.tool('sh'), str(image_helper), str(root),
        str(destination / 'dest'), str(tools), str(os_source), options.root_device,
        str(image), native.tool('sh')], output / 'makefs.log',
        env=image_env)
    kernel = work / 'netbsd-EMBER64'
    with gzip.open(kernel_gz, 'rb') as source, kernel.open('xb') as target:
        shutil.copyfileobj(source, target)
    serial = output / 'target-acceptance.log'
    qemu_log = output / 'qemu.log'
    argv = [qemu, '-machine', 'virt,accel=tcg', '-cpu', 'cortex-a72', '-m', '1024',
        '-smp', '2', '-display', 'none', '-monitor', 'none', '-no-reboot',
        '-serial', 'file:' + str(serial), '-kernel', str(kernel),
        '-append', 'root=' + options.root_device + ' console=plcom0',
        '-drive', 'file=' + str(image) + ',if=none,format=raw,id=root,readonly=on',
        '-device', 'virtio-blk-pci,drive=root', '-net', 'none']
    process.run_logged(argv, qemu_log, env=process.sanitized_environment({'PATH': native.path}),
                       timeout_seconds=options.timeout)
    checks = parse_guest_log(serial.read_bytes(), nonce, options.root_device, corpus='target-runtime-v2')
    runtime_rows = []
    for line in (tests / 'runtime-files.tsv').read_text().splitlines():
        role, guest_path, digest = line.split('\t')
        runtime_rows.append(dict(role=role, path=guest_path, sha256=digest))
    result_data = dict(format='emberbsd-target-acceptance', version=2, corpus='target-runtime-v2',
        execution='EmberBSD aarch64 full-system QEMU TCG', status='passed',
        checks={**compile_checks, **checks}, compiler_family=options.compiler_family,
        sources=dict(os_build=os_build_revision, os_tests=os_test_revision,
                     ports=ports_revision, sdk=sdk_revision),
        os_build_profile_sha256=sha256_file(profile_path),
        base_set_sha256=sha256_file(set_base), kernel_sha256=sha256_file(kernel_gz),
        root_ffs_sha256=sha256_file(image), qemu_sha256=sha256_file(qemu),
        qemu_banner=qemu_banner, guest_log_sha256=sha256_file(serial),
        sysroot_identity_sha256=sha256_file(sysroot_identity_path),
        runtime_overlay=dict(method=('verify-existing-base-runtime' if options.compiler_family == 'clang'
                                     else 'copy-selected-sysroot-runtime-into-guest-root'),
                             source_record=sysroot_identity.get('source_record'),
                             source_record_sha256=sysroot_identity.get('source_record_sha256'),
                             runtime_files=runtime_rows),
        compile_source_inventory_sha256=sha256_file(tests / 'source-sha256.tsv'),
        runtime_manifest_sha256=sha256_file(tests / 'runtime-files.tsv'),
        test_programs_sha256={name: sha256_file(tests / name) for name in
            ('atomic-tls', 'atomic-tls-lto', 'boundary', 'libboundary.so',
             'libc-aliases', 'base-loader-smoke', 'fork-cas', 'fork-cas-static',
             'libm-valid', 'setjmp-valid')},
        root_helper_sha256=sha256_file(image_helper),
        guest_runner_sha256=sha256_file(os_test_source / 'ember/image/target-acceptance-rc'),
        sdk_runner_sha256=sha256_file(Path(__file__).resolve().parents[2] /
                                      'scripts/target-acceptance.py'))
    jsonio.write_json(output / 'target-acceptance.json', result_data)
    if options.compiler_family == expected_family:
        runtime_libraries = {}
        for row in (tests / 'runtime-files.tsv').read_text().splitlines():
            role, guest_path, digest = row.split('\t')
            runtime_libraries[role] = dict(path=guest_path, sha256=digest)
        qualification = dict(corpus='target-runtime-v2', source_commit=profile['source_commit'],
            os_test_source_commit=os_test_revision, ports_source_commit=ports_revision,
            test_source_inventory_sha256=sha256_file(tests / 'source-sha256.tsv'),
            base_sha256=sha256_file(set_base), comp_sha256=sha256_file(set_comp),
            kernel_gzip_sha256=sha256_file(kernel_gz),
            consumer_compiler=(tests / 'compiler.txt').read_text().strip(),
            consumer_compiler_family=options.compiler_family,
            execution='EmberBSD aarch64 full-system QEMU TCG', checks=result_data['checks'],
            runtime_libraries=runtime_libraries,
            evidence_sha256={'target-acceptance.log': sha256_file(serial)})
        jsonio.write_json(output / 'qualification.json', qualification)
    print('AArch64 target acceptance passed: ' + str(output))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--os-build', required=True)
    parser.add_argument('--os-source', required=True)
    parser.add_argument('--os-test-source',
        help='optional clean OS test/helper source snapshot; defaults to --os-source')
    parser.add_argument('--ports-source', required=True)
    parser.add_argument('--sysroot', required=True)
    parser.add_argument('--target-prefix', required=True)
    parser.add_argument('--host-prefix', required=True)
    parser.add_argument('--compiler-family', choices=('clang', 'gcc'), default='clang')
    parser.add_argument('--root-device', required=True)
    parser.add_argument('--timeout', type=int, default=300)
    parser.add_argument('--output', required=True)
    parser.add_argument('--sdk-source', required=True)
    options = parser.parse_args(argv)
    _require(0 < options.timeout <= 3600, 'timeout must be between 1 and 3600 seconds')
    _require(re.fullmatch(r'ld[0-9]+[a-p]', options.root_device) is not None,
             'root device must be an explicit ld unit and partition, such as ld0a')
    run(options)
    return 0
