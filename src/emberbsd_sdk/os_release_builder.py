# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD ember/release/build-{amd64,aarch64}.sh and SDK SysrootBuilder.
# AI-assisted migration to the common SDK producer controls.
"""Complete OS release orchestration on the shared SDK compiler/runtime contract."""

import gzip
import os
from pathlib import Path
import re
import shutil

from .errors import SDKError
from . import inventory, jsonio, libedit, llvm_runtime, llvm_runtime_install
from .target_toolchain import validate_clang_identity
from .sysroot_builder import (SysrootBuilder, native_host_flags, gnu_target_host_flags, check_native_command_identity,
                              validate_native_binding)
from .targets import target_definition
from .os_release import check, full_hash, tag, images, product_version, source_version, KERNEL, SYSROOT_SETS

# Optional Clang-only aarch64 X11 sets built from the prepared xsrc input.
X11_SETS = ['xbase.tar.xz', 'xcomp.tar.xz', 'xetc.tar.xz', 'xfont.tar.xz', 'xserver.tar.xz']
X11_PACKAGE = 'netbsd/xsrc'


def release_sets(x11=False):
    return SYSROOT_SETS + (X11_SETS if x11 else [])


def require_x11_profile(target, compiler_family):
    check(target == 'aarch64' and compiler_family == 'clang',
          'X11 sets require the Clang aarch64-sysroot profile')


def check_x11_pin(os_root, record):
    """Bind the prepared xsrc identity to the archive pinned by the OS source."""
    pin = jsonio.read_json(os.path.join(os_root, 'ember/source.json'))
    identity = record.get('identity') if isinstance(record, dict) else None
    check(isinstance(pin, dict) and isinstance(identity, dict) and
          isinstance(pin.get('x11SourceSha512'), str) and
          full_hash(pin['x11SourceSha512'], 128) and
          identity.get('sha512') == pin['x11SourceSha512'] and
          identity.get('url') == pin.get('x11SourceUrl'),
          'Prepared X11 source differs from the OS x11Source pin')


def flags_for(target, compiler_family=None, x11=False):
    # OS make owns the toolchain and staged runtime integration. HAVE_GCC stays
    # an upstream source selector; it must not be set to 16 for an external GCC.
    common = list(SysrootBuilder.FLAGS)
    replacements = dict(MKFIRMWARE='yes', MKLINKLIB='yes', MKSTATICLIB='yes',
                        MKPIC='yes', MKPICLIB='yes', MKDYNAMICROOT='yes',
                        MKLIBSTDCXX='yes', USE_XZ_SETS='yes')
    if target == 'amd64':
        replacements.update(MKKMOD='yes', MKMAN='yes', MKHTML='yes', MKDOC='yes',
                            MKINFO='yes', MKATF='yes', MKKYUA='yes', MKLINT='yes')
    if x11:
        require_x11_profile(target, compiler_family)
        replacements['MKX11'] = 'yes'
    common = [v for v in common if v.split('=', 1)[0] not in replacements]
    if compiler_family == 'clang':
        replacements.pop('MKLIBSTDCXX')
    return common + [f'{k}={v}' for k, v in replacements.items()]


