# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD ember/release/build-{amd64,aarch64}.sh and SDK SysrootBuilder.
# AI-assisted migration to the common SDK producer controls.
"""Complete OS release orchestration on the shared SDK compiler/runtime contract."""

import gzip
import tempfile
import os
from pathlib import Path
import re
import shutil

from .errors import SDKError
from . import inventory, jsonio, libedit, llvm_runtime, llvm_runtime_install, native_toolset, process
from .target_toolchain import validate_clang_identity
from .sysroot_builder import (SysrootBuilder, native_host_flags, gettext_target_host_flags, gnu_target_host_flags, check_native_command_identity,
                              validate_native_binding)
from .targets import target_definition
from .os_release import (check, full_hash, tag, images, product_version, git_source_identity, release_schema, KERNEL, SYSROOT_SETS,
                         GCC_SCHEMAS, CLANG_SCHEMAS, BASE_CXX_SCHEMAS, SOURCE_TREE_SCHEMAS)

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


def gettext_libtool_command(paths, git, target, jobs, compiler_family, host_family):
    """Build the common cross-libtool in the selected OS tool context."""
    command = [paths['python'], '-B', os.path.join(paths['ports'],
               'profiles/common-build-tools/cross/prepare-libtool.py'),
               '--sdk-source', paths['sdk-source'], '--host-prefix', paths['host-prefix'],
               '--compiler', paths['compiler'], '--os-tools', paths['tools'],
               '--sysroot', os.path.join(paths['destination'], 'dest'),
               '--output', os.path.join(paths['tools'], 'gettext-libtool'),
               '--work-parent', paths['objects'], '--lock-directory', os.path.dirname(paths['host-prefix']),
               '--git', git, '--target', target,
               '--jobs', jobs, '--compiler-family', compiler_family,
               '--host-compiler-family', host_family]
    if 'pkgsrc-export' in paths:
        command += ['--pkgsrc-export', paths['pkgsrc-export']]
    if compiler_family == 'clang' and 'llvm-runtime-stage' in paths:
        command += ['--runtime-stage', paths['llvm-runtime-stage']]
        if 'llvm-runtime-base' in paths:
            command += ['--runtime-base', paths['llvm-runtime-base']]
    return command


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
    for name in ('libarchive', 'libevent', 'openssl', 'libfido2', 'libcbor', 'libfetch', 'openldap', 'unbound', 'wpa', 'heimdal', 'openssh', 'byacc', 'texinfo', 'rcs', 'diffutils', 'grep', 'ekermit', 'nvi', 'tdb', 'ppp', 'top', 'iscsi', 'libpcap', 'tcpdump', 'tcp-wrappers', 'ntp', 'libopts', 'gettext', 'libxml2'):
        if name + '-source' in paths:
            flags.append(f'EMBER_{name.upper().replace("-", "_")}_SOURCE={paths[name + "-source"]}')
    if 'openssl-source' in paths and native_selection is not None:
        flags.append('HOST_PERL=' + native_selection['tools']['perl'])
    if ('openssl-source' in paths or 'tdb-source' in paths) and native_selection is not None:
        flags.append('HOST_PYTHON=' + native_selection['tools']['python'])
    if 'openssl-source' in paths and not any(name in paths for name in ('texinfo-source', 'rcs-source', 'diffutils-source', 'grep-source', 'gettext-source')) and native_selection is not None:
        flags.append('HOST_GMAKE=' + native_selection['tools']['gmake'])
    if any(name in paths for name in ('texinfo-source', 'rcs-source', 'diffutils-source', 'grep-source', 'gettext-source')) and native_selection is not None:
        flags += gnu_target_host_flags(native_selection)
    if 'gettext-source' in paths and native_selection is not None:
        flags += gettext_target_host_flags(native_selection)
        flags += ['GNU_TARGET_LIBTOOL=' + paths['tools'] + '/bin/target-libtool',
                  'EMBER_TARGET_TOOL_DIR=' + paths['tools'] + '/gettext-libtool/current/target-tools']
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
    # gettext's target libtool is configured against a complete foundation.
    # Keep kernel/tool production ahead of the ordinary libraries stage.
    if 'gettext-source' in paths:
        if target == 'aarch64':
            commands.insert(1, base + ['libs'])
        else:
            commands[0] = base + [commands[0][-1]]
            commands[0:0] = [base + ['tools'], base + ['libs']]
        provider = gettext_libtool_command(paths, paths['git'], target, jobs, compiler_family,
                                          native_selection['compiler']['family'])
        commands.insert(2, provider)
    source_flags = [f'EMBER_{name.upper().replace("-", "_")}_SOURCE={paths[name + "-source"]}'
                    for name in ('libarchive', 'libevent', 'openssl', 'libfido2', 'libcbor', 'libfetch', 'openldap', 'unbound', 'wpa', 'heimdal', 'openssh', 'byacc', 'texinfo', 'rcs', 'diffutils', 'grep', 'ekermit', 'nvi', 'tdb', 'ppp', 'top', 'iscsi', 'libpcap', 'tcpdump', 'tcp-wrappers', 'ntp', 'libopts', 'gettext', 'libxml2') if name + '-source' in paths]
    if 'openssl-source' in paths and native_selection is not None:
        source_flags.append('HOST_PERL=' + native_selection['tools']['perl'])
    if ('openssl-source' in paths or 'tdb-source' in paths) and native_selection is not None:
        source_flags.append('HOST_PYTHON=' + native_selection['tools']['python'])
    if 'openssl-source' in paths and not any(name in paths for name in ('texinfo-source', 'rcs-source', 'diffutils-source', 'grep-source', 'gettext-source')) and native_selection is not None:
        source_flags.append('HOST_GMAKE=' + native_selection['tools']['gmake'])
    if any(name in paths for name in ('texinfo-source', 'rcs-source', 'diffutils-source', 'grep-source', 'gettext-source')) and native_selection is not None:
        source_flags += gnu_target_host_flags(native_selection)
    if 'gettext-source' in paths and native_selection is not None:
        source_flags += gettext_target_host_flags(native_selection)
        source_flags += ['GNU_TARGET_LIBTOOL=' + paths['tools'] + '/bin/target-libtool',
                         'EMBER_TARGET_TOOL_DIR=' + paths['tools'] + '/gettext-libtool/current/target-tools']
    for command in commands:
        if command[0].startswith(paths['tools'] + '/bin/nbmake-'):
            command[-1:-1] = source_flags
    return flags, commands


