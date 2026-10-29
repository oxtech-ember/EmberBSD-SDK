# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD; AI-assisted OS and Ports build orchestration.
"""Checked separate-stage OS sysroot builder. No build occurs on import."""

import atexit
import copy
import hashlib
import itertools
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import signal
import stat
import sysconfig
import tempfile
import time
from . import API_VERSION, inventory, jsonio, libedit, native_os_routing, native_toolset, paths, process, provenance, workspace
from .verification_receipts import RunReceipts
from .errors import SDKError, ProcessOwnershipError
from .native_os_routing import gettext_target_host_flags, gnu_target_host_flags, native_host_flags, native_path
from .targets import TARGETS, target_definition

# Shared pkgsrc exports of this process, so that every source owner check of
# one run (preflight, before and after the build, and each LLVM phase) uses
# a single export.  Keyed by Ports root, Ports commit, pkgsrc commit and
# patch tool; each owner also binds the export to its current Ports inputs.
_SHARED_PKGSRC_EXPORTS = {}


def _remove_shared_pkgsrc_exports():
    for directory in _SHARED_PKGSRC_EXPORTS.values():
        shutil.rmtree(os.path.dirname(directory), ignore_errors=True)
    _SHARED_PKGSRC_EXPORTS.clear()

NATIVE_INPUTS = ('native-toolset', 'native-toolset-sha256',
                 'native-producer-receipt', 'native-producer-receipt-sha256')
NATIVE_ALIASES = (('host-cc', 'cc'), ('host-cxx', 'cxx'), ('python', 'python'))


class BuildInterrupted(KeyboardInterrupt):
    pass


def check_native_command_identity(signature, shell):
    """Check portable identity consistency, without establishing qualification."""
    try:
        native = signature['native_toolset']
        native_toolset.validate_selection(native, required_roles=('cc', 'cxx', 'sh', 'python'))
        if set(signature['host_tools']) != {'cc', 'c++'}:
            raise SDKError('Missing native compiler identity')
        for name, role in (('cc', 'cc'), ('c++', 'cxx')):
            record = signature['host_tools'][name]
            prefix = native['format'] == 'emberbsd-host-prefix'
            if (record['origin'] != ('host-prefix' if prefix else 'accepted-native-toolset')
                    or (not prefix and (record['descriptor_sha256'] != native['descriptor_sha256'] or
                        not re.fullmatch('[0-9a-f]{64}', record['sha256'])))
                    or record['version'] != native['compiler']['version']
                    or record.get('gcc_version', record['version']) != native['compiler']['version']
                    or record.get('compiler_family', 'gcc') != native['compiler']['family']
                    or record['machine'] != native['compiler']['triplet']
                    or record['programs'] != {tool: native['tools'][tool] for tool in ('as', 'ld')}):
                raise SDKError('Mixed native compiler identity')
            if record['selected'] != native['tools'][role]:
                raise SDKError('Mixed native selection and producer commands')
        if native['tools']['python'] != signature['python']['path'] or native['tools']['sh'] != shell:
            raise SDKError('Mixed native selection and producer commands')
        return native
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise SDKError('Missing native command identity') from exc


def validate_native_binding(signature, selected_paths, shell=None):
    """Bind saved native selections to producer inputs, flags, and environment."""
    try:
        native = signature['native_toolset']
        check_native_command_identity(signature, shell or native['tools']['sh'])
        inputs = signature['native_inputs']
        prefix = native['format'] == 'emberbsd-host-prefix'
        native_inputs = ('host-prefix',) if prefix else NATIVE_INPUTS
        if set(inputs) != set(native_inputs) | {name for name, _ in NATIVE_ALIASES}:
            raise SDKError('Incomplete native input identity')
        if prefix:
            if inputs['host-prefix'] != native['bundle_root'] or selected_paths['host-prefix'] != native['bundle_root']:
                raise SDKError('Producer paths differ from host prefix')
            if signature['environment'].get('EMBER_NATIVE_TOOLS') != selected_paths['native-routing']:
                raise SDKError('Producer native routing differs from selection')
        elif (inputs['native-toolset-sha256'] != native['descriptor_sha256'] or
                {key: inputs['native-producer-receipt' + suffix] for key, suffix in (('path', ''), ('sha256', '-sha256'))}
                != native['producer_receipt']):
            raise SDKError('Mixed native input pins')
        for name in (() if prefix else ('native-toolset', 'native-producer-receipt')):
            if selected_paths[name] != inputs[name] or not os.path.isabs(inputs[name]):
                raise SDKError('Producer paths differ from native inputs')
        for option, role in NATIVE_ALIASES:
            if selected_paths[option] != native['tools'][role] or not os.path.isabs(inputs[option]):
                raise SDKError('Producer paths differ from native selection')
        for name, option in (('cc', 'host-cc'), ('c++', 'host-cxx')):
            record = signature['host_tools'][name]
            if record['alias'] != inputs[option] or not os.path.isabs(record['path']):
                raise SDKError('Mixed checked native alias')
        expected = ([] if prefix else ['HOST_CC=' + native['tools']['cc'], 'HOST_CXX=' + native['tools']['cxx']]) + native_host_flags(native)
        if prefix and any(flag.split('=', 1)[0] in ('HOST_CC', 'HOST_CXX', 'HOST_SH') for flag in signature['flags']):
            raise SDKError('Producer overrides OS native routing')
        for flag in expected:
            prefix = flag.split('=', 1)[0] + '='
            if [word for word in signature['flags'] if word.startswith(prefix)] != [flag]:
                raise SDKError('Producer flags differ from native selection')
        if signature['environment']['PATH'] != native_path(native):
            raise SDKError('Producer PATH differs from native selection')
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise SDKError('Missing native producer binding') from exc