def producer_commands(target, paths, host, sha, timestamp, jobs, shell, native_selection=None,
                      compiler_family=None, base_cxx=False, install_base_cxx=None):
    if type(base_cxx) is not bool:
        raise SDKError('base C++ selection must be boolean')
    if base_cxx and compiler_family != 'clang':
        raise SDKError('base C++ aliases require the selected Clang runtime')
    if install_base_cxx is None:
        install_base_cxx = base_cxx
    if type(install_base_cxx) is not bool or (install_base_cxx and not base_cxx):
        raise SDKError('runtime installation requires the base C++ selection')
    definition = target_definition(target)
    # Only portable historical validation uses the original command contract.
    cc = host['cc']['path'] if native_selection is None else native_selection['tools']['cc']
    cxx = host['c++']['path'] if native_selection is None else native_selection['tools']['cxx']
    runtime_flags = [f'EXTERNAL_TOOLCHAIN={paths["compiler"]}',
                     f'EMBER_EXTERNAL_GCC_RUNTIME={paths["runtime-prefix"]}']
    x11 = 'x11-source' in paths
    common = flags_for(target, compiler_family, x11)
    if compiler_family == 'clang':
        from .llvm_sysroot import llvm_flags
        runtime_flags = llvm_flags(paths, phase='final', base_cxx=base_cxx)
        replaced = {flag.split('=', 1)[0] for flag in runtime_flags}
        common = [flag for flag in common if flag.split('=', 1)[0] not in replaced]
    flags = common + runtime_flags + [
        f'EMBER_LIBEDIT_SOURCE={paths["libedit-source"]}',
        f'MKREPRO_TIMESTAMP={timestamp}', f'EMBER_BUILD_ID={sha}', f'BUILDINFO=EmberBSD:{sha}',
        f'KERNOBJDIR={paths["objects"]}/kernels'] + (native_host_flags(native_selection) if native_selection is not None else [])
    for name in ('libarchive', 'libevent', 'byacc', 'texinfo', 'rcs', 'diffutils', 'grep', 'ekermit', 'nvi', 'tdb', 'ppp', 'top'):
        if name + '-source' in paths:
            flags.append(f'EMBER_{name.upper()}_SOURCE={paths[name + "-source"]}')
    if 'tdb-source' in paths and native_selection is not None:
        flags.append('HOST_PYTHON=' + native_selection['tools']['python'])
    if any(name in paths for name in ('texinfo-source', 'rcs-source', 'diffutils-source', 'grep-source')) and native_selection is not None:
        flags += gnu_target_host_flags(native_selection)
    # Current preflight requires BFD; historical command records predate it.
    if 'host-binutils' in paths:
        flags.append(f'EMBER_HOST_BFD_CONFIG={paths["host-binutils"]}/host-bfd.mk')
    if x11:
        flags.append('X11SRCDIR=' + paths['x11-source'])
    if compiler_family == 'gcc':
        flags.append('EMBER_COMPILER_FAMILY=' + compiler_family)
        if 'llvm-source' in paths:
            flags.append('EMBER_LLVM_SOURCE=' + paths['llvm-source'])
    check(compiler_family in (None, 'gcc', 'clang'), 'Unsupported release compiler family')
    if native_selection is None or native_selection['format'] != 'emberbsd-host-prefix':
        position = flags.index(f'MKREPRO_TIMESTAMP={timestamp}')
        flags[position:position] = [f'HOST_CC={cc}', f'HOST_CXX={cxx}']
    if target == 'aarch64':
        sets = ' '.join(name.split('.', 1)[0] for name in release_sets(x11))
        flags += ['RELEASEMACHINEDIR=evbarm-aarch64', 'MAKETARSETS=' + sets, 'MKDEBUGKERNEL=no']
    else:
        flags += ['KERNEL_SETS=GENERIC', 'BUILD_KERNELS=INSTALL',
                  'MAKEFLAGS=KERNEL_SETS=GENERIC BUILD_KERNELS=INSTALL',
                  'SYSINST_HTTP_HOST=github.com', 'SYSINST_FTP_HOST=github.com',
                  'SYSINST_FTP_DIR=oxtech-ember/EmberBSD/releases/download/' + tag(sha, 'amd64'),
                  'SYSINST_FTP_SET_DIR_BIN=']
    base = [shell, paths['os'] + '/build.sh', '-U', '-u', '-j' + jobs,
            '-m', definition['machine'], '-a', definition['arch'],
            '-O', paths['objects'], '-T', paths['tools'], '-D', paths['destination'] + '/dest',
            '-R', paths['destination'] + '/release']
    for value in flags:
        base += ['-V', value]
    if target == 'aarch64':
        commands = [base + ['tools', 'kernel=EMBER64'], base + ['distribution', 'sets']]
    else:
        commands = [base + ['tools', 'release'],
                    [paths['tools'] + '/bin/nbmake-amd64', '-C', paths['os'] + '/distrib/amd64/installimage',
                     'INSTIMG_RELEASEDIR=' + paths['destination'] + '/release/images', 'install_image'],
                    base + ['iso-image']]
    if install_base_cxx:
        # Check the ordinary distribution before adding the common runtime.
        # The second check and all set producers share one derived list root.
        overlay = 'EMBER_SETS_SOURCE=' + paths['destination'] + '/llvm-sets'
        make = paths['tools'] + '/bin/nbmake-' + definition['machine']
        check_sets = [make, '-C', paths['os'] + '/distrib/sets', overlay, 'checkflist']
        if target == 'aarch64':
            commands = [commands[0], base + ['distribution'], check_sets,
                        base + ['-V', overlay, 'sets']]
        else:
            commands = [base + ['tools', 'distribution'], check_sets,
                        [make, '-C', paths['os'] + '/etc', overlay, 'release'],
                        *commands[1:]]
    source_flags = [f'EMBER_{name.upper()}_SOURCE={paths[name + "-source"]}'
                    for name in ('libarchive', 'libevent', 'byacc', 'texinfo', 'rcs', 'diffutils', 'grep', 'ekermit', 'nvi', 'tdb', 'ppp', 'top') if name + '-source' in paths]
    if 'tdb-source' in paths and native_selection is not None:
        source_flags.append('HOST_PYTHON=' + native_selection['tools']['python'])
    if any(name in paths for name in ('texinfo-source', 'rcs-source', 'diffutils-source', 'grep-source')) and native_selection is not None:
        source_flags += gnu_target_host_flags(native_selection)
    for command in commands:
        if command[0].startswith(paths['tools'] + '/bin/nbmake-'):
            command[-1:-1] = source_flags
    return flags, commands


