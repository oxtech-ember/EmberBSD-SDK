#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
"""Exercise an explicit native C++ pair using the selected Darwin compiler."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk import compiler_bindings, process

p = argparse.ArgumentParser(description=__doc__)
for name in ('prefix', 'runtime-root', 'sdk', 'work'):
    p.add_argument('--' + name, type=Path, required=True)
p.add_argument('--target', action='append', default=[])
p.add_argument('--deployment', required=True)
p.add_argument('--staged', action='store_true', help='load only this test executable from a staged runtime')
p.add_argument('--cwrapper-config', type=Path)
p.add_argument('--cwrapper-executable', type=Path)
a = p.parse_args()
prefix = a.prefix.resolve(strict=True)
root = a.runtime_root.resolve(strict=True)
sdk = a.sdk.resolve(strict=True)
work = a.work.resolve(strict=True)
headers = root / 'include/libc++/v1'
libs = root / 'lib/libc++'
clang = prefix / 'bin/clang'
cxx = prefix / 'bin/clang++'
linker = prefix / 'bin/ld64.lld'
python = prefix / 'bin/python3.15'
base = ['-isysroot', str(sdk), '--start-no-unused-arguments',
        '--ld-path='+str(linker), '--end-no-unused-arguments']
commands = {'cc':[str(clang), *base], 'cxx':[str(cxx), *base], 'cpp':[str(clang), *base, '-E']}
flags = dict(cppflags=[], cflags=[], cxxflags=[], ldflags=[])
runtime = dict(include_dir=str(headers), library_dir=str(libs),
               sysroot=str(sdk), targets=a.target, deployment=a.deployment)
environment = process.sanitized_environment()
environment['PATH'] = str(prefix/'bin') + ':' + str(prefix/'sbin')

def run(command, **kwargs):
    kwargs.setdefault('env', environment)
    return subprocess.run(command, text=True, capture_output=True, check=True, **kwargs)

with tempfile.TemporaryDirectory(prefix='native cxx binding ', dir=work) as temp:
    root_dir = Path(temp)
    tools = compiler_bindings.bind_compilers(root_dir/'binding', commands=commands,
                    flags=flags, python=str(python), native_cxx_runtime=runtime)
    source = root_dir/'consumer.cc'
    source.write_text('#include <string>\n'
        '#if _LIBCPP_HARDENING_MODE_DEFAULT != _LIBCPP_HARDENING_MODE_EXTENSIVE\n'
        '#error wrong libc++ headers\n#endif\n'
        'int main(){std::string s="ok";return s.size()!=2;}\n')
    exe = root_dir/'consumer'
    run([tools['cxx'], str(source), '-o', str(exe)])
    linked = run([str(prefix/'bin/llvm-otool'), '-L', str(exe)]).stdout
    expected = str(prefix/'lib/libc++/libc++.1.dylib')
    assert expected in linked, linked
    env = dict(environment)
    if a.staged:
        env['DYLD_LIBRARY_PATH'] = str(libs)
    run([str(exe)], env=env)
    run([tools['cxx'], '-Wall', '-Werror', '-x', 'c', '-fsyntax-only', '/dev/null'])
    user_include = root_dir/'user'; user_include.mkdir()
    verbose = run([tools['cxx'], '-isystem', str(user_include), '-x', 'c++',
                   '-E', '-v', '/dev/null']).stderr
    search = verbose.split('#include <...> search starts here:', 1)[1].split('End of search list.', 1)[0]
    assert search.index(str(user_include)) < search.index(str(headers)), search
    no_include = run([tools['cxx'], '-nostdinc++', '-Wall', '-Werror', '-x', 'c++',
                      '-E', '-v', '/dev/null']).stderr
    search = no_include.split('#include <...> search starts here:', 1)[1].split('End of search list.', 1)[0]
    assert str(headers) not in search, search
    for override in (['--target=x86_64-unknown-netbsd'], ['--sysroot=/nonexistent-target'],
                     ['-stdlib++-isystem', '/nonexistent'], ['-mmacos-version-min=25.0']):
        result = subprocess.run([tools['cxx'], *override, '-###', str(source)],
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment)
        assert result.returncode != 0, result.stderr
    result = run([tools['cxx'], '-stdlib=libstdc++', '-###', str(source)]).stderr
    assert str(headers) not in result and str(libs) not in result, result
    if a.target:
        result = run([tools['cxx'], '-target', a.target[-1], '-###', str(source)]).stderr
        assert str(headers) in result and str(libs) in result, result
    if bool(a.cwrapper_config) != bool(a.cwrapper_executable):
        raise ValueError('both cwrapper paths are required')
    if a.cwrapper_config:
        config_dir = root_dir/'cwrapper-config'; config_dir.mkdir()
        wrapper_dir = root_dir/'cwrapper-bin'; wrapper_dir.mkdir()
        wrapper = wrapper_dir/'c++'
        wrapper.symlink_to(a.cwrapper_executable.resolve(strict=True))
        lines = a.cwrapper_config.resolve(strict=True).read_text().splitlines()
        result = []
        for line in lines:
            if line.startswith('exec='):
                line = 'exec='+tools['cxx']
            elif line.startswith('worklog='):
                line = 'worklog='+str(root_dir/'cwrapper.log')
            result.append(line)
        (config_dir/'c++').write_text('\n'.join(result)+'\n')
        wrapper_env = dict(environment, CWRAPPERS_CONFIG_DIR=str(config_dir))
        wrapped_exe = root_dir/'wrapped-consumer'
        run([str(wrapper), str(source), '-o', str(wrapped_exe)], env=wrapper_env)
        wrapped_links = run([str(prefix/'bin/llvm-otool'), '-L', str(wrapped_exe)]).stdout
        assert expected in wrapped_links, wrapped_links
        run([str(wrapped_exe)], env=env)
        no_include = run([str(wrapper), '-nostdinc++', '-x', 'c++',
                          '-E', '-v', '/dev/null'], env=wrapper_env).stderr
        search = no_include.split('#include <...> search starts here:', 1)[1].split('End of search list.', 1)[0]
        assert str(headers) not in search, search
        for override in (['--target=x86_64-unknown-netbsd'],
                         ['--sysroot=/nonexistent-target'], ['-stdlib=libstdc++']):
            result = subprocess.run([str(wrapper), *override, '-###', str(source)],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=wrapper_env)
            assert str(headers) not in result.stderr and str(libs) not in result.stderr, result.stderr
print('native C++ runtime binding: compile, load, precedence and cross overrides PASS')
