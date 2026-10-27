# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD; AI-assisted OS and Ports build orchestration.
"""Checked separate-stage OS sysroot builder. No build occurs on import."""

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
from . import API_VERSION, inventory, jsonio, libedit, native_os_routing, native_toolset, paths, process, provenance, workspace
from .errors import SDKError, ProcessOwnershipError
from .native_os_routing import native_host_flags, native_path
from .targets import TARGETS, target_definition

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
             'MKPROFILE=no MKCVS=no MKGROFF=no MKRUMP=no MAKECONF=/dev/null').split()
    VERIFIER = 'profiles/development-toolchain/runtime/recipe/files/runtime.py'
    CONTEXT = '.prebuild-sdk-context.json'
    QUARANTINE = '.prebuild-sdk-quarantine.json'
    RECEIPT = '.prebuild-sdk-build.json'
    MARKER = '.prebuild-sdk-build-incomplete'
    LOG = '.prebuild-sdk-build.log'

    def __init__(self, options):
        self.options = options.copy()
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
        return self.capture(self.git_command, '-C', root, *arguments)[0].strip()

    def source(self, root, kind=None):
        return provenance.git_source(root, kind=kind, git=self.git_command, env=self.environment)

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

    def verify_runtime(self):
        if self.compiler_family() == 'clang':
            from . import llvm_runtime
            if self.llvm_phase == 'headers':
                return None
            required = (llvm_runtime.FULL_COMPONENTS if self.llvm_phase == 'final'
                        else ('builtins', 'libunwind-static'))
            stage = self.paths['llvm-runtime-stage']
            reported = jsonio.loads_json(process.capture(self.runtime_command, env=self.environment).stdout)
            llvm_runtime.validate_identity(reported, target=self.options['target'], required_components=required)
            record = llvm_runtime.validate_stage(stage, target=self.options['target'],
                required_components=required, ports=self.paths['ports'],
                ports_revision=reported['recipe']['ports_revision'])
            if reported != record:
                raise SDKError('LLVM runtime producer and shared validator disagree')
            if (record['llvm_version'] != self.compiler_record['version'] or
                    record['source'] != {key: self.llvm_source_record['source'][key]
                                         for key in ('archive', 'sha512')} or
                    record['base']['os_revision'] != self.sources['os']['commit']):
                raise SDKError('LLVM runtime compiler, common source or base OS differs from selection')
            rows = inventory.snapshot_tree(self.paths['runtime-prefix'], machine=self.target['elf'])
            self.commands.append(dict(argv=self.runtime_command, scope='runtime-check', status='passed'))
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
                record['format'] != 'emberbsd-sysroot-object-context' or type(record['version']) is not int or record['version'] != 1 or record['signature'] != self.context_signature(path)):
            raise SDKError(f'unknown or incompatible object context; leave it unchanged: {path}')
        try:
            for name in ('os', 'ports', 'sdk'):
                old = record['sources'][name]['commit']
                if not isinstance(old, str) or re.fullmatch('[0-9a-f]{40}', old) is None:
                    raise SDKError('invalid recorded source pin')
                self.git(self.sources[name]['path'], 'merge-base', '--is-ancestor', old, self.sources[name]['commit'])
        except (KeyError, TypeError) as exc:
            raise SDKError(f'invalid object context: {path}: {exc}') from exc
        return record

    def context_signature(self, path):
        if self.compiler_family() != 'clang' or path != self.paths['tools']:
            return self.signature
        # OS native tools do not link the target runtime. Reuse their one tree
        # while target object directories distinguish headers, C base and C++.
        return dict(scope='llvm-native-tools', host_binutils=self.paths['host-binutils'],
                    **{name: self.signature[name] for name in
            ('host', 'target', 'compiler', 'host_tools', 'native_toolset', 'native_inputs',
             'environment', 'tools', 'python', 'process_backend')})

    def verify_llvm_source(self):
        result = process.capture(self.llvm_source_command, env=self.environment)
        record = jsonio.loads_json(result.stdout)
        if (not isinstance(record, dict) or record.get('format') != 'emberbsd-llvm-source' or
                record.get('version') != 1 or
                (self.compiler_family() == 'clang' and record.get('llvm_version') != self.compiler_record['version'])):
            raise SDKError('common LLVM source and selected compiler differ')
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
        module_hashes = {}
        for module in sorted(loaded.glob('*.py')):
            selected = os.path.join(source, 'src', 'emberbsd_sdk', module.name)
            digest = inventory.sha256_file(selected)
            if inventory.sha256_file(module) != digest:
                raise SDKError(f'selected SDK source differs from loaded implementation: {module.name}')
            module_hashes[module.name] = digest
        return dict(path=python, sha256=inventory.sha256_file(self.executable(python)), version=result['version'], api_version=API_VERSION, modules=module_hashes)

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
        if clang:
            if self.llvm_phase != 'headers' and not self.options.get('llvm-runtime-stage'):
                raise SDKError('--llvm-runtime-stage is required for direct final preflight; run() builds its prerequisites')
        elif not self.options.get('gcc-archive') or not self.options.get('pkgsrc'):
            raise SDKError('--gcc-archive and --pkgsrc are required for the explicit GCC runtime')
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
        input_names += (([] if self.llvm_phase == 'headers' else ['llvm-runtime-stage'])
                        if clang else ['runtime-work', 'gcc-archive', 'pkgsrc'])
        for name in input_names:
            self.paths[name] = paths.build_path(paths.canonical_existing(self.options[name]))
        self.paths['python'] = self.native.tool('python')
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
        self.paths['native-routing'] = os.path.join(self.paths['tools'], 'native-routing')
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
        self.sources['sdk'] = self.source(self.paths['sdk-source'], 'sdk')
        self.libedit_record = libedit.validate_source(self.paths['libedit-source'], ports=self.paths['ports'])
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
                prefix = self.paths['runtime-prefix']
                self.runtime_command = producer + ['verify', '--stage', stage, '--target', self.options['target'],
                    '--phase', 'full' if self.llvm_phase == 'final' else 'bootstrap', '--git', self.git_command]
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
        self.signature = dict(host=self.host, target=self.target, compiler=self.compiler_record, libedit=self.libedit_record, runtime=self.runtime_record, runtime_prefix=prefix, flags=self.flags, host_tools=self.host_tools, native_toolset=self.native_selection, environment=self.environment, objects=self.paths['objects'], tools=self.paths['tools'], python=self.python, process_backend=self.process_inspector.describe())
        self.signature['native_inputs'] = self.native_inputs
        self.signature['compiler_family'] = self.compiler_family()
        self.signature['llvm_source'] = self.llvm_source_record
        if clang:
            self.signature['llvm_phase'] = self.llvm_phase
        self.configure_signature()
        self.contexts = {name: self.inspect_context(self.paths[name]) for name in ('objects', 'tools')}
        self.build_command = [self.shell, os.path.join(self.paths['os'], 'build.sh'), '-U', '-u', '-j' + jobs, '-m', self.target['machine'], '-a', self.target['arch'], '-O', self.paths['objects'], '-T', self.paths['tools'], '-D', self.paths['destination']]
        for flag in self.flags + ['BUILDINFO=EmberBSD:' + self.sources['os']['commit']]:
            self.build_command.extend(('-V', flag))
        self.build_command.extend(('tools', 'libs'))
        self.loader_command = [os.path.join(self.paths['tools'], 'bin', 'nbmake-' + self.target['machine']), '-C', os.path.join(self.paths['os'], 'libexec/ld.elf_so'), '-j' + jobs, 'dependall']
        self.install_command = self.loader_command[:-1] + ['install']
        self.receipt = dict(format='emberbsd-sysroot-build', version=3 if clang else 2, scope='sysroot-build', qualification='not-built', execution_acceptance='not-run', workspace=layout, paths=self.paths, sources=self.sources, previous_contexts=self.contexts, signature=self.signature, commands=self.commands, plan=[self.runtime_command, self.build_command, self.loader_command, self.install_command, self.runtime_command, self.runtime_command], working_directory=self.paths['os'], copy=dict(source=prefix, destination=os.path.join(self.paths['destination'], 'usr/pkg/llvm' if clang else 'usr/pkg/gcc16'), verification='exact pre/copy/post inventory'), not_checked=['target execution', 'ABI compatibility', 'SDK release'])
        return self.receipt

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
        if (self.compiler_family() == 'clang' and not hasattr(self, 'llvm_phase') and
                not self.options.get('llvm-runtime-stage')):
            from .llvm_sysroot import LLVMCoordinator
            return LLVMCoordinator(self.options, type(self)).run()
        self.preflight()
        if self.options.get('plan'):
            return self.receipt
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
            if libedit.validate_source(self.paths['libedit-source'], ports=self.paths['ports']) != self.libedit_record:
                raise SDKError('libedit source changed before build')
            self.build_payload()
            if libedit.validate_source(self.paths['libedit-source'], ports=self.paths['ports']) != self.libedit_record:
                raise SDKError('libedit source changed during build')
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