def validate_profile(profile, sha, target):
    """Validate current and historical profiles without reading mutable tool paths."""
    check(type(profile['schema']) is int and profile['schema'] in (2, 3, 4, 5) and profile['status'] == 'built', 'Incomplete release build profile')
    explicit_native = profile['schema'] >= 3
    check(profile['source_commit'] == sha and full_hash(sha, 40), 'Build receipt belongs to another source')
    check(profile['source_repository'] == 'https://github.com/oxtech-ember/EmberBSD', 'Wrong source repository')
    check(profile['architecture'] == target and profile['machine'] == target_definition(target)['machine'], 'Wrong release target')
    check(full_hash(profile['source_sha256']) and type(profile['source_timestamp']) is int and profile['source_timestamp'] > 0,
          'Invalid source export identity')
    version = product_version(profile['product_version'])
    receipt = profile['sdk_receipt']
    check(receipt['format'] == 'emberbsd-os-release-build' and receipt['version'] == profile['schema'] and
          receipt['qualification'] == 'built-and-inspected' and receipt['execution_acceptance'] == 'not-run', 'Incomplete SDK build receipt')
    check(set(receipt['sources']) == {'os', 'ports', 'sdk'} and receipt['sources']['os']['commit'] == sha and
          all(full_hash(v['commit'], 40) for v in receipt['sources'].values()), 'Mixed source receipts')
    signature = receipt['signature']
    check(signature['product_version'] == version, 'Mixed source product version')
    libedit.validate_identity(signature['libedit'])
    check(signature['libedit']['recipe']['ports_commit'] == receipt['sources']['ports']['commit'], 'Mixed libedit Ports source')
    installed = receipt['libedit_installation']
    check(isinstance(installed, dict) and set(installed) == {'distinfo_sha256', 'library', 'library_sha256', 'machine', 'soname'} and
          installed['distinfo_sha256'] == signature['libedit']['recipe']['distinfo_sha256'] and
          full_hash(installed['library_sha256']) and installed['machine'] == target_definition(target)['elf'] and
          installed['soname'] == 'libedit.so.3' and isinstance(installed['library'], str) and
          re.fullmatch(r'(?:lib|usr/lib)/libedit\.so\.3(?:\.[0-9]+)?', installed['library']) is not None,
          'Incomplete native libedit installation receipt')
    compiler = signature['compiler']
    clang = profile['schema'] in (4, 5)
    base_cxx = llvm_runtime_install.base_cxx_enabled(signature.get('flags', []))
    check(profile['schema'] != 5 or base_cxx, 'Common-runtime release must select base C++')
    if clang:
        check(signature['compiler_family'] == 'clang', 'Mixed release compiler family')
        validate_clang_identity(compiler, target_definition(target)['triplet'])
        runtime = llvm_runtime.validate_identity(signature['runtime']['manifest'], target=target,
                                                 required_components=llvm_runtime.FULL_COMPONENTS)
        check(runtime['llvm_version'] == compiler['version'] and
              runtime['source'] == {key: signature['llvm_source']['source'][key] for key in ('archive', 'sha512')},
              'Mixed common LLVM runtime identity')
    else:
        check(not base_cxx, 'GCC release cannot select the LLVM libc++ base aliases')
        check(signature.get('compiler_family', 'gcc') == 'gcc', 'Mixed historical compiler family')
        check(compiler['version'] == '16.2.0' and compiler['threads'] == 'posix' and
          compiler['triplet'] == target_definition(target)['triplet'], 'Unverified common GCC16 compiler')
        binaries = compiler['binaries']
        expected_binaries = {'gcc', 'g++', 'cpp', 'ar', 'as', 'ld', 'nm', 'objcopy', 'objdump', 'ranlib', 'readelf', 'strip',
                         'resolved-cc1', 'resolved-cc1plus', 'resolved-collect2', 'resolved-lto1', 'resolved-as', 'resolved-ld'}
        check(set(binaries) == expected_binaries and all(full_hash(b['sha256']) for b in binaries.values()), 'Missing compiler identity hashes')
        metadata = signature['runtime']['report']['metadata']
        check(metadata['GCC_VERSION'] == '16.2.0' and metadata['GCC_RUNTIME_TRIPLET'] == compiler['triplet'] and
          metadata['GCC_RUNTIME_TARGET'] == target and full_hash(metadata['EMBERBSD_SEED_SOURCE'], 40), 'Mixed GCC16 runtime receipt')
        for key in ('GCC_RUNTIME_INPUTS_SHA256', 'GCC_RUNTIME_STAGE_SHA256', 'GCC_RUNTIME_LINKS_SHA256'):
            check(full_hash(metadata[key]), 'Missing runtime receipt hash')
    paths = receipt['paths']
    if 'common_sources' in signature:
        common_sources = signature['common_sources']
        check(isinstance(common_sources, dict) and {'libarchive', 'libevent'} <= set(common_sources) <=
              {'libarchive', 'libevent', 'byacc', 'texinfo', 'rcs', 'diffutils', 'grep', 'ekermit', 'nvi', 'tdb', 'ppp', 'top', 'x11'}, 'Invalid common source selection')
        check(('x11' in common_sources) == ('x11-source' in paths), 'Mixed X11 source selection')
        for name, package in (('libarchive', 'archivers/libarchive'), ('libevent', 'devel/libevent'), ('byacc', 'devel/byacc'), ('texinfo', 'devel/gtexinfo'), ('rcs', 'devel/rcs'), ('diffutils', 'devel/diffutils'), ('grep', 'textproc/grep'), ('ekermit', 'comms/ekermit'), ('nvi', 'editors/nvi'), ('tdb', 'databases/tdb'), ('ppp', 'net/pppd'), ('top', 'sysutils/top'),
                              ('x11', X11_PACKAGE)):
            if name not in common_sources:
                continue
            record = common_sources[name]
            check(isinstance(record, dict) and record.get('format') == 'emberbsd-prepared-source' and
                  type(record.get('version')) is int and record['version'] == 1 and
                  record.get('package') == package and full_hash(record.get('tree_sha256', '')) and
                  isinstance(record.get('identity'), dict) and bool(record['identity']),
                  'Invalid common source receipt')
            check(isinstance(paths.get(name + '-source'), str) and
                  paths[name + '-source'].startswith('/') and not re.search(r'\s', paths[name + '-source']),
                  'Invalid common source path')
    for key in ('os', 'ports', 'sdk-source', 'compiler', 'runtime-prefix', 'objects', 'tools', 'destination', 'libedit-source'):
        check(isinstance(paths[key], str) and paths[key].startswith('/') and not re.search(r'\s', paths[key]), 'Invalid producer path')
    if 'host-binutils' in paths:
        check(isinstance(paths['host-binutils'], str) and paths['host-binutils'].startswith('/') and
              not re.search(r'\s', paths['host-binutils']), 'Invalid host Binutils producer path')
    check(signature['target'] == target_definition(target), 'Mixed target signature')
    # This checks saved command consistency without reading mutable tools.
    native = None
    if explicit_native:
        validate_native_binding(signature, paths, profile['shell'])
        native = signature['native_toolset']
    flags, commands = producer_commands(target, paths, signature['host_tools'], sha, profile['source_timestamp'], profile['jobs'], profile['shell'], native,
                                       signature.get('compiler_family'), base_cxx=base_cxx,
                                       install_base_cxx=profile['schema'] == 5)
    check(signature['flags'] == flags and profile['commands'] == commands, 'Build command does not select the exact required profile')
    executed = [r for r in receipt['commands'] if r.get('scope') != 'runtime-check']
    check([r['argv'] for r in executed] == commands and all(r['status'] == 'passed' and r['exit_status'] == 0 for r in executed),
          'Missing successful producer execution')
    environment = signature['environment']
    check(set(environment).issubset({'PATH', 'HOME', 'TMPDIR', 'TMP', 'TEMP', 'USER', 'LOGNAME', 'LC_ALL', 'MAKECONF',
                                   'GIT_OPTIONAL_LOCKS', 'GIT_NO_LAZY_FETCH', 'PYTHONDONTWRITEBYTECODE', 'EMBER_NATIVE_TOOLS'}) and
          environment['LC_ALL'] == 'C' and environment['MAKECONF'] == '/dev/null', 'Uncontrolled build environment')
    x11 = 'x11-source' in paths
    if x11:
        require_x11_profile(target, signature.get('compiler_family'))
    outputs = ([KERNEL] + ['release/evbarm-aarch64/binary/sets/' + n for n in release_sets(x11)] if target == 'aarch64'
               else ['release/images/' + n for n in images(version)])
    check(set(profile['output_sha256']) == set(outputs) and all(full_hash(h) for h in profile['output_sha256'].values()), 'Incomplete output hashes')
    check(receipt['outputs'] == profile['output_sha256'], 'Mixed output receipts')