def validate_profile(profile, sha, target):
    """Validate current and historical profiles without reading mutable tool paths."""
    check(type(profile['schema']) is int and profile['schema'] in GCC_SCHEMAS + CLANG_SCHEMAS and profile['status'] == 'built', 'Incomplete release build profile')
    explicit_native = profile['schema'] >= 3
    check(profile['source_commit'] == sha and full_hash(sha, 40), 'Build receipt belongs to another source')
    check(profile['source_repository'] == 'https://github.com/oxtech-ember/EmberBSD', 'Wrong source repository')
    check(profile['architecture'] == target and profile['machine'] == target_definition(target)['machine'], 'Wrong release target')
    if profile['schema'] in SOURCE_TREE_SCHEMAS:
        check(full_hash(profile['source_tree'], 40) and 'source_sha256' not in profile, 'Invalid source tree identity')
    else:
        check(full_hash(profile['source_sha256']), 'Invalid source export identity')
    check(type(profile['source_timestamp']) is int and profile['source_timestamp'] > 0, 'Invalid source timestamp')
    version = product_version(profile['product_version'])
    receipt = profile['sdk_receipt']
    check(receipt['format'] == 'emberbsd-os-release-build' and receipt['version'] == profile['schema'] and
          receipt['qualification'] == 'built-and-inspected' and receipt['execution_acceptance'] == 'not-run', 'Incomplete SDK build receipt')
    check(set(receipt['sources']) == {'os', 'ports', 'sdk'} and receipt['sources']['os']['commit'] == sha and
          all(full_hash(v['commit'], 40) for v in receipt['sources'].values()), 'Mixed source receipts')
    signature = receipt['signature']
    check(signature['product_version'] == version, 'Mixed source product version')
    libedit.validate_identity(signature['libedit'])
    installed = receipt['libedit_installation']
    check(isinstance(installed, dict) and set(installed) == {'distinfo_sha256', 'library', 'library_sha256', 'machine', 'soname'} and
          installed['distinfo_sha256'] == signature['libedit']['recipe']['distinfo_sha256'] and
          full_hash(installed['library_sha256']) and installed['machine'] == target_definition(target)['elf'] and
          installed['soname'] == 'libedit.so.3' and isinstance(installed['library'], str) and
          re.fullmatch(r'(?:lib|usr/lib)/libedit\.so\.3(?:\.[0-9]+)?', installed['library']) is not None,
          'Incomplete native libedit installation receipt')
    compiler = signature['compiler']
    clang = profile['schema'] in CLANG_SCHEMAS
    base_cxx = llvm_runtime_install.base_cxx_enabled(signature.get('flags', []))
    check(profile['schema'] not in BASE_CXX_SCHEMAS or base_cxx, 'Common-runtime release must select base C++')
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
    if any(name + '-source' in paths for name in ('openssl', 'libfido2', 'libcbor', 'libfetch', 'openldap', 'unbound', 'wpa', 'heimdal', 'openssh')) and 'common_sources' not in signature:
        raise SDKError('Missing common source selection')
    if ('gettext-source' in paths or 'libxml2-source' in paths) and 'common_sources' not in signature:
        raise SDKError('Mixed gettext and libxml2 source selection')
    if 'common_sources' in signature:
        common_sources = signature['common_sources']
        check(isinstance(common_sources, dict) and {'libarchive', 'libevent'} <= set(common_sources) <=
              {'libarchive', 'libevent', 'openssl', 'libfido2', 'libcbor', 'libfetch', 'openldap', 'unbound', 'wpa', 'heimdal', 'openssh', 'byacc', 'texinfo', 'rcs', 'diffutils', 'grep', 'ekermit', 'nvi', 'tdb', 'ppp', 'top', 'iscsi', 'libpcap', 'tcpdump', 'tcp-wrappers', 'ntp', 'libopts', 'gettext', 'libxml2', 'x11'}, 'Invalid common source selection')
        check(('x11' in common_sources) == ('x11-source' in paths), 'Mixed X11 source selection')
        check(('openssl' in common_sources) == ('openssl-source' in paths), 'Mixed OpenSSL source selection')
        check(('libfido2' in common_sources) == ('libfido2-source' in paths), 'Mixed libfido2 source selection')
        check(('libcbor' in common_sources) == ('libcbor-source' in paths), 'Mixed libcbor source selection')
        check(('libfetch' in common_sources) == ('libfetch-source' in paths), 'Mixed libfetch source selection')
        check(('openldap' in common_sources) == ('openldap-source' in paths), 'Mixed openldap source selection')
        check(('unbound' in common_sources) == ('unbound-source' in paths), 'Mixed unbound source selection')
        check(('wpa' in common_sources) == ('wpa-source' in paths), 'Mixed wpa source selection')
        check(('heimdal' in common_sources) == ('heimdal-source' in paths), 'Mixed heimdal source selection')
        check(('openssh' in common_sources) == ('openssh-source' in paths), 'Mixed openssh source selection')
        check(('ntp' in common_sources) == ('libopts' in common_sources) ==
              ('ntp-source' in paths) == ('libopts-source' in paths),
              'Mixed NTP and libopts source selection')
        check(('gettext' in common_sources) == ('libxml2' in common_sources) ==
              ('gettext-source' in paths) == ('libxml2-source' in paths),
              'Mixed gettext and libxml2 source selection')
        for name, package in (('libarchive', 'archivers/libarchive'), ('libevent', 'devel/libevent'), ('openssl', 'security/openssl'), ('libfido2', 'security/libfido2'), ('libcbor', 'devel/libcbor'), ('libfetch', 'net/libfetch'), ('openldap', 'databases/openldap'), ('unbound', 'net/unbound'), ('wpa', 'net/wpa_supplicant'), ('heimdal', 'security/heimdal'), ('openssh', 'security/openssh'), ('byacc', 'devel/byacc'), ('texinfo', 'devel/gtexinfo'), ('rcs', 'devel/rcs'), ('diffutils', 'devel/diffutils'), ('grep', 'textproc/grep'), ('ekermit', 'comms/ekermit'), ('nvi', 'editors/nvi'), ('tdb', 'databases/tdb'), ('ppp', 'net/pppd'), ('top', 'sysutils/top'), ('iscsi', 'devel/netbsd-iscsi-lib'), ('libpcap', 'net/libpcap'), ('tcpdump', 'net/tcpdump'), ('tcp-wrappers', 'security/tcp_wrappers'), ('ntp', 'net/ntp4'), ('libopts', 'devel/autogen'), ('gettext', 'devel/gettext'), ('libxml2', 'textproc/libxml2'),
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
                                       install_base_cxx=profile['schema'] in BASE_CXX_SCHEMAS)
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


