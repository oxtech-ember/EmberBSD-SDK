# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Explicit native GCC inputs. Provider declarations are not attestations."""

import os
from pathlib import Path
import platform
import re
import struct
import sys
from urllib.parse import urlsplit

from . import inventory, jsonio, paths, process
from .errors import SDKError

OPTIONS = ('host-toolchain', 'host-cc', 'host-cxx', 'host-toolchain-manifest')
TOOLS = ('cc', 'cxx', 'cc1', 'cc1plus', 'collect2', 'lto1', 'lto-wrapper', 'as', 'ld')
FORMAT = 'emberbsd-native-gcc-inputs'
PROBE_CONTENT = b'/* Native GCC driver language probe. Preprocess only; no declarations. */\n'
SYSTEM_PREFIXES = ('/', '/usr', '/usr/local', '/opt', '/Library', '/Applications')
SYSTEM_TOOL_ROOTS = ('/usr/bin', '/usr/sbin', '/usr/lib', '/usr/libexec', '/usr/pkg',
                     '/usr/local/bin', '/usr/local/sbin', '/usr/local/lib', '/usr/local/libexec',
                     '/usr/local/Cellar', '/usr/local/Homebrew', '/usr/local/opt',
                     '/bin', '/sbin', '/opt/homebrew', '/opt/local',
                     '/Library/Developer', '/System/Library', '/Applications/Xcode.app')


def require(condition, message):
    if not condition:
        raise SDKError('native compiler: ' + message)


def absolute(value):
    require(isinstance(value, str) and os.path.isabs(value), 'an absolute path is required')
    return paths.build_path(value)


def required_options(options):
    for name in OPTIONS:
        require(name in options, 'explicit --' + name + ' is required')
        absolute(options[name])


def provider_root(root, *, live=False):
    absolute(root)
    folded = root.casefold()
    require(folded not in {path.casefold() for path in SYSTEM_PREFIXES} and os.path.normpath(root) == root,
            'invalid SDK provider root')
    require(not any(os.path.commonpath((folded, path.casefold())) == path.casefold() for path in SYSTEM_TOOL_ROOTS),
            'system tool directories cannot be SDK compiler providers')
    if live:
        for path in SYSTEM_PREFIXES:
            if paths.entry(path) is not None:
                require(not os.path.samefile(root, path), 'system root cannot be an SDK compiler provider')
        for path in SYSTEM_TOOL_ROOTS:
            if paths.entry(path) is not None:
                require(paths.relative_inside(root, path) is None, 'system tool tree cannot be an SDK compiler provider')


def machine_name(value):
    return {'arm64': 'aarch64', 'amd64': 'x86_64', 'i686': 'i386', 'i586': 'i386'}.get(value, value)


def current_host():
    return dict(system=platform.system(), machine=machine_name(platform.machine()),
                pointer_bits=struct.calcsize('P') * 8, byteorder=sys.byteorder)


def digest(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def relative(value):
    return (isinstance(value, str) and re.fullmatch('[A-Za-z0-9_.+/-]+', value) is not None
            and not value.startswith('/') and all(p not in ('', '.', '..') for p in value.split('/')))


def validate_manifest(value):
    """Check the portable declaration without reading its mutable paths."""
    try:
        require(isinstance(value, dict) and set(value) ==
                {'format', 'version', 'host', 'gcc_version', 'producer', 'tools', 'abi_inputs', 'provenance'},
                'invalid input manifest fields')
        require(value['format'] == FORMAT and type(value['version']) is int and value['version'] == 1,
                'unsupported input manifest')
        require(value['gcc_version'] == '16.2.0', 'GCC 16.2.0 is required')
        host = value['host']
        require(isinstance(host, dict) and set(host) == {'system', 'machine', 'pointer_bits', 'byteorder', 'triplet'},
                'invalid declared host')
        require(host['system'] in ('Darwin', 'Linux', 'NetBSD') and
                isinstance(host['machine'], str) and re.fullmatch('[A-Za-z0-9_]+', host['machine']) and
                type(host['pointer_bits']) is int and host['pointer_bits'] in (32, 64) and
                host['byteorder'] in ('little', 'big') and isinstance(host['triplet'], str) and
                re.fullmatch('[A-Za-z0-9_.+-]+', host['triplet']), 'invalid host ABI declaration')
        require(native_triplet(host['triplet'], host), 'compiler triplet differs from declared native host')
        producer = value['producer']
        require(isinstance(producer, dict) and set(producer) == {'repository', 'revision', 'recipe'},
                'invalid producer declaration')
        url = urlsplit(producer['repository'])
        require(url.scheme == 'https' and url.netloc and url.path and not url.username and not url.password
                and not url.query and not url.fragment, 'producer repository must be an HTTPS repository URL')
        require(isinstance(producer['revision'], str) and re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}', producer['revision'])
                and relative(producer['recipe']), 'invalid producer revision or recipe')
        require(isinstance(value['tools'], dict) and set(value['tools']) == set(TOOLS), 'incomplete native compiler set')
        for row in value['tools'].values():
            require(isinstance(row, dict) and set(row) == {'path', 'sha256'} and relative(row['path']) and digest(row['sha256']),
                    'invalid compiler path or hash')
        for name in ('abi_inputs', 'provenance'):
            rows = value[name]
            require(isinstance(rows, list) and (name != 'abi_inputs' or rows), 'host ABI inputs are required')
            seen = set()
            for row in rows:
                require(isinstance(row, dict) and set(row) == {'path', 'sha256'} and digest(row['sha256']),
                        'invalid ' + name + ' reference')
                absolute(row['path'])
                require(row['path'] not in seen, 'duplicate ' + name + ' reference')
                seen.add(row['path'])
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise SDKError('native compiler: malformed input manifest') from exc


