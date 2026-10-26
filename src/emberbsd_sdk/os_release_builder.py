# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD ember/release/build-{amd64,aarch64}.sh and SDK SysrootBuilder.
# AI-assisted migration to the common checked GCC16 producer controls.
"""Complete OS release orchestration on the shared SDK compiler/runtime contract."""

import gzip
import os
from pathlib import Path
import re
import shutil

from .errors import SDKError
from . import inventory, jsonio, libedit
from .sysroot_builder import (SysrootBuilder, native_host_flags, check_native_command_identity,
                              validate_native_binding)
from .targets import target_definition
from .os_release import check, full_hash, tag, images, product_version, source_version, KERNEL, SYSROOT_SETS


def flags_for(target):
    # OS make owns the toolchain and staged runtime integration. HAVE_GCC stays
    # an upstream source selector; it must not be set to 16 for an external GCC.
    common = list(SysrootBuilder.FLAGS)
    replacements = dict(MKFIRMWARE='yes', MKLINKLIB='yes', MKSTATICLIB='yes',
                        MKPIC='yes', MKPICLIB='yes', MKDYNAMICROOT='yes',
                        MKLIBSTDCXX='yes', MKREPRO='yes', USE_XZ_SETS='yes')
    if target == 'amd64':
        replacements.update(MKKMOD='yes', MKMAN='yes', MKHTML='yes', MKDOC='yes',
                            MKINFO='yes', MKATF='yes', MKKYUA='yes', MKLINT='yes')
    common = [v for v in common if v.split('=', 1)[0] not in replacements]
    return common + [f'{k}={v}' for k, v in replacements.items()]


def producer_commands(target, paths, host, sha, timestamp, jobs, shell, native_selection=None,
                      compiler_family=None):
    definition = target_definition(target)
    # Only portable historical validation uses the original command contract.
    cc = host['cc']['path'] if native_selection is None else native_selection['tools']['cc']
    cxx = host['c++']['path'] if native_selection is None else native_selection['tools']['cxx']
    flags = flags_for(target) + [f'EXTERNAL_TOOLCHAIN={paths["compiler"]}',
        f'EMBER_EXTERNAL_GCC_RUNTIME={paths["runtime-prefix"]}',
        f'EMBER_LIBEDIT_SOURCE={paths["libedit-source"]}',
        f'MKREPRO_TIMESTAMP={timestamp}', f'EMBER_BUILD_ID={sha}', f'BUILDINFO=EmberBSD:{sha}',
        f'KERNOBJDIR={paths["objects"]}/kernels'] + (native_host_flags(native_selection) if native_selection is not None else [])
    if compiler_family is not None:
        check(compiler_family == 'gcc', 'LLVM target runtime integration is not implemented')
        flags.append('EMBER_COMPILER_FAMILY=' + compiler_family)
    if native_selection is None or native_selection['format'] != 'emberbsd-host-prefix':
        position = flags.index(f'MKREPRO_TIMESTAMP={timestamp}')
        flags[position:position] = [f'HOST_CC={cc}', f'HOST_CXX={cxx}']
    if target == 'aarch64':
        flags += ['RELEASEMACHINEDIR=evbarm-aarch64', 'MAKETARSETS=base comp', 'MKDEBUGKERNEL=no']
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
    return flags, commands