def make_release_profile(receipt, timestamp, jobs, shell, commands, source_tree):
    target = receipt['signature']['release_profile']
    sha = receipt['sources']['os']['commit']
    profile = dict(schema=receipt['version'], status='built',
                   source_repository='https://github.com/oxtech-ember/EmberBSD',
                   source_commit=sha, source_timestamp=timestamp,
                   product_version=receipt['signature']['product_version'],
                   source_tree=source_tree, architecture=target,
                   machine=target_definition(target)['machine'], jobs=jobs, shell=shell,
                   commands=commands, output_sha256=receipt['outputs'], sdk_receipt=receipt)
    validate_profile(profile, sha, target)
    return profile


def publish_release_profile(output, profile):
    """Publish only absent, or exactly matching, profile and success marker."""
    output = Path(output)
    destination = output / 'build-profile.json'
    marker = output / 'build-success'
    marker_content = profile['source_commit'] + '\n'
    if os.path.lexists(marker):
        check(os.path.lexists(destination), 'Success marker exists without release profile')
        check(marker.is_file() and not marker.is_symlink() and marker.read_text() == marker_content,
              'Existing release success marker differs')
    if os.path.lexists(destination):
        check(not destination.is_symlink() and jsonio.read_json(destination) == profile, 'Existing release profile differs')
    else:
        with tempfile.NamedTemporaryFile(dir=output, prefix='.profile-', delete=False) as tmp:
            tmpname = tmp.name
            os.fchmod(tmp.fileno(), 0o644)
            tmp.write(jsonio.json_bytes(profile)); tmp.flush(); os.fsync(tmp.fileno())
        try:
            os.link(tmpname, destination)
        finally:
            os.unlink(tmpname)
    if marker.exists():
        check(marker.is_file() and not marker.is_symlink() and marker.read_text() == marker_content,
              'Existing release success marker differs')
    else:
        with tempfile.NamedTemporaryFile(dir=output, prefix='.success-', delete=False) as tmp:
            tmpname = tmp.name
            os.fchmod(tmp.fileno(), 0o644)
            tmp.write(marker_content.encode()); tmp.flush(); os.fsync(tmp.fileno())
        try:
            os.link(tmpname, marker)
        finally:
            os.unlink(tmpname)


