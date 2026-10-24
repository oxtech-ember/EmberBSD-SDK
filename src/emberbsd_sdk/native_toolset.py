# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Explicit, receipt-bound native build tools. This module runs no tools."""

from dataclasses import dataclass
import hashlib
import os
import platform
import re
import stat
import struct
from types import MappingProxyType

from .errors import SDKError
from . import paths
from .inventory import sha256_file, snapshot_tree
from .jsonio import MAX_JSON, loads_json
from .paths import canonical_existing, inside, open_regular, verify_stable

NATIVE_TOOLSET_API = 1
NATIVE_C_PROBE = (b'#include <stdio.h>\n'
                  b'int main(void) { return puts("emberbsd-native-c-v1") < 0; }\n')
NATIVE_CXX_PROBE = (b'#include <iostream>\n'
                    b'int main() { std::cout << "emberbsd-native-cxx-v1" << std::endl; '
                    b'return !std::cout; }\n')
NATIVE_TOOL_ROLES = ('cc', 'cxx', 'cpp', 'as', 'ld', 'ar', 'ranlib', 'nm',
                     'strip', 'make', 'cmake', 'meson', 'ninja', 'pkg-config',
                     'python', 'sh')
_DRIVERS = ('cc1', 'cc1plus', 'collect2', 'lto1', 'lto-wrapper', 'as', 'ld')
_ABI_FLAGS = ('cppflags', 'cflags', 'cxxflags', 'ldflags')
_SHA = re.compile(r'[0-9a-f]{64}\Z')
_ROLE = re.compile(r'[a-z][a-z0-9_-]*\Z')


def _require(condition, message):
    if not condition:
        raise SDKError(message)


def _text(value, label):
    _require(isinstance(value, str) and value and
             not any(ord(c) < 32 or ord(c) == 127 for c in value),
             f'invalid {label}')
    try:
        value.encode('utf-8', 'strict')
    except UnicodeError as exc:
        raise SDKError(f'invalid {label}') from exc
    return value


def _sha(value):
    _require(isinstance(value, str) and _SHA.fullmatch(value), 'invalid SHA256')
    return value


def _absolute(value):
    value = _text(value, 'absolute path')
    _require(os.path.isabs(value) and os.path.normpath(value) == value,
             f'path must be absolute and normalized: {value}')
    return value


SYSTEM_PREFIXES = ('/', '/usr', '/usr/local', '/opt', '/Library', '/Applications')
SYSTEM_TOOL_ROOTS = ('/usr/bin', '/usr/sbin', '/usr/lib', '/usr/libexec', '/usr/pkg',
                     '/usr/local/bin', '/usr/local/sbin', '/usr/local/lib', '/usr/local/libexec',
                     '/usr/local/Cellar', '/usr/local/Homebrew', '/usr/local/opt',
                     '/bin', '/sbin', '/opt/homebrew', '/opt/local', '/home/linuxbrew',
                     '/Library/Developer', '/System/Library', '/Applications/Xcode.app')


def provider_root(root, *, live=False):
    _absolute(root)
    folded = root.casefold()
    _require(folded not in {path.casefold() for path in SYSTEM_PREFIXES} and os.path.normpath(root) == root,
            'invalid SDK provider root')
    _require(not any(os.path.commonpath((folded, path.casefold())) == path.casefold() for path in SYSTEM_TOOL_ROOTS),
            'system tool directories cannot be SDK compiler providers')
    _require(not folded.startswith('/applications/xcode'), 'Xcode cannot be an SDK compiler provider')
    if live:
        for path in SYSTEM_PREFIXES:
            if paths.entry(path) is not None:
                _require(not os.path.samefile(root, path), 'system root cannot be an SDK compiler provider')
        for path in SYSTEM_TOOL_ROOTS:
            if paths.entry(path) is not None:
                _require(paths.relative_inside(root, path) is None, 'system tool tree cannot be an SDK compiler provider')


def _bytes(reference, limit=MAX_JSON):
    _require(isinstance(reference, dict) and set(reference) == {'path', 'sha256'},
             'expected an explicit path and SHA256 reference')
    path, expected = _absolute(reference['path']), _sha(reference['sha256'])
    handle, before = open_regular(path)
    with handle:
        data = handle.read(limit + 1)
    verify_stable(path, before)
    _require(len(data) <= limit, f'evidence exceeds size limit: {path}')
    _require(hashlib.sha256(data).hexdigest() == expected, f'SHA256 mismatch: {path}')
    return data