def validate_profile(profile, sha, target):
    """Validate current and historical profiles without reading mutable tool paths."""
    check(type(profile['schema']) is int and profile['schema'] in (2, 3) and profile['status'] == 'built', 'Incomplete release build profile')
    explicit_native = profile['schema'] == 3
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
    for key in ('os', 'ports', 'sdk-source', 'compiler', 'runtime-prefix', 'objects', 'tools', 'destination', 'libedit-source'):
        check(isinstance(paths[key], str) and paths[key].startswith('/') and not re.search(r'\s', paths[key]), 'Invalid producer path')
    check(signature['target'] == target_definition(target), 'Mixed target signature')
    # This checks saved command consistency without reading mutable tools.
    native = None
    if explicit_native:
        validate_native_binding(signature, paths, profile['shell'])
        native = signature['native_toolset']
    flags, commands = producer_commands(target, paths, signature['host_tools'], sha, profile['source_timestamp'], profile['jobs'], profile['shell'], native,
                                       signature.get('compiler_family'))
    check(signature['flags'] == flags and profile['commands'] == commands, 'Build command does not select the exact required profile')
    executed = [r for r in receipt['commands'] if r.get('scope') != 'runtime-check']
    check([r['argv'] for r in executed] == commands and all(r['status'] == 'passed' and r['exit_status'] == 0 for r in executed),
          'Missing successful producer execution')
    environment = signature['environment']
    check(set(environment).issubset({'PATH', 'HOME', 'TMPDIR', 'TMP', 'TEMP', 'USER', 'LOGNAME', 'LC_ALL', 'MAKECONF',
                                   'GIT_OPTIONAL_LOCKS', 'GIT_NO_LAZY_FETCH', 'PYTHONDONTWRITEBYTECODE', 'EMBER_NATIVE_TOOLS'}) and
          environment['LC_ALL'] == 'C' and environment['MAKECONF'] == '/dev/null', 'Uncontrolled build environment')
    outputs = ([KERNEL] + ['release/evbarm-aarch64/binary/sets/' + n for n in SYSROOT_SETS] if target == 'aarch64'
               else ['release/images/' + n for n in images(version)])
    check(set(profile['output_sha256']) == set(outputs) and all(full_hash(h) for h in profile['output_sha256'].values()), 'Incomplete output hashes')
    check(receipt['outputs'] == profile['output_sha256'], 'Mixed output receipts')


class OSReleaseBuilder(SysrootBuilder):
    def __init__(self, options):
        target = options.get('target')
        self.FLAGS = flags_for(target)
        super().__init__(options)

    def configure_signature(self):
        self.timestamp = int(self.git(self.paths['os'], 'show', '-s', '--format=%ct', self.sources['os']['commit']))
        self.product_version = source_version(Path(self.paths['os'], 'share/mk/bsd.own.mk').read_text())
        self.signature['product_version'] = self.product_version
        self.flags, self.release_commands = producer_commands(self.options['target'], self.paths, self.host_tools,
            self.sources['os']['commit'], self.timestamp, self.options['jobs'], self.shell, self.native_selection,
            self.compiler_family())
        self.signature['flags'] = self.flags
        self.signature['release_profile'] = self.options['target']

    def preflight(self):
        check('destination' in self.options, 'OS release requires an explicit destination stage')
        super().preflight()
        self.receipt.update(format='emberbsd-os-release-build', version=3, scope='os-release-build',
                            previous_contexts=self.contexts, plan=[self.runtime_command, *self.release_commands, self.runtime_command],
                            not_checked=['target execution', 'VM installation', 'physical hardware', 'binary publication'])
        self.receipt.pop('copy', None)
        return self.receipt

    def build_payload(self):
        output = Path(self.paths['destination'])
        self.capture(self.git_command, '-C', self.paths['os'], 'archive', '--format=tar.gz',
                     '-o', str(output / 'source.tar.gz'), self.sources['os']['commit'])
        self.source_sha256 = inventory.sha256_file(output / 'source.tar.gz')
        for command in self.release_commands:
            self.dispatch(command)
        # The external runtime is a build-only input. A Ports target package
        # owns its deployed DSOs; OS sets must not acquire the SDK payload.
        if self.verify_runtime() != self.runtime_record:
            raise SDKError('runtime inputs changed during release build')
        if self.options['target'] == 'aarch64':
            kernel = Path(self.paths['objects'], 'kernels/EMBER64/netbsd')
            with open(kernel, 'rb') as source, open(output / KERNEL, 'xb') as destination:
                with gzip.GzipFile(filename='', mode='wb', fileobj=destination, mtime=0) as packed:
                    shutil.copyfileobj(source, packed)

    def inspect_payload(self):
        output = Path(self.paths['destination'])
        self.receipt['libedit_installation'] = libedit.validate_installation(
            output / 'dest', self.libedit_record, self.target['elf'])
        names = ([KERNEL] + ['release/evbarm-aarch64/binary/sets/' + n for n in SYSROOT_SETS]
                 if self.options['target'] == 'aarch64' else ['release/images/' + n for n in images(self.product_version)])
        hashes = {}
        for name in names:
            check((output / name).stat().st_size > 0, 'The build did not create ' + name)
            hashes[name] = inventory.sha256_file(output / name)
        return hashes

    def run(self):
        receipt = super().run()
        if self.options.get('plan'):
            return receipt
        output = Path(self.paths['destination'])
        profile = dict(schema=3, status='built', source_repository='https://github.com/oxtech-ember/EmberBSD',
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
