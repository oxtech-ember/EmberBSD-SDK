# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Fake native GCC queries. These files never compile or qualify a provider."""

import json
from pathlib import Path
import sys

from emberbsd_sdk import inventory, jsonio, host_compiler


def fixture(root):
    root = Path(root)
    prefix = root / 'native'
    (prefix / 'bin').mkdir(parents=True)
    host = host_compiler.current_host()
    suffix = {'Darwin': 'apple-darwin27.0.0', 'Linux': 'pc-linux-gnu', 'NetBSD': '-netbsd'}[host['system']]
    host['triplet'] = host['machine'] + '-' + suffix
    cpu = {'aarch64': '__aarch64__', 'x86_64': '__x86_64__', 'i386': '__i386__'}[host['machine']]
    macros = {'__GNUC__': '16', '__SIZEOF_POINTER__': str(host['pointer_bits'] // 8),
              '__BYTE_ORDER__': '__ORDER_' + host['byteorder'].upper() + '_ENDIAN__',
              '__ORDER_LITTLE_ENDIAN__': '1234', '__ORDER_BIG_ENDIAN__': '4321', cpu: '1',
              {'Darwin': '__APPLE__', 'Linux': '__linux__', 'NetBSD': '__NetBSD__'}[host['system']]: '1'}
    control = root / 'native-control.json'
    control.write_text('{}')
    script = f'''#!{sys.executable}
import json, sys
from pathlib import Path
c = json.loads(Path({str(control)!r}).read_text())
c = c | c.get('drivers', {{}}).get(Path(sys.argv[0]).name, {{}})
arg = sys.argv[1]
if arg == '--version':
    print(c.get('banner', 'gcc (SDK fixture) 16.2.0'))
    if c.get('stderr_banner'): print(c['stderr_banner'], file=sys.stderr)
elif arg == '-dumpfullversion': print(c.get('version', '16.2.0'))
elif arg == '-dumpmachine': print(c.get('triplet', {host['triplet']!r}))
elif arg.startswith('-print-prog-name='):
    name = arg.split('=', 1)[1]
    print(c.get('programs', {{}}).get(name, {str(prefix / 'bin')!r} + '/' + name))
elif arg == '-dM':
    macros = {macros!r}
    macros.update(c.get('macros', {{}}))
    if sys.argv[1:] != ['-dM', '-E', sys.argv[-1]] or not sys.argv[-1].endswith('.c'):
        raise SystemExit('Expected the shared .c probe without a forced language')
    if Path(sys.argv[0]).name == 'g++': macros['__cplusplus'] = '202002L'
    print('\\n'.join('#define ' + k + ' ' + v for k, v in macros.items()))
else: raise SystemExit('Unexpected native fixture invocation')
'''
    tools = {}
    for name in host_compiler.TOOLS:
        relative = 'bin/' + {'cc': 'gcc', 'cxx': 'g++'}.get(name, name)
        path = prefix / relative
        path.write_text(script if name in ('cc', 'cxx') else '#!/bin/sh\nexit 0\n')
        path.chmod(0o755)
        tools[name] = dict(path=relative, sha256=inventory.sha256_file(path))
    abi = prefix / 'abi-header.h'
    abi.write_text('Fixture host ABI input; no system header qualification.\n')
    evidence = root / 'native-producer.txt'
    evidence.write_text('Fixture provider declaration; no real compiler build.\n')
    value = dict(format=host_compiler.FORMAT, version=1, host=host, gcc_version='16.2.0',
                 producer=dict(repository='https://example.invalid/sdk-producer', revision='a' * 40, recipe='gcc16/native.py'),
                 tools=tools, abi_inputs=[dict(path=str(abi), sha256=inventory.sha256_file(abi))],
                 provenance=[dict(path=str(evidence), sha256=inventory.sha256_file(evidence))])
    manifest = root / 'native-inputs.json'
    jsonio.write_json(manifest, value)
    return {'host-toolchain': str(prefix), 'host-cc': str(prefix / 'bin/gcc'),
            'host-cxx': str(prefix / 'bin/g++'), 'host-toolchain-manifest': str(manifest)}