def _file(reference):
    """Check stable referenced bytes without buffering or a metadata size cap."""
    _require(isinstance(reference, dict) and set(reference) == {'path', 'sha256'},
             'expected an explicit input path and SHA256')
    path = _absolute(reference['path'])
    _require(sha256_file(path) == _sha(reference['sha256']), 'referenced input changed')
    return path


def _json(reference):
    result = loads_json(_bytes(reference))
    _require(isinstance(result, dict), 'expected a metadata object')
    return result


def _envelope(value, name, *, scope='native-build'):
    _require(value.get('format') == name and type(value.get('version')) is int
             and value['version'] == 1 and value.get('scope') == scope,
             f'unsupported {name} format, version or scope')


def _source(value):
    _require(isinstance(value, dict) and set(value) == {'repository', 'commit'},
             'expected a pinned source')
    _text(value['repository'], 'source repository')
    _require(isinstance(value['commit'], str) and
             re.fullmatch(r'[0-9a-f]{40}', value['commit']), 'invalid source commit')


def _gcc(value, host):
    _require(isinstance(value, dict) and set(value) == {'family', 'version', 'triplet'},
             'invalid native compiler identity')
    _require(value['family'] == 'gcc' and isinstance(value['version'], str) and
             re.fullmatch(r'16\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9_.-]+)?', value['version']),
             'native build tools require GCC16')
    triplet = _text(value['triplet'], 'native compiler triplet')
    cpu = {'arm64': 'aarch64', 'amd64': 'x86_64'}.get(host['cpu'], host['cpu'])
    parts = triplet.split('-')
    triplet_cpu = {'arm64': 'aarch64', 'amd64': 'x86_64'}.get(parts[0], parts[0])
    version = r'(?:[0-9]+(?:\.[0-9]+)*)?'
    os_pattern = {'Darwin': 'darwin' + version, 'Linux': r'linux(?:-(?:gnu|musl))?',
                  'NetBSD': 'netbsd' + version}.get(host['os'])
    suffix = re.fullmatch(r'(?:(?P<vendor>[a-z0-9_]*)-)?' + os_pattern,
                          '-'.join(parts[1:])) if os_pattern else None
    vendor = suffix.group('vendor') if suffix else None
    conflicting_vendor = vendor and re.fullmatch(
        r'(?:linux|darwin|netbsd|freebsd|openbsd|dragonfly|windows|mingw|cygwin)[0-9]*', vendor)
    _require(suffix is not None and triplet_cpu == cpu and not conflicting_vendor,
             'compiler triplet does not describe the actual native host')


def _native_image(path, host, *, object_file=False, shared_library=False):
    """Reject scripts, target images and renamed wrappers before selection."""
    handle, before = open_regular(path)
    with handle:
        header = handle.read(64)
    verify_stable(path, before)
    cpu = {'arm64': 'aarch64', 'amd64': 'x86_64'}.get(host['cpu'], host['cpu'])
    if host['os'] == 'Darwin':
        expected = {'x86_64': 0x01000007, 'aarch64': 0x0100000c}.get(cpu)
        valid = len(header) >= 32 and header[:4] == b'\xcf\xfa\xed\xfe'
        valid = valid and struct.unpack_from('<I', header, 4)[0] == expected
        valid = valid and struct.unpack_from('<I', header, 12)[0] == (1 if object_file else 6 if shared_library else 2)
    else:
        expected = {'x86_64': 62, 'aarch64': 183}.get(cpu)
        valid = len(header) >= 64 and header[:7] == b'\x7fELF\x02\x01\x01'
        valid = valid and struct.unpack_from('<H', header, 18)[0] == expected
        valid = valid and struct.unpack_from('<H', header, 16)[0] in ((1,) if object_file else (3,) if shared_library else (2, 3))
    kind = 'object' if object_file else 'shared library' if shared_library else 'executable, not a wrapper'
    _require(expected is not None and valid, f'expected a native {kind}: {path}')


