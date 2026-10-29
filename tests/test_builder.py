# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Dispatch and filesystem contracts. These fixtures do not qualify a sysroot."""

import copy
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import time
from types import MappingProxyType
from unittest.mock import patch
from unittest import skipUnless

from test_core import TemporaryCase, SDK_SOURCE, header, archive, git_environment
from emberbsd_sdk import inventory, jsonio, process
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.sysroot_builder import SysrootBuilder as ProductionBuilder
from emberbsd_sdk.host_prefix import HostPrefix
from emberbsd_sdk.workspace import create
from emberbsd_sdk.libedit import prepare_source
from libedit_fixture import recipe_fixture, installation_source


def fixture_native_selection(builder):
    """Synthetic dispatch input only. This bypass never qualifies native tools."""
    prefix = Path(builder.options['host-cc']).parent
    builder.native_contract = str(Path(builder.options['os']) / 'tools/native-tools/contract')
    builder.native_contract_sha256 = inventory.sha256_file(builder.native_contract)
    tools = {name: str(prefix / name) for name in ('cc', 'cxx', 'cpp', 'as', 'ld', 'python', 'sh', 'cmp', 'make', 'readlink')}
    if 'hostname hostname -' in Path(builder.native_contract).read_text():
        tools['hostname'] = str(prefix / 'libexec/gettext/hostname')
    return HostPrefix(str(prefix), MappingProxyType({'os': 'Darwin', 'cpu': 'arm64'}),
        MappingProxyType({'family': 'gcc', 'version': '16.2.0', 'triplet': 'aarch64-apple-darwin27'}),
        MappingProxyType(tools), MappingProxyType({name: () for name in ('cppflags', 'cflags', 'cxxflags', 'ldflags')}), MappingProxyType({}))


class B(ProductionBuilder):
    """Exercise dispatch mechanisms with explicit synthetic producer admission."""
    load_native_toolset = fixture_native_selection

    def prepare_native_routing(self):
        pass


def write(path, data, mode=0o644):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data); path.chmod(mode)


def git(path, *args):
    return subprocess.check_output(['git', '-C', str(path), *args], stderr=subprocess.STDOUT, env=git_environment()).decode().strip()