def native_triplet(triplet, host):
    cpu = machine_name(triplet.split('-', 1)[0])
    expected = host['machine']
    if expected.startswith('armv'):
        cpu = expected if cpu in ('arm', expected) else cpu
    os_pattern = {'Darwin': r'-apple-darwin[0-9.]*$', 'Linux': r'-[^ ]*linux-(?:gnu[^ ]*|musl[^ ]*)$',
                  'NetBSD': r'-(?:[^-]*-)?netbsd[^ ]*$'}.get(host['system'])
    return cpu == expected and os_pattern is not None and re.search(os_pattern, triplet) is not None


def validate_record(record):
    """Validate embedded identities for portable release receipt inspection."""
    try:
        require(isinstance(record, dict) and set(record) == {'manifest', 'root', 'binaries', 'references', 'compilers'},
                'invalid native compiler identity')
        manifest = record['manifest']
        require(set(manifest) == {'path', 'sha256', 'content_sha256', 'content'} and digest(manifest['sha256']), 'invalid manifest identity')
        absolute(manifest['path']); provider_root(record['root'])
        validate_manifest(manifest['content'])
        require(manifest['content_sha256'] == inventory_hash(manifest['content']), 'native manifest content hash differs')
        declared = manifest['content']
        require(set(record['binaries']) == set(TOOLS), 'incomplete native binary identity')
        for name, row in record['binaries'].items():
            require(set(row) == {'selected_path', 'path', 'sha256'} and row['sha256'] == declared['tools'][name]['sha256'],
                    'native binary hash differs from declaration')
            require(row['selected_path'] == record['root'] + '/' + declared['tools'][name]['path'],
                    'native binary selection differs from declaration')
            absolute(row['path'])
            require(os.path.commonpath((record['root'], row['path'])) == record['root'] and row['path'] != record['root']
                    and os.path.normpath(row['path']) == row['path'], 'native binary escapes provider')
        require(record['references'] == {n: declared[n] for n in ('abi_inputs', 'provenance')}, 'native input references differ')
        require(set(record['compilers']) == {'cc', 'cxx'}, 'incomplete native compiler query identity')
        for name, row in record['compilers'].items():
            require(set(row) == {'version', 'triplet', 'macros'} and row['version'] == declared['gcc_version']
                    and row['triplet'] == declared['host']['triplet'], 'native compiler identity differs')
            validate_macros(row['macros'], declared['host'])
            require((name == 'cxx') == ('__cplusplus' in row['macros']), 'native C/C++ frontend mismatch')
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise SDKError('native compiler: malformed recorded identity') from exc


def inventory_hash(value):
    import hashlib
    return hashlib.sha256(jsonio.json_bytes(value)).hexdigest()