def _probe_member(reference, host, *, object_file=False):
    _require(isinstance(reference, dict) and set(reference) == {'path', 'sha256'},
             'expected an explicit probe path and SHA256')
    path = _absolute(reference['path'])
    _require(sha256_file(path) == _sha(reference['sha256']), 'native probe member changed')
    _require(object_file or stat.S_IMODE(os.stat(path).st_mode) & 0o111,
             'native probe artifact is not executable')
    _native_image(path, host, object_file=object_file)
    return path


def _command(value, executable):
    _require(isinstance(value, dict) and type(value.get('actual_exit')) is int
             and value['actual_exit'] == 0, 'missing successful actual command')
    argv = value.get('argv')
    _require(isinstance(argv, list) and argv and argv[0] == executable,
             'command does not select the declared absolute executable')
    for item in argv:
        _text(item, 'command argument')
    stdout = _bytes(value.get('stdout'))
    _bytes(value.get('stderr'))
    return stdout


def _flags(value, allowed_roots=None):
    _require(isinstance(value, dict) and set(value) == set(_ABI_FLAGS),
             'expected separate native ABI flag arrays')
    result = {}
    # Keep this API deliberately small. Response files, driver overrides and
    # implicit search directories need a new qualified contract, not a bypass.
    plain = re.compile(r'(?:-O[0-3sg]|-g[0-3]?|-fPIC|-fPIE|-fno-common|-pthread|'
                       r'-std=[A-Za-z0-9+_.-]+|-D[A-Za-z_][A-Za-z0-9_]*(?:=.*)?|'
                       r'-U[A-Za-z_][A-Za-z0-9_]*|-W(?:all|extra|error|no-[A-Za-z0-9_-]+))\Z')
    for name, words in value.items():
        _require(isinstance(words, list), f'expected an array: {name}')
        normalized = []
        index = 0
        while index < len(words):
            word = _text(words[index], 'ABI flag')
            path = None
            if word in ('-I', '-L', '-isystem', '-isysroot', '--sysroot'):
                index += 1
                _require(index < len(words), 'missing ABI search path')
                path = _absolute(words[index])
                normalized.append(word)
                word = path
            elif word.startswith(('-I', '-L')) and len(word) > 2:
                path = _absolute(word[2:])
            elif word.startswith('--sysroot='):
                path = _absolute(word.split('=', 1)[1])
            else:
                _require(plain.fullmatch(word), f'unsupported native ABI flag: {word}')
            if path is not None and allowed_roots is not None:
                resolved = canonical_existing(path)
                _require(any(resolved == root or inside(resolved, root) for root in allowed_roots),
                         f'native ABI search escapes declared inputs: {path}')
            normalized.append(word)
            index += 1
        result[name] = tuple(normalized)
    return MappingProxyType(result)


