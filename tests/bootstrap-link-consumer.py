#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
"""Verify Mach-O IDs by running old and newly linked relocated consumers."""
import argparse
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk.bootstrap_bundle import make_relative
from emberbsd_sdk.darwin_runtime import read_macho_runtime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for role in ('cc', 'install-name-tool', 'codesign'):
        parser.add_argument('--' + role, type=Path, required=True)
    parser.add_argument('--cflag', action='append', default=[])
    args = parser.parse_args()
    for tool in (args.cc, args.install_name_tool, args.codesign):
        if not tool.is_absolute() or not os.access(tool, os.X_OK):
            parser.error('tools must be explicit absolute executable paths')
    if sys.platform != 'darwin':
        parser.error('this check requires Darwin')
    host = {'os': 'Darwin', 'cpu': platform.machine()}
    environment = {key: value for key, value in os.environ.items() if not key.startswith('DYLD_')}
    def run(argv):
        subprocess.run([str(a) for a in argv], check=True, capture_output=True, env=environment)
    with tempfile.TemporaryDirectory(prefix='ember-macho-') as temporary:
        work = Path(temporary).resolve()
        producer, moved = work / 'producer', work / 'relocated'
        (producer / 'bin').mkdir(parents=True)
        (producer / 'lib/nested').mkdir(parents=True)
        lib_source, app_source = work / 'lib.c', work / 'app.c'
        lib_source.write_text('int fixture_value(void) { return 42; }\n')
        app_source.write_text('extern int fixture_value(void); int main(void) { return fixture_value() != 42; }\n')
        cc = [args.cc, *args.cflag]
        for directory, name in (('lib', 'top'), ('lib/nested', 'nested')):
            library = producer / directory / ('lib' + name + '.dylib')
            executable = producer / 'bin' / ('old-' + name)
            run([*cc, '-dynamiclib', lib_source, '-Wl,-install_name,' + str(library), '-o', library])
            run([*cc, app_source, library, '-Wl,-rpath,' + str(producer / directory), '-o', executable])
        changed = make_relative(producer, str(producer), host, args.install_name_tool, args.codesign)
        assert changed, 'expected producer-relative images to change'
        for directory, name in (('lib', 'top'), ('lib/nested', 'nested')):
            library = producer / directory / ('lib' + name + '.dylib')
            assert read_macho_runtime(library, host)['install_name'] == '@rpath/' + library.name
        assert not make_relative(producer, str(producer), host, args.install_name_tool, args.codesign), 'not idempotent'
        producer.rename(moved)
        assert not producer.exists()
        for directory, name in (('lib', 'top'), ('lib/nested', 'nested')):
            library = moved / directory / ('lib' + name + '.dylib')
            run([moved / 'bin' / ('old-' + name)])
            new = moved / 'bin' / ('new-' + name)
            rpath = '@loader_path/../' + directory
            run([*cc, app_source, library, '-Wl,-rpath,' + rpath, '-o', new])
            image = read_macho_runtime(new, host)
            assert any(load['name'] == '@rpath/' + library.name for load in image['loads'])
            assert rpath in image['rpaths']
            run([new])
        print('PASS: existing and newly linked consumers, top-level and nested RPATH; relocation and idempotence')


if __name__ == '__main__':
    main()