def finalize_release(destination):
    """Finalize an unchanged, successfully inspected release without rebuilding."""
    output = Path(destination).resolve(strict=True)
    receipt_path = output / SysrootBuilder.RECEIPT
    receipt = jsonio.read_json(receipt_path)
    paths = receipt['paths']
    check(str(output) == paths['destination'], 'Release destination differs from receipt')
    git = paths.get('git', 'git')
    lock_root = os.path.join(paths['sdk'], 'obj')
    with process.ContextLocks([paths['objects'], paths['tools'], str(output)], lock_root):
        check(jsonio.read_json(receipt_path) == receipt, 'Release receipt changed')
        check(not (output / SysrootBuilder.MARKER).exists(), 'Release remains incomplete')
        check(receipt.get('qualification') == 'built-and-inspected' and 'error' not in receipt,
              'Release has not completed inspection')
        check(all(row.get('status') == 'passed' for row in receipt['commands']),
              'Release contains a failed command')
        # The build checked its sources once and recorded them; finalization
        # publishes the inspected outputs, identified by their hashes below.
        identity = receipt['signature']['libedit']
        target = receipt['signature']['release_profile']
        installed = libedit.validate_installation(output / 'dest', identity, target_definition(target)['elf'])
        check(installed == receipt['libedit_installation'], 'Installed libedit differs from receipt')
        signature = receipt['signature']
        check(signature.get('compiler_family') == 'clang', 'Finalization requires a selected LLVM runtime')
        check(receipt['version'] in SOURCE_TREE_SCHEMAS, 'Release receipt predates Git tree source identity; rebuild it')
        runtime = llvm_runtime.validate_stage(paths['llvm-runtime-stage'], target=target,
            required_components=llvm_runtime.FULL_COMPONENTS, base=paths.get('llvm-runtime-base'),
            content=False)
        check(runtime == signature['runtime']['manifest'], 'LLVM runtime differs from receipt')
        if receipt['version'] in BASE_CXX_SCHEMAS:
            llvm_runtime.validate_install(output / 'dest', runtime, target=target)
            llvm_runtime_install.validate_base_aliases(output / 'dest', runtime)
        # The plan starts with the runtime check. Older receipts also end
        # with it; the runtime is now checked once, before the build.
        plan = receipt['plan']
        commands = plan[1:-1] if len(plan) > 2 and plan[-1] == plan[0] else plan[1:]
        command = commands[0]
        shell = command[0]
        job_values = [v[2:] for v in command if re.fullmatch(r'-j[0-9]+', v)]
        stamps = [v.split('=', 1)[1] for v in signature['flags'] if v.startswith('MKREPRO_TIMESTAMP=')]
        check(len(job_values) == 1 and len(stamps) == 1 and stamps[0].isdigit(),
              'Missing saved release job count or timestamp')
        jobs, timestamp = job_values[0], int(stamps[0])
        # The saved commit names the source; its Git tree is the source identity.
        source_tree, version = git_source_identity(git, paths['os'], receipt['sources']['os']['commit'])
        check(version == signature['product_version'], 'OS product version differs from receipt')
        profile = make_release_profile(receipt, timestamp, jobs, shell, commands, source_tree)
        for name, expected in receipt['outputs'].items():
            check(inventory.sha256_file(output / name) == expected, 'Release output changed: ' + name)
        publish_release_profile(output, profile)
        return profile