def _payload(descriptor, required_roles):
    """Shared current-byte checks for native and separately scoped seed inputs."""
    root = _absolute(descriptor['bundle_root'])
    _require(canonical_existing(root) == root and os.path.isdir(root),
             'bundle root must be a real directory with real ancestors')
    provider_root(root, live=True)
    host, compiler, providers = descriptor['host'], descriptor['compiler'], descriptor['providers']
    inventory = _json(descriptor['inventory'])
    _require(not inside(descriptor['inventory']['path'], root),
             'inventory must be outside its payload')
    _require(snapshot_tree(root) == inventory, 'native bundle inventory changed')
    tools, records = {}, descriptor['tools']
    _require(isinstance(records, dict) and records, 'explicit tool members are required')
    _require(isinstance(required_roles, (list, tuple, set, frozenset)) and required_roles,
             'required_roles must be an explicit nonempty collection')
    required = set(required_roles) | {'cc', 'cxx', 'cpp', 'as', 'ld'}
    for role in required:
        _require(isinstance(role, str) and _ROLE.fullmatch(role) and role in records,
                 f'missing required native tool role: {role}')
    for role, record in records.items():
        _require(isinstance(role, str) and _ROLE.fullmatch(role), 'invalid tool role')
        selected = _absolute(record['path'])
        resolved = canonical_existing(selected)
        _require(inside(selected, root) and inside(resolved, root),
                 f'tool is outside the native bundle: {role}')
        _require(record['provider'] in providers, f'unknown provider: {role}')
        _text(record['version'], 'tool version')
        if role in ('cc', 'cxx', 'cpp'):
            _require(record['version'] == compiler['version'], 'compiler member version mismatch')
        _require(sha256_file(resolved) == _sha(record['sha256']) and
                 stat.S_IMODE(os.stat(resolved).st_mode) & 0o111,
                 f'changed or nonexecutable native member: {role}')
        tools[role] = selected
        if record['kind'] == 'native':
            _native_image(resolved, host)
        else:
            _require(record['kind'] == 'script' and role not in
                     {'cc', 'cxx', 'cpp', *_DRIVERS}, 'compiler wrappers are not native tools')
    for role, record in records.items():
        if record['kind'] == 'script':
            interpreter = record.get('interpreter')
            _require(interpreter in records and records[interpreter]['kind'] == 'native',
                     'script needs an explicit native interpreter member')
            _require(not any(char.isspace() for char in tools[interpreter]),
                     'script interpreter path must not contain whitespace')
            handle, before = open_regular(canonical_existing(tools[role]))
            with handle:
                first = handle.readline(4097)
            verify_stable(canonical_existing(tools[role]), before)
            _require(first == ('#!' + tools[interpreter] + '\n').encode('utf-8'),
                     'script shebang must select exactly its bundled interpreter')
    abi = descriptor['host_abi']
    _require(isinstance(abi, dict) and set(abi) == {'headers', 'libraries'},
             'native ABI headers and libraries must be separate inputs')
    roots, abi_inventories = [root], []
    for kind in ('headers', 'libraries'):
        _require(isinstance(abi[kind], list) and abi[kind], f'missing native ABI {kind}')
        for item in abi[kind]:
            abi_root = _absolute(item['root'])
            _require(canonical_existing(abi_root) == abi_root and os.path.isdir(abi_root),
                     'ABI root must be a real directory')
            abi_inventory = _json(item['inventory'])
            _require(snapshot_tree(abi_root) == abi_inventory,
                     f'native ABI {kind} inventory changed')
            roots.append(abi_root)
            abi_inventories.append((abi_root, abi_inventory))
    flags = _flags(descriptor['abi_flags'], roots)
    return root, inventory, tools, records, abi_inventories, flags


def _driver_queries(descriptor, checks, tools, commands):
    compiler, records = descriptor['compiler'], descriptor['tools']
    for option, expected in (('-dumpfullversion', compiler['version']),
                             ('-dumpmachine', compiler['triplet'])):
        for role in ('cc', 'cxx'):
            query = checks['identity'][role][option]
            _require(query.get('argv') == [*commands[role], option], 'compiler identity query mismatch')
            _require(_command(query, tools[role]).decode('utf-8', 'strict').strip() == expected,
                     'compiler identity output mismatch')
    components = descriptor['driver_components']
    _require(set(components) == set(_DRIVERS), 'incomplete GCC driver closure')
    for component, role in components.items():
        _require(role in tools and records[role]['kind'] == 'native', 'driver member is not native')
        if component in ('as', 'ld'):
            _require(role == component, 'assembler and linker must match selected native roles')
        driver = 'cxx' if component == 'cc1plus' else 'cc'
        query = checks['driver_components'][component]
        _require(query.get('argv') == [*commands[driver], '-print-prog-name=' + component],
                 'driver component query mismatch')
        output = _command(query, tools[driver]).decode('utf-8', 'strict').strip()
        _require(output == tools[role], f'driver selected an undeclared or implicit member: {component}')


@dataclass(frozen=True)
class NativeToolset:
    """Immutable validated selection. Call load_native_toolset to create it."""

    descriptor_sha256: str
    producer_receipt: object
    bundle_root: str
    host: object
    compiler: object
    tools: object
    abi_flags: object

    def tool(self, role):
        if role not in self.tools:
            raise SDKError(f'native tool role is not selected: {role}')
        return self.tools[role]

    def as_dict(self):
        return dict(format='emberbsd-native-toolset-selection', version=1,
                    api=NATIVE_TOOLSET_API, descriptor_sha256=self.descriptor_sha256,
                    producer_receipt=dict(self.producer_receipt), bundle_root=self.bundle_root,
                    host=dict(self.host), compiler=dict(self.compiler), tools=dict(self.tools),
                    abi_flags={key: list(value) for key, value in self.abi_flags.items()})