class SysrootBuilder:
    TARGETS = TARGETS
    FLAGS = ('TOOLCHAIN_MISSING=yes MKGCCCMDS=no MKCOMPAT=no MKCXX=yes '
             'MKDEBUG=no MKDEBUGLIB=no MKX11=no MKLLVM=no MKLLVMRT=no MKLIBCXX=no MKKMOD=no MKGDB=no MKBINUTILS=no '
             'MKATF=no MKKYUA=no MKMAN=no MKDOC=no MKINFO=no MKHTML=no MKLINT=no '
             'MKPROFILE=no MKCVS=no MKGROFF=no MKRUMP=no MKREPRO=yes MAKECONF=/dev/null').split()
    VERIFIER = 'profiles/development-toolchain/runtime/recipe/files/runtime.py'
    CONTEXT = '.prebuild-sdk-context.json'
    QUARANTINE = '.prebuild-sdk-quarantine.json'
    RECEIPT = '.prebuild-sdk-build.json'
    MARKER = '.prebuild-sdk-build-incomplete'
    LOG = '.prebuild-sdk-build.log'

    def __init__(self, options, *, receipts=None):
        self.options = options.copy()
        # One run shares one set of in-memory verification receipts.
        self.verification = RunReceipts() if receipts is None else receipts
        if 'base-cxx' not in self.options:
            self.options['base-cxx'] = self.options.get('compiler-family', 'clang') == 'clang'
        self.commands = []
        self.git_command = None
        self.handles = []
        self.environment = process.sanitized_environment()
        self.environment['PYTHONDONTWRITEBYTECODE'] = '1'
        self.interrupted_signal = None
        self.locks = None
        self.receipt_file = None
        self.signal_handlers = {}
        self.contexts_prepared = False

    def capture(self, *argv, directory=None):
        result = process.capture(argv, cwd=directory, env=self.environment)
        return result.stdout.decode('utf-8', 'strict'), result.stderr.decode('utf-8', 'strict')

    def executable(self, value):
        path = paths.canonical_existing(value)
        paths.regular_file(path)
        if not os.access(path, os.X_OK):
            raise SDKError(f'not executable: {path}')
        return path

    def find_command(self, name):
        for directory in self.environment.get('PATH', '').split(os.pathsep):
            if not directory:
                continue
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return self.executable(candidate)
        raise SDKError(f'required host command is unavailable: {name}')

    def load_native_toolset(self):
        """Select the installed Ports prefix and the roles required by this OS."""
        if not self.options.get('host-prefix'):
            raise SDKError('--host-prefix is required: select the native Ports tools')
        contract = os.path.join(self.options['os'], 'tools/native-tools/contract')
        self.native_contract_sha256 = inventory.sha256_file(contract)
        _, _, roles = native_os_routing.read_native_os_contract(contract, sha256=self.native_contract_sha256)
        self.native_contract = contract
        if self.options.get('texinfo-source') or self.options.get('rcs-source') or self.options.get('diffutils-source') or self.options.get('grep-source'):
            roles = roles | {'gmake'}
        if self.options.get('gettext-source'):
            roles |= {'gmake', 'pkg-config', 'od', 'msgfmt', 'msgmerge', 'xgettext', 'msginit', 'msgconv', 'msgfilter', 'sed'}
        return native_toolset.load_host_prefix(self.options['host-prefix'], required_roles=roles | {'python'},
            compiler_family=self.options.get('host-compiler-family', 'clang'))

    def native_identity(self, native):
        # Optional selectors are checked aliases, never alternate providers.
        for option, role in NATIVE_ALIASES:
            value = self.options.setdefault(option, native.tool(role))
            if not isinstance(value, str) or not os.path.isabs(value):
                raise SDKError(f'--{option} must be the absolute native {role} path')
            if self.executable(value) != self.executable(native.tool(role)):
                raise SDKError(f'--{option} differs from the native {role} selection')
        host_tools = {}
        for name, role, option in (('cc', 'cc', 'host-cc'), ('c++', 'cxx', 'host-cxx')):
            selected = native.tool(role)
            canonical = self.executable(selected)
            host_tools[name] = dict(path=canonical, selected=selected,
                alias=self.options[option], origin='host-prefix',
                version=native.compiler['version'], compiler_family=native.compiler['family'],
                machine=native.compiler['triplet'],
                programs={tool: native.tool(tool) for tool in ('as', 'ld')})
        return host_tools

    def select_native_toolset(self):
        self.native = self.load_native_toolset()
        self.native_selection = self.native.as_dict()
        self.host_tools = self.native_identity(self.native)
        self.shell = self.native.tool('sh')
        self.options['host-prefix'] = self.native.bundle_root
        self.native_inputs = {name: self.options[name] for name in ('host-prefix', *(name for name, _ in NATIVE_ALIASES))}

    def verify_native_toolset(self):
        try:
            native = self.load_native_toolset()
            if (native.as_dict() != self.native_selection or self.native_identity(native) != self.host_tools or
                    any(self.options[name] != value for name, value in self.native_inputs.items())):
                raise SDKError('native toolset inputs changed after preflight')
        except (SDKError, OSError, UnicodeError):
            if self.contexts_prepared:
                for name in ('objects', 'tools'):
                    path = os.path.join(self.paths[name], self.QUARANTINE)
                    if paths.entry(path) is None:
                        jsonio.write_json(path, dict(reason='native toolset inputs changed during build'))
            raise

    def prepare_native_routing(self):
        """Install the OS-owned command mapping inside the locked tools tree."""
        destination = self.paths['native-routing']
        native_os_routing.ensure_native_os_routing(self.native,
            contract_path=self.native_contract, contract_sha256=self.native_contract_sha256,
            destination=destination)

    def git(self, root, *arguments):
        result = process.capture([self.git_command, '-C', root, *arguments],
                                 env=process.git_environment(self.environment))
        return result.stdout.decode('utf-8', 'strict').strip()

    def source(self, root, kind=None):
        def check():
            return provenance.git_source(root, kind=kind, git=self.git_command, env=self.environment)
        inputs = self.git_source_inputs(root)
        if inputs is None:
            return check()
        environment = tuple(sorted(process.git_environment(self.environment).items()))
        return self.verification.verify(('git-source', root, kind, self.git_command, environment),
                                        inputs, check)

    def git_source_inputs(self, root):
        """Files that decide a clean-checkout check, or None to check Git every time.

        The checkout and its Git directory must lie below the root, and so
        must the Git directory of every submodule. Otherwise a commit or
        reset could change the result without changing a file below it.
        """
        if not os.path.isabs(self.git_command) or not os.path.isfile(self.git_command):
            return None
        try:
            root = paths.canonical_existing(root)
            gitdir = os.path.join(root, '.git')
            if os.path.islink(gitdir) or not os.path.isdir(gitdir):
                return None
            modules = os.path.join(root, '.gitmodules')
            if os.path.lexists(modules):
                listed = self.git(root, 'config', '--file', modules, '-z', '--get-regexp', r'^submodule\..*\.path$')
                for row in listed.split('\0'):
                    if not row:
                        continue
                    path = os.path.join(root, row.split('\n', 1)[1])
                    marker = os.path.join(path, '.git')
                    if os.path.islink(marker):
                        return None
                    if os.path.isfile(marker):
                        with open(marker, encoding='utf-8') as handle:
                            line = handle.read().strip()
                        if not line.startswith('gitdir: '):
                            return None
                        target = os.path.realpath(os.path.join(path, line[len('gitdir: '):]))
                        if not paths.inside(target, gitdir):
                            return None
        except (OSError, UnicodeError, ValueError, SDKError):
            return None
        return [root, self.git_command]

    def output_path(self, value, boundary):
        path = paths.build_path(value)
        # Inspect the selected spelling before canonicalization. Only the
        # explicitly adopted workspace root can be an output-path alias.
        logical_parts = tuple(part for part in self.logical_sdk.split(os.sep) if part)
        selected_parts = []
        current = os.path.sep
        for name in path.split(os.sep):
            if not name:
                continue
            selected_parts.append(name)
            current = os.path.join(current, name)
            info = paths.entry(current)
            if info is not None and not stat.S_ISDIR(info.st_mode):
                allowed_alias = stat.S_ISLNK(info.st_mode) and tuple(selected_parts) == logical_parts[:len(selected_parts)]
                if not allowed_alias:
                    raise SDKError(f'output ancestor is not a real directory: {current}')
        relative = paths.relative_inside(path, boundary)
        if relative is None or relative == '':
            raise SDKError(f'output must be inside {boundary}: {path}')
        path = os.path.join(boundary, relative)
        current = boundary
        for name in relative.split(os.sep):
            current = os.path.join(current, name)
            info = paths.entry(current)
            if info is not None and not stat.S_ISDIR(info.st_mode):
                raise SDKError(f'output ancestor is not a real directory: {current}')
        return path

    def empty_destination(self):
        destination = self.paths['destination']
        if paths.entry(destination) is not None:
            paths.real_directory(destination)
            if os.listdir(destination):
                raise SDKError('destination must be new or empty; active sysroot is never overwritten')

    def compiler(self):
        prefix, triplet = self.paths['compiler'], self.target['triplet']
        if self.compiler_family() == 'clang':
            from .target_toolchain import clang_compiler
            return clang_compiler(prefix, triplet, host=self.native.host, env=self.environment)
        binaries = {}
        for name in ('gcc', 'g++', 'cpp', 'ar', 'as', 'ld', 'nm', 'objcopy', 'objdump', 'ranlib', 'readelf', 'strip'):
            selected = os.path.join(prefix, 'bin', triplet + '-' + name)
            path = self.executable(selected)
            if name in ('gcc', 'g++', 'cpp') and not paths.inside(path, prefix):
                raise SDKError(f'compiler frontend escapes prefix: {path}')
            binaries[name] = dict(selected_path=selected, path=path, sha256=inventory.sha256_file(path))
        for name in ('gcc', 'g++'):
            tool = binaries[name]['path']
            if (self.capture(tool, '-dumpfullversion')[0].strip() != '16.2.0' or
                    self.capture(tool, '-dumpmachine')[0].strip() != triplet or
                    re.search(r'^Thread model: posix$', '\n'.join(self.capture(tool, '-v')), re.M) is None):
                raise SDKError(f'expected GCC 16.2.0 {triplet} with POSIX threads: {tool}')
        for name in ('cc1', 'cc1plus', 'collect2', 'lto1', 'as', 'ld'):
            driver = binaries['g++' if name == 'cc1plus' else 'gcc']['path']
            reported = self.capture(driver, '-print-prog-name=' + name)[0].strip()
            if not os.path.isabs(reported):
                reported = os.path.join(prefix, 'bin', reported)
            path = self.executable(reported)
            if name in ('as', 'ld'):
                if path != binaries[name]['path']:
                    raise SDKError(f'driver and prefix select different Binutils providers: {name}')
            elif not paths.inside(path, prefix):
                raise SDKError(f'compiler subprocess escapes prefix: {path}')
            binaries['resolved-' + name] = dict(reported_path=reported, path=path, sha256=inventory.sha256_file(path))
        return dict(version='16.2.0', triplet=triplet, threads='posix', binaries=binaries)

    def compiler_family(self):
        family = self.options.get('compiler-family', 'clang')
        if family not in ('clang', 'gcc'):
            raise SDKError('unsupported target compiler family')
        return family

    def verify_runtime(self, *, base=None):
        if self.compiler_family() == 'clang':
            from . import llvm_runtime
            if self.llvm_phase == 'headers':
                return None
            required = (llvm_runtime.FULL_COMPONENTS if self.llvm_phase == 'final'
                        else ('builtins', 'libunwind-static'))
            stage = self.paths['llvm-runtime-stage']
            if base is None and self.llvm_phase == 'base':
                base = self.paths.get('llvm-runtime-base')
            command = list(self.runtime_command)

            def check():
                reported = jsonio.loads_json(process.capture(command, env=self.environment).stdout)
                llvm_runtime.validate_identity(reported, target=self.options['target'], required_components=required)
                recipe_check = ({} if 'llvm-runtime-original-source' in self.paths else
                    dict(ports=self.paths['ports'], ports_revision=reported['recipe']['ports_revision']))
                record = llvm_runtime.validate_stage(stage, target=self.options['target'],
                    required_components=required, base=base, **recipe_check)
                if reported != record:
                    raise SDKError('LLVM runtime producer and shared validator disagree')
                return record
            inputs = [stage, *([base] if base else []), *self.receipt_tools(command)]
            inputs += [self.paths[name] for name in ('llvm-source', 'llvm-runtime-original-source')
                       if name in self.paths and self.paths[name] in command]
            record = self.verification.verify(('llvm-runtime', tuple(command), tuple(required), base), inputs, check)
            if (record['llvm_version'] != self.compiler_record['version'] or
                    record['source'] != {key: self.llvm_source_record['source'][key]
                                         for key in ('archive', 'sha512')}):
                raise SDKError('LLVM runtime compiler or common source differs from selection')
            if self.llvm_phase == 'base' and record['base']['os_revision'] != self.sources['os']['commit']:
                if base is None:
                    raise SDKError('LLVM runtime base OS differs from selection')
                # Bootstrap archives depend on measured headers, not the entire
                # later OS tree. The base phase checks its new headers again
                # before accepting any libc/CRT output.
                self.git(self.sources['os']['path'], 'merge-base', '--is-ancestor',
                         record['base']['os_revision'], self.sources['os']['commit'])
            rows = inventory.snapshot_tree(self.paths['runtime-prefix'], machine=self.target['elf'])
            self.commands.append(dict(argv=self.runtime_command, scope='runtime-check', status='passed'))
            if self.llvm_phase == 'final':
                llvm_runtime.validate_bootstrap_pair(
                    self.paths['llvm-bootstrap-stage'], record, target=self.options['target'])
            return dict(manifest=record, inventory=rows,
                        root_mode=stat.S_IMODE(os.stat(self.paths['runtime-prefix']).st_mode))
        result = process.capture(self.runtime_command, env=self.environment)
        report = jsonio.loads_json(result.stdout)
        try:
            metadata = report['metadata']
            expected = dict(OPSYS='NetBSD', OS_VERSION='11.0', GCC_VERSION='16.2.0', MACHINE_ARCH=self.target['arch'], GCC_RUNTIME_MACHINE_ARCH=self.target['arch'], GCC_RUNTIME_TARGET=self.options['target'], GCC_RUNTIME_TRIPLET=self.target['triplet'])
            if not isinstance(metadata, dict) or any(metadata.get(key) != value for key, value in expected.items()):
                raise SDKError('runtime target metadata mismatch')
            if not isinstance(metadata.get('EMBERBSD_SEED_SOURCE'), str) or re.fullmatch('[0-9a-f]{40}', metadata['EMBERBSD_SEED_SOURCE']) is None:
                raise SDKError('runtime seed provenance is missing')
            receipt_hashes = {'inputs.txt': 'GCC_RUNTIME_INPUTS_SHA256', 'stage.sha256': 'GCC_RUNTIME_STAGE_SHA256', 'stage.symlinks': 'GCC_RUNTIME_LINKS_SHA256'}
            for file, key in receipt_hashes.items():
                if inventory.sha256_file(os.path.join(self.paths['runtime-work'], file)) != metadata[key]:
                    raise SDKError(f'runtime receipt changed: {file}')
            rows = inventory.runtime(self.paths['runtime-prefix'], self.target['triplet'], self.target['elf'])
            files, links = {}, {}
            with open(os.path.join(self.paths['runtime-work'], 'stage.sha256'), encoding='utf-8', newline='') as handle:
                for line in handle:
                    match = re.fullmatch(r'([0-9a-f]{64})  \./usr/pkg/gcc16/(.+)\n', line)
                    if match is None or match[2] in files:
                        raise SDKError('unsupported runtime file manifest')
                    files[match[2]] = match[1]
            with open(os.path.join(self.paths['runtime-work'], 'stage.symlinks'), encoding='utf-8', newline='') as handle:
                for line in handle:
                    match = re.fullmatch(r'\./usr/pkg/gcc16/([^\t\n]+)\t([^\n]+)\n', line)
                    if match is None or match[1] in links:
                        raise SDKError('unsupported runtime link manifest')
                    links[match[1]] = match[2]
            actual_files = {path: row['sha256'] for path, row in rows.items() if row['kind'] == 'file'}
            actual_links = {path: row['target'] for path, row in rows.items() if row['kind'] == 'link'}
            if actual_files != files or actual_links != links:
                raise SDKError('runtime payload does not match the verified manifests')
            for file in ('stage.sha256', 'stage.symlinks'):
                if inventory.sha256_file(os.path.join(self.paths['runtime-work'], file)) != metadata[receipt_hashes[file]]:
                    raise SDKError('runtime manifest changed during inspection')
            self.commands.append(dict(argv=self.runtime_command, scope='runtime-check', status='passed'))
            return dict(report=report, inventory=rows, root_mode=stat.S_IMODE(os.stat(self.paths['runtime-prefix']).st_mode))
        except (KeyError, TypeError, AttributeError, UnicodeError) as exc:
            raise SDKError(f'malformed runtime verifier result: {exc}') from exc

    def inspect_context(self, path):
        if paths.entry(path) is None:
            return None
        paths.real_directory(path)
        if paths.entry(os.path.join(path, self.QUARANTINE)) is not None:
            raise SDKError(f'object context is quarantined: {path}')
        if not os.listdir(path):
            return None
        record = jsonio.read_json(os.path.join(path, self.CONTEXT))
        if (not isinstance(record, dict) or set(record) != {'format', 'signature', 'sources', 'version'} or
                record['format'] != 'emberbsd-sysroot-object-context' or type(record['version']) is not int or record['version'] != 1 or not self.compatible_context(record, path)):
            raise SDKError(f'unknown or incompatible object context; leave it unchanged: {path}')
        self.validate_source_ancestry(record, path)
        return record

    def validate_source_ancestry(self, record, path):
        """Check prior pins from a context or completed stage in current sources."""
        try:
            for name in ('os', 'ports', 'sdk'):
                old = record['sources'][name]['commit']
                if not isinstance(old, str) or re.fullmatch('[0-9a-f]{40}', old) is None:
                    raise SDKError('invalid recorded source pin')
                self.git(self.sources[name]['path'], 'merge-base', '--is-ancestor', old, self.sources[name]['commit'])
        except (KeyError, TypeError) as exc:
            raise SDKError(f'invalid source history: {path}: {exc}') from exc

    def compatible_context(self, record, path):
        """Compare compiled inputs, allowing controller updates and new utilities."""
        old = copy.deepcopy(record['signature'])
        new = copy.deepcopy(self.context_signature(path))
        if old == new:
            return True
        try:
            # Loaded modules still match selected sources before/during a run.
            # Their hashes alone do not describe the compiler's object inputs.
            old['python'].pop('modules', None)
            new['python'].pop('modules', None)
            if (self.compiler_family() == 'clang' and
                    (path == self.paths['tools'] or getattr(self, 'llvm_phase', None) == 'headers')):
                # Native tools and target headers never invoke the target linker.
                # Keep its selected route and all compiler/ABI inputs, but do not
                # discard these outputs solely because ld.lld bytes changed.
                for signature in (old, new):
                    signature['compiler']['binaries']['ld'].pop('sha256', None)
            if (self.compiler_family() == 'clang' and
                    getattr(self, 'llvm_phase', None) == 'headers' and
                    path != self.paths['tools']):
                # bootstrap-headers only installs OS/libedit headers. It does
                # not enter libc or consume the common LLVM runtime source.
                # Source selection is still verified during preflight.
                for signature in (old, new):
                    signature.pop('llvm_source', None)
                    common_sources = signature.pop('common_sources', {})
                    # TDB includes install tdb.h during the headers phase.
                    if 'tdb' in common_sources:
                        signature['header_tdb_source'] = common_sources['tdb']
                    if 'iscsi' in common_sources:
                        signature['header_iscsi_source'] = common_sources['iscsi']
                    if 'libpcap' in common_sources:
                        signature['header_libpcap_source'] = common_sources['libpcap']
                    if 'tcp-wrappers' in common_sources:
                        signature['header_tcp_wrappers_source'] = common_sources['tcp-wrappers']
                    signature['flags'] = [flag for flag in signature['flags']
                                          if not flag.startswith(('EMBER_LLVM_SOURCE=',
                                                                  'EMBER_LIBARCHIVE_SOURCE=',
                                                                  'EMBER_LIBEVENT_SOURCE=',
                                                                  'EMBER_BYACC_SOURCE=',
                                                                  'EMBER_TEXINFO_SOURCE=',
                                                                  'EMBER_RCS_SOURCE=',
                                                                  'EMBER_DIFFUTILS_SOURCE=',
                                                                  'EMBER_GREP_SOURCE=',
                                                                  'HOST_PYTHON=',
                                                                  'EMBER_EKERMIT_SOURCE=',
                                                                  'EMBER_NVI_SOURCE=',
                                                                  'EMBER_TOP_SOURCE=',
                                                                  'EMBER_NTP_SOURCE=',
                                                                  'EMBER_LIBOPTS_SOURCE=',
                                                                  'EMBER_GETTEXT_SOURCE=',
                                                                  'EMBER_LIBXML2_SOURCE=',
                                                                  'GNU_TARGET_LIBTOOL=',
                                                                  'EMBER_TARGET_TOOL_DIR=',
                                                                  'HOST_PKG_CONFIG=', 'HOST_OD=',
                                                                  'HOST_MSGFMT=', 'HOST_MSGMERGE=',
                                                                  'HOST_XGETTEXT=', 'HOST_MSGINIT=',
                                                                  'HOST_MSGCONV=', 'HOST_MSGFILTER=', 'HOST_SED=',
                                                                  'EMBER_PPP_SOURCE=',
                                                                  'EMBER_TCPDUMP_SOURCE=',
                                                                  'HOST_GMAKE=',
                                                                  'EMBER_HOST_GNU_TRIPLE='))]
            previous, selected = old['native_toolset'], new['native_toolset']
            if any(selected['tools'].get(role) != value
                   for role, value in previous['tools'].items()):
                return False
            added = selected['tools'].keys() - previous['tools'].keys()
            # Optional target-only GNU make cannot affect host tools or headers.
            irrelevant = ({'gmake', 'pkg-config', 'od', 'msgfmt', 'msgmerge', 'xgettext', 'msginit', 'msgconv', 'msgfilter'} if path == self.paths['tools'] or
                          (self.compiler_family() == 'clang' and self.llvm_phase == 'headers')
                          else set())
            added_contract = added - irrelevant
            old_env, new_env = old['environment'], new['environment']
            routing_changed = old_env['EMBER_NATIVE_TOOLS'] != new_env['EMBER_NATIVE_TOOLS']
            if added_contract or routing_changed:
                # Compare the actual OS-owned rows from both source revisions.
                # Existing commands/bindings cannot change during object reuse.
                source = record['sources']['os']
                contract = 'tools/native-tools/contract'
                # Objects from an OS revision without the contract have no
                # recorded rows to compare, so they cannot be reused.
                if self.git(self.sources['os']['path'], 'ls-tree', '--name-only',
                            source['commit'], '--', contract) != contract:
                    return False
                data = self.git(self.sources['os']['path'], 'show', source['commit'] +
                                ':' + contract) + '\n'
                old_rows = data.splitlines()
                new_rows = Path(self.native_contract).read_text(encoding='ascii').splitlines()
                if (old_rows[:1] != new_rows[:1] or
                        not set(old_rows[1:]) <= set(new_rows[1:])):
                    return False
                extra = [row.split(' ') for row in new_rows[1:] if row not in old_rows[1:]]
                if (any(len(row) != 3 or row[2] != '-' for row in extra) or
                        not added_contract <= {row[1] for row in extra}):
                    return False
                old_digest = hashlib.sha256(data.encode('ascii')).hexdigest()
                allowed = (os.path.join(old['tools'], 'native-routing'),
                           os.path.join(old['tools'], 'native-routing-' + old_digest))
                if (old_env['EMBER_NATIVE_TOOLS'] not in allowed or
                        new_env['EMBER_NATIVE_TOOLS'] != self.paths['native-routing']):
                    return False
                new_env['EMBER_NATIVE_TOOLS'] = old_env['EMBER_NATIVE_TOOLS']
            # Only the derived PATH may gain new utility directories. Preserve
            # the relative order of every old search directory.
            if (old_env['PATH'] != native_path(previous) or
                    new_env['PATH'] != native_path(selected)):
                return False
            remaining = iter(new_env['PATH'].split(os.pathsep))
            if not all(directory in remaining for directory in old_env['PATH'].split(os.pathsep)):
                return False
            new_env['PATH'] = old_env['PATH']
            for role in added:
                del selected['tools'][role]
            return old == new
        except (KeyError, TypeError, AttributeError, ValueError):
            return False

    def context_signature(self, path):
        if self.compiler_family() != 'clang' or path != self.paths['tools']:
            return self.signature
        # OS native tools do not link the target runtime. Reuse their one tree
        # while target object directories distinguish headers, C base and C++.
        signature = dict(scope='llvm-native-tools', host_binutils=self.paths['host-binutils'],
                    **{name: self.signature[name] for name in
            ('host', 'target', 'compiler', 'host_tools', 'native_toolset', 'native_inputs',
             'environment', 'tools', 'python', 'process_backend')})
        if 'byacc-source' in self.paths:
            signature['byacc_source'] = {'path': self.paths['byacc-source'],
                                         'record': self.signature['common_sources']['byacc']}
        if 'x11-source' in self.paths:
            # OS tools build X11 host font tools from X11SRCDIR.
            signature['x11_source'] = {'path': self.paths['x11-source'],
                                       'record': self.signature['common_sources']['x11']}
        return signature

    @staticmethod
    def receipt_tools(command):
        """Executables, scripts, archives and other files named by a verifier command."""
        return [value for value in command if os.path.isabs(value) and os.path.isfile(value)]

    def verify_llvm_source(self):
        command = self.llvm_source_command
        record = self.verification.verify(('llvm-source', tuple(command)),
            [self.paths['llvm-source'], *self.receipt_tools(command)],
            lambda: jsonio.loads_json(process.capture(command, env=self.environment).stdout))
        if (not isinstance(record, dict) or record.get('format') != 'emberbsd-llvm-source' or
                record.get('version') != 1 or
                (self.compiler_family() == 'clang' and record.get('llvm_version') != self.compiler_record['version'])):
            raise SDKError('common LLVM source and selected compiler differ')
        return record

    SHARED_EXPORT_SCRIPT = 'scripts/pkgsrc-shared-export.py'
    SHARED_EXPORT_OWNERS = ('libarchive', 'libevent', 'byacc', 'texinfo', 'rcs', 'diffutils',
                            'grep', 'ekermit', 'nvi', 'tcpdump', 'top', 'iscsi', 'ntp',
                            'libopts', 'gettext', 'libxml2')

    def shared_pkgsrc_export(self, patch):
        """Return the one pkgsrc export of this run, or None for older Ports."""
        script = os.path.join(self.paths['ports'], self.SHARED_EXPORT_SCRIPT)
        if not os.path.isfile(script):
            return None
        key = (self.paths['ports'], self.git(self.paths['ports'], 'rev-parse', 'HEAD'),
               self.git(self.paths['ports'], 'rev-parse', 'HEAD:upstream/pkgsrc'),
               patch, inventory.sha256_file(patch))
        directory = _SHARED_PKGSRC_EXPORTS.get(key)
        if directory is not None:
            return directory
        parent = tempfile.mkdtemp(prefix='.pkgsrc-shared-export-')
        directory = os.path.join(parent, 'export')
        try:
            process.capture([self.paths['python'], '-B', script, directory,
                             '--sdk-source', self.paths['sdk-source'], '--git', self.git_command,
                             '--patch', patch], env=self.environment)
        except BaseException:
            shutil.rmtree(parent, ignore_errors=True)
            raise
        if not _SHARED_PKGSRC_EXPORTS:
            atexit.register(_remove_shared_pkgsrc_exports)
        _SHARED_PKGSRC_EXPORTS[key] = directory
        return directory

    def verify_common_sources(self):
        result = {}
        patch = self.options.get('source-patch')
        if patch:
            patch = self.executable(patch)
            selector = native_toolset.load_host_prefix(self.native.bundle_root, required_roles={'gpatch'})
            if patch != self.executable(selector.tool('gpatch')):
                raise SDKError('source patch differs from the selected native gpatch')
        lzip = None
        if 'rcs-source' in self.paths and self.options.get('rcs-archive'):
            # Select every host tool before the first owner verifier runs, so
            # a missing tool fails the run before minutes of source checks.
            lzip = native_toolset.load_host_prefix(self.native.bundle_root, required_roles={'lzip'}).tool('lzip')
        shared = None
        memo = {}
        for name, package, argv in (
            ('libarchive', 'archivers/libarchive', ['scripts/libarchive-source.py', 'verify']),
            ('libevent', 'devel/libevent', ['scripts/source-inputs/libevent/source.py']),
            ('byacc', 'devel/byacc', ['scripts/source-inputs/byacc/source.py']),
            ('texinfo', 'devel/gtexinfo', ['scripts/source-inputs/texinfo/source.py']),
            ('rcs', 'devel/rcs', ['scripts/source-inputs/rcs/source.py']),
            ('diffutils', 'devel/diffutils', ['scripts/source-inputs/diffutils/source.py']),
            ('grep', 'textproc/grep', ['scripts/source-inputs/grep/source.py']),
            ('ekermit', 'comms/ekermit', ['scripts/source-inputs/ekermit/source.py']),
            ('nvi', 'editors/nvi', ['scripts/source-inputs/nvi/source.py']),
            ('top', 'sysutils/top', ['scripts/source-inputs/top/source.py']),
            ('iscsi', 'devel/netbsd-iscsi-lib', ['scripts/source-inputs/iscsi/source.py']),
            ('ntp', 'net/ntp4', ['scripts/source-inputs/ntp/source.py']),
            ('libopts', 'devel/autogen', ['scripts/source-inputs/autogen/source.py']),
            ('libpcap', 'net/libpcap', ['scripts/source-inputs/libpcap/source.py']),
            ('tcpdump', 'net/tcpdump', ['scripts/source-inputs/tcpdump/source.py']),
            ('tcp-wrappers', 'security/tcp_wrappers', ['scripts/source-inputs/tcp_wrappers/source.py']),
            ('gettext', 'devel/gettext', ['scripts/source-inputs/gettext/source.py']),
            ('libxml2', 'textproc/libxml2', ['scripts/source-inputs/libxml2/source.py']),
            ('ppp', 'net/pppd', ['scripts/source-inputs/ppp/source.py']),
            ('tdb', 'databases/tdb', ['scripts/source-inputs/tdb/source.py']),
            ('x11', 'netbsd/xsrc', ['scripts/source-inputs/xsrc/source.py']),
        ):
            if name + '-source' not in self.paths:
                continue
            command = [self.paths['python'], '-B', os.path.join(self.paths['ports'], argv[0])]
            if name == 'x11':
                # The xsrc pin is an archive identity; it inspects no Git input.
                command += ['--sdk-source', self.paths['sdk-source'], 'verify-source', '--source', self.paths[name + '-source']]
            elif name == 'libarchive':
                command += ['verify', self.paths[name + '-source'], '--sdk-source', self.paths['sdk-source'], '--git', self.git_command]
            else:
                command += ['--sdk-source', self.paths['sdk-source'], '--git', self.git_command, 'verify-source', '--source', self.paths[name + '-source']]
            if name in ('libarchive', 'libevent', 'byacc', 'texinfo', 'rcs', 'diffutils', 'grep', 'ekermit', 'nvi', 'tdb', 'ppp', 'top', 'iscsi', 'libpcap', 'tcpdump', 'tcp-wrappers', 'ntp', 'libopts', 'gettext', 'libxml2') and patch:
                command += ['--patch', patch]
                if name in self.SHARED_EXPORT_OWNERS:
                    if shared is None:
                        shared = self.shared_pkgsrc_export(patch) or ''
                    if shared:
                        command += ['--pkgsrc-export', shared]
            if name in ('libevent', 'byacc', 'texinfo', 'rcs', 'diffutils', 'grep', 'ekermit', 'nvi', 'tdb', 'ppp', 'top', 'iscsi', 'libpcap', 'tcpdump', 'tcp-wrappers', 'ntp', 'gettext', 'libxml2') and self.options.get(name + '-archive'):
                archive = paths.canonical_existing(self.options[name + '-archive'])
                paths.regular_file(archive)
                command += ['--archive', archive]
                if name == 'rcs':
                    command += ['--lzip', lzip]
            inputs = [self.paths[name + '-source'], *self.receipt_tools(command)]
            if shared:
                inputs.append(shared)
            record = self.verification.verify(('common-source', name, tuple(command)), inputs,
                lambda: jsonio.loads_json(process.capture(command, env=self.environment).stdout), memo)
            if (not isinstance(record, dict) or record.get('format') != 'emberbsd-prepared-source' or
                    type(record.get('version')) is not int or record['version'] != 1 or
                    record.get('package') != package):
                raise SDKError(f'invalid {name} prepared source receipt')
            result[name] = record
        return result

    def python_identity(self):
        python = self.paths['python']
        source = self.paths['sdk-source']
        probe = ('import json,sys; sys.dont_write_bytecode=True; sys.path.insert(0,sys.argv[1]); '
                 'import emberbsd_sdk; print(json.dumps({"api":emberbsd_sdk.API_VERSION,"version":list(sys.version_info[:3]),"source":emberbsd_sdk.__file__}))')
        result = jsonio.loads_json(process.capture([python, '-B', '-I', '-S', '-c', probe, os.path.join(source, 'src')], env=self.environment).stdout)
        if not isinstance(result, dict) or type(result.get('api')) is not int or result['api'] != API_VERSION:
            raise SDKError('selected SDK host API version must be 1')
        if not isinstance(result.get('version'), list) or len(result['version']) != 3 or any(type(v) is not int for v in result['version']) or result['version'] < [3, 15, 0]:
            raise SDKError('host tools require Python 3.15 or later')
        expected = os.path.join(source, 'src', 'emberbsd_sdk', '__init__.py')
        if paths.canonical_existing(result['source']) != expected:
            raise SDKError('selected Python imported a different SDK source')
        # Programmatic callers must also use exactly the selected implementation.
        loaded = Path(__file__).resolve().parent
        module_hashes = {}
        for module in sorted(loaded.glob('*.py')):
            selected = os.path.join(source, 'src', 'emberbsd_sdk', module.name)
            digest = inventory.sha256_file(selected)
            if inventory.sha256_file(module) != digest:
                raise SDKError(f'selected SDK source differs from loaded implementation: {module.name}')
            module_hashes[module.name] = digest
        return dict(path=python, sha256=inventory.sha256_file(self.executable(python)), version=result['version'], api_version=API_VERSION, modules=module_hashes)

    def require_byacc_source_support(self):
        if 'byacc-source' not in self.paths:
            return
        root = self.paths['os']
        env = dict(self.environment)
        env.pop('EMBER_NATIVE_TOOLS', None)
        for producer in ('external/bsd/byacc/bin', 'tools/yacc'):
            command = [self.native.tool('make'), '-r', '-m', os.path.join(root, 'share/mk'),
                       '-C', os.path.join(root, producer), 'MAKECONF=/dev/null', '_SRC_TOP_=',
                       'NETBSDSRCDIR=' + root, 'MACHINE=' + self.target['machine'],
                       'MACHINE_ARCH=' + self.target['arch'], 'USETOOLS=yes',
                       'TOOLDIR=' + self.paths['tools'],
                       'EMBER_BYACC_SOURCE=' + self.paths['byacc-source'], '-V', '${IDIST}']
            try:
                selected = process.capture(command, env=env).stdout.decode('utf-8').strip()
            except (SDKError, UnicodeError) as exc:
                raise SDKError(f'selected OS cannot resolve byacc source for {producer}') from exc
            if selected != self.paths['byacc-source']:
                raise SDKError(f'selected OS does not support EMBER_BYACC_SOURCE for {producer}')

    def require_texinfo_source_support(self):
        if 'texinfo-source' not in self.paths:
            return
        root = self.paths['os']
        env = dict(self.environment)
        env.pop('EMBER_NATIVE_TOOLS', None)
        command = [self.native.tool('make'), '-r', '-m', os.path.join(root, 'share/mk'),
                   '-C', os.path.join(root, 'external/gpl2/texinfo/bin'), 'MAKECONF=/dev/null', '_SRC_TOP_=',
                   'NETBSDSRCDIR=' + root, 'MACHINE=' + self.target['machine'],
                   'MACHINE_ARCH=' + self.target['arch'], 'USETOOLS=yes',
                   'TOOLDIR=' + self.paths['tools'],
                   'EMBER_TEXINFO_SOURCE=' + self.paths['texinfo-source'],
                   'HOST_GMAKE=' + self.native.tool('gmake'),
                   'HOST_SH=' + self.native.tool('sh'),
                   'HOST_CC=' + self.native.tool('cc'),
                   'HOST_CXX=' + self.native.tool('cxx'),
                   'HOST_AR=' + self.native.tool('ar'),
                   'HOST_RANLIB=' + self.native.tool('ranlib'),
                   'HOST_LD=' + self.native.tool('ld'),
                   'EMBER_HOST_GNU_TRIPLE=' + self.native.compiler['triplet'],
                   '-V', '${GNU_SOURCE}', '-V', '${GNU_CONFIGURE_ARGS}',
                   '-V', '${GNU_HOST_MAKE_DIRS}', '-V', '${GNU_HOST_MAKE_TARGETS.tools/info}',
                   '-V', '${_GNU_HOST_CONTEXT}']
        try:
            output = process.capture(command, env=env).stdout.decode('utf-8').splitlines()
            flags = shlex.split(output[1]) if len(output) == 5 else []
        except (SDKError, UnicodeError, ValueError) as exc:
            raise SDKError('selected OS cannot resolve the Texinfo C-only producer') from exc
        if (len(output) != 5 or output[0] != self.paths['texinfo-source'] or
                '--disable-perl-tools' not in flags or '--enable-perl-tools' in flags or
                '--enable-native-c-only-tools' not in flags or
                output[2].split() != ['tools/gnulib/lib', 'tools/info'] or
                output[3].strip() != 'makedoc' or
                any(path not in output[4] for path in ('tools/gnulib/lib', 'tools/info'))):
            raise SDKError('selected OS does not support EMBER_TEXINFO_SOURCE in C-only mode')

    def require_rcs_source_support(self):
        if 'rcs-source' not in self.paths:
            return
        root = self.paths['os']
        env = dict(self.environment)
        env.pop('EMBER_NATIVE_TOOLS', None)
        command = [self.native.tool('make'), '-r', '-m', os.path.join(root, 'share/mk'),
                   '-C', os.path.join(root, 'external/gpl2/rcs/gnu'), 'MAKECONF=/dev/null', '_SRC_TOP_=',
                   'NETBSDSRCDIR=' + root, 'MACHINE=' + self.target['machine'],
                   'MACHINE_ARCH=' + self.target['arch'], 'USETOOLS=yes',
                   'TOOLDIR=' + self.paths['tools'],
                   'EMBER_RCS_SOURCE=' + self.paths['rcs-source'],
                   'HOST_GMAKE=' + self.native.tool('gmake'),
                   'HOST_SH=' + self.native.tool('sh'),
                   'EMBER_HOST_GNU_TRIPLE=' + self.native.compiler['triplet'],
                   '-V', '${GNU_SOURCE}']
        try:
            selected = process.capture(command, env=env).stdout.decode('utf-8').strip()
        except (SDKError, UnicodeError) as exc:
            raise SDKError('selected OS cannot resolve the RCS producer') from exc
        if selected != self.paths['rcs-source']:
            raise SDKError('selected OS does not support EMBER_RCS_SOURCE')

    def require_diffutils_source_support(self):
        if 'diffutils-source' not in self.paths:
            return
        root = self.paths['os']
        env = dict(self.environment)
        env.pop('EMBER_NATIVE_TOOLS', None)
        command = [self.native.tool('make'), '-r', '-m', os.path.join(root, 'share/mk'),
                   '-C', os.path.join(root, 'external/gpl2/diffutils/gnu'), 'MAKECONF=/dev/null', '_SRC_TOP_=',
                   'NETBSDSRCDIR=' + root, 'MACHINE=' + self.target['machine'],
                   'MACHINE_ARCH=' + self.target['arch'], 'USETOOLS=yes',
                   'TOOLDIR=' + self.paths['tools'],
                   'EMBER_DIFFUTILS_SOURCE=' + self.paths['diffutils-source'],
                   'HOST_GMAKE=' + self.native.tool('gmake'),
                   'HOST_SH=' + self.native.tool('sh'),
                   'EMBER_HOST_GNU_TRIPLE=' + self.native.compiler['triplet'],
                   '-V', '${GNU_SOURCE}']
        try:
            selected = process.capture(command, env=env).stdout.decode('utf-8').strip()
        except (SDKError, UnicodeError) as exc:
            raise SDKError('selected OS cannot resolve the DIFFUTILS producer') from exc
        if selected != self.paths['diffutils-source']:
            raise SDKError('selected OS does not support EMBER_DIFFUTILS_SOURCE')

    def require_tdb_source_support(self):
        if 'tdb-source' not in self.paths:
            return
        root = self.paths['os']
        env = dict(self.environment)
        env.pop('EMBER_NATIVE_TOOLS', None)
        command = [self.native.tool('make'), '-r', '-m', os.path.join(root, 'share/mk'),
                   '-C', os.path.join(root, 'external/bsd/tdb/lib'), 'MAKECONF=/dev/null', '_SRC_TOP_=',
                   'NETBSDSRCDIR=' + root, 'MACHINE=' + self.target['machine'],
                   'MACHINE_ARCH=' + self.target['arch'], 'USETOOLS=yes',
                   'TOOLDIR=' + self.paths['tools'],
                   'EMBER_TDB_SOURCE=' + self.paths['tdb-source'],
                   'HOST_PYTHON=' + self.native.tool('python'),
                   'HOST_SH=' + self.native.tool('sh'), '-V', '${WAF_SOURCE}']
        try:
            selected = process.capture(command, env=env).stdout.decode('utf-8').strip()
        except (SDKError, UnicodeError) as exc:
            raise SDKError('selected OS cannot resolve the TDB producer') from exc
        if selected != self.paths['tdb-source']:
            raise SDKError('selected OS does not support EMBER_TDB_SOURCE')

    def require_grep_source_support(self):
        if 'grep-source' not in self.paths:
            return
        root = self.paths['os']
        env = dict(self.environment)
        env.pop('EMBER_NATIVE_TOOLS', None)
        command = [self.native.tool('make'), '-r', '-m', os.path.join(root, 'share/mk'),
                   '-C', os.path.join(root, 'external/gpl2/grep/bin'), 'MAKECONF=/dev/null', '_SRC_TOP_=',
                   'NETBSDSRCDIR=' + root, 'MACHINE=' + self.target['machine'],
                   'MACHINE_ARCH=' + self.target['arch'], 'USETOOLS=yes',
                   'TOOLDIR=' + self.paths['tools'],
                   'EMBER_GREP_SOURCE=' + self.paths['grep-source'],
                   'HOST_GMAKE=' + self.native.tool('gmake'),
                   'HOST_SH=' + self.native.tool('sh'),
                   'EMBER_HOST_GNU_TRIPLE=' + self.native.compiler['triplet'],
                   '-V', '${GNU_SOURCE}']
        try:
            selected = process.capture(command, env=env).stdout.decode('utf-8').strip()
        except (SDKError, UnicodeError) as exc:
            raise SDKError('selected OS cannot resolve the GREP producer') from exc
        if selected != self.paths['grep-source']:
            raise SDKError('selected OS does not support EMBER_GREP_SOURCE')

    def require_gettext_source_support(self):
        if 'gettext-source' not in self.paths:
            return
        root = self.paths['os']
        env = dict(self.environment)
        env.pop('EMBER_NATIVE_TOOLS', None)
        common = [self.native.tool('make'), '-r', '-m', os.path.join(root, 'share/mk'),
                  'MAKECONF=/dev/null', '_SRC_TOP_=', 'NETBSDSRCDIR=' + root,
                  'MACHINE=' + self.target['machine'], 'MACHINE_ARCH=' + self.target['arch'],
                  'USETOOLS=yes', 'TOOLDIR=' + self.paths['tools'],
                  'EMBER_GETTEXT_SOURCE=' + self.paths['gettext-source'],
                  'EMBER_LIBXML2_SOURCE=' + self.paths['libxml2-source'],
                  'HOST_GMAKE=' + self.native.tool('gmake'), 'HOST_SH=' + self.native.tool('sh'),
                  'HOST_OD=' + self.native.tool('od'),
                  'HOST_PKG_CONFIG=' + self.native.tool('pkg-config'),
                  'EMBER_HOST_GNU_TRIPLE=' + self.native.compiler['triplet']]
        try:
            xml = process.capture([*common, '-C', os.path.join(root, 'external/bsd/libxml2/gnu'),
                                   '-V', '${GNU_SOURCE}'], env=env).stdout.decode('utf-8').strip()
            output = process.capture([*common, '-C', os.path.join(root, 'external/gpl2/gettext/gnu'),
                                      '-V', '${GNU_SOURCE}', '-V', '${GNU_CONFIGURE_ENV}',
                                      '-V', '${GNU_CONFIGURE_DEPENDENCY}'], env=env).stdout.decode('utf-8').splitlines()
        except (SDKError, UnicodeError) as exc:
            raise SDKError('selected OS cannot resolve the common gettext and libxml2 producers') from exc
        if (xml != self.paths['libxml2-source'] or len(output) != 3 or
                output[0] != self.paths['gettext-source'] or
                'EMBER_GETTEXT_PLAIN_STYLE=yes' not in output[1] or
                not output[2].endswith('/gnu-build/.libs/libxml2.a')):
            raise SDKError('selected OS does not support the common gettext and libxml2 producers')

    def require_ekermit_source_support(self):
        if 'ekermit-source' not in self.paths:
            return
        root = self.paths['os']
        env = dict(self.environment)
        env.pop('EMBER_NATIVE_TOOLS', None)
        command = [self.native.tool('make'), '-r', '-m', os.path.join(root, 'share/mk'),
                   '-C', os.path.join(root, 'external/bsd/ekermit/bin/ekermit'),
                   'MAKECONF=/dev/null', '_SRC_TOP_=', 'NETBSDSRCDIR=' + root,
                   'MACHINE=' + self.target['machine'], 'MACHINE_ARCH=' + self.target['arch'],
                   'USETOOLS=yes', 'TOOLDIR=' + self.paths['tools'],
                   'EMBER_EKERMIT_SOURCE=' + self.paths['ekermit-source'], '-V', '${EKERMITDIST}']
        try:
            selected = process.capture(command, env=env).stdout.decode('utf-8').strip()
        except (SDKError, UnicodeError) as exc:
            raise SDKError('selected OS cannot resolve the E-Kermit producer') from exc
        if selected != self.paths['ekermit-source']:
            raise SDKError('selected OS does not support EMBER_EKERMIT_SOURCE')

    def require_nvi_source_support(self):
        if 'nvi-source' not in self.paths:
            return
        root = self.paths['os']
        env = dict(self.environment)
        env.pop('EMBER_NATIVE_TOOLS', None)
        command = [self.native.tool('make'), '-r', '-m', os.path.join(root, 'share/mk'),
                   '-C', os.path.join(root, 'external/bsd/nvi/usr.bin/nvi'),
                   'MAKECONF=/dev/null', '_SRC_TOP_=', 'NETBSDSRCDIR=' + root,
                   'MACHINE=' + self.target['machine'], 'MACHINE_ARCH=' + self.target['arch'],
                   'USETOOLS=yes', 'TOOLDIR=' + self.paths['tools'],
                   'EMBER_NVI_SOURCE=' + self.paths['nvi-source'], '-V', '${DIST}']
        try:
            selected = process.capture(command, env=env).stdout.decode('utf-8').strip()
        except (SDKError, UnicodeError) as exc:
            raise SDKError('selected OS cannot resolve the nvi producer') from exc
        if selected != self.paths['nvi-source']:
            raise SDKError('selected OS does not support EMBER_NVI_SOURCE')

    def require_top_source_support(self):
        if 'top-source' not in self.paths:
            return
        root = self.paths['os']
        env = dict(self.environment)
        env.pop('EMBER_NATIVE_TOOLS', None)
        command = [self.native.tool('make'), '-r', '-m', os.path.join(root, 'share/mk'),
                   '-C', os.path.join(root, 'external/bsd/top/bin'),
                   'MAKECONF=/dev/null', '_SRC_TOP_=', 'NETBSDSRCDIR=' + root,
                   'MACHINE=' + self.target['machine'], 'MACHINE_ARCH=' + self.target['arch'],
                   'USETOOLS=yes', 'TOOLDIR=' + self.paths['tools'],
                   'EMBER_TOP_SOURCE=' + self.paths['top-source'], '-V', '${TOPDIR}']
        try:
            selected = process.capture(command, env=env).stdout.decode('utf-8').strip()
        except (SDKError, UnicodeError) as exc:
            raise SDKError('selected OS cannot resolve the top producer') from exc
        if selected != self.paths['top-source']:
            raise SDKError('selected OS does not support EMBER_TOP_SOURCE')

    def require_iscsi_source_support(self):
        if 'iscsi-source' not in self.paths:
            return
        root = self.paths['os']
        env = dict(self.environment)
        env.pop('EMBER_NATIVE_TOOLS', None)
        command = [self.native.tool('make'), '-r', '-m', os.path.join(root, 'share/mk'),
                   '-C', os.path.join(root, 'external/bsd/iscsi/lib'),
                   'MAKECONF=/dev/null', '_SRC_TOP_=', 'NETBSDSRCDIR=' + root,
                   'MACHINE=' + self.target['machine'], 'MACHINE_ARCH=' + self.target['arch'],
                   'USETOOLS=yes', 'TOOLDIR=' + self.paths['tools'],
                   'EMBER_ISCSI_SOURCE=' + self.paths['iscsi-source'], '-V', '${ISCSIDIST}']
        try:
            selected = process.capture(command, env=env).stdout.decode('utf-8').strip()
        except (SDKError, UnicodeError) as exc:
            raise SDKError('selected OS cannot resolve the iSCSI producer') from exc
        if selected != self.paths['iscsi-source']:
            raise SDKError('selected OS does not support EMBER_ISCSI_SOURCE')

    def require_ntp_source_support(self):
        selected = ('ntp-source' in self.paths, 'libopts-source' in self.paths)
        if selected == (False, False):
            return
        if selected != (True, True):
            raise SDKError('NTP requires both common NTP and AutoGen libopts sources')
        root = self.paths['os']
        env = dict(self.environment)
        env.pop('EMBER_NATIVE_TOOLS', None)
        command = [self.native.tool('make'), '-m', os.path.join(root, 'share/mk'),
                   '-C', os.path.join(root, 'external/bsd/ntp/lib/libntp'),
                   'MAKECONF=/dev/null', '_SRC_TOP_=', 'NETBSDSRCDIR=' + root,
                   'MACHINE=' + self.target['machine'], 'MACHINE_ARCH=' + self.target['arch'],
                   'USETOOLS=yes', 'TOOLDIR=' + self.paths['tools'],
                   'EMBER_NTP_SOURCE=' + self.paths['ntp-source'],
                   'EMBER_LIBOPTS_SOURCE=' + self.paths['libopts-source'],
                   '-V', '${IDIST}', '-V', '${NTP_LIBOPTS_SOURCE}']
        try:
            found = process.capture(command, env=env).stdout.decode('utf-8').splitlines()
        except (SDKError, UnicodeError) as exc:
            raise SDKError('selected OS cannot resolve the NTP producer') from exc
        if found != [self.paths['ntp-source'], self.paths['libopts-source']]:
            raise SDKError('selected OS does not support common NTP and libopts sources')

    def require_libpcap_source_support(self):
        if 'libpcap-source' not in self.paths:
            return
        root = self.paths['os']
        env = dict(self.environment)
        env.pop('EMBER_NATIVE_TOOLS', None)
        command = [self.native.tool('make'), '-r', '-m', os.path.join(root, 'share/mk'),
                   '-C', os.path.join(root, 'external/bsd/libpcap/lib'),
                   'MAKECONF=/dev/null', '_SRC_TOP_=', 'NETBSDSRCDIR=' + root,
                   'MACHINE=' + self.target['machine'], 'MACHINE_ARCH=' + self.target['arch'],
                   'USETOOLS=yes', 'TOOLDIR=' + self.paths['tools'],
                   'EMBER_LIBPCAP_SOURCE=' + self.paths['libpcap-source'], '-V', '${SRCDIR}']
        try:
            selected = process.capture(command, env=env).stdout.decode('utf-8').strip()
        except (SDKError, UnicodeError) as exc:
            raise SDKError('selected OS cannot resolve the libpcap producer') from exc
        if selected != self.paths['libpcap-source']:
            raise SDKError('selected OS does not support EMBER_LIBPCAP_SOURCE')

    def require_tcpdump_source_support(self):
        if 'tcpdump-source' not in self.paths:
            return
        root = self.paths['os']
        env = dict(self.environment)
        env.pop('EMBER_NATIVE_TOOLS', None)
        command = [self.native.tool('make'), '-r', '-m', os.path.join(root, 'share/mk'),
                   '-C', os.path.join(root, 'external/bsd/tcpdump/bin'),
                   'MAKECONF=/dev/null', '_SRC_TOP_=', 'NETBSDSRCDIR=' + root,
                   'MACHINE=' + self.target['machine'], 'MACHINE_ARCH=' + self.target['arch'],
                   'USETOOLS=yes', 'TOOLDIR=' + self.paths['tools'],
                   'EMBER_TCPDUMP_SOURCE=' + self.paths['tcpdump-source'], '-V', '${SRCDIR}']
        try:
            selected = process.capture(command, env=env).stdout.decode('utf-8').strip()
        except (SDKError, UnicodeError) as exc:
            raise SDKError('selected OS cannot resolve the tcpdump producer') from exc
        if selected != self.paths['tcpdump-source']:
            raise SDKError('selected OS does not support EMBER_TCPDUMP_SOURCE')

    def require_tcp_wrappers_source_support(self):
        if 'tcp-wrappers-source' not in self.paths:
            return
        root = self.paths['os']
        env = dict(self.environment)
        env.pop('EMBER_NATIVE_TOOLS', None)
        command = [self.native.tool('make'), '-r', '-m', os.path.join(root, 'share/mk'),
                   '-C', os.path.join(root, 'lib/libwrap'),
                   'MAKECONF=/dev/null', '_SRC_TOP_=', 'NETBSDSRCDIR=' + root,
                   'MACHINE=' + self.target['machine'], 'MACHINE_ARCH=' + self.target['arch'],
                   'USETOOLS=yes', 'TOOLDIR=' + self.paths['tools'],
                   'EMBER_TCP_WRAPPERS_SOURCE=' + self.paths['tcp-wrappers-source'], '-V', '${TCPWRAPPERS_SRCDIR}']
        try:
            selected = process.capture(command, env=env).stdout.decode('utf-8').strip()
        except (SDKError, UnicodeError) as exc:
            raise SDKError('selected OS cannot resolve the TCP wrappers producer') from exc
        if selected != self.paths['tcp-wrappers-source']:
            raise SDKError('selected OS does not support EMBER_TCP_WRAPPERS_SOURCE')

    def require_ppp_source_support(self):
        if 'ppp-source' not in self.paths:
            return
        root = self.paths['os']
        env = dict(self.environment)
        env.pop('EMBER_NATIVE_TOOLS', None)
        command = [self.native.tool('make'), '-r', '-m', os.path.join(root, 'share/mk'),
                   '-C', os.path.join(root, 'external/bsd/ppp/usr.sbin/pppd'),
                   'MAKECONF=/dev/null', '_SRC_TOP_=', 'NETBSDSRCDIR=' + root,
                   'MACHINE=' + self.target['machine'], 'MACHINE_ARCH=' + self.target['arch'],
                   'USETOOLS=yes', 'TOOLDIR=' + self.paths['tools'],
                   'EMBER_PPP_SOURCE=' + self.paths['ppp-source'], '-V', '${DIST}']
        try:
            selected = process.capture(command, env=env).stdout.decode('utf-8').strip()
        except (SDKError, UnicodeError) as exc:
            raise SDKError('selected OS cannot resolve the ppp producer') from exc
        if selected != self.paths['ppp-source']:
            raise SDKError('selected OS does not support EMBER_PPP_SOURCE')

    def require_runtime_work(self):
        if (not self.options.get('runtime-work') and
                (self.compiler_family() == 'gcc' or
                 (not self.options.get('llvm-runtime-stage') and not hasattr(self, 'llvm_phase')))):
            raise SDKError('--runtime-work is required for GCC or when building the LLVM runtime')

    def preflight(self):
        process.require_posix()
        if type(self.options.get('base-cxx', False)) is not bool:
            raise SDKError('base C++ selection must be boolean')
        clang = self.compiler_family() == 'clang'
        if self.options.get('base-cxx', False) and not clang:
            raise SDKError('base C++ aliases require the selected Clang runtime')
        self.target = target_definition(self.options.get('target'))
        jobs = self.options.get('jobs')
        if not isinstance(jobs, str) or re.fullmatch('[1-9][0-9]*', jobs) is None:
            raise SDKError('jobs must be a positive integer')
        # Check the workspace target before querying any host tools.
        layout = workspace.inspect_layout(self.options['sdk'])
        workspace.require_target(layout, self.options['target'])
        self.llvm_phase = getattr(self, 'llvm_phase', 'final')
        if not self.options.get('llvm-source'):
            raise SDKError('--llvm-source is required: prepare the common Ports LLVM source')
        if bool(self.options.get('gettext-source')) != bool(self.options.get('libxml2-source')):
            raise SDKError('gettext and libxml2 sources must be selected together')
        for name in ('libarchive-source', 'libevent-source'):
            if self.llvm_phase != 'headers' and not self.options.get(name):
                raise SDKError(f'--{name} is required: prepare the common Ports source')
        if bool(self.options.get('libarchive-source')) != bool(self.options.get('libevent-source')):
            raise SDKError('select both common sources together')
        if clang:
            if self.llvm_phase != 'headers' and not self.options.get('llvm-runtime-stage'):
                raise SDKError('--llvm-runtime-stage is required for direct final preflight; run() builds its prerequisites')
            if self.llvm_phase == 'final' and not self.options.get('llvm-bootstrap-stage'):
                raise SDKError('--llvm-bootstrap-stage is required with a full LLVM runtime')
        elif not self.options.get('gcc-archive') or not self.options.get('pkgsrc'):
            raise SDKError('--gcc-archive and --pkgsrc are required for the explicit GCC runtime')
        # After the target and selected-input checks, before any host query.
        self.require_runtime_work()
        if not self.options.get('host-binutils'):
            raise SDKError('--host-binutils is required for the OS host BFD tools')
        self.select_native_toolset()
        # Git is a source-inspection service. It is not a build-tool provider.
        if self.git_command is None:
            self.git_command = self.find_command('git')
        else:
            self.executable(self.git_command)
        self.environment['PATH'] = native_path(self.native_selection)
        # Darwin PID churn can delay a complete empty snapshot after a producer exits.
        self.process_inspector = process.SessionInspector(env=self.environment, empty_snapshot_timeout=120)
        self.logical_sdk = layout['logical_root']
        self.paths = dict(sdk=paths.build_path(layout['canonical_root']))
        input_names = ['os', 'ports', 'compiler', 'sdk-source', 'libedit-source', 'llvm-source', 'host-binutils']
        input_names += [name for name in ('libarchive-source', 'libevent-source', 'byacc-source', 'texinfo-source', 'rcs-source', 'diffutils-source', 'grep-source', 'ekermit-source', 'nvi-source', 'tdb-source', 'ppp-source', 'top-source', 'iscsi-source', 'libpcap-source', 'tcpdump-source', 'tcp-wrappers-source', 'ntp-source', 'libopts-source', 'gettext-source', 'libxml2-source', 'x11-source') if self.options.get(name)]
        if clang and self.llvm_phase != 'headers' and self.options.get('llvm-runtime-original-source'):
            input_names.append('llvm-runtime-original-source')
        input_names += (([] if self.llvm_phase == 'headers' else ['llvm-runtime-stage'])
                        if clang else ['runtime-work', 'gcc-archive', 'pkgsrc'])
        if clang and self.llvm_phase == 'final':
            input_names.append('llvm-bootstrap-stage')
        if clang and self.llvm_phase == 'base' and self.options.get('llvm-runtime-base'):
            input_names.append('llvm-runtime-base')
        for name in input_names:
            self.paths[name] = paths.build_path(paths.canonical_existing(self.options[name]))
        self.paths['python'] = self.native.tool('python')
        if self.options.get('gettext-source'):
            self.paths['git'] = self.git_command
        self.paths['host-prefix'] = self.native.bundle_root
        self.paths.update({option: self.native.tool(role) for option, role in NATIVE_ALIASES})
        for name in input_names:
            if name == 'gcc-archive':
                paths.regular_file(self.paths[name])
                continue
            paths.real_directory(self.paths[name])
        paths.regular_file(os.path.join(self.paths['host-binutils'], 'host-bfd.mk'))
        sdk = self.paths['sdk']
        host = sysconfig.get_config_var('HOST_GNU_TYPE') or f'{platform.machine()}-{sysconfig.get_platform()}'
        self.host = re.sub('[^A-Za-z0-9_.+-]', '_', host)
        profile = self.host + '-' + self.options['target']
        self.paths['destination'] = self.output_path(self.options.get('destination', os.path.join(sdk, 'sysroot')), sdk)
        if paths.inside(self.paths['destination'], os.path.join(sdk, 'sysroot')):
            raise SDKError('destination must not be below the active sysroot; use SDK/stages/NAME')
        for name, directory in (('objects', 'os'), ('tools', 'tools')):
            self.paths[name] = self.output_path(self.options.get(name, os.path.join(sdk, 'obj', directory, profile)), os.path.join(sdk, 'obj'))
        self.paths['native-routing'] = os.path.join(
            self.paths['tools'], 'native-routing-' + self.native_contract_sha256)
        self.environment['EMBER_NATIVE_TOOLS'] = self.paths['native-routing']
        self.empty_destination()
        for name in ('obj', 'packages', 'receipts'):
            paths.reject_overlap(self.paths['destination'], os.path.join(sdk, name))
        for left, right in itertools.combinations(('destination', 'objects', 'tools'), 2):
            paths.reject_overlap(self.paths[left], self.paths[right])
        for input_name in input_names + ['python']:
            for output_name in ('destination', 'objects', 'tools'):
                paths.reject_overlap(self.paths[input_name], self.paths[output_name])
        for value in (self.native.bundle_root,):
            for name in ('destination', 'objects', 'tools'):
                paths.reject_overlap(value, self.paths[name])
        for name in ('include', 'lib', 'libexec/ld.elf_so', 'tools', 'share/mk', 'sys', 'distrib', 'etc'):
            paths.real_directory(os.path.join(self.paths['os'], name))
        for name in ('build.sh', 'Makefile', 'libexec/ld.elf_so/Makefile', 'share/mk/bsd.own.mk'):
            paths.regular_file(os.path.join(self.paths['os'], name))
        from .llvm_sysroot import PRODUCER, runtime_paths, llvm_flags
        if clang:
            verifier = PRODUCER
        else:
            verifier = self.VERIFIER
        paths.regular_file(os.path.join(self.paths['ports'], verifier))
        paths.regular_file(os.path.join(self.paths['ports'], PRODUCER))
        self.sources = {name: self.source(self.paths[name], name) for name in ('os', 'ports')}
        self.require_byacc_source_support()
        self.require_texinfo_source_support()
        self.require_rcs_source_support()
        self.require_diffutils_source_support()
        self.require_grep_source_support()
        self.require_tdb_source_support()
        self.require_ekermit_source_support()
        self.require_nvi_source_support()
        self.require_top_source_support()
        self.require_iscsi_source_support()
        self.require_ntp_source_support()
        self.require_libpcap_source_support()
        self.require_tcpdump_source_support()
        self.require_tcp_wrappers_source_support()
        self.require_ppp_source_support()
        self.require_gettext_source_support()
        self.sources['sdk'] = self.source(self.paths['sdk-source'], 'sdk')
        self.libedit_record = libedit.validate_source(self.paths['libedit-source'], ports=self.paths['ports'], git=self.git_command)
        self.common_source_records = (self.verify_common_sources()
                                      if any(name + '-source' in self.paths for name in ('libarchive', 'libevent', 'byacc', 'texinfo', 'rcs', 'diffutils', 'grep', 'ekermit', 'nvi', 'tdb', 'ppp', 'top', 'iscsi', 'libpcap', 'tcpdump', 'tcp-wrappers', 'ntp', 'libopts', 'gettext', 'libxml2', 'x11'))
                                      else {})
        self.python = self.python_identity()
        self.git(self.paths['os'], 'ls-files', '--error-unmatch', 'build.sh', 'Makefile', 'libexec/ld.elf_so/Makefile', 'share/mk/bsd.own.mk')
        self.git(self.paths['ports'], 'ls-files', '--error-unmatch', verifier)
        self.git(self.paths['ports'], 'ls-files', '--error-unmatch', PRODUCER)
        self.compiler_record = self.compiler()
        producer = [self.paths['python'], '-B', os.path.join(self.paths['ports'], PRODUCER),
                    '--sdk-source', self.paths['sdk-source']]
        self.llvm_source_command = producer + ['verify-source', '--source', self.paths['llvm-source'],
                                               '--git', self.git_command]
        self.llvm_source_record = self.verify_llvm_source()
        if clang:
            self.runtime_command = self.llvm_source_command
            prefix = None
            if self.llvm_phase != 'headers':
                from . import llvm_runtime
                stage = self.paths['llvm-runtime-stage']
                record = llvm_runtime.validate_stage(stage, target=self.options['target'],
                    required_components=('builtins', 'libunwind-static'))
                self.paths.update(runtime_paths(stage, record))
                if self.llvm_phase == 'final':
                    boot = llvm_runtime.validate_bootstrap_pair(self.paths['llvm-bootstrap-stage'], record, target=self.options['target'])
                    self.paths['llvm-libc-unwind'] = runtime_paths(self.paths['llvm-bootstrap-stage'], boot)['llvm-unwind']
                prefix = self.paths['runtime-prefix']
                self.runtime_command = producer + ['verify', '--stage', stage, '--target', self.options['target'],
                    '--phase', 'full' if self.llvm_phase == 'final' else 'bootstrap', '--git', self.git_command]
                if 'llvm-runtime-original-source' in self.paths:
                    self.runtime_command += ['--source', self.paths['llvm-source'],
                        '--original-source', self.paths['llvm-runtime-original-source']]
            selected_flags = llvm_flags(self.paths, phase=self.llvm_phase,
                                        base_cxx=self.options.get('base-cxx', False))
            replaced = {value.split('=', 1)[0] for value in selected_flags}
            common_flags = [value for value in self.FLAGS if value.split('=', 1)[0] not in replaced]
            self.flags = common_flags + selected_flags
        else:
            prefix = os.path.join(self.paths['runtime-work'], 'stage/usr/pkg/gcc16')
            if paths.canonical_existing(prefix) != prefix:
                raise SDKError('runtime prefix must be the real WORK/stage/usr/pkg/gcc16 tree')
            self.paths['runtime-prefix'] = prefix
            self.runtime_command = [self.paths['python'], '-B', os.path.join(self.paths['ports'], verifier), 'check', self.paths['runtime-work'], self.paths['gcc-archive'], self.paths['pkgsrc'], '--sdk-source', self.paths['sdk-source']]
            self.flags = self.FLAGS + ['EMBER_COMPILER_FAMILY=gcc', f'EXTERNAL_TOOLCHAIN={self.paths["compiler"]}', f'EMBER_EXTERNAL_GCC_RUNTIME={prefix}', f'EMBER_LLVM_SOURCE={self.paths["llvm-source"]}']
        self.runtime_record = self.verify_runtime()
        self.flags += [f'EMBER_LIBEDIT_SOURCE={self.paths["libedit-source"]}',
                       f'EMBER_HOST_BFD_CONFIG={self.paths["host-binutils"]}/host-bfd.mk'] + native_host_flags(self.native_selection)
        self.flags += [f'EMBER_{name.upper().replace("-", "_")}_SOURCE={self.paths[name + "-source"]}'
                       for name in ('libarchive', 'libevent', 'byacc', 'texinfo', 'rcs', 'diffutils', 'grep', 'ekermit', 'nvi', 'tdb', 'ppp', 'top', 'iscsi', 'libpcap', 'tcpdump', 'tcp-wrappers', 'ntp', 'libopts', 'gettext', 'libxml2') if name + '-source' in self.paths]
        if 'tdb-source' in self.paths:
            self.flags.append('HOST_PYTHON=' + self.native.tool('python'))
        if any(name + '-source' in self.paths for name in ('texinfo', 'rcs', 'diffutils', 'grep', 'gettext')):
            self.flags += gnu_target_host_flags(self.native_selection)
        if 'gettext-source' in self.paths:
            self.flags += gettext_target_host_flags(self.native_selection)
        self.signature = dict(host=self.host, target=self.target, compiler=self.compiler_record, libedit=self.libedit_record, runtime=self.runtime_record, runtime_prefix=prefix, flags=self.flags, host_tools=self.host_tools, native_toolset=self.native_selection, environment=self.environment, objects=self.paths['objects'], tools=self.paths['tools'], python=self.python, process_backend=self.process_inspector.describe())
        self.signature['native_inputs'] = self.native_inputs
        self.signature['compiler_family'] = self.compiler_family()
        self.signature['llvm_source'] = self.llvm_source_record
        self.signature['common_sources'] = self.common_source_records
        if clang:
            self.signature['llvm_phase'] = self.llvm_phase
        self.configure_signature()
        self.contexts = {name: self.inspect_context(self.paths[name]) for name in ('objects', 'tools')}
        self.build_command = [self.shell, os.path.join(self.paths['os'], 'build.sh'), '-U', '-u', '-j' + jobs, '-m', self.target['machine'], '-a', self.target['arch'], '-O', self.paths['objects'], '-T', self.paths['tools'], '-D', self.paths['destination']]
        # Commit metadata stays out of the signature: the same OS tree at a new
        # commit reuses compatible objects. build.sh derives a missing
        # MKREPRO_TIMESTAMP from VCS tools that the sanitized PATH omits.
        for flag in self.flags + ['MKREPRO_TIMESTAMP=' + str(self.source_timestamp()),
                                  'BUILDINFO=EmberBSD:' + self.sources['os']['commit']]:
            self.build_command.extend(('-V', flag))
        self.build_command.extend(('tools', 'libs'))
        self.loader_command = [os.path.join(self.paths['tools'], 'bin', 'nbmake-' + self.target['machine']), '-C', os.path.join(self.paths['os'], 'libexec/ld.elf_so'), '-j' + jobs, 'dependall']
        self.install_command = self.loader_command[:-1] + ['install']
        self.receipt = dict(format='emberbsd-sysroot-build', version=3 if clang else 2, scope='sysroot-build', qualification='not-built', execution_acceptance='not-run', workspace=layout, paths=self.paths, sources=self.sources, previous_contexts=self.contexts, signature=self.signature, commands=self.commands, plan=[self.runtime_command, self.build_command, self.loader_command, self.install_command, self.runtime_command, self.runtime_command], working_directory=self.paths['os'], copy=dict(source=prefix, destination=os.path.join(self.paths['destination'], 'usr/pkg/llvm' if clang else 'usr/pkg/gcc16'), verification='exact pre/copy/post inventory'), not_checked=['target execution', 'ABI compatibility', 'SDK release'])
        return self.receipt

    def source_timestamp(self):
        return int(self.git(self.paths['os'], 'show', '-s', '--format=%ct', self.sources['os']['commit']))

    def mkdirs(self, path, boundary):
        current = boundary
        for name in os.path.relpath(path, boundary).split(os.sep):
            current = os.path.join(current, name)
            try:
                os.mkdir(current)
            except FileExistsError:
                paths.real_directory(current)

    def exclusive_file(self, path, mode=0o644):
        handle = os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode), 'wb')
        self.handles.append(handle)
        return handle

    def save_receipt(self):
        self.receipt_file.seek(0)
        self.receipt_file.write(jsonio.json_bytes(self.receipt))
        self.receipt_file.truncate()
        self.receipt_file.flush()
        os.fsync(self.receipt_file.fileno())

    def prepare_contexts(self):
        for name in ('objects', 'tools'):
            if self.inspect_context(self.paths[name]) != self.contexts[name]:
                raise SDKError('object context changed after preflight')
        for name in ('objects', 'tools'):
            path = self.paths[name]
            record = dict(format='emberbsd-sysroot-object-context', version=1,
                          signature=self.context_signature(path), sources=self.sources)
            self.mkdirs(path, os.path.join(self.paths['sdk'], 'obj'))
            jsonio.write_json(os.path.join(path, self.CONTEXT), record, exclusive=self.contexts[name] is None)

    def interrupted(self):
        if self.interrupted_signal:
            raise BuildInterrupted(f'received {self.interrupted_signal}')

    def quarantine_contexts(self, sid):
        for name in ('objects', 'tools'):
            path = os.path.join(self.paths[name], self.QUARANTINE)
            if paths.entry(path) is None:
                jsonio.write_json(path, dict(reason='owned session did not stop', sid=sid))

    def stop_group(self, group):
        try:
            group.stop()
        except (SDKError, OSError) as exc:
            self.quarantine_contexts(group.pid)
            raise ProcessOwnershipError(group.pid, f'object contexts are quarantined: {exc}') from exc

    def dispatch(self, argv):
        self.verify_native_toolset()
        row = dict(argv=argv, status='running')
        self.commands.append(row)
        self.save_receipt()
        group = process.OwnedProcessGroup(argv, cwd=self.paths['os'], env=self.environment, stdout=self.log, stderr=self.log, inspector=self.process_inspector)
        cleanup_attempted = False
        try:
            self.interrupted()
            group.start()
            while True:
                self.interrupted()
                status = group.poll()
                if status is not None:
                    break
                time.sleep(0.05)
            row['exit_status'] = status if status >= 0 else None
            if group.alive():
                row.update(status='failed', error='producer left live descendants in its owned session')
                cleanup_attempted = True
                self.stop_group(group)
                self.save_receipt()
                raise SDKError(row['error'])
            if status < 0:
                row['signal'] = -status
            row['status'] = 'passed' if status == 0 else 'failed'
            self.save_receipt()
            if status != 0:
                raise SDKError(f'build command failed; inspect {os.path.join(self.paths["destination"], self.LOG)}')
        except KeyboardInterrupt:
            for name in ('SIGINT', 'SIGTERM', 'SIGHUP'):
                signal.signal(getattr(signal, name), signal.SIG_IGN)
            cleanup_attempted = True
            self.stop_group(group)
            row['status'] = 'interrupted'
            raise
        finally:
            if not cleanup_attempted and group.pid is not None:
                self.stop_group(group)
            self.verify_native_toolset()

    def execute_external(self, command, receipt_path, log_path):
        """Run a prerequisite producer with the same ownership and signal rules."""
        self.handles, self.signal_handlers = [], {}
        self.receipt = dict(format='emberbsd-runtime-command', version=1,
                            commands=self.commands, status='running')
        try:
            self.receipt_file = self.exclusive_file(receipt_path)
            self.log = self.exclusive_file(log_path)
            for name in ('SIGINT', 'SIGTERM', 'SIGHUP'):
                number = getattr(signal, name)
                self.signal_handlers[number] = signal.signal(number,
                    lambda signum, frame: setattr(self, 'interrupted_signal',
                        signal.Signals(signum).name.removeprefix('SIG')))
            self.dispatch(command)
            self.receipt['status'] = 'passed'
            self.save_receipt()
        finally:
            for handle in reversed(self.handles):
                handle.close()
            self.receipt_file = None
            for number, previous in self.signal_handlers.items():
                signal.signal(number, previous)

    def copy_runtime(self, *, destination=None, installer=None):
        clang = self.compiler_family() == 'clang'
        from . import llvm_runtime_install
        base_cxx = llvm_runtime_install.base_cxx_enabled(self.flags)
        if base_cxx and (not clang or self.llvm_phase != 'final'):
            raise SDKError('base C++ aliases require the final Clang producer profile')
        if self.verify_runtime() != self.runtime_record:
            raise SDKError('runtime changed since preflight')
        root = self.paths['destination'] if destination is None else paths.path_string(destination)
        paths.real_directory(root)
        if ((root != self.paths['destination'] and not paths.inside(root, self.paths['destination'])) or
                paths.canonical_existing(root) != root):
            raise SDKError('runtime destination must stay within the owned producer output')
        if installer is not None and getattr(installer, 'root', None) != root:
            raise SDKError('runtime installer does not match its destination')
        source = self.paths['runtime-prefix']
        destination = os.path.join(root, 'usr/pkg/llvm' if clang else 'usr/pkg/gcc16')
        if paths.entry(destination) is not None:
            raise SDKError('runtime destination already exists')
        alias_plan = None
        if base_cxx:
            alias_plan = llvm_runtime_install.preflight_base_aliases(
                root, self.runtime_record['manifest'])
        if installer is None:
            self.mkdirs(os.path.dirname(destination), root)
            os.mkdir(destination)
        else:
            parent = root
            for name in os.path.relpath(os.path.dirname(destination), root).split(os.sep):
                parent = os.path.join(parent, name)
                installer.directory(parent, 0o755)
            installer.directory(destination, self.runtime_record['root_mode'])
        rows = self.runtime_record['inventory']
        for relative, row in rows.items():
            self.interrupted()
            src, dst = os.path.join(source, relative), os.path.join(destination, relative)
            if installer is not None:
                if row['kind'] == 'directory':
                    installer.directory(dst, row['mode'])
                elif row['kind'] == 'link':
                    installer.link(row['target'], dst, row['mode'])
                else:
                    installer.file(src, dst, row['mode'])
            elif row['kind'] == 'directory':
                os.mkdir(dst)
            elif row['kind'] == 'link':
                os.symlink(row['target'], dst)
            elif row['kind'] == 'file':
                with open(dst, 'xb') as output, open(src, 'rb') as input_file:
                    shutil.copyfileobj(input_file, output, 1024 * 1024)
                os.chmod(dst, row['mode'])
        for relative, row in reversed(list(rows.items())):
            path = os.path.join(destination, relative)
            if row['kind'] == 'directory':
                os.chmod(path, row['mode'])
            elif row['kind'] == 'link' and stat.S_IMODE(os.lstat(path).st_mode) != row['mode']:
                if not hasattr(os, 'lchmod'):
                    raise SDKError('this host cannot preserve the audited symbolic-link mode')
                os.lchmod(path, row['mode'])
        os.chmod(destination, self.runtime_record['root_mode'])
        actual = (inventory.snapshot_tree(destination, machine=self.target['elf']) if clang else
                  inventory.runtime(destination, self.target['triplet'], self.target['elf']))
        if actual != rows:
            raise SDKError('copied runtime differs from the verified payload')
        if self.verify_runtime() != self.runtime_record:
            raise SDKError('runtime inputs changed during copy')
        if alias_plan is not None:
            llvm_runtime_install.install_base_aliases(root,
                self.runtime_record['manifest'], installer=installer)

    def configure_signature(self):
        """Allow a producer profile to bind its flags before object reuse checks."""

    def build_payload(self):
        """Run the producer sequence under the shared ownership contract."""
        for command in (self.build_command, self.loader_command, self.install_command):
            self.dispatch(command)
        self.copy_runtime()

    def inspect_payload(self):
        self.receipt['libedit_installation'] = libedit.validate_installation(
            self.paths['destination'], self.libedit_record, self.target['elf'])
        return inventory.sysroot(self.paths['destination'], self.target['elf'],
                                 exclude=(self.RECEIPT, self.MARKER, self.LOG))

    def run(self):
        self.require_runtime_work()
        if (self.compiler_family() == 'clang' and not hasattr(self, 'llvm_phase') and
                not self.options.get('llvm-runtime-stage')):
            from .llvm_sysroot import LLVMCoordinator
            return LLVMCoordinator(self.options, type(self), receipts=self.verification).run()
        self.preflight()
        if self.options.get('plan'):
            return self.receipt
        return self._run_preflighted()

    def _run_preflighted(self):
        try:
            for name in ('SIGINT', 'SIGTERM', 'SIGHUP'):
                number = getattr(signal, name)
                self.signal_handlers[number] = signal.signal(number, lambda signum, frame: setattr(self, 'interrupted_signal', signal.Signals(signum).name.removeprefix('SIG')))
            self.interrupted()
            self.locks = process.ContextLocks([self.paths[name] for name in ('objects', 'tools')], os.path.join(self.paths['sdk'], 'obj')).acquire()
            self.empty_destination()
            self.verify_native_toolset()
            self.mkdirs(self.paths['destination'], self.paths['sdk'])
            marker = os.path.join(self.paths['destination'], self.MARKER)
            handle = self.exclusive_file(marker)
            handle.write(b'This sysroot build is incomplete. Do not accept this payload.\n')
            handle.flush()
            if os.listdir(self.paths['destination']) != [self.MARKER]:
                raise SDKError('destination changed during claim')
            self.receipt_file = self.exclusive_file(os.path.join(self.paths['destination'], self.RECEIPT))
            self.log = self.exclusive_file(os.path.join(self.paths['destination'], self.LOG))
            self.receipt['qualification'] = 'incomplete'
            self.save_receipt()
            self.prepare_contexts()
            self.contexts_prepared = True
            self.prepare_native_routing()
            if libedit.validate_source(self.paths['libedit-source'], ports=self.paths['ports'], git=self.git_command) != self.libedit_record:
                raise SDKError('libedit source changed before build')
            if self.common_source_records and self.verify_common_sources() != self.common_source_records:
                raise SDKError('common sources changed before build')
            self.build_payload()
            if libedit.validate_source(self.paths['libedit-source'], ports=self.paths['ports'], git=self.git_command) != self.libedit_record:
                raise SDKError('libedit source changed during build')
            if self.common_source_records and self.verify_common_sources() != self.common_source_records:
                raise SDKError('common sources changed during build')
            for name, expected in self.sources.items():
                if self.source(expected['path'], name) != expected:
                    raise SDKError(f'{name} source changed during build')
            if self.compiler() != self.compiler_record:
                raise SDKError('compiler changed during build')
            if self.python_identity() != self.python:
                raise SDKError('Python or SDK implementation changed during build')
            if self.verify_llvm_source() != self.llvm_source_record:
                raise SDKError('common LLVM source changed during build')
            self.verify_native_toolset()
            self.receipt['outputs'] = self.inspect_payload()
            self.interrupted()
            self.receipt['qualification'] = 'built-and-inspected'
            self.save_receipt()
            os.unlink(marker)
            return self.receipt
        except (Exception, KeyboardInterrupt) as exc:
            if self.receipt_file is not None:
                self.receipt.update(qualification='incomplete', error=str(exc))
                self.save_receipt()
            raise
        finally:
            for handle in reversed(self.handles):
                handle.close()
            if self.locks:
                self.locks.close()
            for number, previous in self.signal_handlers.items():
                signal.signal(number, previous)
