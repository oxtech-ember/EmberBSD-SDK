# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD; AI-assisted OS and Ports build orchestration.
"""Checked separate-stage OS sysroot builder. No build occurs on import."""

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
import time
from . import API_VERSION, inventory, jsonio, libedit, native_os_routing, native_toolset, paths, prepared_sources, prepared_refresh, process, provenance, runtime_refresh, llvm_source_refresh, sourceprep, workspace
from .verification_receipts import RunReceipts
from .cpu_capacity import resolve_jobs
from .errors import CommandError, SDKError, ProcessOwnershipError
from .native_os_routing import (gettext_target_host_flags, gnu_target_host_flags,
                                native_host_flags,
                                native_path, read_native_os_contract)
from .targets import TARGETS, target_definition

NATIVE_INPUTS = ('native-toolset', 'native-toolset-sha256',
                 'native-producer-receipt', 'native-producer-receipt-sha256')
NATIVE_ALIASES = (('host-cc', 'cc'), ('host-cxx', 'cxx'), ('python', 'python'))
OPENSSH_LOCAL_GENERATOR_ROLES = ('autoconf', 'autom4te', 'gm4')


def _canonicalize_unique_flag_assignments(flags):
    """Sort unique make assignments for comparison; keep duplicates ordered."""
    if not isinstance(flags, list):
        return flags
    assignments = []
    names = set()
    for flag in flags:
        if not isinstance(flag, str) or '=' not in flag:
            return flags
        name, value = flag.split('=', 1)
        if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name) is None or name in names:
            return flags
        names.add(name)
        assignments.append((name, value))
    return [name + '=' + value for name, value in sorted(assignments)]


def _canonicalize_native_sources(signature):
    """Fold the former byacc field into the common native-source mapping."""
    if 'byacc_source' not in signature:
        return signature
    legacy = signature.pop('byacc_source')
    native = signature.setdefault('native_sources', {})
    if (not isinstance(legacy, dict) or not isinstance(native, dict) or
            ('byacc' in native and native['byacc'] != legacy)):
        raise SDKError('conflicting or malformed native byacc source context')
    native['byacc'] = legacy
    return signature


def _match_clang_resource_binding(old, new):
    """Account for the newly explicit binding to an unchanged compiler resource."""
    try:
        prefix = 'EMBER_CLANG_RESOURCE_DIR='
        old_compiler, new_compiler = old['compiler'], new['compiler']
        if old_compiler['family'] != 'clang' or new_compiler['family'] != 'clang':
            return
        resource = old_compiler['resource_dir']
        if not isinstance(resource, str) or not resource or resource != new_compiler['resource_dir']:
            return
        old_flags, new_flags = old['flags'], new['flags']
        if not isinstance(old_flags, list) or not isinstance(new_flags, list):
            return
        old_values = [f for f in old_flags if isinstance(f, str) and f.startswith(prefix)]
        new_values = [f for f in new_flags if isinstance(f, str) and f.startswith(prefix)]
        if not old_values and new_values == [prefix + resource]:
            old_flags.append(prefix + resource)
    except (KeyError, TypeError):
        return


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
        expected = ([] if prefix else ['HOST_CC=' + native['tools']['cc'], 'HOST_CXX=' + native['tools']['cxx']])
        current_flags = expected + native_host_flags(native)
        if prefix and any(flag.split('=', 1)[0] in ('HOST_CC', 'HOST_CXX', 'HOST_SH') for flag in signature['flags']):
            raise SDKError('Producer overrides OS native routing')
        def flags_match(expected_flags, forbidden_names):
            for flag in expected_flags:
                binding = flag.split('=', 1)[0] + '='
                if [word for word in signature['flags'] if word.startswith(binding)] != [flag]:
                    return False
            return not any(word.split('=', 1)[0] in forbidden_names
                           for word in signature['flags'])
        forbidden_abi_aliases = {'HOST_CPPFLAGS', 'HOST_CFLAGS', 'HOST_CXXFLAGS', 'HOST_LDFLAGS'}
        if not flags_match(current_flags, forbidden_abi_aliases):
            raise SDKError('Producer flags differ from native selection')
        if signature['environment']['PATH'] != native_path(native):
            raise SDKError('Producer PATH differs from native selection')
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise SDKError('Missing native producer binding') from exc


# The manifest the Ports LLVM owner writes into a prepared source tree.
LLVM_SOURCE_MANIFEST = 'llvm-source.json'

def _compatible_native_contract(old_rows, new_rows, tools, previous_flags, selected_flags, added_roles):
    """Allow generator bindings only when both contexts selected the same tool."""
    if not old_rows or not new_rows or old_rows[0] != new_rows[0]:
        return False
    def rows(lines):
        result = {}
        for line in lines[1:]:
            fields = line.split(' ')
            if len(fields) != 3 or (fields[0], fields[1]) in result:
                return None
            result[(fields[0], fields[1])] = fields[2]
        return result
    old, new = rows(old_rows), rows(new_rows)
    if old is None or new is None or not old.keys() <= new.keys():
        return False
    bindings = {'perl': 'HOST_PERL', 'python': 'HOST_PYTHON',
                'gmake': 'HOST_GMAKE', 'autoconf': 'HOST_AUTOCONF'}
    def equivalent(role, binding):
        path = tools.get(role)
        if binding != bindings.get(role) or not isinstance(path, str) or not os.path.isabs(path):
            return False
        expected = binding + '=' + path
        return ([flag for flag in previous_flags if flag.startswith(binding + '=')] == [expected] and
                [flag for flag in selected_flags if flag.startswith(binding + '=')] == [expected])
    for key, binding in old.items():
        replacement = new[key]
        if binding != replacement and not (binding == '-' and equivalent(key[1], replacement)):
            return False
    added = new.keys() - old.keys()
    if not added_roles <= {role for _, role in added}:
        return False
    return all(new[key] == '-' or equivalent(key[1], new[key]) for key in added)


def _native_tool_context_flags(old_object_record, selected_object, tools_path, old_tools, selected_tools):
    """Use flags only from the object context that owns this exact tool tree."""
    try:
        if (old_object_record['format'] != 'emberbsd-sysroot-object-context' or
                old_object_record['version'] != 1):
            return None
        old_object = old_object_record['signature']
        if old_object['tools'] != tools_path or selected_object['tools'] != tools_path:
            return None
        roles = ('perl', 'python', 'gmake', 'autoconf')
        previous = old_object['native_toolset']['tools']
        selected = selected_object['native_toolset']['tools']
        if any(previous[role] != old_tools[role] or selected[role] != selected_tools[role]
               for role in roles):
            return None
        old_flags, new_flags = old_object['flags'], selected_object['flags']
        if not isinstance(old_flags, list) or not isinstance(new_flags, list):
            return None
        return old_flags, new_flags
    except (KeyError, TypeError, AttributeError):
        return None