def validate_selection(value, *, required_roles):
    """Check saved selection consistency without reading files or qualifying it."""
    try:
        _require(isinstance(value, dict) and set(value) ==
                 {'format', 'version', 'api', 'descriptor_sha256', 'producer_receipt',
                  'bundle_root', 'host', 'compiler', 'tools', 'abi_flags'}, 'invalid saved native selection')
        _require(value['format'] == 'emberbsd-native-toolset-selection' and type(value['version']) is int and
                 value['version'] == 1 and type(value['api']) is int and value['api'] == NATIVE_TOOLSET_API,
                 'unsupported saved native selection')
        _sha(value['descriptor_sha256'])
        receipt = value['producer_receipt']
        _require(isinstance(receipt, dict) and set(receipt) == {'path', 'sha256'}, 'invalid producer receipt reference')
        _absolute(receipt['path']); _sha(receipt['sha256'])
        root = _absolute(value['bundle_root']); provider_root(root)
        host = value['host']
        _require(isinstance(host, dict) and set(host) == {'os', 'cpu'} and
                 host['os'] in ('Darwin', 'Linux', 'NetBSD'), 'invalid recorded native host')
        _text(host['cpu'], 'native CPU'); _gcc(value['compiler'], host)
        tools = value['tools']
        _require(isinstance(tools, dict) and set(required_roles) | {'cc', 'cxx', 'cpp', 'as', 'ld'} <= set(tools),
                 'missing recorded native roles')
        for role, path in tools.items():
            _require(isinstance(role, str) and _ROLE.fullmatch(role), 'invalid recorded native role')
            _absolute(path)
            _require(os.path.commonpath((root, path)) == root and path != root, 'recorded tool escapes native bundle')
        # Reuse the live flag grammar. Portable verification cannot repeat
        # filesystem containment checks against the original ABI directories.
        _flags(value['abi_flags'])
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise SDKError('invalid saved native selection') from exc


def load_native_toolset(path, *, descriptor_sha256, accepted_producer_receipt, required_roles):
    """Check explicit bytes and accepted producer evidence. Never run a tool.

    The caller must obtain the receipt hash from its trusted qualification gate.
    A receipt supplied by an untrusted bundle is not an acceptance decision.
    """
    try:
        return _load(path, descriptor_sha256, accepted_producer_receipt, required_roles)
    except (OSError, KeyError, TypeError, ValueError, AttributeError) as exc:
        raise SDKError(f'invalid native toolset: {exc}') from exc


