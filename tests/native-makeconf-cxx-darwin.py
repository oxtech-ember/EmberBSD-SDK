#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
"""Exercise a selected C++ runtime through the Darwin Ports configuration."""
import argparse
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk import process
from emberbsd_sdk.darwin_makeconf import LLVM, REQUIRED, write_makeconf

parser = argparse.ArgumentParser(description=__doc__)
for name in ('prefix', 'base', 'pkgsrc', 'runtime-root', 'cwrapper-config', 'work'):
    parser.add_argument('--' + name, type=Path, required=True)
parser.add_argument('--target', action='append', required=True)
parser.add_argument('--deployment', required=True)
a = parser.parse_args()
prefix = a.prefix.resolve(strict=True)
base = a.base.resolve(strict=True)
conf = {}
for line in (base / 'mk.conf').read_text().splitlines():
    if '= ' in line and not line.startswith(('#', '.')):
        key, value = line.split('= ', 1)
        conf[key.strip()] = value
roles = {key[21:]:value for key, value in conf.items() if key.startswith('EMBERBSD_DARWIN_TOOL.')}
commands = {}
for role in ('cc', 'cxx', 'cpp'):
    words = shlex.split(conf['EMBERBSD_DARWIN_SEED_' + role.upper()])
    roles[role], commands[role] = words[0], words[1:]
for name, role in LLVM.items():
    roles[role] = conf['EMBERBSD_DARWIN_' + name]
for role, name in (('make', 'MAKE'), ('gmake', 'GMAKE'), ('sh', 'SH')):
    roles[role] = shlex.split(conf['EMBERBSD_DARWIN_' + name])[0]
for role in ('pkg_info', 'pkg_admin', 'pkg_add', 'pkg_create'):
    roles[role] = conf['NATIVE_PKG_' + role[4:].upper() + '_CMD']
# Only selected-prefix tools run in this check; other roles stay source inputs.
omitted = {role for role, path in roles.items() if not path.startswith(str(prefix) + '/')}
roles = {role:path for role, path in roles.items() if role not in omitted}
assert REQUIRED <= set(roles)
sdk = conf['OSX_SDK_PATH']
stage = a.runtime_root.resolve(strict=True)
with tempfile.TemporaryDirectory(dir=a.work.resolve(strict=True), prefix='native-makeconf-') as temp:
    work = Path(temp)
    pair = dict(include_dir=str(stage / 'include/libc++/v1'),
                library_dir=str(stage / 'lib/libc++'), sysroot=sdk,
                targets=a.target, deployment=a.deployment)
    result = write_makeconf(pkgsrc=str(a.pkgsrc.resolve(strict=True)), output=str(work / 'mk.conf'),
        prefix=str(prefix), work_root=str(work), distfiles=conf['DISTDIR'], packages=conf['PACKAGES'],
        pkgdb=conf['PKG_DBDIR'], utility_bin=str(work / 'tools'), makefiles=str(base / 'initial-mk'),
        macos_sdk=sdk, deployment=a.deployment, tools=roles, compiler_args=commands,
        tool_roots=[str(prefix)], jobs=1,
        source_tools=list(set(conf['EMBERBSD_DARWIN_SOURCE_TOOLS'].split()) | omitted),
        native_cxx_runtime=pair, native_cxx_bindings=str(work / 'bindings'),
        native_cxx_python=sys.executable)
    line = next(line for line in Path(result['makeconf']).read_text().splitlines()
                if line.startswith('EMBERBSD_DARWIN_SEED_CXX='))
    adapter = shlex.split(line.split('=', 1)[1])[0]
    record = json.loads((work / 'bindings/commands.json').read_text())
    assert record['commands']['cxx']['argv'][-1] == '--target=' + a.target[0]
    driver = subprocess.run([adapter, '-###', '-x', 'c++', '-c', '/dev/null'],
                            env=result['environment'], capture_output=True, text=True, check=True)
    assert a.target[0].split('-', 1)[0] + '-apple-macosx' in driver.stderr, driver.stderr
    query = subprocess.run([roles['make'], '-m', str(base / 'initial-mk'), '-C',
        str(a.pkgsrc / 'math/fast_float'), 'MAKECONF=' + result['makeconf'],
        'EMBERBSD_DARWIN_BOOTSTRAP=yes', '-v', 'CXXPATH'],
        env=result['environment'], capture_output=True, text=True)
    assert query.returncode == 0, query.stderr
    assert query.stdout.strip() == adapter, query.stdout
    record_query = subprocess.run([roles['make'], '-m', str(base / 'initial-mk'), '-C',
        str(a.pkgsrc / 'math/fast_float'), 'MAKECONF=' + result['makeconf'],
        'EMBERBSD_DARWIN_BOOTSTRAP=yes', '-v', 'EMBERBSD_NATIVE_CXX_BINDING_RECORD'],
        env=result['environment'], capture_output=True, text=True, check=True)
    assert record_query.stdout.strip() == str(work / 'bindings/commands.json')
    config = work / 'config'
    config.mkdir()
    wrapper_dir = work / 'bin'
    wrapper_dir.mkdir()
    wrapper = wrapper_dir / 'c++'
    wrapper.symlink_to(prefix / 'libexec/cwrappers/c++-wrapper')
    lines = []
    for line in a.cwrapper_config.resolve(strict=True).read_text().splitlines():
        if line.startswith('exec='):
            line = 'exec=' + adapter
        elif line.startswith('worklog='):
            line = 'worklog=' + str(work / 'log')
        lines.append(line)
    (config / 'c++').write_text('\n'.join(lines) + '\n')
    env = process.sanitized_environment()
    env.update(PATH=str(prefix / 'bin'), CWRAPPERS_CONFIG_DIR=str(config))
    runtime_env = dict(env, DYLD_LIBRARY_PATH=str(stage / 'lib/libc++'))
    source = work / 'a.cc'
    source.write_text('#include <string>\nint main(){return std::string("ok").size()!=2;}\n')
    output = work / 'a'
    subprocess.run([str(wrapper), str(source), '-o', str(output)], env=env, check=True, capture_output=True)
    linked = subprocess.run([str(prefix / 'bin/llvm-otool'), '-L', str(output)],
                            capture_output=True, text=True, check=True).stdout
    assert str(prefix / 'lib/libc++/libc++.1.dylib') in linked
    subprocess.run([str(output)], env=runtime_env, check=True)
    assert subprocess.run([str(wrapper), '--target=x86_64-unknown-netbsd', '-c', str(source)],
                          env=env, capture_output=True).returncode != 0
print('production MAKECONF, pkgsrc wrapper, shared pair compile/load and target rejection PASS')