class OSReleaseBuilder(SysrootBuilder):
    def __init__(self, options):
        target = options.get('target')
        self.FLAGS = flags_for(target, options.get('compiler-family', 'clang'), bool(options.get('x11-source')))
        super().__init__(options)

    def configure_signature(self):
        if type(self.options.get('base-cxx', False)) is not bool:
            raise SDKError('base C++ selection must be boolean')
        if self.options.get('base-cxx', False) and self.compiler_family() != 'clang':
            raise SDKError('base C++ aliases require the selected Clang runtime')
        if 'x11-source' in self.paths:
            require_x11_profile(self.options['target'], self.compiler_family())
            check_x11_pin(self.paths['os'], self.common_source_records.get('x11'))
        self.timestamp = self.source_timestamp()
        self.product_version = source_version(Path(self.paths['os'], 'share/mk/bsd.own.mk').read_text())
        self.signature['product_version'] = self.product_version
        self.flags, self.release_commands = producer_commands(self.options['target'], self.paths, self.host_tools,
            self.sources['os']['commit'], self.timestamp, self.options['jobs'], self.shell, self.native_selection,
            self.compiler_family(), base_cxx=self.options.get('base-cxx', False))
        self.signature['flags'] = self.flags
        self.signature['release_profile'] = self.options['target']

    def preflight(self):
        check('destination' in self.options, 'OS release requires an explicit destination stage')
        super().preflight()
        version = (5 if llvm_runtime_install.base_cxx_enabled(self.flags) else 4) if self.compiler_family() == 'clang' else 3
        self.receipt.update(format='emberbsd-os-release-build', version=version, scope='os-release-build',
                            previous_contexts=self.contexts, plan=[self.runtime_command, *self.release_commands, self.runtime_command],
                            not_checked=['target execution', 'VM installation', 'physical hardware', 'binary publication'])
        self.receipt.pop('copy', None)
        return self.receipt

    def build_payload(self):
        output = Path(self.paths['destination'])
        self.git(self.paths['os'], 'archive', '--format=tar.gz',
                 '-o', str(output / 'source.tar.gz'), self.sources['os']['commit'])
        self.source_sha256 = inventory.sha256_file(output / 'source.tar.gz')
        base_cxx = llvm_runtime_install.base_cxx_enabled(self.flags)
        imported = False
        for command in self.release_commands:
            if base_cxx and command[-1] == 'checkflist':
                self.import_base_runtime()
                imported = True
            self.dispatch(command)
        if base_cxx and not imported:
            raise SDKError('base C++ release has no runtime installation step')
        # Historical profiles keep their original build-only runtime input.
        # An enabled base C++ profile imports the same common payload and
        # records its ownership through the native unprivileged installer.
        if self.verify_runtime() != self.runtime_record:
            raise SDKError('runtime inputs changed during release build')
        if self.options['target'] == 'aarch64':
            kernel = Path(self.paths['objects'], 'kernels/EMBER64/netbsd')
            with open(kernel, 'rb') as source, open(output / KERNEL, 'xb') as destination:
                with gzip.GzipFile(filename='', mode='wb', fileobj=destination, mtime=0) as packed:
                    shutil.copyfileobj(source, packed)

    def import_base_runtime(self):
        from .native_runtime_install import NativeRuntimeInstaller
        from .llvm_runtime_sets import prepare_sets_overlay
        destination = os.path.join(self.paths['destination'], 'dest')
        installer = NativeRuntimeInstaller(destination,
            {'install': self.paths['tools'] + '/bin/' + self.target['triplet'] + '-install'},
            self.paths['os'], lambda argv: self.capture(*argv))
        self.copy_runtime(destination=destination, installer=installer)
        prepare_sets_overlay(os.path.join(self.paths['os'], 'distrib/sets'),
                             os.path.join(self.paths['destination'], 'llvm-sets'),
                             self.runtime_record)

    def inspect_payload(self):
        output = Path(self.paths['destination'])
        if llvm_runtime_install.base_cxx_enabled(self.flags):
            llvm_runtime_install.validate_base_aliases(output / 'dest',
                                                       self.runtime_record['manifest'])
        self.receipt['libedit_installation'] = libedit.validate_installation(
            output / 'dest', self.libedit_record, self.target['elf'])
        names = ([KERNEL] + ['release/evbarm-aarch64/binary/sets/' + n for n in release_sets('x11-source' in self.paths)]
                 if self.options['target'] == 'aarch64' else ['release/images/' + n for n in images(self.product_version)])
        hashes = {}
        for name in names:
            check((output / name).stat().st_size > 0, 'The build did not create ' + name)
            hashes[name] = inventory.sha256_file(output / name)
        return hashes

    def run(self):
        receipt = super().run()
        if self.options.get('plan') or receipt['format'] != 'emberbsd-os-release-build':
            return receipt
        # The coordinator's final child already wrote and checked this profile.
        if not hasattr(self, 'source_sha256'):
            return receipt
        output = Path(self.paths['destination'])
        profile = dict(schema=receipt['version'], status='built', source_repository='https://github.com/oxtech-ember/EmberBSD',
                       source_commit=self.sources['os']['commit'], source_timestamp=self.timestamp, product_version=self.product_version,
                       source_sha256=self.source_sha256, architecture=self.options['target'], machine=self.target['machine'],
                       jobs=self.options['jobs'], shell=self.shell, commands=self.release_commands,
                       output_sha256=receipt['outputs'], sdk_receipt=receipt)
        validate_profile(profile, self.sources['os']['commit'], self.options['target'])
        jsonio.write_json(output / 'build-profile.json', profile)
        # Expose success only after receipts and all output checks finish.
        with open(output / 'build-success', 'x') as handle:
            handle.write(self.sources['os']['commit'] + '\n')
        return receipt
