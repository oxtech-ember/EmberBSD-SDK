#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
# Origin: EmberBSD contributors (AI-assisted).
"""Check staged linker scope, three CMake link modes and object reuse.

Pass explicitly selected host tools, an SDK-generated linker adapter and its
runtime view. This uses a disposable source/build tree and no installed prefix.
"""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile

p = argparse.ArgumentParser(description=__doc__)
for name in ('cmake', 'ninja', 'cxx', 'ar', 'ranlib', 'python', 'adapter',
             'runtime-view', 'lock-root', 'link-context'):
    p.add_argument('--'+name, type=Path, required=True)
p.add_argument('--jobs', type=int, required=True)
a = p.parse_args()
if a.jobs < 1:
    p.error('--jobs must be positive')
sdk = Path(__file__).resolve().parents[1]
env = dict(os.environ)
env.pop('DYLD_LIBRARY_PATH', None)
def run(cmd):
    result = subprocess.run([str(x) for x in cmd], text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    if result.returncode:
        raise RuntimeError(result.stdout[-4000:])
    return result.stdout
with tempfile.TemporaryDirectory(prefix='stage-linker-') as temporary:
    root = Path(temporary).resolve(); source = root/'src'; source.mkdir(); build = root/'build'
    (source/'CMakeLists.txt').write_text('''cmake_minimum_required(VERSION 3.22)
project(stage_link LANGUAGES CXX)
add_library(common OBJECT common.cpp)
set_target_properties(common PROPERTIES POSITION_INDEPENDENT_CODE ON)
add_executable(check main.cpp $<TARGET_OBJECTS:common>)
add_library(check_shared SHARED $<TARGET_OBJECTS:common>)
add_library(check_module MODULE $<TARGET_OBJECTS:common>)
''')
    (source/'common.cpp').write_text('extern "C" int stage_answer() { return 42; }\n')
    (source/'main.cpp').write_text('extern "C" int stage_answer(); int main() { return stage_answer() == 42 ? 0 : 1; }\n')
    launcher = ';'.join((str(a.python), str(sdk/'scripts/link-launcher.py'),
        '--lock-root', str(a.lock_root), '--context', str(a.link_context), '--'))
    cmake = [a.cmake, '-S', source, '-B', build, '-G', 'Ninja',
        '-DCMAKE_MAKE_PROGRAM='+str(a.ninja), '-DCMAKE_CXX_COMPILER='+str(a.cxx),
        '-DCMAKE_AR='+str(a.ar), '-DCMAKE_RANLIB='+str(a.ranlib),
        '-DCMAKE_TRY_COMPILE_TARGET_TYPE=STATIC_LIBRARY', '-DCMAKE_CXX_LINKER_LAUNCHER='+launcher]
    run(cmake); run([a.ninja, '-C', build, '-j', str(a.jobs), 'common'])
    before = run([a.ninja, '-C', build, '-t', 'commands', 'common'])
    flags = ['-DCMAKE_'+kind+'_LINKER_FLAGS=--ld-path='+str(a.adapter)
             for kind in ('EXE', 'SHARED', 'MODULE')]
    run(cmake+flags)
    assert before == run([a.ninja, '-C', build, '-t', 'commands', 'common'])
    assert 'no work to do' in run([a.ninja, '-C', build, '-n', 'common'])
    for target in ('check', 'check_shared', 'check_module'):
        assert '--ld-path='+str(a.adapter) in run([a.ninja, '-C', build, '-t', 'commands', target])
    run([a.ninja, '-C', build, '-j', str(a.jobs), 'all'])
    run([a.python, sdk/'scripts/run-darwin-with-runtime.py', '--runtime-view',
         a.runtime_view, '--', build/'check'])
print('PASS: object reuse, executable/shared/module link and selected runtime')