def validate_macros(macros, host):
    require(isinstance(macros, dict) and macros.get('__GNUC__') == '16' and '__clang__' not in macros,
            'expected genuine GCC16 predefined macros')
    require(macros.get('__SIZEOF_POINTER__') == str(host['pointer_bits'] // 8), 'native pointer ABI mismatch')
    endian = '__ORDER_LITTLE_ENDIAN__' if host['byteorder'] == 'little' else '__ORDER_BIG_ENDIAN__'
    require(macros.get('__BYTE_ORDER__') in (endian, macros.get(endian)) and macros.get(endian) is not None,
            'native byte order mismatch')
    os_macro = {'Darwin': '__APPLE__', 'Linux': '__linux__', 'NetBSD': '__NetBSD__'}[host['system']]
    require(os_macro in macros and not any(key in macros for key in
            {'__APPLE__', '__linux__', '__NetBSD__'} - {os_macro}), 'native operating system mismatch')
    cpu_macros = {'aarch64': '__aarch64__', 'x86_64': '__x86_64__', 'i386': '__i386__',
                  'riscv64': '__riscv', 'powerpc64': '__powerpc64__', 'ppc64': '__powerpc64__',
                  'ppc64le': '__powerpc64__', 's390x': '__s390x__'}
    cpu = '__arm__' if host['machine'].startswith('armv') else cpu_macros.get(host['machine'])
    require(cpu is not None and cpu in macros, 'unsupported or mismatched native CPU macros')


def inspect(options, environment):
    """Bind explicit tools to a caller-selected provider declaration. No compilation."""
    required_options(options)
    root = paths.canonical_existing(options['host-toolchain'])
    provider_root(root, live=True)
    paths.real_directory(root)
    manifest_path = paths.canonical_existing(options['host-toolchain-manifest'])
    paths.regular_file(manifest_path)
    manifest_file_hash = inventory.sha256_file(manifest_path)
    manifest = jsonio.read_json(manifest_path)
    validate_manifest(manifest)
    host = manifest['host']
    require({k: host[k] for k in current_host()} == current_host(), 'declared host differs from the running host')
    binaries = {}
    for name in TOOLS:
        row = manifest['tools'][name]
        selected = os.path.join(root, row['path'])
        path = paths.canonical_existing(selected)
        require(paths.inside(path, root), 'compiler or driver subprocess escapes provider: ' + name)
        paths.regular_file(path)
        require(os.access(path, os.X_OK), 'native tool is not executable: ' + name)
        require(inventory.sha256_file(path) == row['sha256'], 'native tool hash differs: ' + name)
        binaries[name] = dict(selected_path=selected, path=path, sha256=row['sha256'])
    for name, option in (('cc', 'host-cc'), ('cxx', 'host-cxx')):
        require(options[option] == binaries[name]['selected_path'], 'selector differs from manifest: --' + option)
    for name in ('abi_inputs', 'provenance'):
        for row in manifest[name]:
            paths.regular_file(row['path'])
            require(inventory.sha256_file(row['path']) == row['sha256'], name + ' hash differs: ' + row['path'])
    # Query the same environment used by the producer. Refuse implicit or
    # external driver selections; a temporary PATH must not conceal them.
    env = environment
    def query(driver, *args, input=None):
        return process.capture([driver, *args], env=env, input=input).stdout.decode('utf-8', 'strict').strip()
    compilers = {}
    probe = Path(__file__).with_name('native_probe.c')
    require(probe.read_bytes() == PROBE_CONTENT, 'native language probe bytes differ')
    for name, language in (('cc', 'c'), ('cxx', 'c++')):
        driver = binaries[name]['path']
        version, triplet = query(driver, '-dumpfullversion'), query(driver, '-dumpmachine')
        require(version == manifest['gcc_version'] and triplet == host['triplet'], 'expected native GCC 16.2.0: ' + name)
        for tool in ('cc1plus' if name == 'cxx' else 'cc1', 'collect2', 'lto1', 'lto-wrapper', 'as', 'ld'):
            reported = query(driver, '-print-prog-name=' + tool)
            require(os.path.isabs(reported), 'driver reports an implicit subprocess: ' + tool)
            require(paths.canonical_existing(reported) == binaries[tool]['path'], 'driver selects a different provider: ' + tool)
        # The same .c input exposes each driver's default language. Forcing -x
        # would let a C driver impersonate the required C++ driver.
        require(probe.read_bytes() == PROBE_CONTENT, 'native language probe bytes differ')
        text = query(driver, '-dM', '-E', str(probe))
        require(probe.read_bytes() == PROBE_CONTENT, 'native language probe changed during query')
        macros = dict(re.findall(r'^#define ([A-Za-z_][A-Za-z0-9_]*) (.*)$', text, re.M))
        validate_macros(macros, host)
        require((language == 'c++') == ('__cplusplus' in macros), 'native C/C++ frontend mismatch')
        compilers[name] = dict(version=version, triplet=triplet, macros=macros)
    for row in binaries.values():
        require(paths.canonical_existing(row['selected_path']) == row['path'] and
                inventory.sha256_file(row['path']) == row['sha256'], 'native tool changed during inspection')
    require(inventory.sha256_file(manifest_path) == manifest_file_hash, 'input manifest changed during inspection')
    record = dict(manifest=dict(path=manifest_path, sha256=manifest_file_hash,
                               content_sha256=inventory_hash(manifest), content=manifest), root=root,
                  binaries=binaries, references={n: manifest[n] for n in ('abi_inputs', 'provenance')}, compilers=compilers)
    validate_record(record)
    return record


def host_tools(record):
    return {key: record['binaries'][name] | record['compilers'][name] for key, name in (('cc', 'cc'), ('c++', 'cxx'))}


def validate_binding(signature, selection):
    """Bind embedded identities to canonical producer inputs and native flags."""
    try:
        record = signature['native_compiler']
        validate_record(record)
        require(signature['host_tools'] == host_tools(record), 'mixed native compiler identity')
        expected = {'host-toolchain': record['root'], 'host-toolchain-manifest': record['manifest']['path'],
                    'host-cc': record['binaries']['cc']['path'], 'host-cxx': record['binaries']['cxx']['path']}
        require(all(selection[key] == value for key, value in expected.items()), 'producer paths differ from native identity')
        for name, option in (('HOST_CC', 'host-cc'), ('HOST_CXX', 'host-cxx')):
            require([flag for flag in signature['flags'] if flag.startswith(name + '=')] == [name + '=' + expected[option]],
                    'producer flags differ from native identity')
    except (KeyError, TypeError, AttributeError) as exc:
        raise SDKError('native compiler: missing producer identity') from exc