class SysrootBuilder:
    TARGETS = TARGETS
    FLAGS = ('TOOLCHAIN_MISSING=yes MKGCCCMDS=no MKCOMPAT=no MKCXX=yes '
             'MKDEBUG=no MKDEBUGLIB=no MKX11=no MKLLVM=no MKLLVMRT=no MKLIBCXX=no MKKMOD=no MKGDB=no MKBINUTILS=no '
             'MKATF=no MKKYUA=no MKMAN=no MKDOC=no MKINFO=no MKHTML=no MKLINT=no '
             'MKPROFILE=no MKCVS=no MKGROFF=no MKRUMP=no MKREPRO=yes MAKECONF=/dev/null').split()
    VERIFIER = 'profiles/development-toolchain/runtime/recipe/files/runtime.py'
    CONTEXT = '.prebuild-sdk-context.json'
    QUARANTINE = '.prebuild-sdk-quarantine.json'
    RUNTIME_QUARANTINE = '.prebuild-sdk-runtime-refresh'
    RECEIPT = '.prebuild-sdk-build.json'
    # A development run restarts SDK-named LLVM stages that a failed run left.
    RESUMES_FAILED_STAGES = True
    MARKER = '.prebuild-sdk-build-incomplete'
    LOG = '.prebuild-sdk-build.log'

    def __init__(self, options, *, receipts=None):
        self.options = options.copy()
        # One run shares one set of in-memory verification receipts.
        self.verification = RunReceipts() if receipts is None else receipts
        if 'base-cxx' not in self.options:
            self.options['base-cxx'] = (self.options.get('compiler-family', 'clang') == 'clang'
                                        and not self.options.get('c-foundation'))
        self.commands = []
        self.git_command = None
        self.handles = []
        self.environment = process.sanitized_environment()
        self.environment['PYTHONDONTWRITEBYTECODE'] = '1'
        self.interrupted_signal = None
        self.locks = None
        self.receipt_file = None
        self.signal_handlers = {}

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
        if self.options.get('openssl-source'):
            roles |= {'perl', 'gmake'}
        if self.options.get('openssh-source'):
            roles |= set(OPENSSH_LOCAL_GENERATOR_ROLES) | {'perl', 'gmake'}
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
        """Clean-checkout record of a Git root, checked with Git once per run.

        Later checks in the same run, by any phase that shares this run's
        receipts, reuse the record without asking Git or walking the tree
        again. The record names the commit the run builds; the outputs are
        identified by their own hashes, so the checkouts are not checked
        again after the build.
        """
        environment = tuple(sorted(process.git_environment(self.environment).items()))
        key = ('git-source', os.path.realpath(root), kind, self.git_command, environment)
        return self.verification.once(
            key, lambda: provenance.git_source(root, kind=kind, git=self.git_command, env=self.environment))

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
            if base is None:
                base = self.paths.get('llvm-runtime-base')
            command = list(self.runtime_command)

            def check():
                reported = jsonio.loads_json(process.capture(command, env=self.environment).stdout)
                llvm_runtime.validate_identity(reported, target=self.options['target'], required_components=required)
                recipe_check = ({} if 'llvm-runtime-original-source' in self.paths else
                    dict(ports=self.paths['ports'], ports_revision=reported['recipe']['ports_revision']))
                record = llvm_runtime.validate_stage(stage, target=self.options['target'],
                    required_components=required, base=base, content=False, **recipe_check)
                if reported != record:
                    raise SDKError('LLVM runtime producer and shared validator disagree')
                return record
            def full():
                record = check()
                if (record['llvm_version'] != self.compiler_record['version'] or
                        record['source'] != {key: self.llvm_source_record['source'][key]
                                             for key in ('archive', 'sha512')}):
                    raise SDKError('LLVM runtime compiler or common source differs from selection')
                if (self.llvm_phase == 'base' or (self.llvm_phase == 'final' and base is not None)) and record['base']['os_revision'] != self.sources['os']['commit']:
                    if base is None:
                        raise SDKError('LLVM runtime base OS differs from selection')
                    # A measured foundation permits a descendant OS source while
                    # preserving the runtime's original recorded revision.
                    self.git(self.sources['os']['path'], 'merge-base', '--is-ancestor',
                             record['base']['os_revision'], self.sources['os']['commit'])
                # The stage manifest is the input key of the reused runtime; its
                # recorded digests stand for the bytes under the runtime prefix.
                prefix = record['prefix'] + '/'
                rows = inventory.snapshot_tree(self.paths['runtime-prefix'], machine=self.target['elf'],
                    digests={name[len(prefix):]: digest for name, digest in record['files'].items()
                             if name.startswith(prefix)})
                if self.llvm_phase == 'final':
                    llvm_runtime.validate_bootstrap_pair(
                        self.paths['llvm-bootstrap-stage'], record, target=self.options['target'], content=False)
                return dict(manifest=record, inventory=rows,
                            root_mode=stat.S_IMODE(os.stat(self.paths['runtime-prefix']).st_mode))
            # The input key of the runtime: its stage manifest, the verifier
            # files, the selected sources, compiler, phase and base path. The
            # stage, the base and the runtime prefix are not walked again in
            # the same run; a new run checks them once.
            inputs = [os.path.join(stage, llvm_runtime.MANIFEST), *self.receipt_tools(command)]
            inputs += [os.path.join(self.paths[name], LLVM_SOURCE_MANIFEST)
                       for name in ('llvm-source', 'llvm-runtime-original-source')
                       if name in self.paths and self.paths[name] in command]
            if self.llvm_phase == 'final':
                inputs.append(os.path.join(self.paths['llvm-bootstrap-stage'], llvm_runtime.MANIFEST))
            key = ('llvm-runtime', tuple(command), tuple(required), base, self.llvm_phase,
                   self.paths['runtime-prefix'], self.paths.get('llvm-bootstrap-stage'),
                   self.sources.get('os', {}).get('commit'), self.compiler_record['version'],
                   repr(sorted(self.llvm_source_record['source'].items())))
            result = self.verification.verify(key, inputs, full)
            self.commands.append(dict(argv=self.runtime_command, scope='runtime-check', status='passed'))
            return result
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
        if path == self.paths['objects'] and os.path.lexists(os.path.join(path, self.RUNTIME_QUARANTINE)):
            raise SDKError(f'incomplete runtime refresh is quarantined: {path}')
        if not os.listdir(path):
            return None
        record = jsonio.read_json(os.path.join(path, self.CONTEXT))
        if (not isinstance(record, dict) or
                set(record) not in ({'format', 'signature', 'sources', 'version'},
                                    {'format', 'signature', 'sources', 'version', 'pending_native'}) or
                record['format'] != 'emberbsd-sysroot-object-context' or type(record['version']) is not int or record['version'] != 1):
            raise SDKError(f'unknown or incompatible object context; leave it unchanged: {path}')
        if 'pending_native' in record:
            pending = record['pending_native']
            if (path != self.paths['tools'] or not isinstance(pending, dict) or
                    set(pending) != {'signature', 'sources'} or
                    not isinstance(pending['signature'], dict) or
                    not isinstance(pending['sources'], list) or
                    not self.plan_native_refresh(record, path)):
                raise SDKError(f'cannot safely resume incomplete native source refresh: {path}')
        elif not self.compatible_context(record, path):
            if not ((path == self.paths['objects'] and
                     (self.plan_prepared_refresh(record, path) or
                      self.plan_header_tools_refresh(record, path))) or
                    (path == self.paths['tools'] and self.plan_native_refresh(record, path))):
                raise SDKError(f'unknown or incompatible object context; leave it unchanged: {path}')
        self.validate_source_ancestry(record, path)
        return record

    def plan_header_tools_refresh(self, record, path):
        """Refresh path-sensitive host tool objects before reusing header objects."""
        if path != self.paths['objects'] or getattr(self, 'llvm_phase', None) != 'headers':
            return False
        try:
            original = record['signature']
            previous_contract = self.recorded_native_contract(record, original)
            if previous_contract is None or previous_contract != Path(self.native_contract).read_text(encoding='ascii'):
                return False
            old = copy.deepcopy(original)
            new = self.context_signature(path)
            old_tools, new_tools = old['tools'], new['tools']
            if (not all(isinstance(p, str) and os.path.isabs(p) and os.path.normpath(p) == p
                        for p in (old_tools, new_tools)) or old_tools == new_tools):
                return False
            old_env, new_env = old['environment'], new['environment']
            digest = hashlib.sha256(previous_contract.encode('ascii')).hexdigest()
            allowed = (os.path.join(old_tools, 'native-routing'),
                       os.path.join(old_tools, 'native-routing-' + digest))
            if (old_env['EMBER_NATIVE_TOOLS'] not in allowed or
                    new_env['EMBER_NATIVE_TOOLS'] != self.paths['native-routing']):
                return False
            old['tools'] = copy.deepcopy(new_tools)
            for key in ('EMBER_NATIVE_TOOLS', 'HOME', 'LOGNAME', 'TMPDIR', 'USER'):
                if key in new_env:
                    old_env[key] = copy.deepcopy(new_env[key])
                else:
                    old_env.pop(key, None)
            if not self.compatible_context(dict(record, signature=old), path):
                return False
            self.refresh_dirs = ['tools']
            self.refresh_nested_outputs = []
            return True
        except (KeyError, TypeError, AttributeError, OSError, UnicodeError):
            return False

    def llvm_changed_paths(self, before, selected):
        """Use owner proof when available; otherwise retain conservative refresh."""
        stage = self.paths.get('pkgsrc-export')
        if not stage:
            return None
        script = os.path.join(self.paths['ports'], 'pkgsrc/lang/compiler-rt/files/refresh-profile-source.py')
        if not os.path.isfile(script):
            return None
        command = [self.paths['python'], '-B', script, '--sdk-source', self.paths['sdk-source'],
                   '--ports', self.paths['ports'], '--pkgsrc-root', os.path.join(stage, 'pkgsrc'),
                   '--git', self.git_command, '--before', before, '--source', selected, '--list-changes']
        try:
            result = jsonio.loads_json(process.capture(command, env=self.environment).stdout)
        except (CommandError, SDKError, OSError, ValueError):
            return None
        if (not isinstance(result, dict) or set(result) != {'version', 'changed_paths'} or
                type(result['version']) is not int or result['version'] != 1 or
                not isinstance(result['changed_paths'], list)):
            return None
        return result['changed_paths']

    def plan_prepared_refresh(self, record, path):
        """Refresh declared source consumers and compatible C++ runtime users."""
        try:
            old = copy.deepcopy(record['signature'])
            new = copy.deepcopy(self.context_signature(path))
            _match_clang_resource_binding(old, new)
            before, after = old['common_sources'], new['common_sources']
            if not isinstance(before, dict) or not isinstance(after, dict) or set(before) - set(after):
                return False
            changed = {name for name in after if name not in before or
                       before[name].get('tree_sha256') != after[name].get('tree_sha256')}
            old_flags = _canonicalize_unique_flag_assignments(self.reuse_signature(copy.deepcopy(old))['flags'])
            new_flags = _canonicalize_unique_flag_assignments(self.reuse_signature(copy.deepcopy(new))['flags'])
            checked_runtime = {}
            def runtime_sources():
                selector = 'EMBER_LLVM_SOURCE='
                previous = [f[len(selector):] for f in old_flags if f.startswith(selector)]
                selected = [f[len(selector):] for f in new_flags if f.startswith(selector)]
                if (len(previous) != 1 or len(selected) != 1 or
                        selected[0] != self.paths.get('llvm-source') or
                        not os.path.isabs(previous[0])):
                    return None
                if not checked_runtime:
                    changed_files = runtime_refresh.changed_builtins(previous[0], selected[0])
                    checked_runtime.update(previous=previous[0], selected=selected[0], changed=changed_files)
                return checked_runtime
            def recipe_guard(before_recipe, after_recipe):
                checked = runtime_sources()
                if checked is None:
                    return False
                changed_paths = list(checked['changed'])
                darwin = 'compiler-rt/cmake/Modules/AddCompilerRT.cmake'
                if (Path(checked['previous'], darwin).read_bytes() !=
                        Path(checked['selected'], darwin).read_bytes()):
                    changed_paths.append(darwin)
                return runtime_refresh.compatible_recipe_transition(
                    before_recipe, after_recipe, changed_paths,
                    ports=self.paths['ports'], python=self.paths['python'], git=self.git_command,
                    target=self.options['target'], machine_arch=self.target['arch'],
                    environment=self.environment)
            runtime = None
            runtime_kind = None
            llvm_prefix = 'EMBER_LLVM_SOURCE='
            llvm_old = [f[len(llvm_prefix):] for f in old['flags'] if f.startswith(llvm_prefix)]
            llvm_new = [f[len(llvm_prefix):] for f in new['flags'] if f.startswith(llvm_prefix)]
            llvm_changed = old.get('llvm_source') != new.get('llvm_source') or llvm_old != llvm_new
            if llvm_changed and (len(llvm_old) != 1 or len(llvm_new) != 1):
                return False
            if llvm_changed or any(old.get(key) != new.get(key) for key in ('runtime', 'runtime_prefix')):
                runtime = runtime_refresh.bootstrap_update(old, new)
                if runtime is not None:
                    runtime_kind = 'bootstrap'
                else:
                    runtime = runtime_refresh.cpp_update(old, new)
                    if runtime is not None:
                        runtime_kind = 'cpp'
                    else:
                        runtime = runtime_refresh.builtins_update(old, new, recipe_guard=recipe_guard)
                        if runtime is not None:
                            runtime_kind = 'builtins'
                if runtime is not None:
                    for key in ('runtime', 'runtime_prefix', 'llvm_source'):
                        old[key] = copy.deepcopy(new[key])
            common_packages = dict(self.COMMON_SOURCES)

            def prepared_identity(record, package):
                if (not isinstance(record, dict) or record.get('format') != 'emberbsd-prepared-source' or
                        type(record.get('version')) is not int or record['version'] != 1 or
                        record.get('package') != package or not isinstance(record.get('tree_sha256'), str) or
                        re.fullmatch('[0-9a-f]{64}', record['tree_sha256']) is None):
                    return None
                return tuple(record[key] for key in ('format', 'version', 'package', 'tree_sha256'))

            if not changed and runtime is None:
                return False
            moved = set()
            if changed or runtime is not None:
                for name in before.keys() & after.keys() & common_packages.keys():
                    package = common_packages[name]
                    identity = prepared_identity(before[name], package)
                    if identity is None or identity != prepared_identity(after[name], package):
                        continue
                    prefix = 'EMBER_' + name.upper().replace('-', '_') + '_SOURCE='
                    old_binding = [flag for flag in old_flags if flag.startswith(prefix)]
                    new_binding = [flag for flag in new_flags if flag.startswith(prefix)]
                    selected_path = self.paths.get(name + '-source')
                    if (len(old_binding) == len(new_binding) == 1 and old_binding[0] != new_binding[0] and
                            selected_path is not None and new_binding[0] == prefix + selected_path and
                            os.path.isabs(old_binding[0][len(prefix):])):
                        moved.add(name)

            prefixes = tuple('EMBER_' + name.upper().replace('-', '_') + '_SOURCE='
                             for name in changed | moved if name in common_packages)
            if runtime is not None:
                prefixes += ('EMBER_EXTERNAL_LLVM_RUNTIME=', 'EMBER_LLVM_RESOURCE_DIR=', 'EMBER_LLVM_SOURCE=')
            if runtime_kind == 'bootstrap':
                # bootstrap_update checked both bindings against the manifests.
                prefixes += ('EMBER_LIBUNWIND_ARCHIVE=',)
            if runtime_kind == 'builtins':
                binding = 'EMBER_LIBUNWIND_ARCHIVE='
                previous = [f[len(binding):] for f in old_flags if f.startswith(binding)]
                selected = [f[len(binding):] for f in new_flags if f.startswith(binding)]
                manifest = new['runtime']['manifest']
                unwind = manifest['artifacts']['libunwind']['static']
                digest = manifest['bootstrap']['files'][unwind]
                if (len(previous) != 1 or len(selected) != 1 or
                        not runtime_refresh.equivalent_unwind_binding(
                            previous[0], selected[0], self.paths['llvm-bootstrap-stage'], unwind, digest)):
                    return False
                prefixes += (binding,)
            comparable_new_flags = new_flags
            openssh_autoconf_added = False
            if 'openssh' in changed and not any(flag.startswith('HOST_AUTOCONF=') for flag in old_flags):
                autoconf = 'HOST_AUTOCONF=' + self.native.tool('autoconf')
                selected_autoconf = [flag for flag in new_flags if flag.startswith('HOST_AUTOCONF=')]
                if selected_autoconf == [autoconf]:
                    comparable_new_flags = [flag for flag in new_flags if flag != autoconf]
                    openssh_autoconf_added = True
            if ([f for f in old_flags if not f.startswith(prefixes)] !=
                    [f for f in comparable_new_flags if not f.startswith(prefixes)]):
                return False
            for name in changed:
                before[name] = copy.deepcopy(after[name])
            old['flags'] = copy.deepcopy(new['flags'])
            candidate = dict(record, signature=old)
            candidate['_compat_previous_flags'] = copy.deepcopy(record['signature']['flags'])
            if openssh_autoconf_added:
                previous_tools = candidate['signature']['native_toolset']['tools']
                selected_tools = new['native_toolset']['tools']
                for role in OPENSSH_LOCAL_GENERATOR_ROLES:
                    if role not in previous_tools:
                        if role not in selected_tools:
                            return False
                        previous_tools[role] = copy.deepcopy(selected_tools[role])
            if not self.compatible_context(candidate, path):
                return False
            def members(archive):
                result = process.capture([self.compiler_record['binaries']['ar']['selected_path'],
                                          't', str(archive)], env=self.environment)
                return result.stdout.decode('utf-8', 'strict').splitlines()
            self.refresh_nested_outputs = []
            if llvm_changed and runtime_kind == 'cpp':
                self.llvm_refresh_roots = (llvm_old[0], llvm_new[0])
                self.llvm_changed_source_paths = SysrootBuilder.llvm_changed_paths(self, *self.llvm_refresh_roots)
                self.llvm_refresh_plan = llvm_source_refresh.plan(
                    path, *self.llvm_refresh_roots, self.llvm_changed_source_paths)
            else:
                self.llvm_refresh_plan = None
            if runtime_kind == 'builtins':
                checked = runtime_sources()
                if checked is None:
                    return False
                patterns = prepared_refresh.runtime_independent_patterns(self.paths['os'])
                self.runtime_refresh_files = prepared_refresh.runtime_objects(
                    path, checked['previous'], checked['changed'], patterns)
                self.runtime_refresh_source = checked['selected']
                self.runtime_refresh_changed = checked['changed']
            extra = (dict(extra_headers=runtime['headers'], extra_linked=runtime['linked'])
                     if runtime_kind in ('cpp', 'bootstrap') else {})
            if getattr(self, 'llvm_phase', None) == 'headers' and self.compiler_family() == 'clang':
                # Native producers have their own tools-context refresh.
                extra['scan_consumers'] = False
            source_roots = set()
            for name in before.keys() | after.keys():
                if name not in common_packages:
                    continue
                prefix = 'EMBER_' + name.upper().replace('-', '_') + '_SOURCE='
                for flags in (old_flags, new_flags):
                    bindings = [flag[len(prefix):] for flag in flags if flag.startswith(prefix)]
                    if len(bindings) == 1 and os.path.isabs(bindings[0]):
                        source_roots.add(bindings[0])
            self.refresh_file_outputs = []
            self.refresh_dirs = prepared_refresh.plan(self.paths['os'], path, changed, members,
                                                      self.refresh_nested_outputs,
                                                      allowed_source_roots=sorted(source_roots),
                                                      file_outputs=self.refresh_file_outputs, **extra)
            return bool(self.refresh_dirs) or runtime is not None or bool(changed)
        except (KeyError, TypeError, AttributeError):
            return False

    def plan_native_refresh(self, record, path):
        """Rebuild OS-declared tools whenever their prepared source changes."""
        try:
            old = copy.deepcopy(record['signature'])
            new = copy.deepcopy(self.context_signature(path))
            pending_record = record.get('pending_native')
            pending = copy.deepcopy(pending_record['signature'] if pending_record else old)
            pending_names = set(pending_record['sources']) if pending_record else set()
            for signature in (old, new, pending):
                _canonicalize_native_sources(signature)
            before = old.pop('native_sources', {})
            after = new.pop('native_sources', {})
            interrupted = pending.pop('native_sources', {})
            if not all(isinstance(value, dict) for value in (before, after, interrupted)):
                return False
            if not self.compatible_context(dict(record, signature=old), path):
                candidate = dict(record, signature=copy.deepcopy(old))
                if after:
                    candidate['signature']['native_sources'] = copy.deepcopy(after)
                if not self.compatible_context(candidate, path):
                    return False
            pending['native_sources'] = copy.deepcopy(after)
            if not after:
                pending.pop('native_sources')
            if not self.compatible_context(dict(record, signature=pending), path):
                return False
            names = set(before) | set(after) | set(interrupted)
            changed = pending_names | {name for name in names if before.get(name) != after.get(name) or
                                       before.get(name) != interrupted.get(name)}
            if not changed:
                return False
            self.native_refresh_dirs = prepared_refresh.plan_native(self.paths['os'], self.paths['objects'], changed)
            self.native_refresh_sources = sorted(changed)
            return bool(self.native_refresh_dirs)
        except (KeyError, TypeError, AttributeError):
            return False

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

    def reuse_signature(self, signature):
        """Compare compiled payload, keeping provenance in the saved context."""
        _canonicalize_native_sources(signature)
        def payload(name, record):
            if (name != 'x11' and isinstance(record, dict) and
                    record.get('format') == 'emberbsd-prepared-source' and
                    record.get('version') == 1 and isinstance(record.get('package'), str) and
                    isinstance(record.get('tree_sha256'), str) and
                    re.fullmatch('[0-9a-f]{64}', record['tree_sha256'])):
                return {key: record[key] for key in ('format', 'version', 'package', 'tree_sha256')}
            return record
        records = signature.get('common_sources')
        if isinstance(records, dict):
            for name, record in records.items():
                records[name] = payload(name, record)
        native = signature.get('native_sources')
        if isinstance(native, dict):
            for name, value in native.items():
                if isinstance(value, dict) and 'record' in value:
                    value['record'] = payload(name, value['record'])
        return signature

    def compatible_context(self, record, path):
        """Compare compiled inputs, allowing controller updates and new utilities."""
        old = self.reuse_signature(copy.deepcopy(record['signature']))
        new = self.reuse_signature(copy.deepcopy(self.context_signature(path)))
        _match_clang_resource_binding(old, new)
        old['flags'] = _canonicalize_unique_flag_assignments(old.get('flags'))
        new['flags'] = _canonicalize_unique_flag_assignments(new.get('flags'))
        # Routing evidence must retain bindings that phase-specific comparison
        # may later remove as irrelevant to header object contents.
        selected_binding_flags = copy.deepcopy(new.get('flags'))
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
                    if 'openssl' in common_sources:
                        signature['header_openssl_source'] = common_sources['openssl']
                    elif 'openssh' in common_sources:
                        signature['flags'] = [flag for flag in signature['flags']
                                              if not flag.startswith('HOST_PERL=')]
                    if 'libfido2' in common_sources:
                        signature['header_libfido2_source'] = common_sources['libfido2']
                    if 'libcbor' in common_sources:
                        signature['header_libcbor_source'] = common_sources['libcbor']
                    if 'libfetch' in common_sources:
                        signature['header_libfetch_source'] = common_sources['libfetch']
                    if 'openldap' in common_sources:
                        signature['header_openldap_source'] = common_sources['openldap']
                    if 'unbound' in common_sources:
                        signature['header_unbound_source'] = common_sources['unbound']
                    if 'mdns' in common_sources:
                        signature['header_mdns_source'] = common_sources['mdns']
                    if 'heimdal' in common_sources:
                        signature['header_heimdal_source'] = common_sources['heimdal']
                    if 'zlib' in common_sources:
                        signature['header_zlib_source'] = common_sources['zlib']
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
                                                                  'EMBER_WPA_SOURCE=',
                                                                  'EMBER_MDNS_SOURCE=',
                                                                  'EMBER_DHCPCD_SOURCE=',
                                                                  'EMBER_OPENSSH_SOURCE=',
                                                                  'EMBER_BYACC_SOURCE=',
                                                                  'EMBER_TEXINFO_SOURCE=',
                                                                  'EMBER_RCS_SOURCE=',
                                                                  'EMBER_DIFFUTILS_SOURCE=',
                                                                  'EMBER_GREP_SOURCE=',
                                                                  'HOST_PYTHON=', 'HOST_AUTOCONF=',
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
            # Optional target-only GNU/Autoconf tools cannot affect host tools or headers.
            irrelevant = ({'gmake', 'autoconf', 'autom4te', 'gm4', 'pkg-config', 'od', 'msgfmt', 'msgmerge', 'xgettext', 'msginit', 'msgconv', 'msgfilter'} if path == self.paths['tools'] or
                          (self.compiler_family() == 'clang' and self.llvm_phase == 'headers')
                          else set())
            added_contract = added - irrelevant
            old_env, new_env = old['environment'], new['environment']
            routing_changed = old_env['EMBER_NATIVE_TOOLS'] != new_env['EMBER_NATIVE_TOOLS']
            if added_contract or routing_changed:
                # Compare the actual OS-owned rows from both source revisions.
                # Existing commands/bindings cannot change during object reuse.
                data = self.recorded_native_contract(record, old)
                if data is None:
                    return False
                old_rows = data.splitlines()
                selected_contract = Path(self.native_contract).read_text(encoding='ascii')
                new_rows = selected_contract.splitlines()
                previous_flags = record.get('_compat_previous_flags', record['signature'].get('flags'))
                selected_flags = selected_binding_flags
                if (previous_flags is None or selected_flags is None) and path == self.paths['tools']:
                    object_context = os.path.join(self.paths['objects'], self.CONTEXT)
                    if paths.entry(object_context) is None:
                        return False
                    evidence = _native_tool_context_flags(
                        jsonio.read_json(object_context), self.context_signature(self.paths['objects']),
                        path, previous['tools'], selected['tools'])
                    if evidence is None:
                        return False
                    previous_flags, selected_flags = evidence
                if previous_flags is None or selected_flags is None:
                    return False
                if not _compatible_native_contract(old_rows, new_rows, selected['tools'],
                                                   previous_flags, selected_flags, added_contract):
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

    def recorded_native_contract(self, record, signature):
        """Return the native-tool contract the reused objects were built with.

        The routing tree of the old objects keeps the contract named by its
        digest. Only a former fixed routing name needs the OS commit, read
        from local objects: a partial OS checkout must not fetch it.
        """
        routing = signature['environment']['EMBER_NATIVE_TOOLS']
        prefix = os.path.join(signature['tools'], 'native-routing-')
        digest = routing[len(prefix):] if routing.startswith(prefix) else ''
        recorded = os.path.join(routing, 'contract')
        if re.fullmatch(r'[0-9a-f]{64}', digest) and paths.entry(recorded) is not None:
            try:
                data, _, _ = native_os_routing.read_native_os_contract(recorded, sha256=digest)
            except SDKError:
                return None
            return data.decode('ascii')
        source = record['sources']['os']
        contract = 'tools/native-tools/contract'
        try:
            # Objects from an OS revision without the contract have no
            # recorded rows to compare, so they cannot be reused.
            if self.git(self.sources['os']['path'], 'ls-tree', '--name-only',
                        source['commit'], '--', contract) != contract:
                return None
            data = self.git(self.sources['os']['path'], 'show', source['commit'] + ':' + contract) + '\n'
        except SDKError as exc:
            raise SDKError('the OS checkout lacks the native-tool contract of the reused objects at '
                           f'{source["commit"]}; fetch that commit into the checkout or select new '
                           'objects') from exc
        try:
            native_os_routing.parse_native_os_contract(data.encode('ascii'))
        except SDKError:
            return None
        return data

    def context_signature(self, path):
        if self.compiler_family() != 'clang' or path != self.paths['tools']:
            return self.signature
        # OS native tools do not link the target runtime. Reuse their one tree
        # while target object directories distinguish headers, C base and C++.
        signature = dict(scope='llvm-native-tools', host_binutils=self.paths['host-binutils'],
                    **{name: self.signature[name] for name in
            ('host', 'target', 'compiler', 'host_tools', 'native_toolset', 'native_inputs',
             'environment', 'tools', 'python', 'process_backend')})
        native_sources = set(prepared_refresh.native_sources(self.paths['os']))
        if 'byacc-source' in self.paths:
            # Retain this input even for OS contracts predating native producers.
            native_sources.add('byacc')
        selected = {name: {'path': self.paths[name + '-source'],
                           'record': self.signature['common_sources'][name]}
                    for name in native_sources if name + '-source' in self.paths}
        if selected:
            signature['native_sources'] = selected
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
        # The owner identifies a prepared LLVM tree by its manifest, so the
        # manifest, not a walk of the whole tree, keys the verification.
        record = self.verification.verify(('llvm-source', tuple(command)),
            [os.path.join(self.paths['llvm-source'], LLVM_SOURCE_MANIFEST), *self.receipt_tools(command)],
            lambda: jsonio.loads_json(process.capture(command, env=self.environment).stdout))
        if (not isinstance(record, dict) or record.get('format') != 'emberbsd-llvm-source' or
                (self.compiler_family() == 'clang' and record.get('llvm_version') != self.compiler_record['version'])):
            raise SDKError('common LLVM source and selected compiler differ')
        return record

    # Prepared sources and their Ports packages.  Each owner writes the
    # receipt of a prepared tree once, when it prepares it from the distfile
    # Ports fetch checked against distinfo; the receipt names the package
    # inputs and the digest of the tree it published.
    COMMON_SOURCES = (
        ('libarchive', 'archivers/libarchive'), ('libevent', 'devel/libevent'),
        ('openssl', 'security/openssl'), ('libfido2', 'security/libfido2'), ('libcbor', 'devel/libcbor'), ('libfetch', 'net/libfetch'), ('openldap', 'databases/openldap'), ('unbound', 'net/unbound'), ('wpa', 'net/wpa_supplicant'), ('mdns', 'net/mDNSResponder'), ('dhcpcd', 'net/dhcpcd'), ('heimdal', 'security/heimdal'), ('openssh', 'security/openssh'), ('byacc', 'devel/byacc'), ('texinfo', 'devel/gtexinfo'), ('rcs', 'devel/rcs'),
        ('diffutils', 'devel/diffutils'), ('grep', 'textproc/grep'), ('ekermit', 'comms/ekermit'),
        ('nvi', 'editors/nvi'), ('top', 'sysutils/top'), ('iscsi', 'devel/netbsd-iscsi-lib'),
        ('ntp', 'net/ntp4'), ('libopts', 'devel/autogen'), ('libpcap', 'net/libpcap'),
        ('tcpdump', 'net/tcpdump'), ('tcp-wrappers', 'security/tcp_wrappers'),
        ('gettext', 'devel/gettext'), ('libxml2', 'textproc/libxml2'), ('zlib', 'devel/zlib'), ('ppp', 'net/pppd'),
        ('tdb', 'databases/tdb'), ('x11', 'netbsd/xsrc'))

    def verify_common_sources(self):
        """The prepare receipts of the selected prepared sources.

        A tree is identified by the receipt its owner wrote when it prepared
        it; the SDK neither runs the owner again nor reads or hashes the tree.
        The receipts are read once, at preflight, into the build signature."""
        result = {}
        for name, package in self.COMMON_SOURCES:
            if name + '-source' in self.paths:
                try:
                    record = sourceprep.read_prepared_tree_receipt(self.paths[name + '-source'], package=package)
                except SDKError as exc:
                    raise SDKError(f'invalid {name} prepared source receipt: {exc}') from exc
                result[name] = self.prepared_source_record(name, package, record)
        return result

    # The Ports check that each prepared source names the effective recipe of
    # the selected Ports checkout.  It reads Git object ids and the small
    # recipe files only, never a prepared tree or a pkgsrc export.
    CURRENT_SOURCES = 'scripts/prepared-source-current.py'

    def require_current_common_sources(self):
        """Refuse a prepared source made from another Ports recipe.

        A receipt names the recipe its tree was prepared from; building it
        with a Ports checkout whose recipe differs (a stale Ports pin, or a
        source prepared before a recipe change) would publish a sysroot that
        the selected Ports does not describe.  Ports compares the receipts
        with its current recipes once per run, from Git, in seconds."""
        names = [name for name, _ in self.COMMON_SOURCES if name + '-source' in self.paths]
        if not names:
            return
        script = os.path.join(self.paths['ports'], self.CURRENT_SOURCES)
        if not os.path.isfile(script):
            raise SDKError('selected Ports cannot check prepared sources against its recipes')
        command = [self.paths['python'], '-B', script, '--sdk-source', self.paths['sdk-source'],
                   '--git', self.git_command]
        if 'libopts-source' in self.paths:
            # Ports applies the infrastructure patches of the AutoGen recipe
            # to its few recipe files with the selected native patch tool.
            selector = native_toolset.load_host_prefix(self.native.bundle_root, required_roles={'gpatch'})
            command += ['--patch', self.executable(selector.tool('gpatch'))]
        for name in names:
            command += ['--source', f"{name}={self.paths[name + '-source']}"]
        try:
            result = jsonio.loads_json(process.capture(command, env=self.environment).stdout)
        except CommandError as exc:
            detail = exc.stderr.decode('utf-8', 'replace').strip()
            raise SDKError('prepared sources are stale for the selected Ports; prepare them again: ' + detail) from exc
        if result != {'current': sorted(names)}:
            raise SDKError('invalid prepared source check result')

    @staticmethod
    def prepared_source_record(name, package, record):
        if (not isinstance(record, dict) or record.get('format') != 'emberbsd-prepared-source' or
                type(record.get('version')) is not int or record['version'] != 1 or
                record.get('package') != package):
            raise SDKError(f'invalid {name} prepared source receipt')
        return record

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
        selected_dir = Path(paths.canonical_existing(os.path.join(source, 'src', 'emberbsd_sdk')))
        if loaded == selected_dir:
            # The loaded package is the selected clean SDK checkout, identified
            # by its Git commit; its modules are not hashed again.
            return dict(path=python, sha256=inventory.sha256_file(self.executable(python)), version=result['version'],
                        api_version=API_VERSION, modules=dict(source_commit=self.sources['sdk']['commit']))
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

    @classmethod
    def prepared_source_profile(cls, options):
        """Return the make flags and traversals of this builder's OS build.

        build.sh "tools libs": includes from the top, then lib build_install."""
        return list(cls.FLAGS), prepared_sources.LIBS_TRAVERSALS

    def prepared_source_traversals(self):
        return prepared_sources.TOOLS_TRAVERSALS + self.prepared_source_profile(self.options)[1]

    def require_prepared_sources(self, flags, traversals):
        """Refuse a build whose OS tree reaches a prepared source that is not selected.

        Need is taken from the directories the build enters under FLAGS and
        the makefiles they read, not from the tree's list of consumers."""
        if not traversals:
            return {}
        root = self.paths['os']
        env = dict(self.environment)
        env.pop('EMBER_NATIVE_TOOLS', None)
        common = ['MAKECONF=/dev/null', '_SRC_TOP_=', 'NETBSDSRCDIR=' + root,
                  'NETBSDOBJDIR=' + self.paths['objects'],
                  'MACHINE=' + self.target['machine'], 'MACHINE_ARCH=' + self.target['arch'],
                  'USETOOLS=yes', 'TOOLDIR=' + self.paths['tools'],
                  'MKREPRO_TIMESTAMP=' + str(self.source_timestamp())]
        _, rows, _ = read_native_os_contract(self.native_contract, sha256=self.native_contract_sha256)
        common += ['TOOL_SED=' + self.native.tool(role) for _, role, binding in rows if binding == 'SED']
        if any(start == 'tools' for start, _ in traversals):
            common += [binding + '=' + self.native.tool(role) for _, role, binding in rows
                       if binding in ('HOST_YACC', 'HOST_MV_CP', 'HOST_MV_RM')]
        query = prepared_sources.bmake_query(self.native.tool('make'), root, common + list(flags), env=env)
        needed = prepared_sources.required_sources(root, query, traversals,
                                                   jobs=max(1, min(8, int(self.options['jobs']))))
        missing = prepared_sources.missing_sources(needed, self.paths)
        if missing:
            raise SDKError('selected OS build needs prepared sources that are not selected: ' + ', '.join(missing))
        return needed

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
        jobs = str(resolve_jobs(self.options.get('jobs')))
        self.options['jobs'] = jobs
        # Check the workspace target before querying any host tools.
        layout = workspace.inspect_layout(self.options['sdk'])
        workspace.require_target(layout, self.options['target'])
        self.llvm_phase = getattr(self, 'llvm_phase', 'final')
        if not self.options.get('llvm-source'):
            raise SDKError('--llvm-source is required: prepare the common Ports LLVM source')
        if bool(self.options.get('gettext-source')) != bool(self.options.get('libxml2-source')):
            raise SDKError('gettext and libxml2 sources must be selected together')
        for name in ('libarchive-source', 'libevent-source'):
            if self.llvm_phase != 'headers' and not self.options.get('c-foundation') and not self.options.get(name):
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
        input_names += [name for name in ('libarchive-source', 'libevent-source', 'openssl-source', 'libfido2-source', 'libcbor-source', 'libfetch-source', 'openldap-source', 'unbound-source', 'wpa-source', 'mdns-source', 'dhcpcd-source', 'heimdal-source', 'openssh-source', 'byacc-source', 'texinfo-source', 'rcs-source', 'diffutils-source', 'grep-source', 'ekermit-source', 'nvi-source', 'tdb-source', 'ppp-source', 'top-source', 'iscsi-source', 'libpcap-source', 'tcpdump-source', 'tcp-wrappers-source', 'ntp-source', 'libopts-source', 'gettext-source', 'libxml2-source', 'zlib-source', 'x11-source') if self.options.get(name)]
        if clang and self.llvm_phase != 'headers' and self.options.get('llvm-runtime-original-source'):
            input_names.append('llvm-runtime-original-source')
        input_names += (([] if self.llvm_phase == 'headers' else ['llvm-runtime-stage'])
                        if clang else ['runtime-work', 'gcc-archive', 'pkgsrc'])
        if clang and self.llvm_phase == 'final':
            input_names.append('llvm-bootstrap-stage')
        if clang and self.llvm_phase != 'headers' and self.options.get('llvm-runtime-base'):
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
        self.libedit_record = libedit.validate_source(self.paths['libedit-source'], ports_source=self.sources['ports'], content=False)
        self.common_source_records = (self.verify_common_sources()
                                      if any(name + '-source' in self.paths for name, _ in self.COMMON_SOURCES)
                                      else {})
        self.require_current_common_sources()
        self.python = self.python_identity()
        self.git(self.paths['os'], 'ls-files', '--error-unmatch', 'build.sh', 'Makefile', 'libexec/ld.elf_so/Makefile', 'share/mk/bsd.own.mk')
        self.git(self.paths['ports'], 'ls-files', '--error-unmatch', verifier)
        self.git(self.paths['ports'], 'ls-files', '--error-unmatch', PRODUCER)
        self.compiler_record = self.compiler()
        if clang:
            self.paths['clang-resource'] = self.compiler_record['resource_dir']
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
                record = llvm_runtime.validate_identity(
                    jsonio.read_json(os.path.join(stage, llvm_runtime.MANIFEST)),
                    target=self.options['target'],
                    required_components=('builtins', 'libunwind-static'))
                self.paths.update(runtime_paths(stage, record))
                if self.llvm_phase == 'final':
                    bootstrap_stage = self.paths['llvm-bootstrap-stage']
                    boot = llvm_runtime.validate_identity(
                        jsonio.read_json(os.path.join(bootstrap_stage, llvm_runtime.MANIFEST)),
                        target=self.options['target'],
                        required_components=('builtins', 'libunwind-static'))
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
                       for name in ('libarchive', 'libevent', 'openssl', 'libfido2', 'libcbor', 'libfetch', 'openldap', 'unbound', 'wpa', 'mdns', 'dhcpcd', 'heimdal', 'openssh', 'byacc', 'texinfo', 'rcs', 'diffutils', 'grep', 'ekermit', 'nvi', 'tdb', 'ppp', 'top', 'iscsi', 'libpcap', 'tcpdump', 'tcp-wrappers', 'ntp', 'libopts', 'gettext', 'libxml2', 'zlib') if name + '-source' in self.paths]
        if 'openssl-source' in self.paths or 'openssh-source' in self.paths:
            self.flags.append('HOST_PERL=' + self.native.tool('perl'))
        if 'openssh-source' in self.paths:
            self.flags.append('HOST_AUTOCONF=' + self.native.tool('autoconf'))
        if any(name in self.paths for name in ('openssl-source', 'tdb-source', 'openssh-source')):
            self.flags.append('HOST_PYTHON=' + self.native.tool('python'))
        if 'openssl-source' in self.paths and not any(name + '-source' in self.paths for name in ('texinfo', 'rcs', 'diffutils', 'grep', 'gettext', 'openssh')):
            self.flags.append('HOST_GMAKE=' + self.native.tool('gmake'))
        if any(name + '-source' in self.paths for name in ('texinfo', 'rcs', 'diffutils', 'grep', 'gettext', 'openssh')):
            self.flags += gnu_target_host_flags(self.native_selection)
        if 'gettext-source' in self.paths:
            self.flags += gettext_target_host_flags(self.native_selection)
        if self.options.get('c-foundation'):
            self.flags += ['MKPIC=no', 'MKPICINSTALL=no', 'MKPIE=no', 'LDSTATIC=-static']
            if self.options.get('target') == 'riscv32':
                self.flags += ['MKSOFTFLOAT=yes']
        self.signature = dict(host=self.host, target=self.target, compiler=self.compiler_record, libedit=self.libedit_record, runtime=self.runtime_record, runtime_prefix=prefix, flags=self.flags, host_tools=self.host_tools, native_toolset=self.native_selection, environment=self.environment, objects=self.paths['objects'], tools=self.paths['tools'], python=self.python, process_backend=self.process_inspector.describe())
        self.signature['native_inputs'] = self.native_inputs
        self.signature['compiler_family'] = self.compiler_family()
        self.signature['llvm_source'] = self.llvm_source_record
        self.signature['common_sources'] = self.common_source_records
        if clang:
            self.signature['llvm_phase'] = self.llvm_phase
        self.configure_signature()
        # After the final flags are known and before any output is claimed.
        # The graph walk is a plan-time diagnostic. A normal build keeps the
        # selected inputs validated here and lets the actual OS make guards
        # reject any reached, unselected prepared source.
        self.prepared_source_needs = (self.require_prepared_sources(
            self.flags, self.prepared_source_traversals()) if self.options.get('plan') else {})
        self.contexts = {name: self.inspect_context(self.paths[name]) for name in ('objects', 'tools')}
        self.build_command = [self.shell, os.path.join(self.paths['os'], 'build.sh'), '-U', '-u', '-j' + jobs, '-m', self.target['machine'], '-a', self.target['arch'], '-O', self.paths['objects'], '-T', self.paths['tools'], '-D', self.paths['destination']]
        # Commit metadata stays out of the signature: the same OS tree at a new
        # commit reuses compatible objects. build.sh derives a missing
        # MKREPRO_TIMESTAMP from VCS tools that the sanitized PATH omits.
        for flag in self.flags + ['MKREPRO_TIMESTAMP=' + str(self.source_timestamp()),
                                  'BUILDINFO=EmberBSD:' + self.sources['os']['commit']]:
            self.build_command.extend(('-V', flag))
        self.os_build_operations = (['libs'] if getattr(self, 'coordinator_tools_ready', False)
                                    else ['tools', 'libs'])
        self.builds_native_tools = 'tools' in self.os_build_operations
        self.build_command.extend(self.os_build_operations)
        self.loader_command = [os.path.join(self.paths['tools'], 'bin', 'nbmake-' + self.target['machine']), '-C', os.path.join(self.paths['os'], 'libexec/ld.elf_so'), '-j' + jobs, 'dependall']
        self.install_command = self.loader_command[:-1] + ['install']
        self.receipt = dict(format='emberbsd-sysroot-build', version=3 if clang else 2, scope='sysroot-build', qualification='not-built', execution_acceptance='not-run', workspace=layout, paths=self.paths, sources=self.sources, previous_contexts=self.contexts, signature=self.signature, commands=self.commands, plan=[self.runtime_command, self.build_command, self.loader_command, self.install_command, self.runtime_command, self.runtime_command], working_directory=self.paths['os'], copy=dict(source=prefix, destination=os.path.join(self.paths['destination'], 'usr/pkg/llvm' if clang else 'usr/pkg/gcc16'), verification='exact pre/copy/post inventory'), not_checked=['target execution', 'ABI compatibility', 'SDK release'])
        if getattr(self, 'refresh_dirs', None):
            self.receipt['prepared_source_refresh'] = list(self.refresh_dirs)
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
        # Hold the existing object-tree lease throughout cleanup and context update.
        llvm_plan = getattr(self, 'llvm_refresh_plan', None)
        if llvm_plan is not None:
            llvm_source_refresh.apply(self.paths['objects'], llvm_plan, *self.llvm_refresh_roots,
                                      getattr(self, 'llvm_changed_source_paths', None))
        if getattr(self, 'refresh_file_outputs', None):
            prepared_refresh.remove_lint_outputs(self.paths['objects'], self.refresh_file_outputs)
        if hasattr(self, 'runtime_refresh_files'):
            self.begin_runtime_refresh()
        native_dirs = getattr(self, 'native_refresh_dirs', [])
        if getattr(self, 'refresh_dirs', []) or native_dirs:
            # TOOLDIR has one nbmake wrapper, and each build.sh invocation
            # replaces it with the current phase's -O/-T/-D and flags. Refresh
            # queries run before the next tools/libs build, so select this
            # phase's wrapper explicitly without rebuilding tools or libraries.
            self.dispatch(self.native_makewrapper_command())
        pending_written = False
        for directory in getattr(self, 'refresh_dirs', []):
            wrapper = os.path.join(self.paths['tools'], 'bin', 'nbmake-' + self.target['machine'])
            base = [wrapper, '-C', os.path.join(self.paths['os'], directory),
                    'DESTDIR=' + self.paths['destination'],
                    'EMBER_NATIVE_TOOLS=' + self.paths['native-routing'], *self.flags]
            if 'openssl-source' in self.paths:
                generated = os.path.join(self.paths['objects'], 'crypto/external/apache2/openssl/generated')
                base.append('EMBER_OPENSSL_GENERATED=' + generated)
            selected = process.capture(base + ['-V', '${.OBJDIR}'], env=self.environment).stdout.decode('utf-8').strip()
            expected = os.path.join(self.paths['objects'], directory)
            if selected != expected or os.path.realpath(selected) != expected:
                raise SDKError('prepared source refresh selected the wrong object directory: ' + directory)
            # BSD clean normally visits both the object and source directories.
            # A refresh owns only the selected objects, never tracked sources.
            self.dispatch(base + ['MKCLEANSRC=no', 'cleandir'])
        for nested in getattr(self, 'refresh_nested_outputs', []):
            if os.path.lexists(nested):
                raise SDKError('nested object output remains after owner cleanup: ' + nested)
        # Mark the current context before the first install can change a tool.
        # Only the same selected inputs may retry a partially installed set.
        if native_dirs and not pending_written:
            old = copy.deepcopy(self.contexts['tools'])
            old['pending_native'] = {'signature': self.context_signature(self.paths['tools']),
                                     'sources': self.native_refresh_sources}
            jsonio.write_json(os.path.join(self.paths['tools'], self.CONTEXT), old, exclusive=False)
            pending_written = True
            if 'tools' in self.os_build_operations:
                self.pending_native_refresh = old
        # A selected tools traversal creates object directories and builds the
        # shared native prerequisites before its producers. Do not run a leaf
        # producer early when resuming an incomplete initial tools build.
        install_native_now = bool(native_dirs) and 'tools' not in self.os_build_operations
        for directory in native_dirs:
            expected = os.path.join(self.paths['objects'], directory)
            if not install_native_now and paths.entry(expected) is None:
                continue
            wrapper = os.path.join(self.paths['tools'], 'bin', 'nbmake-' + self.target['machine'])
            base = [wrapper, '-C', os.path.join(self.paths['os'], directory),
                    'DESTDIR=' + self.paths['destination'],
                    'EMBER_NATIVE_TOOLS=' + self.paths['native-routing'], *self.flags]
            selected = process.capture(base + ['-V', '${.OBJDIR}'], env=self.environment).stdout.decode('utf-8').strip()
            if selected != expected or os.path.realpath(selected) != expected:
                raise SDKError('native source refresh selected the wrong object directory: ' + directory)
            self.dispatch(base + ['MKCLEANSRC=no', 'cleandir'])
            if install_native_now:
                self.dispatch(base + ['-j' + self.options['jobs'], 'dependall', 'install'])
        for name in ('objects', 'tools'):
            if name == 'tools' and getattr(self, 'pending_native_refresh', None) is not None:
                continue
            if name == 'objects' and hasattr(self, 'runtime_refresh_files'):
                # Old context stays authoritative until rebuilt archives pass.
                continue
            path = self.paths[name]
            record = dict(format='emberbsd-sysroot-object-context', version=1,
                          signature=self.context_signature(path), sources=self.sources)
            self.mkdirs(path, os.path.join(self.paths['sdk'], 'obj'))
            jsonio.write_json(os.path.join(path, self.CONTEXT), record, exclusive=self.contexts[name] is None)

    def finish_native_refresh(self):
        """Commit a deferred native-source context only after tools succeeded."""
        pending = getattr(self, 'pending_native_refresh', None)
        if pending is None:
            return
        path = self.paths['tools']
        context = os.path.join(path, self.CONTEXT)
        if jsonio.read_json(context) != pending:
            raise SDKError('native source context changed during tools build')
        record = dict(format='emberbsd-sysroot-object-context', version=1,
                      signature=self.context_signature(path), sources=self.sources)
        jsonio.write_json(context, record, exclusive=False)
        del self.pending_native_refresh

    def begin_runtime_refresh(self):
        if not hasattr(self, 'runtime_refresh_files'):
            return
        for directory in getattr(self, 'refresh_dirs', []):
            if any(name == directory or name.startswith(directory + '/')
                   for name in self.runtime_refresh_files):
                raise SDKError('prepared source cleanup overlaps runtime objects: ' + directory)
        self.runtime_before_shared = prepared_refresh.runtime_shared_identity(
            self.paths['objects'], self.runtime_refresh_files)
        backup = os.path.join(self.paths['objects'], self.RUNTIME_QUARANTINE)
        prepared_refresh.quarantine_runtime_objects(
            self.paths['objects'], self.runtime_refresh_files, backup)

    def finish_runtime_refresh(self):
        if not hasattr(self, 'runtime_refresh_files'):
            return
        ar = self.compiler_record['binaries']['ar']['selected_path']
        def members(archive):
            return process.capture([ar, 't', str(archive)], env=self.environment).stdout.decode('utf-8', 'strict').splitlines()
        def contents(archive, member):
            return process.capture([ar, 'p', str(archive), member], env=self.environment).stdout
        prepared_refresh.verify_runtime_outputs(
            self.paths['objects'], self.runtime_refresh_files,
            self.runtime_refresh_source, self.runtime_refresh_changed, members, contents,
            self.runtime_before_shared)
        path = self.paths['objects']
        record = dict(format='emberbsd-sysroot-object-context', version=1,
                      signature=self.context_signature(path), sources=self.sources)
        jsonio.write_json(os.path.join(path, self.CONTEXT), record, exclusive=False)
        prepared_refresh.cleanup_runtime_quarantine(
            path, self.runtime_refresh_files, os.path.join(path, self.RUNTIME_QUARANTINE))

    def native_makewrapper_command(self):
        operations = list(self.os_build_operations)
        build = list(self.build_command)
        if (not operations or len(build) <= len(operations) or
                build[-len(operations):] != operations):
            raise SDKError('cannot select the current OS make wrapper for source refresh')
        return build[:-len(operations)] + ['makewrapper']

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
        # The preflight record identifies the runtime by its input key. The
        # copy and the runtime stage are not read or hashed again.
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
        if self.options.get('c-foundation') and (self.compiler_family() != 'clang' or self.options.get('llvm-runtime-stage')):
            raise SDKError('C foundation requires the first-base Clang coordinator')
        if self.options.get('target') == 'riscv32' and not self.options.get('c-foundation'):
            raise SDKError('riscv32 currently supports only the intermediate C foundation')
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
            refresh = bool(getattr(self, 'refresh_dirs', None) or
                           getattr(self, 'native_refresh_dirs', None))
            if refresh:
                # Prepared-source cleanup runs nbmake through the routing
                # in the existing tools tree.
                self.prepare_native_routing()
            self.prepare_contexts()
            if not refresh:
                # A new tools tree exists only once its context is written.
                self.prepare_native_routing()
            # Preflight fixes the selected inputs for this run. Reuse their
            # records instead of probing tools and hashing inputs again.
            self.build_payload()
            self.finish_native_refresh()
            self.receipt['outputs'] = self.inspect_payload()
            self.interrupted()
            if hasattr(self, 'runtime_refresh_files'):
                self.finish_runtime_refresh()
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