class OSReleaseBuilder(SysrootBuilder):
    # A publishable release does not continue after failed stages.
    RESUMES_FAILED_STAGES = False
    OBJECT_MODES = ('incremental', 'fresh')
    # Flags that only name the OS commit being built. build.sh -u rebuilds
    # what an OS source change affects; these values do not select objects.
    COMMIT_FLAGS = ('MKREPRO_TIMESTAMP=', 'EMBER_BUILD_ID=', 'BUILDINFO=')
    # The Ports pkgsrc export stage, keyed by the Git fingerprint of its inputs.
    PKGSRC_EXPORT_STAGE = 'scripts/pkgsrc-export-stage.py'

    def __init__(self, options, *, receipts=None):
        self.object_mode = options.get('object-mode', 'incremental')
        if self.object_mode not in self.OBJECT_MODES:
            raise SDKError('object mode must be incremental or fresh')
        # Set by the LLVM coordinator when it found the tools directory new
        # before this run's first phase.
        self.tools_new_in_run = False
        target = options.get('target')
        self.FLAGS = flags_for(target, options.get('compiler-family', 'clang'), bool(options.get('x11-source')))
        super().__init__(options, receipts=receipts)

    @classmethod
    def prepared_source_profile(cls, options):
        # build.sh release: includes, libraries and the whole tree.
        from .prepared_sources import RELEASE_TRAVERSALS
        return (flags_for(options.get('target'), options.get('compiler-family', 'clang'), bool(options.get('x11-source'))),
                RELEASE_TRAVERSALS)

    def reuse_signature(self, signature):
        # The same profile, toolchain and configuration at another OS commit
        # updates the kept objects; the commit metadata itself is not compared.
        if isinstance(signature.get('flags'), list):
            signature['flags'] = [flag for flag in signature['flags']
                                  if not (isinstance(flag, str) and flag.startswith(self.COMMIT_FLAGS))]
        return super().reuse_signature(signature)

    def configure_signature(self):
        if type(self.options.get('base-cxx', False)) is not bool:
            raise SDKError('base C++ selection must be boolean')
        if self.options.get('base-cxx', False) and self.compiler_family() != 'clang':
            raise SDKError('base C++ aliases require the selected Clang runtime')
        if 'x11-source' in self.paths:
            require_x11_profile(self.options['target'], self.compiler_family())
            check_x11_pin(self.paths['os'], self.common_source_records.get('x11'))
        self.timestamp = self.source_timestamp()
        self.select_pkgsrc_export()
        self.source_tree, self.product_version = git_source_identity(
            self.git_command, self.paths['os'], self.sources['os']['commit'], self.environment)
        self.signature['product_version'] = self.product_version
        self.flags, self.release_commands = producer_commands(self.options['target'], self.paths, self.host_tools,
            self.sources['os']['commit'], self.timestamp, self.options['jobs'], self.shell, self.native_selection,
            self.compiler_family(), base_cxx=self.options.get('base-cxx', False))
        self.signature['flags'] = self.flags
        self.signature['release_profile'] = self.options['target']

    def pkgsrc_export_command(self, action, *args):
        patch = native_toolset.load_host_prefix(self.native.bundle_root, required_roles={'gpatch'}).tool('gpatch')
        return [self.paths['python'], '-B', os.path.join(self.paths['ports'], self.PKGSRC_EXPORT_STAGE),
                action, *args, '--git', self.paths['git'], '--patch', patch]

    def select_pkgsrc_export(self):
        # Only the gettext cross-libtool reads a pkgsrc export. Ports pins
        # without the stage script keep the per-run export.
        if ('gettext-source' not in self.paths or
                not os.path.isfile(os.path.join(self.paths['ports'], self.PKGSRC_EXPORT_STAGE))):
            return
        record = jsonio.loads_json(self.capture(*self.pkgsrc_export_command('fingerprint'))[0])
        fingerprint = record.get('fingerprint') if isinstance(record, dict) else None
        check(full_hash(fingerprint), 'invalid pkgsrc export fingerprint')
        self.paths['pkgsrc-export'] = os.path.join(self.paths['sdk'], 'stages', 'pkgsrc-export', fingerprint)

    def prepare_pkgsrc_export(self):
        # A separate step before the first OS build command: it never runs
        # alongside the compile, and later builds reuse the same stage.
        root = os.path.dirname(self.paths['pkgsrc-export'])
        self.mkdirs(root, self.paths['sdk'])
        result = jsonio.loads_json(self.capture(*self.pkgsrc_export_command('prepare', root))[0])
        check(isinstance(result, dict) and result.get('directory') == self.paths['pkgsrc-export'] and
              type(result.get('reused')) is bool, 'pkgsrc export stage differs from the selected fingerprint')
        self.receipt['pkgsrc_export'] = dict(fingerprint=result['fingerprint'], reused=result['reused'])

    def preflight(self):
        check('destination' in self.options, 'OS release requires an explicit destination stage')
        super().preflight()
        if self.object_mode == 'fresh':
            # A published release is built once from new trees.
            check(self.contexts['objects'] is None,
                  'fresh object mode needs a new or empty objects directory: ' + self.paths['objects'])
            check(self.contexts['tools'] is None or self.tools_new_in_run,
                  'fresh object mode needs a new or empty tools directory: ' + self.paths['tools'])
        version = release_schema(self.compiler_family() == 'clang', llvm_runtime_install.base_cxx_enabled(self.flags))
        self.receipt.update(format='emberbsd-os-release-build', version=version, scope='os-release-build',
                            previous_contexts=self.contexts, object_mode=self.object_mode, plan=[self.runtime_command, *self.release_commands],
                            not_checked=['target execution', 'VM installation', 'physical hardware', 'binary publication'])
        self.receipt.pop('copy', None)
        return self.receipt

    def build_payload(self):
        output = Path(self.paths['destination'])
        self.payload_built = True
        base_cxx = llvm_runtime_install.base_cxx_enabled(self.flags)
        imported = False
        if 'pkgsrc-export' in self.paths:
            self.prepare_pkgsrc_export()
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
        if not getattr(self, 'payload_built', False):
            return receipt
        output = Path(self.paths['destination'])
        profile = make_release_profile(receipt, self.timestamp, self.options['jobs'], self.shell,
                                       self.release_commands, self.source_tree)
        publish_release_profile(output, profile)
        return receipt