def _load(path, descriptor_sha256, accepted_producer_receipt, required_roles):
    descriptor_sha256 = _sha(descriptor_sha256)
    descriptor = _json(dict(path=os.fspath(path), sha256=descriptor_sha256))
    _envelope(descriptor, 'emberbsd-native-toolset')
    receipt = _json(accepted_producer_receipt)
    _envelope(receipt, 'emberbsd-native-toolset-qualification')
    _require(receipt.get('status') == 'PASS' and
             receipt.get('descriptor_sha256') == descriptor_sha256,
             'producer receipt does not accept this descriptor')
    root = _absolute(descriptor['bundle_root'])
    _require(canonical_existing(root) == root and os.path.isdir(root),
             'bundle root must be a real directory with real ancestors')
    provider_root(root, live=True)
    host = descriptor['host']
    _require(host == {'os': platform.system(), 'cpu': platform.machine()},
             'native toolset host does not match the actual OS and CPU')
    compiler = descriptor['compiler']
    _gcc(compiler, host)
    for key in ('host', 'compiler', 'sources', 'providers'):
        _require(receipt.get(key) == descriptor[key], f'producer {key} mismatch')
    sources, providers = descriptor['sources'], descriptor['providers']
    _require(isinstance(sources, dict) and {'sdk', 'ports'} <= set(sources),
             'SDK and Ports source pins are required')
    for source in sources.values():
        _source(source)
    _require(isinstance(providers, dict) and providers, 'provider provenance is required')
    for name, provider in providers.items():
        _require(isinstance(name, str) and _ROLE.fullmatch(name), 'invalid provider name')
        _require(provider['source'] in sources, 'unknown provider source')
        recipe = _text(provider['recipe'], 'provider recipe')
        _require(not os.path.isabs(recipe) and all(part not in ('', '.', '..')
                                                  for part in recipe.split('/')),
                 'provider recipe must be source-relative without traversal')
        _sha(provider['recipe_sha256'])
        build = _json(receipt['provider_builds'][name])
        _envelope(build, 'emberbsd-native-provider-build')
        _require(build.get('status') == 'PASS' and build.get('host') == host and
                 build.get('source') == sources[provider['source']] and
                 build.get('recipe') == provider['recipe'] and
                 build.get('recipe_sha256') == provider['recipe_sha256'],
                 'provider build does not match its source and recipe')
        _gcc(build['compiler'], host)
        if build.get('accepted_input_scope') == 'bootstrap-seed':
            # Only the first native GCC16 recipe closure can graduate. The seed
            # scope remains unchanged. Final ordinary qualification is still required.
            from .bootstrap_seed import verify_native_gcc16_graduation
            verify_native_gcc16_graduation(
                build.get('bootstrap_graduation'), output_descriptor=descriptor,
                output_descriptor_sha256=descriptor_sha256, provider=provider)
        else:
            _require(build.get('accepted_input_scope') == 'qualified-native',
                     'bootstrap seed is not an ordinary native provider')
            _require('bootstrap_graduation' not in build,
                     'bootstrap input scope cannot be relabeled as qualified-native')
        _file(build['build_log'])
    root, inventory, tools, records, abi_inventories, flags = _payload(descriptor, required_roles)
    abi = descriptor['host_abi']
    _require(receipt.get('host_abi') == abi and receipt.get('abi_flags') == descriptor['abi_flags'],
             'producer native ABI inputs mismatch')
    checks = receipt['checks']
    probe_inodes = {(os.stat(canonical_existing(path)).st_dev, os.stat(canonical_existing(path)).st_ino)
                    for path in tools.values()}
    for role in ('cc', 'cxx'):
        probe = checks[role]
        expected_source = NATIVE_C_PROBE if role == 'cc' else NATIVE_CXX_PROBE
        _require(_bytes(probe['source']) == expected_source, 'native probe source content mismatch')
        source = _absolute(probe['source']['path'])
        _require(source.endswith('.c' if role == 'cc' else '.cc'), 'native probe source suffix mismatch')
        object_path = _probe_member(probe['object'], host, object_file=True)
        artifact_path = _probe_member(probe['artifact'], host)
        for member in (source, object_path, artifact_path):
            info = os.stat(member)
            identity = (info.st_dev, info.st_ino)
            _require(identity not in probe_inodes, 'probe inputs and outputs must have distinct identities')
            probe_inodes.add(identity)
        language_flags = flags['cflags' if role == 'cc' else 'cxxflags']
        compile_argv = [tools[role], *flags['cppflags'], *language_flags,
                        '-c', source, '-o', object_path]
        link_argv = [tools[role], *language_flags, object_path, *flags['ldflags'], '-o', artifact_path]
        for phase, argv in (('compile', compile_argv), ('link', link_argv), ('run', [artifact_path])):
            _require(probe[phase].get('argv') == argv, f'native {phase} route does not bind probe files')
            stdout = _command(probe[phase], argv[0])
            expected_output = (b'emberbsd-native-c-v1\n' if role == 'cc' else b'emberbsd-native-cxx-v1\n')
            _require(stdout == (expected_output if phase == 'run' else b''),
                     f'native {phase} output mismatch')
        _require(_bytes(probe['run']['stderr']) == b'', 'native probe wrote unexpected stderr')
    _driver_queries(descriptor, checks, tools, {role: (path,) for role, path in tools.items()})
    audit = _json(receipt['execution_audit'])
    _envelope(audit, 'emberbsd-native-execution-audit')
    _require(audit.get('status') == 'PASS' and audit.get('descriptor_sha256') == descriptor_sha256
             and audit.get('host') == host and audit.get('tools') == records
             and audit.get('undeclared_tools') == [] and audit.get('path_searches') == []
             and audit.get('undeclared_abi_inputs') == [], 'native execution closure was not accepted')
    # Recheck payload bytes after evidence inspection. Consumers still must keep
    # the selected bundle immutable and hold their normal workspace leases.
    _require(snapshot_tree(root) == inventory, 'native bundle changed during validation')
    for abi_root, abi_inventory in abi_inventories:
        _require(snapshot_tree(abi_root) == abi_inventory, 'native ABI changed during validation')
    return NativeToolset(descriptor_sha256, MappingProxyType(dict(accepted_producer_receipt)), root,
                         MappingProxyType(dict(host)), MappingProxyType(dict(compiler)),
                         MappingProxyType(tools), flags)