def commit(path):
    git(path, 'add', '-f', '.')
    git(path, '-c', 'user.name=SDK fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'Fixture source')


def gitlink(root, name):
    pin = git(root, 'rev-parse', 'HEAD'); (Path(root) / name).mkdir(parents=True)
    git(root, 'update-index', '--add', '--cacheinfo', f'160000,{pin},{name}')
    git(root, '-c', 'user.name=SDK fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'Fixture external pin')
    return pin


def snapshot(root):
    rows = {}
    for directory, dirs, files in os.walk(root):
        dirs[:] = sorted(name for name in dirs if name != '.git')
        for path in [Path(directory), *(Path(directory) / n for n in dirs + files)]:
            info = path.lstat()
            value = os.readlink(path) if path.is_symlink() else hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
            rows[str(path)] = (info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, value)
    return rows


def manifest(work):
    work = Path(work); rows = inventory.snapshot_tree(work / 'stage/usr/pkg/gcc16')
    write(work / 'stage.sha256', ''.join(f'{row["sha256"]}  ./usr/pkg/gcc16/{name}\n' for name, row in rows.items() if row['kind'] == 'file'))
    write(work / 'stage.symlinks', ''.join(f'./usr/pkg/gcc16/{name}\t{row["target"]}\n' for name, row in rows.items() if row['kind'] == 'link'))
    report = json.loads((work / 'report.json').read_text())
    for file, key in (('inputs.txt', 'GCC_RUNTIME_INPUTS_SHA256'), ('stage.sha256', 'GCC_RUNTIME_STAGE_SHA256'), ('stage.symlinks', 'GCC_RUNTIME_LINKS_SHA256')):
        report['metadata'][key] = inventory.sha256_file(work / file)
    write(work / 'report.json', json.dumps(report))


def fixture(root, target='aarch64'):
    root = Path(root); t = B.TARGETS[target]
    for name in ('os', 'ports', 'compiler', 'runtime', 'pkgsrc', 'sdk-source', 'host-binutils'):
        (root / name).mkdir()
    write(root / 'host-binutils/host-bfd.mk', '# Synthetic OS dispatch input, not built BFD.\n')
    sdk, os_source, ports, compiler, work = (root / name for name in ('sdk', 'os', 'ports', 'compiler', 'runtime'))
    create(sdk, target=target); control = root / 'control.json'; write(control, '{}')
    for name in ('include', 'lib', 'libexec/ld.elf_so', 'tools', 'share/mk', 'sys', 'distrib', 'etc'):
        (os_source / name).mkdir(parents=True, exist_ok=True)
    write(os_source / 'tools/native-tools/contract',
          'EmberBSD-native-tools 1\ncc cc HOST_CC\ncmp cmp -\nmake make MAKE\n'
          'readlink readlink -\nsh sh HOST_SH\n')
    for name in ('Makefile', 'libexec/ld.elf_so/Makefile', 'share/mk/bsd.own.mk'):
        write(os_source / name, '# Fixture\n')
    tool = f'''#!{sys.executable}
import json, sys
from pathlib import Path
c = json.loads(Path({str(control)!r}).read_text())
arg = sys.argv[1]
if arg == '-dumpfullversion': print(c.get('version', '16.2.0'))
elif arg == '-dumpmachine': print(c.get('triplet', {t['triplet']!r}))
elif arg == '-v': print('Thread model: ' + c.get('threads', 'posix'), file=sys.stderr)
elif arg.startswith('-print-prog-name='): print({str(compiler)!r} + '/bin/' + arg.split('=', 1)[1])
else: raise SystemExit('Unexpected fixture compiler invocation')
'''
    for name in ('gcc', 'g++', 'cpp', 'ar', 'as', 'ld', 'nm', 'objcopy', 'objdump', 'ranlib', 'readelf', 'strip'):
        write(compiler / 'bin' / (t['triplet'] + '-' + name), tool, 0o755)
    for name in ('cc1', 'cc1plus', 'collect2', 'lto1'):
        write(compiler / 'bin' / name, '#!/bin/sh\nexit 0\n', 0o755)
    for name in ('as', 'ld'):
        (compiler / 'bin' / name).symlink_to(t['triplet'] + '-' + name)
    prefix = work / 'stage/usr/pkg/gcc16'
    for name in ('include/c++/vector', f'include/c++/{t["triplet"]}/bits/c++config.h', 'include/c++/backward/backward_warning.h'):
        write(prefix / name, '// Fixture\n')
    for name in ('crtbegin.o', 'crtbeginS.o', 'crtend.o', 'crtendS.o'):
        write(prefix / f'lib/gcc/{t["triplet"]}/16.2.0' / name, header(t['elf'], 1))
    for name in (f'lib/gcc/{t["triplet"]}/16.2.0/libgcc.a', f'lib/gcc/{t["triplet"]}/16.2.0/libgcc_eh.a', 'lib/libstdc++.a', 'lib/libsupc++.a'):
        write(prefix / name, archive(header(t['elf'], 1)))
    for name in ('libgcc_s.so.1', 'libstdc++.so.7'):
        write(prefix / 'lib' / name, header(t['elf'], 3))
    (prefix / 'lib/libstdc++.so').symlink_to('libstdc++.so.7')
    write(prefix / 'empty', '')
    write(work / 'inputs.txt', 'Fixture provenance\n')
    write(work / 'report.json', json.dumps(dict(metadata=dict(OPSYS='NetBSD', OS_VERSION='11.0', GCC_VERSION='16.2.0', MACHINE_ARCH=t['arch'], GCC_RUNTIME_MACHINE_ARCH=t['arch'], GCC_RUNTIME_TARGET=target, GCC_RUNTIME_TRIPLET=t['triplet'], EMBERBSD_SEED_SOURCE='a' * 40, fixture_only=True))))
    manifest(work)
    verifier = f'''import json, sys
from pathlib import Path
if len(sys.argv) != 7 or sys.argv[1] != 'check' or sys.argv[5] != '--sdk-source': raise SystemExit('Expected normal check dispatch')
c = json.loads(Path({str(control)!r}).read_text())
if c.get('runtime_fail'): raise SystemExit('Injected verifier failure')
print((Path(sys.argv[2]) / 'report.json').read_text())
'''
    write(ports / B.VERIFIER, verifier)
    llvm_source = root / 'llvm-source'
    write(llvm_source / 'llvm-source.json', json.dumps(dict(format='emberbsd-llvm-source',
        version=1, llvm_version='23.1.4', source=dict(archive='llvm-fixture.tar.xz', sha512='a' * 128))))
    write(ports / 'pkgsrc/lang/compiler-rt/files/runtime.py', '''import sys
from pathlib import Path
a=sys.argv[1:]
if 'verify-source' not in a: raise SystemExit('Expected common source verification')
print((Path(a[a.index('--source')+1])/'llvm-source.json').read_text())
''')
    wrapper = f'''#!{sys.executable}
import json, sys
from pathlib import Path
c = json.loads(Path({str(control)!r}).read_text())
dest = Path((Path(__file__).parent / 'fixture-destination').read_text())
if sys.argv[-1] == 'dependall':
    if c.get('loader_fail'): raise SystemExit('Injected loader failure')
    (dest / 'loader-dependall').write_text('done')
elif sys.argv[-1] == 'install':
    if not (dest / 'loader-dependall').is_file(): raise SystemExit('Install before dependall')
    (dest / 'libexec').mkdir(exist_ok=True)
    (dest / 'libexec/ld.elf_so').write_bytes({header(t['elf'], 3)!r})
else: raise SystemExit('Wrong loader dispatch')
'''
    producer = f'''#!{sys.executable}
import json, os, signal, sys, time
from pathlib import Path
c = json.loads(Path({str(control)!r}).read_text())
a = sys.argv[1:]
def value(flag): return a[a.index(flag) + 1]
if a[-2:] != ['tools', 'libs'] or '-u' not in a or '-U' not in a: raise SystemExit('Wrong producer operations')
if value('-m') != {t['machine']!r} or value('-a') != {t['arch']!r}: raise SystemExit('Incorrect target')
if any(os.environ.get(n) for n in ('CC', 'CXX', 'CFLAGS', 'MAKEFLAGS')): raise SystemExit('Inherited target compiler')
dest = Path(value('-D')); (dest / 'producer-argv.json').write_text(json.dumps(a))
if c.get('build_fail'): raise SystemExit('Injected build failure')
if c.get('build_wait'):
    if c.get('build_ignore_child') and os.fork() == 0:
        if c.get('build_new_group'): os.setpgid(0, 0)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        (dest / 'descendant-pid').write_text(str(os.getpid()))
        while True:
            with (Path(value('-O')) / 'fixture-writer.log').open('a') as f: f.write('writing\\n')
            time.sleep(0.05)
    (dest / 'child-pid').write_text(str(os.getpid()))
    if 'build_orphan_exit' in c:
        while not (dest / 'descendant-pid').is_file(): time.sleep(0.01)
        raise SystemExit(c['build_orphan_exit'])
    time.sleep(60)
for name in ('usr/include/stdio.h', 'usr/include/sys/types.h', 'usr/include/math.h', 'usr/include/pthread.h'):
    path = dest / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text('// Fixture')
(dest / 'usr/lib').mkdir(parents=True, exist_ok=True)
for name in ('crt0.o', 'crti.o', 'crtn.o'): (dest / 'usr/lib' / name).write_bytes({header(t['elf'], 1)!r})
for name in ('libc.so', 'libm.so', 'libpthread.so'): (dest / 'usr/lib' / name).write_bytes({header(t['elf'], 3)!r})
if c.get('missing_header'): (dest / 'usr/include/stdio.h').unlink()
if c.get('wrong_elf'): (dest / 'usr/lib/crt0.o').write_bytes({header(999, 1)!r})
for key, target in (('absolute_link', '/outside'), ('escaped_link', '../../control.json'), ('dangling_link', 'missing')):
    if c.get(key): (dest / key).symlink_to(target)
(dest / 'empty-file').write_bytes(b'')
wrapper = Path(value('-T')) / 'bin' / {'nbmake-' + t['machine']!r}
wrapper.parent.mkdir(parents=True, exist_ok=True); wrapper.write_text({wrapper!r}); wrapper.chmod(0o755)
(wrapper.parent / 'fixture-destination').write_text(str(dest))
'''
    producer += installation_source(t['elf'])
    write(os_source / 'fixture-producer.py', producer)
    write(os_source / 'build.sh', '#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' -B ' + shlex.quote(str(os_source / 'fixture-producer.py')) + ' "$@"\n', 0o755)
    shutil.copytree(SDK_SOURCE / 'src', root / 'sdk-source/src', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    recipe_fixture(ports, root / 'libedit.tar.gz')
    for name, package, script in (
        ('libarchive', 'archivers/libarchive', 'scripts/libarchive-source.py'),
        ('libevent', 'devel/libevent', 'scripts/source-inputs/libevent/source.py'),
    ):
        source = root / (name + '-source')
        source.mkdir()
        write(source / 'receipt.json', json.dumps(dict(format='emberbsd-prepared-source',
            version=1, package=package, identity={'fixture': name}, tree_sha256='0' * 64)))
        write(ports / script, 'import sys\nfrom pathlib import Path\na=sys.argv[1:]\n'
              'source=a[a.index("--source")+1] if "--source" in a else a[a.index("verify")+1]\n'
              'print((Path(source)/"receipt.json").read_text())\n')
    for path in (os_source, ports, root / 'sdk-source'):
        git(path, 'init', '-q'); commit(path)
    prepare_source(ports, root / 'libedit.tar.gz', root / 'libedit-source')
    write(root / 'gcc.tar.xz', 'Fixture archive; no extraction')
    native = root / 'host-gcc16'; native.mkdir()
    for name in ('cc', 'cxx', 'cpp', 'as', 'ld', 'cmp', 'readlink'):
        write(native / name, '#!/bin/sh\nexit 0\n', 0o755)
    # The prepared-source preflight asks make for ${SUBDIR} and the makefiles
    # it read; this tree has no subdirectories and no source guards.
    write(native / 'make', '#!/bin/sh\ncase " $* " in *" -V "*) printf "\\n\\n" ;; esac\nexit 0\n', 0o755)
    (native / 'python').symlink_to(os.path.realpath(sys.executable))
    (native / 'sh').symlink_to(os.path.realpath(shutil.which('sh')))
    return {key: str(value) for key, value in ({'compiler-family': 'gcc', 'sdk': sdk, 'os': os_source, 'ports': ports, 'compiler': compiler, 'runtime-work': work, 'gcc-archive': root / 'gcc.tar.xz', 'pkgsrc': root / 'pkgsrc', 'target': target, 'jobs': '2', 'python': sys.executable, 'sdk-source': root / 'sdk-source', 'libedit-source': root / 'libedit-source', 'libarchive-source': root / 'libarchive-source', 'libevent-source': root / 'libevent-source', 'llvm-source': llvm_source, 'host-prefix': native, 'host-binutils': root / 'host-binutils'} | {'host-cc': native / 'cc', 'host-cxx': native / 'cxx'}).items()}


class BuilderTests(TemporaryCase):
    def test_gcc_requires_gettext_and_libxml2_as_a_pair(self):
        for name in ('gettext-source', 'libxml2-source'):
            with self.subTest(name=name), self.assertRaisesRegex(SDKError, 'selected together'):
                B(self.options | {name: '/prepared/source'}).preflight()

    def test_common_source_missing_and_wrong_package_are_refused(self):
        for name in ('libarchive', 'libevent'):
            options = dict(self.options)
            options.pop(name + '-source')
            with self.assertRaisesRegex(SDKError, '--' + name + '-source'):
                B(options).preflight()
            path = Path(self.options[name + '-source']) / 'receipt.json'
            original = path.read_text()
            record = json.loads(original)
            record['package'] = 'other/package'
            path.write_text(json.dumps(record))
            try:
                with self.assertRaisesRegex(SDKError, 'invalid ' + name):
                    B(self.options).preflight()
            finally:
                path.write_text(original)

    def setUp(self):
        super().setUp()
        self.options = fixture(self.root)

    def test_repeated_preflight_retains_source_git_outside_build_path(self):
        builder = B(self.options)
        first = builder.preflight()
        selected_git = builder.git_command
        build_path = builder.environment['PATH']
        self.assertIsNone(shutil.which('git', path=build_path))
        second = builder.preflight()
        self.assertEqual(builder.git_command, selected_git)
        self.assertEqual(builder.environment['PATH'], build_path)
        self.assertEqual(first['sources'], second['sources'])
        self.assertEqual(first['signature'], second['signature'])

    def test_repeated_preflight_rejects_removed_source_git(self):
        selected = self.root / 'source-service/git'
        write(selected, '#!/bin/sh\nexec ' + shlex.quote(shutil.which('git')) + ' "$@"\n', 0o755)
        builder = B(self.options)
        builder.environment['PATH'] = str(selected.parent) + os.pathsep + builder.environment['PATH']
        builder.preflight()
        self.assertEqual(builder.git_command, str(selected))
        selected.unlink()
        with self.assertRaises(SDKError):
            builder.preflight()
        self.assertFalse(Path(builder.paths['destination'], builder.MARKER).exists())

    def test_gcc_selects_common_llvm_sources_independently_of_its_runtime(self):
        builder = B(self.options | {'plan': True})
        result = builder.run()
        self.assertEqual(result['signature']['compiler']['version'], '16.2.0')
        self.assertEqual(result['signature']['llvm_source']['llvm_version'], '23.1.4')
        self.assertIn('EMBER_LLVM_SOURCE=' + self.options['llvm-source'], result['signature']['flags'])
        self.assertTrue(result['signature']['runtime_prefix'].endswith('/usr/pkg/gcc16'))
        self.assertFalse(any(v.startswith('EMBER_EXTERNAL_LLVM_RUNTIME=') for v in result['signature']['flags']))
        missing = self.options.copy(); del missing['llvm-source']
        with self.assertRaisesRegex(SDKError, '--llvm-source'):
            B(missing).preflight()

    def control(self, **changes):
        write(self.root / 'control.json', json.dumps(changes))

    def run_builder(self, **changes):
        return B(self.options | changes).run()

    def test_native_drift_before_output_keeps_contexts_untouched(self):
        builder = B(self.options)
        preflight = builder.preflight
        def changed_after_plan():
            preflight()
            builder.options['host-prefix'] += '-changed'
        with patch.object(builder, 'preflight', changed_after_plan), self.assertRaisesRegex(SDKError, 'native toolset inputs changed'):
            builder.run()
        self.assertFalse(Path(builder.paths['destination'], builder.MARKER).exists())
        for name in ('objects', 'tools'): self.assertFalse(Path(builder.paths[name]).exists())

    def test_native_drift_after_producer_quarantines_restored_context(self):
        builder = B(self.options)
        def drift():
            write(Path(builder.paths['objects']) / 'partial-object', 'possible changed tool output')
            builder.options['host-prefix'] += '-changed'
        with patch.object(builder, 'build_payload', drift), self.assertRaisesRegex(SDKError, 'native toolset inputs changed'):
            builder.run()
        for name in ('objects', 'tools'):
            self.assertTrue(Path(builder.paths[name], builder.QUARANTINE).is_file())
        with self.assertRaisesRegex(SDKError, 'quarantined'):
            B(self.options | dict(plan=True, destination=self.options['sdk'] + '/stages/retry')).run()

    def refusal_unchanged(self, match=None, **changes):
        before = snapshot(self.root)
        with self.assertRaisesRegex((SDKError, OSError), match or '.'):
            self.run_builder(**changes)
        self.assertEqual(snapshot(self.root), before)

    def cli(self, **changes):
        args = []
        for key, value in (self.options | changes).items():
            args.extend(['--' + key, value])
        # The real CLI is covered by native-consumer refusal contracts. This
        # harness preserves existing signal/dispatch tests for the fake producer.
        setup = ('import sys,runpy; '
                 f'sys.path.insert(0,{str(Path(self.options["sdk-source"]) / "src")!r}); '
                 f'sys.path.insert(0,{str(SDK_SOURCE / "tests")!r}); '
                 'from test_builder import fixture_native_selection; '
                 'from emberbsd_sdk.sysroot_builder import SysrootBuilder; '
                 'SysrootBuilder.load_native_toolset=fixture_native_selection; '
                 'SysrootBuilder.prepare_native_routing=lambda self: None; '
                 'sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0],run_name="__main__")')
        return [sys.executable, '-B', '-c', setup, str(SDK_SOURCE / 'scripts/build-sysroot.py'), *args]

    def test_builder_allows_darwin_session_churn_without_weakening_proof(self):
        builder = B(self.options | dict(plan=True))
        builder.run()
        self.assertEqual(builder.process_inspector.empty_snapshot_timeout, 120)

    def test_host_binutils_input_is_required_before_build_writes(self):
        self.refusal_unchanged('--host-binutils', **{'host-binutils': ''})
        (Path(self.options['host-binutils']) / 'host-bfd.mk').unlink()
        self.refusal_unchanged()

    def test_ordinary_builder_plan_preflights_each_run(self):
        original = ProductionBuilder.preflight
        with patch.object(ProductionBuilder, 'preflight', autospec=True,
                          side_effect=original) as preflight:
            first = B(self.options | {'plan': True})
            second = B(self.options | {'plan': True})
            self.assertEqual(first.run()['qualification'], 'not-built')
            self.assertEqual(second.run()['qualification'], 'not-built')
            self.assertEqual(preflight.call_count, 2)
            self.assertEqual([call.args[0] for call in preflight.call_args_list],
                             [first, second])

    def test_read_only_plan_and_exact_native_commands(self):
        before = snapshot(self.root)
        record = self.run_builder(plan=True)
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(record['qualification'], 'not-built')
        self.assertEqual(record['workspace']['target'], 'aarch64')
        self.assertEqual(record['workspace']['triplet'], 'aarch64--netbsd')
        self.assertEqual(record['workspace']['target_status'], 'declared')
        self.assertEqual(record['workspace']['qualification'], 'not-assessed')
        self.assertEqual(record['execution_acceptance'], 'not-run')
        self.assertEqual(record['plan'][0][3], 'check')
        self.assertEqual(record['plan'][1][-2:], ['tools', 'libs'])
        for flag in ('MKCXX=yes', 'MKGCCCMDS=no', 'MKLLVM=no', 'MKLLVMRT=no', 'MKLIBCXX=no'):
            self.assertIn(flag, record['plan'][1])
        self.assertNotIn('MKOBJDIRS=no', record['plan'][1])
        timestamp = git(self.options['os'], 'show', '-s', '--format=%ct', record['sources']['os']['commit']).strip()
        self.assertIn('MKREPRO=yes', record['plan'][1])
        self.assertIn('MKREPRO_TIMESTAMP=' + timestamp, record['plan'][1])
        self.assertIn('EMBER_LIBEDIT_SOURCE=' + self.options['libedit-source'], record['plan'][1])
        self.assertIn('EMBER_HOST_BFD_CONFIG=' + self.options['host-binutils'] + '/host-bfd.mk', record['plan'][1])
        self.assertEqual(record['signature']['libedit']['recipe']['ports_commit'], record['sources']['ports']['commit'])
        self.assertEqual(record['plan'][2][-1], 'dependall'); self.assertEqual(record['plan'][3][-1], 'install')
        self.assertEqual(record['paths']['destination'], self.options['sdk'] + '/sysroot')
        self.assertEqual(record['signature']['python']['api_version'], 1)
        self.assertEqual(record['sources']['sdk']['path'], self.options['sdk-source'])
        self.assertEqual(self.run_builder(plan=True), record)
        self.assertEqual(snapshot(self.root), before)

    def test_wrong_target_cli_refuses_without_writes(self):
        before = snapshot(self.root)
        result = subprocess.run(self.cli(target='amd64'), cwd=self.root, capture_output=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(b'workspace target mismatch', result.stderr)
        self.assertEqual(snapshot(self.root), before)

    def test_legacy_cli_refuses_without_migration(self):
        (Path(self.options['sdk']) / '.prebuild-sdk.json').unlink()
        before = snapshot(self.root)
        result = subprocess.run(self.cli(), cwd=self.root, capture_output=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(b'workspace target is unknown', result.stderr)
        self.assertEqual(snapshot(self.root), before)

    def test_actual_subprocess_and_output_inspection(self):
        with patch.dict(os.environ, CC='clang', CXX='clang++', CFLAGS='bad', MAKEFLAGS='bad'):
            record = self.run_builder()
        self.assertEqual(record['qualification'], 'built-and-inspected')
        commands = [r for r in record['commands'] if r.get('scope') != 'runtime-check']
        self.assertEqual(len(commands), 3); self.assertTrue(all(r['exit_status'] == 0 for r in commands))
        self.assertEqual(sum(r.get('scope') == 'runtime-check' for r in record['commands']), 3)
        self.assertFalse((Path(self.options['sdk']) / 'sysroot' / B.MARKER).exists())
        self.assertEqual(record['outputs']['usr/pkg/gcc16/lib/libstdc++.so']['kind'], 'link')
        self.assertEqual(record['outputs']['empty-file']['size'], 0)
        self.assertNotIn('var/db/pkg', record['outputs'])

    def test_directory_modes_survive_restrictive_umask(self):
        prefix = Path(self.options['runtime-work']) / 'stage/usr/pkg/gcc16'
        (prefix / 'include').chmod(0o775); (prefix / 'include/c++/backward').chmod(0o555)
        old = os.umask(0o077)
        try:
            result = self.run_builder()
            self.assertEqual(result['outputs']['usr/pkg/gcc16/include']['mode'], 0o775)
            self.assertEqual(result['outputs']['usr/pkg/gcc16/include/c++/backward']['mode'], 0o555)
            self.assertEqual(result['outputs']['usr/pkg/gcc16']['mode'], prefix.stat().st_mode & 0o7777)
        finally:
            os.umask(old); (prefix / 'include/c++/backward').chmod(0o755)
            copied = Path(self.options['sdk']) / 'sysroot/usr/pkg/gcc16/include/c++/backward'
            if copied.exists(): copied.chmod(0o755)

    def test_explicit_alias_stage_is_canonical(self):
        alias = self.root / 'prebuild-sdk'; alias.symlink_to(self.options['sdk'])
        record = self.run_builder(sdk=str(alias), destination=str(alias / 'stages/next'), plan=True)
        self.assertEqual(record['paths']['destination'], self.options['sdk'] + '/stages/next')

    def test_amd64_dispatch(self):
        other = self.root / 'amd64'; other.mkdir()
        record = B(fixture(other, 'amd64')).run()
        self.assertEqual(record['outputs']['usr/lib/crt0.o']['elf']['machine'], 62)

    def test_nonempty_active_destination(self):
        write(Path(self.options['sdk']) / 'sysroot/active', 'preserve')
        self.refusal_unchanged('new or empty')

    def test_nested_active_destination_alias_and_empty(self):
        self.refusal_unchanged('must not be below', destination=self.options['sdk'] + '/sysroot/next', plan=True)
        write(Path(self.options['sdk']) / 'sysroot/active', 'preserve')
        alias = self.root / 'prebuild-sdk'; alias.symlink_to(self.options['sdk'])
        for sdk in (self.options['sdk'], str(alias)):
            self.refusal_unchanged('must not be below', sdk=sdk, destination=sdk + '/sysroot/next')

    def test_case_variant_active_sysroot_refused_without_writes(self):
        sdk = Path(self.options['sdk']); alias = sdk.with_name('SDK')
        if not alias.exists() or not os.path.samefile(sdk, alias):
            self.skipTest('filesystem does not expose case aliases')
        self.refusal_unchanged('must not be below', destination=str(alias / 'SYSROOT/next'), plan=True)

    def test_sibling_stage_preserves_active(self):
        write(Path(self.options['sdk']) / 'sysroot/active', 'preserve')
        before = snapshot(self.root)
        self.assertEqual(self.run_builder(destination=self.options['sdk'] + '/stages/next', plan=True)['qualification'], 'not-built')
        self.assertEqual(snapshot(self.root), before)

    def test_runtime_metadata_mismatch(self):
        path = Path(self.options['runtime-work']) / 'report.json'; report = json.loads(path.read_text())
        report['metadata']['OPSYS'] = 'Linux'; write(path, json.dumps(report))
        self.refusal_unchanged('runtime target metadata mismatch')

    def test_actual_runtime_bytes_match_audited_manifest(self):
        write(Path(self.options['runtime-work']) / 'stage/usr/pkg/gcc16/include/c++/vector', 'changed')
        self.refusal_unchanged('does not match')

    def test_runtime_receipt_hashes_enforced(self):
        path = Path(self.options['runtime-work']) / 'stage.sha256'; path.write_text(path.read_text() + 'extra')
        self.refusal_unchanged('runtime receipt changed')

    def test_missing_developer_crt(self):
        (Path(self.options['runtime-work']) / 'stage/usr/pkg/gcc16/lib/gcc/aarch64--netbsd/16.2.0/crtbegin.o').unlink()
        manifest(self.options['runtime-work']); self.refusal_unchanged()

    def test_assume_unchanged_source(self):
        root = Path(self.options['os']); write(root / 'external/fixture.c', '/* Fixture. */'); commit(root)
        git(root, 'update-index', '--assume-unchanged', 'external/fixture.c'); (root / 'external/fixture.c').unlink()
        self.assertFalse(git(root, 'status', '--porcelain=v1'))
        self.refusal_unchanged('must not hide tracked paths')

    def test_unused_pkgsrc_gitlink_records_pin_without_checkout(self):
        pin = gitlink(self.options['ports'], 'upstream/pkgsrc')
        updated = self.root / 'libedit-with-gitlink'
        prepare_source(self.options['ports'], self.root / 'libedit.tar.gz', updated)
        self.options['libedit-source'] = str(updated)
        before = snapshot(self.root)
        record = self.run_builder(plan=True)
        self.assertEqual(record['sources']['ports']['external_gitlinks'], [dict(path='upstream/pkgsrc', commit=pin, usage='not-used-by-selected-producer', verification='explicit-pkgsrc-verified-separately', qualification='not-assessed')])
        self.assertFalse(list((Path(self.options['ports']) / 'upstream/pkgsrc').iterdir()))
        self.assertEqual(snapshot(self.root), before)

    def test_installed_libedit_is_required_before_acceptance(self):
        for failure in ('missing_libedit', 'missing_histedit', 'missing_readline', 'missing_editline_marker',
                        'wrong_editline_marker', 'wrong_libedit_soname', 'extra_libedit_provider'):
            with self.subTest(failure=failure):
                root = self.root / failure; root.mkdir()
                options = fixture(root)
                write(root / 'control.json', json.dumps({failure: True}))
                with self.assertRaises((SDKError, OSError)):
                    B(options).run()
                record = jsonio.read_json(root / 'sdk/sysroot' / B.RECEIPT)
                self.assertEqual(record['qualification'], 'incomplete')
                self.assertTrue((root / 'sdk/sysroot' / B.MARKER).exists())

    def test_libedit_tamper_refuses_before_writes(self):
        (Path(self.options['libedit-source']) / 'src/input0').write_text('changed')
        self.refusal_unchanged('libedit source tree changed')

    def test_libedit_replacement_invalidates_object_reuse(self):
        self.run_builder()
        updated = self.root / 'libedit-replacement'
        prepare_source(self.options['ports'], self.root / 'libedit.tar.gz', updated)
        self.refusal_unchanged('incompatible', **{'libedit-source': str(updated), 'destination': str(self.root / 'sdk/stages/next')})

    def test_libedit_tamper_during_producer_leaves_incomplete_receipt(self):
        original = B.build_payload
        def changed(builder):
            original(builder)
            (Path(builder.paths['libedit-source']) / 'src/input0').write_text('changed')
        with patch.object(B, 'build_payload', changed):
            with self.assertRaisesRegex(SDKError, 'libedit source tree changed'):
                self.run_builder()
        receipt = jsonio.read_json(self.root / 'sdk/sysroot' / B.RECEIPT)
        self.assertEqual(receipt['qualification'], 'incomplete')
        self.assertTrue((self.root / 'sdk/sysroot' / B.MARKER).exists())

    def test_missing_source_subtree(self):
        (Path(self.options['os']) / 'lib').rmdir(); self.refusal_unchanged('real directory')

    def test_unknown_object_cache_unchanged(self):
        plan = self.run_builder(plan=True); write(Path(plan['paths']['objects']) / 'foreign', 'preserve')
        self.refusal_unchanged()

    def test_reuse_context_and_fast_forward_source(self):
        first = self.run_builder(); write(Path(self.options['os']) / 'revision', 'next'); commit(self.options['os'])
        changes = dict(destination=self.options['sdk'] + '/stages/next')
        plan = self.run_builder(**changes, plan=True)
        self.assertEqual(plan['previous_contexts']['objects']['sources']['os']['commit'], first['sources']['os']['commit'])
        self.assertNotEqual(plan['sources']['os']['commit'], first['sources']['os']['commit'])
        record = self.run_builder(**changes); self.assertEqual(record['qualification'], 'built-and-inspected')
        self.assertEqual(record['paths']['objects'], first['paths']['objects'])

    def test_reuse_after_controller_only_update_keeps_object_inputs(self):
        first = self.run_builder()
        write(Path(self.options['sdk-source']) / 'controller-update', 'new controller revision')
        commit(self.options['sdk-source'])
        original = B.python_identity
        def updated(builder):
            result = original(builder)
            result['modules']['controller_fixture.py'] = '1' * 64
            return result
        with patch.object(B, 'python_identity', updated):
            result = self.run_builder(destination=self.options['sdk'] + '/stages/next')
        self.assertEqual(result['qualification'], 'built-and-inspected')
        self.assertEqual(result['paths']['objects'], first['paths']['objects'])
        self.assertIn('controller_fixture.py', result['signature']['python']['modules'])

    def test_new_native_utility_reuses_objects_and_preserves_old_mapping(self):
        with patch.object(B, 'prepare_native_routing', ProductionBuilder.prepare_native_routing):
            first = self.run_builder()
            routing = Path(first['paths']['native-routing'])
            before = snapshot(routing)
            contract = Path(self.options['os']) / 'tools/native-tools/contract'
            write(contract, contract.read_text() + 'hostname hostname -\n')
            tool = Path(self.options['host-prefix']) / 'libexec/gettext/hostname'
            write(tool, '#!/bin/sh\nexit 0\n', 0o755)
            commit(self.options['os'])
            result = self.run_builder(destination=self.options['sdk'] + '/stages/next')
        self.assertEqual(result['qualification'], 'built-and-inspected')
        self.assertEqual(result['paths']['objects'], first['paths']['objects'])
        self.assertEqual(snapshot(routing), before)
        current = Path(result['paths']['native-routing'])
        self.assertNotEqual(current, routing)
        self.assertEqual(os.readlink(current / 'hostname'), str(tool))
        self.assertEqual((current / 'contract').read_bytes(), contract.read_bytes())

    def drop_promised_blob(self, spec):
        """Make the OS checkout a partial clone that lacks one blob."""
        os_source = Path(self.options['os'])
        oid = git(os_source, 'rev-parse', spec)
        promisor = self.root / 'os-promisor.git'
        git(self.root, 'clone', '-q', '--bare', '--no-local', str(os_source), str(promisor))
        for key, value in (('core.repositoryformatversion', '1'), ('remote.origin.url', promisor.as_uri()),
                           ('remote.origin.promisor', 'true'), ('extensions.partialClone', 'origin')):
            git(os_source, 'config', key, value)
        (os_source / '.git/objects' / oid[:2] / oid[2:]).unlink()
        with self.assertRaises(subprocess.CalledProcessError):
            git(os_source, 'cat-file', '-e', oid)
        return oid

    def test_partial_os_checkout_reuses_objects_without_fetching_the_old_contract(self):
        with patch.object(B, 'prepare_native_routing', ProductionBuilder.prepare_native_routing):
            first = self.run_builder()
            contract = Path(self.options['os']) / 'tools/native-tools/contract'
            write(contract, contract.read_text() + 'hostname hostname -\n')
            write(Path(self.options['host-prefix']) / 'libexec/gettext/hostname', '#!/bin/sh\nexit 0\n', 0o755)
            commit(self.options['os'])
            # The old contract exists only in the promisor remote. The SDK
            # disables lazy fetching and reads the routing tree of the objects.
            missing = self.drop_promised_blob('HEAD~1:tools/native-tools/contract')
            result = self.run_builder(destination=self.options['sdk'] + '/stages/next')
        self.assertEqual(result['qualification'], 'built-and-inspected')
        self.assertEqual(result['paths']['objects'], first['paths']['objects'])
        with self.assertRaises(subprocess.CalledProcessError):
            git(self.options['os'], 'cat-file', '-e', missing)

    def test_former_routing_name_without_local_contract_fails_clearly(self):
        builder = B(self.options)
        builder.run()
        path = builder.paths['objects']
        record = jsonio.read_json(Path(path) / builder.CONTEXT)
        contract = Path(self.options['os']) / 'tools/native-tools/contract'
        write(contract, contract.read_text() + 'hostname hostname -\n')
        commit(self.options['os'])
        self.drop_promised_blob('HEAD~1:tools/native-tools/contract')
        legacy = copy.deepcopy(record)
        legacy['signature']['environment']['EMBER_NATIVE_TOOLS'] = str(Path(builder.paths['tools']) / 'native-routing')
        with self.assertRaisesRegex(SDKError, 'lacks the native-tool contract of the reused objects'):
            builder.compatible_context(legacy, path)

    def test_context_keeps_compiler_abi_and_existing_tool_identity(self):
        builder = B(self.options)
        result = builder.run()
        path = builder.paths['objects']
        record = jsonio.read_json(Path(path) / builder.CONTEXT)
        mutations = (
            lambda s: s['flags'].append('CPPFLAGS=-DCHANGED'),
            lambda s: s['compiler'].update(version='different'),
            lambda s: s.update(compiler_family='clang'),
            lambda s: s['target'].update(arch='different'),
            lambda s: s['native_toolset']['abi_flags'].update(cflags=['-m32']),
            lambda s: s['native_toolset']['tools'].update(cmp='/different/cmp'),
            lambda s: s['native_toolset']['tools'].pop('cmp'),
            lambda s: s['environment'].update(PATH=':'.join(reversed(s['environment']['PATH'].split(':')))),
            lambda s: s['python'].update(sha256='0' * 64))
        for index, mutate in enumerate(mutations):
            with self.subTest(mutation=index):
                changed = copy.deepcopy(result['signature'])
                mutate(changed)
                with patch.object(builder, 'context_signature', return_value=changed):
                    self.assertFalse(builder.compatible_context(record, path))
        # Migrate the former fixed routing name without rewriting its mapping.
        legacy = copy.deepcopy(record)
        legacy['sources']['os']['path'] = '/former/source-checkout'
        legacy['signature']['environment']['EMBER_NATIVE_TOOLS'] = str(Path(builder.paths['tools']) / 'native-routing')
        self.assertTrue(builder.compatible_context(legacy, path))

    def test_context_from_os_revision_without_native_contract_is_incompatible(self):
        builder = B(self.options)
        builder.run()
        path = builder.paths['objects']
        record = jsonio.read_json(Path(path) / builder.CONTEXT)
        contract = Path(self.options['os']) / 'tools/native-tools/contract'
        content = contract.read_text()
        git(self.options['os'], 'rm', '-q', 'tools/native-tools/contract')
        commit(self.options['os'])
        without = git(self.options['os'], 'rev-parse', 'HEAD')
        write(contract, content)
        commit(self.options['os'])
        # An older OS revision has no rows to compare with the selected
        # contract; reuse is refused instead of failing in git show.
        legacy = copy.deepcopy(record)
        legacy['sources']['os']['commit'] = without
        legacy['signature']['environment']['EMBER_NATIVE_TOOLS'] = str(Path(builder.paths['tools']) / 'native-routing')
        self.assertFalse(builder.compatible_context(legacy, path))

    def test_native_contract_rebinding_or_new_compiler_binding_refuses_reuse(self):
        self.run_builder()
        contract = Path(self.options['os']) / 'tools/native-tools/contract'
        original = contract.read_text()
        for content in (original.replace('cmp cmp -', 'cmp sh -'),
                        original + 'c++ cxx HOST_CXX\n'):
            with self.subTest(contract=content):
                write(contract, content)
                commit(self.options['os'])
                self.refusal_unchanged('incompatible object context',
                    destination=self.options['sdk'] + '/stages/next')

    def test_incompatible_compiler_cache(self):
        self.run_builder(); path = Path(self.options['compiler']) / 'bin/aarch64--netbsd-gcc'
        path.write_text(path.read_text() + '# changed\n')
        self.refusal_unchanged('incompatible object context', destination=self.options['sdk'] + '/next')

    def test_non_ancestor_os_source(self):
        self.run_builder(); git(self.options['os'], 'checkout', '--orphan', 'other')
        # An identical root tree/message/timestamp can recreate the original commit.
        write(Path(self.options['os']) / 'unrelated-root', 'independent history\n')
        commit(self.options['os'])
        self.refusal_unchanged('command failed', destination=self.options['sdk'] + '/next')

    def test_output_alias_escape_and_overlap_refused(self):
        alias = Path(self.options['sdk']) / 'alias'; alias.symlink_to(self.options['sdk'] + '/sysroot')
        self.refusal_unchanged('not a real directory', destination=str(alias))
        alias = Path(self.options['sdk']) / 'obj/alias'; alias.symlink_to(self.options['sdk'] + '/packages')
        self.refusal_unchanged('not a real directory', objects=str(alias))
        self.refusal_unchanged('output must be inside', destination=str(self.root / 'outside'))
        self.refusal_unchanged('overlapping', destination=self.options['sdk'] + '/obj/new')
        self.refusal_unchanged('overlapping', objects=self.options['sdk'] + '/obj/shared', tools=self.options['sdk'] + '/obj/shared')

    def test_copy_aba_compares_precopy_inventory(self):
        original = shutil.copyfileobj
        target = Path(self.options['runtime-work']) / 'stage/usr/pkg/gcc16/include/c++/vector'; touched = []
        def corrupt(input_file, output, *args):
            if input_file.name == str(target):
                old = target.read_bytes(); target.write_bytes(b'corrupt during copy')
                try: original(input_file, output, *args)
                finally: target.write_bytes(old)
                touched.append(True)
            else: original(input_file, output, *args)
        with patch.object(shutil, 'copyfileobj', corrupt), self.assertRaisesRegex(SDKError, 'copied runtime differs'):
            self.run_builder()
        self.assertTrue(touched)

    def test_required_elf_type_and_truncation(self):
        path = Path(self.options['runtime-work']) / 'stage/usr/pkg/gcc16/lib/gcc/aarch64--netbsd/16.2.0/crtbegin.o'
        for data, error in ((header(kind=3), 'wrong ELF type'), (b'\x7fELF', 'ELF64 little-endian')):
            write(path, data); manifest(self.options['runtime-work']); self.refusal_unchanged(error)

    def test_archive_machine_format_truncation(self):
        path = Path(self.options['runtime-work']) / 'stage/usr/pkg/gcc16/lib/libstdc++.a'
        for data, error in ((archive(header(62, 1)), 'ELF64 little-endian'), (archive(b'text'), 'non-ELF archive member'), (archive(header(kind=1))[:-1], 'truncated archive member'), (b'!<thin>\n', 'normal static archive')):
            write(path, data); manifest(self.options['runtime-work']); self.refusal_unchanged(error)

    def test_busy_and_quarantined_contexts(self):
        plan = self.run_builder(plan=True)
        with process.ContextLocks([plan['paths']['objects']], Path(self.options['sdk']) / 'obj'):
            self.refusal_unchanged('context is busy')
        record = self.run_builder(); write(Path(record['paths']['objects']) / B.QUARANTINE, '{}')
        self.refusal_unchanged('quarantined', destination=self.options['sdk'] + '/next', plan=True)

    def test_shared_binutils_aliases_pinned(self):
        shared = self.root / 'shared-binutils'; shared.mkdir()
        for name in ('ar', 'as', 'ld', 'nm', 'objcopy', 'objdump', 'ranlib', 'readelf', 'strip'):
            alias = Path(self.options['compiler']) / 'bin' / ('aarch64--netbsd-' + name)
            provider = shared / name; alias.rename(provider); alias.symlink_to(provider)
        record = self.run_builder(); binaries = record['signature']['compiler']['binaries']
        self.assertEqual(binaries['as']['path'], str(shared / 'as'))
        self.assertEqual(binaries['resolved-as']['path'], binaries['as']['path'])
        self.assertEqual(binaries['as']['selected_path'], self.options['compiler'] + '/bin/aarch64--netbsd-as')
        path = shared / 'as'; path.write_text(path.read_text() + '# changed provider\n')
        self.refusal_unchanged('incompatible object context', destination=self.options['sdk'] + '/next')

    def test_external_frontend_and_split_binutils_refused(self):
        frontend = Path(self.options['compiler']) / 'bin/aarch64--netbsd-gcc'; other = self.root / 'gcc'
        frontend.rename(other); frontend.symlink_to(other); self.refusal_unchanged('frontend escapes prefix')
        frontend.unlink(); other.rename(frontend)
        alias = Path(self.options['compiler']) / 'bin/as'; alias.unlink(); write(alias, '#!/bin/sh\nexit 0\n', 0o755)
        self.refusal_unchanged('different Binutils providers')

    def test_cli_plan_repeat_read_only_and_usage(self):
        before = snapshot(self.root)
        for _ in range(2):
            result = subprocess.run(self.cli() + ['--plan'], cwd='/', capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['qualification'], 'not-built')
            self.assertEqual(snapshot(self.root), before)
        result = subprocess.run(self.cli() + ['--jobs', '2', '--plan'], capture_output=True)
        self.assertEqual(result.returncode, 2); self.assertIn(b'duplicate --jobs', result.stderr)

    def wait_file(self, path, timeout=30):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if path.is_file() and path.stat().st_size: return int(path.read_text())
            time.sleep(0.05)
        self.fail(f'producer did not create {path}')

    def assert_gone(self, pid):
        try: os.kill(pid, 0)
        except ProcessLookupError: return
        self.fail(f'owned producer still exists: {pid}')

    def signal_case(self, descendant, new_group=False, sent_signal=signal.SIGTERM):
        self.control(build_wait=True, build_ignore_child=descendant, build_new_group=new_group)
        log = self.root / 'signal.log'
        with log.open('wb') as output:
            child = subprocess.Popen(self.cli(), stdout=output, stderr=output)
            self.addCleanup(lambda: child.poll() is None and (child.kill(), child.wait()))
            destination = Path(self.options['sdk']) / 'sysroot'
            producer = self.wait_file(destination / ('descendant-pid' if descendant else 'child-pid'))
            if new_group:
                leader = self.wait_file(destination / 'child-pid')
                self.assertEqual(os.getsid(producer), os.getsid(leader))
                self.assertNotEqual(os.getsid(producer), os.getsid(child.pid))
                self.assertEqual(os.getpgid(producer), producer)
            child.send_signal(sent_signal)
            if descendant:
                # The writer ignores TERM, so the caller must retain its locks
                # while waiting to escalate to KILL.
                time.sleep(0.1)
                plan = json.loads((destination / B.RECEIPT).read_text())
                with self.assertRaisesRegex(SDKError, 'context is busy'):
                    process.ContextLocks([plan['paths']['objects']], Path(self.options['sdk']) / 'obj').acquire()
            self.assertEqual(child.wait(timeout=20), 130, log.read_bytes())
            self.assert_gone(producer)
        record = json.loads((destination / B.RECEIPT).read_text())
        self.assertEqual(record['qualification'], 'incomplete'); self.assertIn(signal.Signals(sent_signal).name.removeprefix('SIG'), record['error'])
        self.assertTrue((destination / B.MARKER).is_file())
        with process.ContextLocks([record['paths']['objects']], Path(self.options['sdk']) / 'obj'):
            pass
        if descendant:
            writer = Path(record['paths']['objects']) / 'fixture-writer.log'; before = writer.read_bytes(); time.sleep(0.2)
            self.assertEqual(writer.read_bytes(), before)

    def test_term_stops_child_before_lock_release(self): self.signal_case(False)
    def test_term_drains_ignoring_descendant(self): self.signal_case(True)
    def test_term_drains_ignoring_new_group(self): self.signal_case(True, new_group=True)
    def test_hup_drains_ignoring_new_group(self): self.signal_case(True, new_group=True, sent_signal=signal.SIGHUP)

    def exited_producer_case(self, new_group):
        for status in (7, 0):
            self.control(build_wait=True, build_ignore_child=True, build_new_group=new_group, build_orphan_exit=status)
            destination = Path(self.options['sdk']) / f'stage-{status}'
            with self.assertRaisesRegex(SDKError, 'producer left live descendants'):
                self.run_builder(destination=str(destination))
            descendant = int((destination / 'descendant-pid').read_text()); self.assert_gone(descendant)
            record = json.loads((destination / B.RECEIPT).read_text()); self.assertEqual(record['qualification'], 'incomplete')
            writer = Path(record['paths']['objects']) / 'fixture-writer.log'; before = writer.read_bytes(); time.sleep(0.2)
            self.assertEqual(writer.read_bytes(), before)

    def test_exited_producer_cannot_leave_writer(self): self.exited_producer_case(False)
    def test_exited_producer_cannot_leave_new_group_writer(self): self.exited_producer_case(True)

    def test_quarantine_when_group_cannot_drain(self):
        # Causal control: without a failed stop, accepted fixtures have no quarantine.
        self.control(build_wait=True, build_ignore_child=True, build_orphan_exit=0)
        original = process.OwnedProcessGroup.stop
        def refused(group):
            original(group)  # Stop real fixture writers, then simulate an unprovable drain.
            raise SDKError('injected drain failure')
        with patch.object(process.OwnedProcessGroup, 'stop', refused), self.assertRaisesRegex(SDKError, 'quarantined'):
            self.run_builder()
        record = json.loads((Path(self.options['sdk']) / 'sysroot' / B.RECEIPT).read_text())
        for name in ('objects', 'tools'):
            self.assertTrue((Path(record['paths'][name]) / B.QUARANTINE).is_file())

    def inspection_failure_quarantines(self, inject):
        from emberbsd_sdk.errors import ProcessOwnershipError
        original_start = process.OwnedProcessGroup.start
        original_members = process.SessionInspector.members
        started = []
        def start(group):
            pid = original_start(group)
            started.append(group)
            return pid
        def members(inspector, sid):
            if sid in [group.pid for group in started]:
                return inject(inspector, sid)
            return original_members(inspector, sid)
        try:
            with patch.object(process.OwnedProcessGroup, 'start', start), patch.object(process.SessionInspector, 'members', members):
                with self.assertRaises(ProcessOwnershipError): self.run_builder()
        finally:
            # The fixture producer is bounded. Drain it using the unpatched backend.
            inspector = process.SessionInspector(env=process.sanitized_environment())
            for group in started:
                group.stop()
                self.assertFalse(inspector.members(group.pid))
        receipt = json.loads((Path(self.options['sdk']) / 'sysroot' / B.RECEIPT).read_text())
        for name in ('objects', 'tools'):
            self.assertTrue((Path(receipt['paths'][name]) / B.QUARANTINE).is_file())
        self.refusal_unchanged('quarantined', destination=self.options['sdk'] + '/next', plan=True)

    def test_inspection_failure_quarantines_started_contexts(self):
        def inject(inspector, sid):
            raise SDKError('injected active session inspection failure')
        self.inspection_failure_quarantines(inject)

    @skipUnless(sys.platform == 'darwin', 'Darwin native process profile')
    def test_foreign_getsid_denied_quarantines_started_contexts(self):
        def inject(inspector, sid):
            rows = {2147000000: (1, inspector.uid + 1, b'R')}
            with patch.object(inspector, '_rows', return_value=rows), patch.object(process.os, 'getsid', side_effect=PermissionError('injected foreign inspection refusal')):
                return inspector._scan_darwin(sid)
        self.inspection_failure_quarantines(inject)


def compiler_refusal(key, bad):
    def test(self):
        self.control(**{key: bad}); self.refusal_unchanged('expected GCC')
    return test


def dirty_source_case(name):
    def test(self):
        write(Path(self.options[name]) / 'untracked', 'dirty'); self.refusal_unchanged('clean Git root')
    return test


def source_flag_case(name, flag):
    def test(self):
        root = Path(self.options[name]); write(root / 'external/fixture.c', '/* Fixture. */'); commit(root)
        if flag == 'sparse':
            git(root, 'sparse-checkout', 'set', '--no-cone', '/*', '!/external/')
            error = 'sparse checkout is not a complete source tree'
        else:
            git(root, 'update-index', '--skip-worktree', 'external/fixture.c'); (root / 'external/fixture.c').unlink(); error = 'must not hide tracked paths'
        self.assertFalse((root / 'external/fixture.c').exists()); self.assertFalse(git(root, 'status', '--porcelain=v1'))
        self.refusal_unchanged(error)
    return test


def unexpected_gitlink_case(name, path):
    def test(self):
        gitlink(self.options[name], path); self.refusal_unchanged('unexpected source gitlink')
    return test


def hidden_gitlink_case(flag):
    def test(self):
        gitlink(self.options['ports'], 'upstream/pkgsrc')
        git(self.options['ports'], 'update-index', '--' + flag, 'upstream/pkgsrc'); self.refusal_unchanged('must not hide tracked paths')
    return test


def incomplete_case(failure):
    def test(self):
        self.control(**{failure: True})
        with self.assertRaises((SDKError, OSError)): self.run_builder()
        destination = Path(self.options['sdk']) / 'sysroot'
        record = json.loads((destination / B.RECEIPT).read_text())
        self.assertEqual(record['qualification'], 'incomplete'); self.assertTrue(record['error'])
        self.assertTrue((destination / B.MARKER).is_file()); self.assertTrue((destination / 'producer-argv.json').is_file())
        self.refusal_unchanged('new or empty')
    return test


for _key, _bad in (('version', '15.2.0'), ('triplet', 'other'), ('threads', 'none')):
    setattr(BuilderTests, 'test_compiler_' + _key, compiler_refusal(_key, _bad))
for _name in ('os', 'ports', 'sdk-source'):
    setattr(BuilderTests, 'test_dirty_' + _name.replace('-', '_'), dirty_source_case(_name))
for _name in ('os', 'ports'):
    for _flag in ('sparse', 'skip-worktree'):
        setattr(BuilderTests, 'test_source_' + _name + '_' + _flag.replace('-', '_'), source_flag_case(_name, _flag))
for _name, _path in (('os', 'upstream/pkgsrc'), ('ports', 'external/unused')):
    setattr(BuilderTests, 'test_unexpected_gitlink_' + _name, unexpected_gitlink_case(_name, _path))
for _flag in ('skip-worktree', 'assume-unchanged'):
    setattr(BuilderTests, 'test_hidden_gitlink_' + _flag.replace('-', '_'), hidden_gitlink_case(_flag))
for _failure in ('build_fail', 'loader_fail', 'missing_header', 'wrong_elf', 'absolute_link', 'escaped_link', 'dangling_link'):
    setattr(BuilderTests, 'test_incomplete_' + _failure, incomplete_case(_failure))


def runtime_required(self):
    self.control(runtime_fail=True); self.refusal_unchanged('Injected verifier failure')
BuilderTests.test_normal_runtime_check_is_mandatory = runtime_required
