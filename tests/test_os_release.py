# SPDX-License-Identifier: BSD-2-Clause
# Origin: EmberBSD release-*.rb contracts; AI-assisted Python migration.
"""Structural release fixtures. No compiler, VM or GitHub acceptance is claimed."""
import copy
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
from unittest.mock import patch

from test_core import TemporaryCase, header, SDK_SOURCE, git_environment
from test_builder import fixture, write, commit, snapshot, fixture_native_selection
from libedit_fixture import installation_source
from emberbsd_sdk import os_release as kit, jsonio
from emberbsd_sdk.os_release_builder import OSReleaseBuilder as ProductionReleaseBuilder, validate_profile, producer_commands
from emberbsd_sdk.os_release_github import GitHub
from emberbsd_sdk.errors import SDKError
import test_llvm_builder as llvm_fixtures


class OSReleaseBuilder(ProductionReleaseBuilder):
    """Keep original fake release dispatch tests separate from admission."""
    load_native_toolset = fixture_native_selection

    def prepare_native_routing(self):
        pass


def tar_bytes(files, mode='w:xz'):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode=mode) as archive:
        for name, data in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return output.getvalue()


def set_files():
    base = {'./libexec/ld.elf_so': header(), './lib/libc.so.12.229': header()}
    comp = {'./usr/include/' + n: b'/* fixture */' for n in ('stdio.h', 'stdlib.h', 'pthread.h')}
    comp.update({'./usr/lib/' + n: header() if n.endswith('.o') else b'!<arch>\n'
                 for n in ('crt0.o', 'crti.o', 'crtn.o', 'libc.a', 'libm.a', 'libpthread.a')})
    return base, comp


def legacy_build(root):
    build = root / 'historical'
    build.mkdir()
    sha = 'a' * 40
    base, comp = set_files()
    for name, files in zip(kit.SYSROOT_SETS, (base, comp)):
        write(build / 'release/evbarm-aarch64/binary/sets' / name, tar_bytes(files))
    write(build / kit.KERNEL, gzip.compress(header() + f'EmberBSD:{sha}\n\0'.encode(), mtime=0))
    write(build / 'source.tar.gz', tar_bytes({'share/mk/bsd.own.mk': b'EMBER_VERSION?= 1.0\n'}, mode='w:gz'))
    write(build / 'build-success', sha + '\n')
    # Freeze the old producer layout; do not derive it from the new builder.
    output = '/fixture/historical'
    environment = dict(PATH='/usr/bin:/bin', HOME='/fixture', TMPDIR='/fixture/tmp', LC_ALL='C', MAKECONF='/dev/null')
    host = dict(cc='/usr/bin/cc', cxx='/usr/bin/c++')
    flags = list(kit.LEGACY_FLAGS) + ['EMBER_VERSION=1.0'] + [f'MKREPRO_TIMESTAMP=1', f'EMBER_BUILD_ID={sha}', f'BUILDINFO=EmberBSD:{sha}',
                                    'HOST_CC=/usr/bin/cc', 'HOST_CXX=/usr/bin/c++', 'KERNOBJDIR=' + output + '/obj/kernels']
    argv = ['env', '-i'] + [f'{n}={v}' for n, v in environment.items()] + ['sh', './build.sh', '-U', '-u', '-m', 'evbarm', '-a', 'aarch64', '-j2']
    for flag, name in (('-O', 'obj'), ('-T', 'tools'), ('-D', 'dest'), ('-R', 'release')):
        argv += [flag, output + '/' + name]
    for flag in flags:
        argv += ['-V', flag]
    names = [kit.KERNEL] + ['release/evbarm-aarch64/binary/sets/' + n for n in kit.SYSROOT_SETS]
    p = dict(schema=1, status='built', machine='evbarm', architecture='aarch64', kernel_config='EMBER64',
             source_repository='https://github.com/' + kit.REPOSITORY, source_commit=sha, source_timestamp=1,
             source_sha256=kit.sha256_file(build / 'source.tar.gz'), target_cc_version='fixture bootstrap GCC 12.5.0',
             commands=[argv + ['tools', 'kernel=EMBER64'], argv + ['distribution', 'sets']], host=host,
             build_environment=environment, output_sha256={n: kit.sha256_file(build / n) for n in names})
    jsonio.write_json(build / 'build-profile.json', p)
    return build


def qualify(build, manifest, profile):
    files = {f['name']: f['sha256'] for f in manifest['files']}
    if profile == 'amd64':
        q = dict(source_commit=manifest['source_commit'], image_sha256=files[kit.images(manifest['product_version'])[0]], usb_image_sha256=files[kit.images(manifest['product_version'])[1]],
                 installed_kernel_sha256=manifest['components']['kernel']['sha256'], checks={n: True for n in kit.VM_CHECKS})
    else:
        write(build / kit.EVIDENCE, 'Fixture evidence. No target execution occurred.\n')
        q = dict(source_commit=manifest['source_commit'], base_sha256=files[kit.SYSROOT_SETS[0]], comp_sha256=files[kit.SYSROOT_SETS[1]],
                 kernel_gzip_sha256=files[kit.KERNEL], consumer_compiler='fixture GCC 16.2.0',
                 execution='EmberBSD aarch64 full-system QEMU TCG', checks={n: True for n in kit.SYSROOT_CHECKS},
                 evidence_sha256={kit.EVIDENCE: kit.sha256_file(build / kit.EVIDENCE)})
    path = build / 'fixture-qualification.json'
    jsonio.write_json(path, q)
    return path


def reseal(directory, change=None):
    m = jsonio.read_json(directory / 'manifest.json')
    if change:
        change(m)
    m['files'] = [kit.asset(directory, f['name']) for f in m['files']]
    (directory / 'manifest.json').unlink()
    kit.seal(directory, m)


class FixtureNativeRuntimeInstaller:
    """Filesystem-only stand-in for install(1); not native install evidence."""

    def __init__(self, destination, tools, os_source, run):
        self.root = os.fspath(destination)
        self.metalog = Path(self.root) / 'METALOG'
        if not self.metalog.is_file() or not Path(os_source, 'etc').is_dir():
            raise SDKError('fixture distribution lacks METALOG or OS database')
        selected = tools.get('install')
        if not isinstance(selected, str) or not selected.endswith('-install'):
            raise SDKError('fixture did not select the target OS install tool')
        self.run = run

    def _record(self, path, kind, mode, extra=''):
        name = './' + Path(path).relative_to(self.root).as_posix()
        with self.metalog.open('a') as stream:
            stream.write(f'{name}\ttype={kind}\tmode={mode:04o}\tuid=0\tgid=0{extra}\n')

    def directory(self, path, mode):
        path = Path(path)
        if path.exists():
            if not path.is_dir():
                raise SDKError('fixture runtime directory conflict')
            return
        path.mkdir()
        path.chmod(mode)
        self._record(path, 'dir', mode)

    def file(self, source, destination, mode):
        destination = Path(destination)
        shutil.copyfile(source, destination)
        destination.chmod(mode)
        self._record(destination, 'file', mode)

    def link(self, target, destination, mode):
        destination = Path(destination)
        destination.symlink_to(target)
        if hasattr(os, 'lchmod'):
            os.lchmod(destination, mode)
        self._record(destination, 'link', mode, '\tlink=' + target)


class HistoricalKitTests(TemporaryCase):
    def setUp(self):
        super().setUp()
        self.build = legacy_build(self.root)
        self.directory = self.root / 'kit'
        self.manifest = kit.prepare(self.build, self.directory, 'aarch64-sysroot')

    def test_roundtrip_and_cli(self):
        self.assertEqual(kit.verify(self.directory, 'aarch64-sysroot'), self.manifest)
        self.assertIsNone(self.manifest['qualification'])
        result = subprocess.run([sys.executable, str(SDK_SOURCE / 'scripts/os-release.py'), '--profile', 'aarch64-sysroot', 'verify', str(self.directory)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.manifest['build_profile']['schema'], 1)

    def test_extra_file_symlink_bytes_and_missing_input(self):
        for name in ('extra', 'link', 'changed'):
            with self.subTest(name=name):
                path = self.root / name
                shutil.copytree(self.directory, path)
                if name == 'extra':
                    (path / 'foreign').write_text('foreign')
                elif name == 'link':
                    (path / kit.KERNEL).unlink()
                    (path / kit.KERNEL).symlink_to(self.directory / kit.KERNEL)
                else:
                    (path / kit.KERNEL).write_bytes(b'changed')
                with self.assertRaises((SDKError, OSError)):
                    kit.verify(path, 'aarch64-sysroot')
        (self.build / 'build-success').unlink()
        with self.assertRaisesRegex(SDKError, 'build-success'):
            kit.prepare(self.build, self.root / 'missing-success', 'aarch64-sysroot')

    def test_malformed_and_mixed_manifests(self):
        changes = {
            'unsafe': lambda m: m['files'][0].update(name='../base.tar.xz'),
            'duplicate': lambda m: m['files'].append(m['files'][0]),
            'wrong-source': lambda m: m.update(source_commit='b' * 40),
            'boolean-size': lambda m: m['files'][0].update(size=True),
            'wrong-profile': lambda m: m['build_profile'].update(source_commit='b' * 40),
            'mixed-set': lambda m: m['build_profile']['output_sha256'].update({'release/evbarm-aarch64/binary/sets/base.tar.xz': '0' * 64}),
            'wrong-type': lambda m: m.update(files={}),
            'false-gcc16-schema': lambda m: m.update(schema='emberbsd.aarch64.sysroot.v2'),
        }
        for name, change in changes.items():
            with self.subTest(name=name):
                path = self.root / name
                shutil.copytree(self.directory, path)
                m = jsonio.read_json(path / 'manifest.json')
                change(m)
                jsonio.write_json(path / 'manifest.json', m, exclusive=False)
                (path / 'SHA256').write_text(kit.checksum_text(path, [f['name'] for f in self.manifest['files']] + ['manifest.json']))
                with self.assertRaises(SDKError):
                    kit.verify(path, 'aarch64-sysroot')

    def test_profile_controls_and_source_export(self):
        for control in ('NOCHECKFLIST=yes', 'HAVE_GCC=16', 'BUILDINFO=foreign'):
            p = copy.deepcopy(self.manifest['build_profile'])
            p['commands'][0][-2:-2] = ['-V', control]
            with self.assertRaises(SDKError):
                kit.profile_check(p, self.manifest['source_commit'], self.manifest['product_version'])
        p = copy.deepcopy(self.manifest['build_profile'])
        p['commands'][1].insert(-2, 'release')
        with self.assertRaises(SDKError):
            kit.profile_check(p, self.manifest['source_commit'], self.manifest['product_version'])
        (self.build / 'source.tar.gz').write_bytes(b'changed')
        with self.assertRaisesRegex(SDKError, 'Source export differs'):
            kit.prepare(self.build, self.root / 'changed-source', 'aarch64-sysroot')

    def test_archive_content_refusals(self):
        base, comp = set_files()
        cases = [('missing-header', 'comp.tar.xz', {k: v for k, v in comp.items() if 'stdio.h' not in k}),
                 ('missing-crt', 'comp.tar.xz', {k: v for k, v in comp.items() if 'crt0.o' not in k}),
                 ('missing-library', 'comp.tar.xz', {k: v for k, v in comp.items() if 'libc.a' not in k}),
                 ('native-gcc', 'comp.tar.xz', comp | {'./usr/bin/gcc': b'forbidden'}),
                 ('wrong-crt', 'comp.tar.xz', comp | {'./usr/lib/crt0.o': header(62)}),
                 ('loader', 'base.tar.xz', {'./lib/libc.so.12.229': header()}),
                 ('unsafe', 'base.tar.xz', {'../outside': header()})]
        for label, name, files in cases:
            with self.subTest(label=label):
                path = self.root / label
                shutil.copytree(self.directory, path)
                (path / name).write_bytes(tar_bytes(files))
                p = jsonio.read_json(path / 'build-profile.json')
                p['output_sha256']['release/evbarm-aarch64/binary/sets/' + name] = kit.sha256_file(path / name)
                jsonio.write_json(path / 'build-profile.json', p, exclusive=False)
                reseal(path, lambda m: m.update(build_profile=p))
                with self.assertRaises(SDKError):
                    kit.verify(path, 'aarch64-sysroot')

    def test_extended_corpus_requires_its_additional_checks(self):
        q = qualify(self.build, self.manifest, 'aarch64-sysroot')
        m = kit.prepare(self.build, self.root / 'corpus-v2', 'aarch64-sysroot', q)
        kit.require_qualification(m, 'aarch64-sysroot')
        legacy_claim = copy.deepcopy(m)
        legacy_claim['qualification']['checks']['libm_valid'] = True
        with self.assertRaisesRegex(SDKError, 'Legacy target corpus'):
            kit.require_qualification(legacy_claim, 'aarch64-sysroot')
        m['qualification']['corpus'] = 'target-runtime-v2'
        with self.assertRaises(SDKError):
            kit.require_qualification(m, 'aarch64-sysroot')
        m['qualification']['checks'].update(libm_valid=True, setjmp_valid=True)
        with self.assertRaisesRegex(SDKError, 'source provenance'):
            kit.require_qualification(m, 'aarch64-sysroot')
        m['qualification'].update(os_test_source_commit='a' * 40,
                                  ports_source_commit='b' * 40,
                                  test_source_inventory_sha256='c' * 64)
        kit.require_qualification(m, 'aarch64-sysroot')
        raw = jsonio.read_json(q)
        raw['corpus'] = 'target-runtime-v2'
        raw.update(os_test_source_commit='a' * 40, ports_source_commit='b' * 40,
                   test_source_inventory_sha256='c' * 64)
        raw['checks'].update(libm_valid=True, setjmp_valid=True)
        jsonio.write_json(q, raw, exclusive=False)
        roundtrip = kit.prepare(self.build, self.root / 'corpus-v2-roundtrip',
                                'aarch64-sysroot', q)
        kit.require_qualification(roundtrip, 'aarch64-sysroot')
        self.assertEqual(roundtrip['qualification']['corpus'], 'target-runtime-v2')
        self.assertTrue(roundtrip['qualification']['checks']['libm_valid'])
        self.assertTrue(roundtrip['qualification']['checks']['setjmp_valid'])
        raw.pop('corpus')
        jsonio.write_json(q, raw, exclusive=False)
        legacy = kit.prepare(self.build, self.root / 'corpus-v1-roundtrip',
                             'aarch64-sysroot', q)
        self.assertNotIn('libm_valid', legacy['qualification']['checks'])
        self.assertNotIn('setjmp_valid', legacy['qualification']['checks'])
        legacy['qualification']['corpus'] = 'unknown'
        with self.assertRaises(SDKError):
            kit.require_qualification(legacy, 'aarch64-sysroot')

    def test_optional_qualification_binding(self):
        q = qualify(self.build, self.manifest, 'aarch64-sysroot')
        qualified = self.root / 'qualified'
        m = kit.prepare(self.build, qualified, 'aarch64-sysroot', q)
        kit.require_qualification(m, 'aarch64-sysroot')
        for field, value in [('source_commit', 'b' * 40), ('base_sha256', '0' * 64), ('comp_sha256', '0' * 64),
                             ('kernel_gzip_sha256', '0' * 64), ('consumer_compiler', 'GCC 12.5.0'), ('execution', 'host')]:
            broken = copy.deepcopy(m)
            broken['qualification'][field] = value
            with self.assertRaises(SDKError):
                kit.require_qualification(broken, 'aarch64-sysroot')
        for name in kit.SYSROOT_CHECKS:
            broken = copy.deepcopy(m)
            broken['qualification']['checks'][name] = False
            with self.assertRaises(SDKError):
                kit.require_qualification(broken, 'aarch64-sysroot')
        m['qualification']['evidence_sha256'][kit.EVIDENCE] = '0' * 64
        with self.assertRaisesRegex(SDKError, 'bound target evidence'):
            kit.require_qualification(m, 'aarch64-sysroot')

    def test_qualification_uses_evidence_sidecar_without_mutating_os_build(self):
        qualification = qualify(self.build, self.manifest, 'aarch64-sysroot')
        evidence_bytes = (self.build / kit.EVIDENCE).read_bytes()
        sidecar = self.root / 'acceptance'
        sidecar.mkdir()
        moved_qualification = sidecar / 'qualification.json'
        shutil.move(qualification, moved_qualification)
        shutil.move(self.build / kit.EVIDENCE, sidecar / kit.EVIDENCE)
        result = kit.prepare(self.build, self.root / 'sidecar-qualified',
                             'aarch64-sysroot', moved_qualification)
        self.assertEqual((self.root / 'sidecar-qualified' / kit.EVIDENCE).read_bytes(), evidence_bytes)
        self.assertFalse((self.build / kit.EVIDENCE).exists())
        kit.require_qualification(result, 'aarch64-sysroot')

    def test_canonical_version_and_historical_archive_cleanup(self):
        before = snapshot(self.directory)
        kit.verify(self.directory, 'aarch64-sysroot')
        self.assertEqual(snapshot(self.directory), before)
        (self.build / 'source.tar.gz').unlink()
        old = kit.prepare(self.build, self.root / 'without-source', 'aarch64-sysroot')
        self.assertEqual(old['product_version'], '1.0')
        p = jsonio.read_json(self.build / 'build-profile.json')
        p['commands'][0] = ['EMBER_VERSION=9.9' if a == 'EMBER_VERSION=1.0' else a for a in p['commands'][0]]
        with self.assertRaisesRegex(SDKError, 'another product version'):
            kit.legacy_profile_check(p, old['source_commit'], '1.0')
        for argv in p['commands']:
            i = next(i for i, a in enumerate(argv) if a.startswith('EMBER_VERSION='))
            del argv[i-1:i+1]
        jsonio.write_json(self.build / 'build-profile.json', p, exclusive=False)
        with self.assertRaisesRegex(SDKError, 'Source archive is required'):
            kit.prepare(self.build, self.root / 'no-version', 'aarch64-sysroot')
        write(self.build / 'source.tar.gz', tar_bytes({'share/mk/bsd.own.mk': b'EMBER_VERSION?= 2.7+fixture # canonical\n'}, mode='w:gz'))
        p['source_sha256'] = kit.sha256_file(self.build / 'source.tar.gz')
        jsonio.write_json(self.build / 'build-profile.json', p, exclusive=False)
        m = kit.prepare(self.build, self.root / 'source-version', 'aarch64-sysroot')
        self.assertEqual(m['product_version'], '2.7+fixture')
        kit.verify(self.root / 'source-version', 'aarch64-sysroot')

    def test_invalid_source_and_product_versions(self):
        for value in ('', '../bad', '1..2', '1.0.lock', '1/2', '1.0 ', '1+bad;cmd'):
            with self.subTest(value=value), self.assertRaises(SDKError):
                kit.product_version(value)
        for text in ('', 'EMBER_VERSION=1.0\n', 'EMBER_VERSION?= 1.0 extra\n',
                     'EMBER_VERSION?=1.0\nEMBER_VERSION?=2.0\n'):
            with self.subTest(text=text), self.assertRaises(SDKError):
                kit.source_version(text)
        archive = self.root / 'source.tar'
        write(archive, tar_bytes({'share/mk/bsd.own.mk': b'EMBER_VERSION?=2.7+fixture\n'}, mode='w'))
        write(self.root / 'share/mk/bsd.own.mk', 'EMBER_VERSION?=9.9\n')
        self.assertEqual(kit.archive_version(archive), '2.7+fixture')


def release_fixture(root, target, version='1.0'):
    options = fixture(root, target)
    source = Path(options['os'])
    write(source / 'share/mk/bsd.own.mk', f'EMBER_VERSION?= {version}\n')
    write(source / 'distrib/sets/lists/base/mi', '# base list fixture\n')
    write(source / 'distrib/sets/lists/comp/mi', '# comp list fixture\n')
    if target == 'amd64':
        write(source / 'ember/tests/installer-image.py',
              'import sys\nassert len(sys.argv) == 5\n')
    base, comp = set_files()
    # This producer emits archives and kernel identity bytes. It never compiles.
    producer = '''import json, os, sys, gzip, hashlib, io, tarfile, shlex
from pathlib import Path
args = sys.argv[1:]
def value(k): return args[args.index(k)+1]
control = json.loads(Path(CONTROL).read_text())
if any(os.environ.get(k) for k in ('NOCHECKFLIST', 'NOGCCERROR', 'MAKEFLAGS', 'CFLAGS', 'EMBER_VERSION')): raise SystemExit('inherited control')
release = Path(value('-R')); output = release.parent
version = Path(__file__).with_name('share').joinpath('mk/bsd.own.mk').read_text().split('=',1)[1].strip()
with (output / 'observed-argv.jsonl').open('a') as handle: handle.write(json.dumps(args)+'\\n')
if control.get('fail') == args[-1]: raise SystemExit(77)
sets_source = next((a.split('=',1)[1] for a in args if a.startswith('EMBER_SETS_SOURCE=')), None)
sha = next(a.split('=',1)[1] for a in args if a.startswith('EMBER_BUILD_ID='))
kernel = ELF + ('EmberBSD:'+sha+'\\n\\0').encode()
if args[-1] == 'kernel=EMBER64':
    path = Path(value('-O'))/'kernels/EMBER64/netbsd'; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(kernel)
elif args[-1] == 'distribution':
    (output/'fixture-distribution-complete').write_text('fixture only; no target build')
    dest = Path(value('-D'))
    dest.mkdir(parents=True, exist_ok=True)
    (dest/'METALOG').write_text('./\\ttype=dir\\tmode=0755\\tuid=0\\tgid=0\\n')
    tools = Path(value('-T'))/'bin'
    tools.mkdir(parents=True, exist_ok=True)
    install = tools/(('aarch64--netbsd' if '-a' in args and value('-a') == 'aarch64' else 'x86_64--netbsd') + '-install')
    install.write_text('#!/bin/sh\\nexit 0\\n'); install.chmod(0o755)
    wrapper = tools/('nbmake-evbarm' if value('-a') == 'aarch64' else 'nbmake-amd64')
    checkscript = output/'fixture-checkflist.py'
    checkscript.write_text(f"""import gzip, json, sys
from pathlib import Path
args=sys.argv[1:]
def value(key): return args[args.index(key)+1]
dest=Path({str(dest)!r})
output=Path({str(output)!r})
release=Path({str(release)!r})
version={version!r}
target=args[-1]
if target == 'checkflist':
    overlay=next((a.split('=',1)[1] for a in args if a.startswith('EMBER_SETS_SOURCE=')), None)
    if overlay is None or not (dest/'usr/include/c++').is_dir() or not Path(overlay).is_dir():
        raise SystemExit('checkflist ran before common runtime import')
    (output/'fixture-checkflist-complete').write_text('fixture only')
elif target == 'release':
    if not (dest/'usr/include/c++').is_dir() or not (output/'fixture-checkflist-complete').is_file():
        raise SystemExit('etc release ran before common runtime import and checkflist')
    (output/'fixture-etc-release-complete').write_text('fixture only')
elif target == 'install_image':
    overlay=next((a.split('=',1)[1] for a in args if a.startswith('EMBER_SETS_SOURCE=')), None)
    if overlay is not None and not (output/'fixture-etc-release-complete').is_file():
        raise SystemExit('install_image ran before etc release')
    images=release/'images'
    images.mkdir(parents=True, exist_ok=True)
    (images/f'EmberBSD-{version}-amd64-install.img.gz').write_bytes(gzip.compress(b'USB fixture'))
elif target == 'iso-image':
    overlay=next((a.split('=',1)[1] for a in args if a.startswith('EMBER_SETS_SOURCE=')), None)
    if overlay is not None and not (output/'fixture-etc-release-complete').is_file():
        raise SystemExit('iso-image ran before etc release')
    images=release/'images'
    images.mkdir(parents=True, exist_ok=True)
    (images/f'EmberBSD-{version}-amd64.iso').write_bytes(b'ISO fixture')
else:
    raise SystemExit('unexpected fixture make target: '+target)
with (output/'observed-argv.jsonl').open('a') as handle:
    handle.write(json.dumps([target])+'\\\\n')
""")
    wrapper.write_text('#!/bin/sh\\nexec ' + shlex.quote(sys.executable) + ' ' + shlex.quote(str(checkscript)) + ' "$@"\\n')
    wrapper.chmod(0o755)
elif args[-1] == 'sets':
    if sets_source is not None:
        dest = Path(value('-D'))
        if not (dest/'usr/pkg/llvm').is_dir() or not (output/'fixture-checkflist-complete').is_file(): raise SystemExit('sets ran before runtime import and checkflist')
    path = release/'evbarm-aarch64/binary/sets'; path.mkdir(parents=True)
    (path/'base.tar.xz').write_bytes(BASE)
    if not control.get('omit_comp'): (path/'comp.tar.xz').write_bytes(COMP)
elif args[-1] == 'release':
    if sets_source is not None and not (output/'fixture-etc-release-complete').is_file(): raise SystemExit('release media ran before etc release')
    path = release/'images'; path.mkdir(parents=True, exist_ok=True)
    (path/f'EmberBSD-{version}-amd64-install.img.gz').write_bytes(gzip.compress(b'USB fixture'))
    wrapper = Path(value('-T'))/'bin/nbmake-amd64'; wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_text('#!/bin/sh\\nexit 0\\n')
    wrapper.chmod(0o755)
elif args[-1] == 'install_image':
    if not (output/'fixture-etc-release-complete').is_file(): raise SystemExit('install_image ran before etc release')
    path = release/'images'; path.mkdir(parents=True, exist_ok=True)
    (path/f'EmberBSD-{version}-amd64-install.img.gz').write_bytes(gzip.compress(b'USB fixture'))
elif args[-1] == 'iso-image':
    if sets_source is not None and not (output/'fixture-etc-release-complete').is_file(): raise SystemExit('iso-image ran before etc release')
    set_payloads = {}
    for name in SETS:
        members = {'./netbsd': kernel} if name == 'kern-GENERIC' else {'./fixture': b'fixture'}
        blob = io.BytesIO()
        with tarfile.open(fileobj=blob, mode='w:xz') as archive:
            for member, data in members.items():
                info = tarfile.TarInfo(member); info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        set_payloads[name + '.tar.xz'] = blob.getvalue()
    sums = ''.join('SHA512 (' + name + ') = ' + hashlib.sha512(data).hexdigest() + '\\n'
                   for name, data in set_payloads.items()).encode()
    image_files = {'amd64/binary/sets/' + name: data for name, data in set_payloads.items()}
    image_files['amd64/binary/sets/SHA512'] = sums
    image_files['netbsd'] = gzip.compress(kernel)
    path = release/f'images/EmberBSD-{version}-amd64.iso'
    with tarfile.open(path, mode='w') as archive:
        for name, data in image_files.items():
            info = tarfile.TarInfo(name); info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
else: raise SystemExit('unexpected operation')
'''
    producer = ('CONTROL=' + repr(str(root / 'control.json')) + '\nELF=' + repr(header(183 if target == 'aarch64' else 62)) +
                '\nSETS=' + repr(kit.SETS) + '\nBASE=' + repr(tar_bytes(base)) + '\nCOMP=' + repr(tar_bytes(comp)) + '\n') + producer
    producer += "dest = Path(value('-D'))\n" + installation_source(183 if target == 'aarch64' else 62, arguments='args', control='control')
    write(source / 'fixture-producer.py', producer)
    commit(source)
    options['destination'] = str(Path(options['sdk']) / 'stages/release')
    return options


def retarget_runtime_amd64(runtime):
    """Rewrite a synthetic aarch64 LLVM runtime stage as a consistent amd64 stage."""
    old_builtins = runtime.record['artifacts']['builtins']
    new_builtins = old_builtins.replace('-aarch64.', '-x86_64.')
    (runtime.stage / old_builtins).rename(runtime.stage / new_builtins)
    runtime.builtins = new_builtins
    runtime.record['artifacts']['builtins'] = new_builtins
    runtime.record.update(target='amd64', triplet='x86_64--netbsd',
                          machine_arch='x86_64', elf_machine=62)
    for path in runtime.stage.rglob('*'):
        if not path.is_file() or path.name == 'runtime.json':
            continue
        data = bytearray(path.read_bytes())
        if data.startswith(b'\x7fELF'):
            data[18:20] = (62).to_bytes(2, 'little')
        elif data.startswith(b'!<arch>\n') and data[68:72] == b'\x7fELF':
            data[86:88] = (62).to_bytes(2, 'little')
        path.write_bytes(data)
    runtime.save()


class BuilderReleaseTests(TemporaryCase):
    def test_producer_commands_reject_invalid_or_non_clang_base_cxx(self):
        with self.assertRaisesRegex(SDKError, 'must be boolean'):
            producer_commands('aarch64', {}, {}, 'a' * 40, 1, '2', '/bin/sh',
                compiler_family='clang', base_cxx='false')
        with self.assertRaisesRegex(SDKError, 'require the selected Clang'):
            producer_commands('aarch64', {}, {}, 'a' * 40, 1, '2', '/bin/sh',
                compiler_family='gcc', base_cxx=True)

    def test_current_profile_accepts_gettext_foundation_and_provider_sequence(self):
        from emberbsd_sdk.native_os_routing import native_path
        options = release_fixture(self.root, 'aarch64')
        receipt = OSReleaseBuilder(options).run()
        profile = jsonio.read_json(Path(options['destination']) / 'build-profile.json')
        saved = profile['sdk_receipt']
        signature, paths = saved['signature'], saved['paths']
        paths.update({'gettext-source': '/prepared/gettext', 'libxml2-source': '/prepared/xml',
                      'git': '/usr/bin/git'})
        for name, package in (('gettext', 'devel/gettext'), ('libxml2', 'textproc/libxml2')):
            signature['common_sources'][name] = {'format': 'emberbsd-prepared-source', 'version': 1,
                'package': package, 'tree_sha256': 'a' * 64, 'identity': {'fixture': True}}
        native = signature['native_toolset']
        for role in ('gmake', 'pkg-config', 'od', 'msgfmt', 'msgmerge', 'xgettext', 'msginit', 'msgconv', 'msgfilter', 'sed'):
            native['tools'][role] = native['bundle_root'] + '/bin/' + role
        signature['environment']['PATH'] = native_path(native)
        signature['flags'], profile['commands'] = producer_commands('aarch64', paths, signature['host_tools'],
            receipt['sources']['os']['commit'], profile['source_timestamp'], profile['jobs'], profile['shell'],
            native, signature['compiler_family'])
        saved['commands'] = [dict(argv=argv, status='passed', exit_status=0) for argv in profile['commands']]
        self.assertGreater(len(profile['commands']), 2)
        kit.profile_check(profile, receipt['sources']['os']['commit'], profile['product_version'])

    def test_profile_rejects_gettext_path_without_source_record(self):
        options = release_fixture(self.root, 'aarch64')
        receipt = OSReleaseBuilder(options).run()
        profile = jsonio.read_json(Path(options['destination']) / 'build-profile.json')
        profile['sdk_receipt']['paths']['gettext-source'] = '/prepared/gettext'
        with self.assertRaisesRegex(SDKError, 'Mixed gettext and libxml2 source selection'):
            validate_profile(profile, receipt['sources']['os']['commit'], 'aarch64')
        profile['sdk_receipt']['paths']['libxml2-source'] = '/prepared/libxml2'
        with self.assertRaisesRegex(SDKError, 'Mixed gettext and libxml2 source selection'):
            validate_profile(profile, receipt['sources']['os']['commit'], 'aarch64')
        profile['sdk_receipt']['signature'].pop('common_sources', None)
        with self.assertRaisesRegex(SDKError, 'Mixed gettext and libxml2 source selection'):
            validate_profile(profile, receipt['sources']['os']['commit'], 'aarch64')

    def test_clang_release_base_cxx_selection_is_reconstructed_from_flags(self):
        class ClangRelease(OSReleaseBuilder):
            compiler = llvm_fixtures.ClangBuilder.compiler
        options = release_fixture(self.root, 'aarch64')
        builder, runtime = llvm_fixtures.LLVMBuilderTests.builder(self, options, ClangRelease)
        llvm_fixtures.write(Path(builder.options['os']) / 'changed', 'new source')
        llvm_fixtures.commit(builder.options['os'])
        with patch('emberbsd_sdk.native_runtime_install.NativeRuntimeInstaller',
                   FixtureNativeRuntimeInstaller):
            receipt = builder.run()
        output = Path(builder.paths['destination'])
        profile = jsonio.read_json(output / 'build-profile.json')
        self.assertEqual((profile['schema'], receipt['version']), (8, 8))
        self.assertNotEqual(receipt['sources']['os']['commit'], runtime.record['base']['os_revision'])
        self.assertIn('MKLIBCXX=yes', receipt['signature']['flags'])
        validate_profile(profile, receipt['sources']['os']['commit'], 'aarch64')
        manifest = kit.prepare(output, self.root / 'common-runtime-kit', 'aarch64-sysroot')
        self.assertEqual(manifest['schema'], 'emberbsd.aarch64.sysroot.v4')
        self.assertEqual(manifest['build_profile']['schema'], 8)
        self.assertTrue((output / 'dest/usr/include/c++').is_dir())
        observed = [json.loads(line) for line in (output / 'observed-argv.jsonl').read_text().splitlines()]
        self.assertEqual([row[-1] for row in observed],
                         ['kernel=EMBER64', 'distribution', 'checkflist', 'sets'])
        forged = copy.deepcopy(profile)
        flags = forged['sdk_receipt']['signature']['flags']
        flags[flags.index('MKCXX=yes')] = 'MKCXX=no'
        with self.assertRaisesRegex(SDKError, 'MKLIBCXX=yes requires MKCXX=yes'):
            validate_profile(forged, receipt['sources']['os']['commit'], 'aarch64')

        forged = copy.deepcopy(profile)
        flags = forged['sdk_receipt']['signature']['flags']
        flags[flags.index('MKLIBCXX=yes')] = 'MKLIBCXX=no'
        with self.assertRaisesRegex(SDKError, 'Common-runtime release must select base C\\+\\+'):
            validate_profile(forged, receipt['sources']['os']['commit'], 'aarch64')

        wrong_manifest = self.root / 'wrong-outer-schema'
        shutil.copytree(self.root / 'common-runtime-kit', wrong_manifest)
        # A complete git-archive era wrapper around a Git-tree profile.
        (wrong_manifest / kit.SOURCE_EXPORT).write_text('0' * 64 + '  source.tar.gz\n')
        def older_wrapper(m):
            m.update(schema='emberbsd.aarch64.sysroot.v2', source_archive_sha256='0' * 64)
            del m['source_tree']
            m['files'].append(dict(name=kit.SOURCE_EXPORT))
        reseal(wrong_manifest, older_wrapper)
        with self.assertRaisesRegex(SDKError, 'Manifest and build profile schema differ'):
            kit.verify(wrong_manifest, 'aarch64-sysroot')

    def test_runtime_import_failure_prevents_checkflist_sets_and_success(self):
        class FailingImportRelease(OSReleaseBuilder):
            compiler = llvm_fixtures.ClangBuilder.compiler

            def import_base_runtime(self):
                raise SDKError('fixture runtime importer failed')
        options = release_fixture(self.root, 'aarch64')
        builder, _ = llvm_fixtures.LLVMBuilderTests.builder(self, options, FailingImportRelease)
        builder.options['base-cxx'] = True
        with self.assertRaisesRegex(SDKError, 'fixture runtime importer failed'):
            builder.run()
        output = Path(builder.paths['destination'])
        observed = [json.loads(line) for line in (output / 'observed-argv.jsonl').read_text().splitlines()]
        self.assertEqual([row[-1] for row in observed], ['kernel=EMBER64', 'distribution'])
        self.assertFalse((output / 'build-success').exists())
        self.assertFalse((output / 'fixture-checkflist-complete').exists())

    def test_clang_amd64_base_runtime_precedes_release_media(self):
        class AMD64ClangIdentity:
            def compiler(self):
                record = llvm_fixtures.ClangBuilder.compiler(self)
                record['triplet'] = 'x86_64--netbsd'
                record['reported_triplet'] = 'x86_64-unknown-netbsd'
                for role, command in record['commands'].items():
                    record['commands'][role] = [arg.replace('aarch64--netbsd', 'x86_64--netbsd')
                                                for arg in command]
                return record

        class ClangRelease(AMD64ClangIdentity, OSReleaseBuilder):
            pass
        options = release_fixture(self.root, 'amd64')
        builder, _ = llvm_fixtures.LLVMBuilderTests.builder(self, options, ClangRelease,
                                                            retarget=retarget_runtime_amd64)
        with patch('emberbsd_sdk.native_runtime_install.NativeRuntimeInstaller',
                   FixtureNativeRuntimeInstaller):
            receipt = builder.run()
        output = Path(builder.paths['destination'])
        profile = jsonio.read_json(output / 'build-profile.json')
        self.assertEqual((profile['schema'], receipt['version']), (8, 8))
        validate_profile(profile, receipt['sources']['os']['commit'], 'amd64')
        observed = [json.loads(line) for line in (output / 'observed-argv.jsonl').read_text().splitlines()]
        self.assertEqual([row[-1] for row in observed],
                         ['distribution', 'checkflist', 'release', 'install_image', 'iso-image'])
        manifest = kit.prepare(output, self.root / 'common-runtime-amd64-kit', 'amd64',
                               os_source=Path(builder.paths['os']))
        self.assertEqual(manifest['schema'], 'emberbsd.amd64.release.v4')
        self.assertEqual(manifest['build_profile']['schema'], 8)
        self.assertTrue((output / 'dest/usr/include/c++').is_dir())

    def test_clang_schema7_base_cxx_selection_keeps_old_commands(self):
        class ClangRelease(OSReleaseBuilder):
            compiler = llvm_fixtures.ClangBuilder.compiler
        builder, _ = llvm_fixtures.LLVMBuilderTests.builder(
            self, release_fixture(self.root, 'aarch64'), ClangRelease)
        builder.options['base-cxx'] = False
        receipt = builder.run()
        profile = jsonio.read_json(Path(builder.paths['destination']) / 'build-profile.json')
        signature = profile['sdk_receipt']['signature']
        flags, commands = producer_commands(
            'aarch64', profile['sdk_receipt']['paths'], signature['host_tools'],
            profile['source_commit'], profile['source_timestamp'], profile['jobs'],
            profile['shell'], signature['native_toolset'], 'clang', base_cxx=True,
            install_base_cxx=False)
        profile['commands'] = commands
        signature['flags'] = flags
        profile['sdk_receipt']['plan'] = [builder.runtime_command, *commands,
                                           builder.runtime_command]
        profile['sdk_receipt']['commands'] = [
            dict(scope='producer', argv=argv, status='passed', exit_status=0)
            for argv in commands]
        self.assertEqual((profile['schema'], receipt['version']), (7, 7))
        self.assertIn('MKLIBCXX=yes', flags)
        self.assertEqual([command[-1] for command in commands],
                         ['kernel=EMBER64', 'sets'])
        self.assertEqual(commands[1][-2:], ['distribution', 'sets'])
        validate_profile(profile, receipt['sources']['os']['commit'], 'aarch64')

    def test_clang_release_uses_versioned_runtime_and_qualification(self):
        class ClangRelease(OSReleaseBuilder):
            compiler = llvm_fixtures.ClangBuilder.compiler
        builder, runtime = llvm_fixtures.LLVMBuilderTests.builder(self,
            release_fixture(self.root, 'aarch64'), ClangRelease)
        builder.options['base-cxx'] = False
        receipt = builder.run()
        output = Path(builder.paths['destination'])
        profile = jsonio.read_json(output / 'build-profile.json')
        self.assertEqual((profile['schema'], receipt['version']), (7, 7))
        self.assertIn('EMBER_COMPILER_FAMILY=clang', receipt['signature']['flags'])
        self.assertFalse(any(v.startswith(('EMBER_EXTERNAL_GCC_RUNTIME=', 'MKLIBSTDCXX='))
                             for v in receipt['signature']['flags']))
        directory = self.root / 'llvm-kit'
        manifest = kit.prepare(output, directory, 'aarch64-sysroot')
        self.assertEqual(manifest['schema'], 'emberbsd.aarch64.sysroot.v4')
        self.assertEqual(kit.verify(directory, 'aarch64-sysroot'), manifest)
        advanced = copy.deepcopy(profile)
        advanced['sdk_receipt']['sources']['ports']['commit'] = '1' * 40
        advanced['sdk_receipt']['signature']['libedit']['recipe']['ports_commit'] = '1' * 40
        validate_profile(advanced, receipt['sources']['os']['commit'], 'aarch64')
        qualification = qualify(output, manifest, 'aarch64-sysroot')
        data = jsonio.read_json(qualification)
        data.update(consumer_compiler_family='clang', consumer_compiler='fixture clang version 23.1.4')
        jsonio.write_json(qualification, data, exclusive=False)
        qualified = kit.prepare(output, self.root / 'llvm-qualified', 'aarch64-sysroot', qualification)
        kit.require_qualification(qualified, 'aarch64-sysroot')
        qualified['qualification']['consumer_compiler_family'] = 'gcc'
        with self.assertRaisesRegex(SDKError, 'Clang consumers'):
            kit.require_qualification(qualified, 'aarch64-sysroot')

    def test_native_schema6_identity_and_historical_schema2(self):
        options = release_fixture(self.root, 'aarch64')
        receipt = OSReleaseBuilder(options).run()
        output = Path(options['destination'])
        profile = jsonio.read_json(output / 'build-profile.json')
        sha = receipt['sources']['os']['commit']
        self.assertEqual(profile['schema'], 6)
        for mutation in ('identity', 'inputs', 'pin', 'host-cc', 'host-cxx', 'python',
                         'host-prefix', 'host-binutils', 'native-routing', 'flags', 'environment'):
            changed = copy.deepcopy(profile)
            signature = changed['sdk_receipt']['signature']
            if mutation == 'identity': del signature['native_toolset']
            elif mutation == 'inputs': del signature['native_inputs']
            elif mutation == 'pin': signature['native_inputs']['host-prefix'] += '-changed'
            elif mutation == 'flags': signature['flags'].append('HOST_CC=/usr/bin/cc')
            elif mutation == 'environment': signature['environment']['PATH'] = '/usr/bin:/bin'
            else: changed['sdk_receipt']['paths'][mutation] = '/different/provider'
            with self.subTest(mutation=mutation), self.assertRaises(SDKError): validate_profile(changed, sha, 'aarch64')
        historical = copy.deepcopy(profile)
        historical['schema'] = historical['sdk_receipt']['version'] = 2
        # Schema 2 named its source by a git-archive export of the commit.
        write(output / 'source.tar.gz', tar_bytes({'share/mk/bsd.own.mk': b'EMBER_VERSION?= 1.0\n'}, mode='w:gz'))
        del historical['source_tree']
        historical['source_sha256'] = kit.sha256_file(output / 'source.tar.gz')
        signature = historical['sdk_receipt']['signature']
        del signature['native_toolset']; del signature['native_inputs']; del signature['compiler_family']
        for key in ('host-cc', 'host-cxx', 'host-prefix', 'host-binutils', 'native-routing'):
            del historical['sdk_receipt']['paths'][key]
        signature['host_tools'] = {name: dict(path='/usr/bin/' + name, sha256='0' * 64,
                                             version='historical system compiler') for name in ('cc', 'c++')}
        signature['flags'], historical['commands'] = producer_commands(
            'aarch64', historical['sdk_receipt']['paths'], signature['host_tools'], sha,
            historical['source_timestamp'], historical['jobs'], historical['shell'])
        historical['sdk_receipt']['commands'] = [dict(argv=argv, status='passed', exit_status=0)
                                                 for argv in historical['commands']]
        with patch('emberbsd_sdk.native_toolset.canonical_existing', side_effect=AssertionError('offline filesystem read')):
            validate_profile(profile, sha, 'aarch64')
            validate_profile(historical, sha, 'aarch64')
        jsonio.write_json(output / 'build-profile.json', historical, exclusive=False)
        kit.prepare(output, self.root / 'historical-kit', 'aarch64-sysroot')
        result = kit.verify(self.root / 'historical-kit', 'aarch64-sysroot')
        self.assertEqual(result['build_profile']['schema'], 2)
        self.assertNotIn('native_toolset', result['build_profile']['sdk_receipt']['signature'])

    def test_saved_profile_preserves_accepted_native_version_suffix(self):
        options = release_fixture(self.root, 'aarch64')
        receipt = OSReleaseBuilder(options).run()
        profile = jsonio.read_json(Path(options['destination']) / 'build-profile.json')
        signature = profile['sdk_receipt']['signature']
        signature['native_toolset']['compiler']['version'] = '16.2.0+sdk.1'
        for record in signature['host_tools'].values():
            record['version'] = record['gcc_version'] = '16.2.0+sdk.1'
        # The old standalone regex rejected this otherwise consistent profile.
        validate_profile(profile, receipt['sources']['os']['commit'], 'aarch64')
        previous_major = copy.deepcopy(profile)
        old_signature = previous_major['sdk_receipt']['signature']
        old_signature['native_toolset']['compiler']['version'] = '15.2.0'
        for record in old_signature['host_tools'].values():
            record['version'] = record['gcc_version'] = '15.2.0'
        with self.assertRaisesRegex(SDKError, 'native build tools require GCC16'):
            validate_profile(previous_major, receipt['sources']['os']['commit'], 'aarch64')
        signature['host_tools']['c++']['gcc_version'] = '16.2.0'
        with self.assertRaisesRegex(SDKError, 'Mixed native compiler identity'):
            validate_profile(profile, receipt['sources']['os']['commit'], 'aarch64')

    def test_release_rejects_incomplete_installed_libedit(self):
        for failure in ('missing_libedit', 'missing_histedit', 'missing_editline_marker', 'wrong_editline_marker',
                        'wrong_libedit_soname', 'extra_libedit_provider'):
            with self.subTest(failure=failure):
                root = self.root / failure; root.mkdir()
                options = release_fixture(root, 'aarch64')
                write(root / 'control.json', json.dumps({failure: True}))
                with self.assertRaises((SDKError, OSError)):
                    OSReleaseBuilder(options).run()
                output = Path(options['destination'])
                self.assertFalse((output / 'build-success').exists())
                self.assertFalse((output / 'build-profile.json').exists())

    def test_release_accepts_unchanged_libedit_after_unrelated_ports_commit(self):
        options = release_fixture(self.root, 'aarch64')
        source = Path(options['libedit-source'])
        prepared = jsonio.read_json(source / 'libedit-source.json')
        ports = Path(options['ports'])
        write(ports / 'unrelated-package.txt', 'unrelated change\n')
        commit(ports)
        receipt = OSReleaseBuilder(options).run()
        profile = jsonio.read_json(Path(options['destination']) / 'build-profile.json')
        historical = prepared['recipe']['ports_commit']
        self.assertNotEqual(historical, receipt['sources']['ports']['commit'])
        self.assertEqual(profile['sdk_receipt']['signature']['libedit'], prepared)
        validate_profile(profile, receipt['sources']['os']['commit'], 'aarch64')

    def test_release_checks_each_checkout_with_git_once(self):
        # The start-of-run clean check is the only Git status of a release:
        # no recheck after the build, none by libedit, none in finalization.
        from collections import Counter
        from emberbsd_sdk import provenance
        from emberbsd_sdk.os_release_builder import finalize_release
        class ClangRelease(OSReleaseBuilder):
            compiler = llvm_fixtures.ClangBuilder.compiler
        builder, _ = llvm_fixtures.LLVMBuilderTests.builder(
            self, release_fixture(self.root, 'aarch64'), ClangRelease)
        builder.options['base-cxx'] = False
        calls, real = [], provenance.git_source
        def counted(root, *args, **kwargs):
            calls.append(os.path.realpath(root))
            return real(root, *args, **kwargs)
        with patch.object(provenance, 'git_source', side_effect=counted):
            builder.run()
            built = list(calls)
            output = Path(builder.paths['destination'])
            (output / 'build-profile.json').unlink()
            (output / 'build-success').unlink()
            finalize_release(output)
        self.assertTrue(built)
        self.assertEqual([n for n in Counter(built).values() if n != 1], [], Counter(built))
        self.assertEqual(calls[len(built):], [])

    def test_finalize_inspected_release_and_repeat(self):
        from emberbsd_sdk.os_release_builder import finalize_release
        class ClangRelease(OSReleaseBuilder):
            compiler = llvm_fixtures.ClangBuilder.compiler
        builder, _ = llvm_fixtures.LLVMBuilderTests.builder(
            self, release_fixture(self.root, 'aarch64'), ClangRelease)
        builder.options['base-cxx'] = False
        receipt = builder.run()
        output = Path(builder.paths['destination'])
        expected = jsonio.read_json(output / 'build-profile.json')
        (output / 'build-profile.json').unlink()
        (output / 'build-success').unlink()
        receipt_path = output / '.prebuild-sdk-build.json'
        original_receipt = receipt_path.read_bytes()
        for mutation in ('incomplete', 'failed', 'output', 'marker', 'old-receipt', 'runtime'):
            with self.subTest(mutation=mutation):
                if mutation in ('incomplete', 'failed', 'old-receipt'):
                    bad = copy.deepcopy(receipt)
                    if mutation == 'incomplete':
                        bad['qualification'] = 'incomplete'
                    elif mutation == 'old-receipt':
                        # A git-archive era receipt cannot be finalized without its export.
                        bad['version'] = 4
                    else:
                        bad['commands'][0]['status'] = 'failed'
                    jsonio.write_json(receipt_path, bad, exclusive=False)
                    changed = receipt_path
                    prior = original_receipt
                elif mutation == 'output':
                    changed = output / next(iter(receipt['outputs']))
                    prior = changed.read_bytes(); changed.write_bytes(prior + b'x')
                elif mutation == 'marker':
                    changed = output / 'dest/usr/share/emberbsd/ports/editline.distinfo'
                    prior = changed.read_bytes(); changed.write_bytes(b'changed')
                else:
                    # A reused stage is named by its manifest, so a missing
                    # file is refused without hashing the others again.
                    stage = Path(receipt['paths']['llvm-runtime-stage'])
                    changed = next(stage / name for name in receipt['signature']['runtime']['manifest']['files'])
                    prior = changed.read_bytes(); changed.unlink()
                try:
                    with self.assertRaises((SDKError, OSError)):
                        finalize_release(output)
                    self.assertFalse((output / 'build-success').exists())
                finally:
                    changed.write_bytes(prior)
        incomplete = output / '.prebuild-sdk-build-incomplete'
        incomplete.write_text('interrupted')
        with self.assertRaises(SDKError):
            finalize_release(output)
        incomplete.unlink()
        premature = output / 'build-success'
        premature.write_text(receipt['sources']['os']['commit'] + '\n')
        with self.assertRaises(SDKError):
            finalize_release(output)
        self.assertFalse((output / 'build-profile.json').exists())
        premature.unlink()
        existing = output / 'build-profile.json'
        existing.write_text('not a matching profile')
        with self.assertRaises(SDKError):
            finalize_release(output)
        self.assertEqual(existing.read_text(), 'not a matching profile')
        self.assertFalse((output / 'build-success').exists())
        existing.unlink()
        finalized = finalize_release(output)
        self.assertEqual(finalized, expected)
        self.assertEqual(jsonio.read_json(output / 'build-profile.json'), expected)
        self.assertEqual(finalize_release(output), expected)
        self.assertEqual((output / 'build-success').read_text(), receipt['sources']['os']['commit'] + '\n')
        # A profile published before interruption is finished without replacement.
        (output / 'build-success').unlink()
        before = (output / 'build-profile.json').read_bytes()
        self.assertEqual(finalize_release(output), expected)
        self.assertEqual((output / 'build-profile.json').read_bytes(), before)
        # A historical SDK commit remains the recorded build provenance.
        (output / 'build-success').unlink()
        (output / 'build-profile.json').unlink()
        historical = copy.deepcopy(receipt)
        historical['sources']['sdk']['commit'] = 'a' * 40
        jsonio.write_json(receipt_path, historical, exclusive=False)
        recovered = finalize_release(output)
        self.assertEqual(recovered['sdk_receipt']['sources']['sdk']['commit'], 'a' * 40)

    def test_finalize_installed_base_runtime(self):
        from emberbsd_sdk.os_release_builder import finalize_release
        class ClangRelease(OSReleaseBuilder):
            compiler = llvm_fixtures.ClangBuilder.compiler
        builder, _ = llvm_fixtures.LLVMBuilderTests.builder(
            self, release_fixture(self.root, 'aarch64'), ClangRelease)
        with patch('emberbsd_sdk.native_runtime_install.NativeRuntimeInstaller',
                   FixtureNativeRuntimeInstaller):
            receipt = builder.run()
        output = Path(builder.paths['destination'])
        expected = jsonio.read_json(output / 'build-profile.json')
        self.assertEqual(receipt['version'], 8)
        (output / 'build-profile.json').unlink()
        (output / 'build-success').unlink()
        runtime = receipt['signature']['runtime']['manifest']
        changed = output / 'dest' / next(iter(runtime['files']))
        prior = changed.read_bytes()
        changed.write_bytes(prior + b'x')
        with self.assertRaises(SDKError):
            finalize_release(output)
        changed.write_bytes(prior)
        alias = output / 'dest/usr/lib/libc++.so'
        target = os.readlink(alias)
        alias.unlink()
        alias.symlink_to('wrong')
        with self.assertRaises((SDKError, OSError)):
            finalize_release(output)
        alias.unlink()
        alias.symlink_to(target)
        self.assertEqual(finalize_release(output), expected)

    def test_profile_binds_libedit_recipe_and_command(self):
        options = release_fixture(self.root, 'aarch64')
        receipt = OSReleaseBuilder(options).run()
        profile = jsonio.read_json(Path(options['destination']) / 'build-profile.json')
        for mutation in ('missing', 'ports', 'tree', 'command', 'installation'):
            with self.subTest(mutation=mutation):
                changed = copy.deepcopy(profile)
                signature = changed['sdk_receipt']['signature']
                if mutation == 'missing':
                    del signature['libedit']
                elif mutation == 'ports':
                    signature['libedit']['recipe']['ports_commit'] = 'invalid'
                elif mutation == 'tree':
                    signature['libedit']['tree_sha256'] = 'bad'
                elif mutation == 'installation':
                    changed['sdk_receipt']['libedit_installation']['distinfo_sha256'] = 'f' * 64
                else:
                    changed['commands'][0] = [v for v in changed['commands'][0] if not v.startswith('EMBER_LIBEDIT_SOURCE=')]
                with self.assertRaises((SDKError, KeyError)):
                    validate_profile(changed, receipt['sources']['os']['commit'], 'aarch64')

    def test_actual_aarch64_dispatch_prepare_and_profile(self):
        options = release_fixture(self.root, 'aarch64', '2.7+fixture')
        with patch.dict(os.environ, {'NOCHECKFLIST': 'yes', 'NOGCCERROR': 'yes', 'MAKEFLAGS': '-i', 'EMBER_VERSION': '9.9'}):
            receipt = OSReleaseBuilder(options).run()
        output = Path(options['destination'])
        p = jsonio.read_json(output / 'build-profile.json')
        validate_profile(p, receipt['sources']['os']['commit'], 'aarch64')
        observed = [json.loads(line) for line in (output / 'observed-argv.jsonl').read_text().splitlines()]
        self.assertEqual(observed, [a[2:] for a in p['commands']])
        self.assertEqual(p['schema'], 6)
        self.assertEqual(p['product_version'], '2.7+fixture')
        self.assertFalse(any(a.startswith('EMBER_VERSION=') for c in p['commands'] for a in c))
        self.assertFalse(any('HAVE_GCC=' in a for c in p['commands'] for a in c))
        self.assertTrue(all('TOOLCHAIN_MISSING=yes' in c for c in p['commands']))
        manifest = kit.prepare(output, self.root / 'kit', 'aarch64-sysroot')
        self.assertEqual(manifest['schema'], 'emberbsd.aarch64.sysroot.v4')
        self.assertIsNone(manifest['qualification'])
        self.assertEqual(manifest['product_version'], '2.7+fixture')
        self.assertIn('EmberBSD 2.7+fixture', GitHub(self.root / 'kit', 'aarch64-sysroot').title)
        self.assertIn('EmberBSD 2.7+fixture', (self.root / 'kit/release-notes.md').read_text())
        reseal(self.root / 'kit', lambda m: m.update(product_version='9.9'))
        with self.assertRaisesRegex(SDKError, 'product versions differ'):
            kit.verify(self.root / 'kit', 'aarch64-sysroot')
        before = snapshot(output)
        with self.assertRaisesRegex(SDKError, 'new or empty'):
            OSReleaseBuilder(options).run()
        self.assertEqual(snapshot(output), before)

    def test_release_source_identity_is_the_git_tree(self):
        options = release_fixture(self.root, 'aarch64')
        receipt = OSReleaseBuilder(options).run()
        output = Path(options['destination'])
        p = jsonio.read_json(output / 'build-profile.json')
        sha = receipt['sources']['os']['commit']
        tree = subprocess.run(['git', '-C', options['os'], 'rev-parse', sha + '^{tree}'], check=True, capture_output=True,
                              env=git_environment()).stdout.decode().strip()
        self.assertEqual(p['source_tree'], tree)
        self.assertNotIn('source_sha256', p)
        # No git-archive export is written, hashed or published.
        self.assertFalse((output / 'source.tar.gz').exists())
        # The product version comes from the commit, not the working tree.
        own = Path(options['os'], 'share/mk/bsd.own.mk')
        committed = own.read_text()
        own.write_text('EMBER_VERSION?= 9.9\n')
        try:
            self.assertEqual(kit.git_source_identity('git', options['os'], sha), (tree, '1.0'))
            manifest = kit.prepare(output, self.root / 'kit', 'aarch64-sysroot')
        finally:
            own.write_text(committed)
        self.assertEqual((manifest['schema'], manifest['source_tree'], manifest['product_version']),
                         ('emberbsd.aarch64.sysroot.v4', tree, '1.0'))
        self.assertNotIn('source_archive_sha256', manifest)
        self.assertNotIn(kit.SOURCE_EXPORT, [f['name'] for f in manifest['files']])
        self.assertIn('Git commit and tree hash', (self.root / 'kit/release-notes.md').read_text())
        reseal(self.root / 'kit', lambda m: m.update(source_tree='0' * 40))
        with self.assertRaisesRegex(SDKError, 'Source/build receipt changed'):
            kit.verify(self.root / 'kit', 'aarch64-sysroot')
        foreign = dict(p, source_tree='0' * 40)
        jsonio.write_json(output / 'build-profile.json', foreign, exclusive=False)
        with self.assertRaisesRegex(SDKError, 'OS source tree differs'):
            kit.prepare(output, self.root / 'foreign-kit', 'aarch64-sysroot')
        with self.assertRaisesRegex(SDKError, 'source tree identity'):
            validate_profile(dict(p, source_sha256='0' * 64), sha, 'aarch64')

    def test_failures_never_expose_success(self):
        for failure in ('kernel=EMBER64', 'sets', 'omit_comp'):
            with self.subTest(failure=failure):
                root = self.root / failure.replace('=', '-')
                root.mkdir()
                options = release_fixture(root, 'aarch64')
                write(root / 'control.json', json.dumps({'omit_comp': True} if failure == 'omit_comp' else {'fail': failure}))
                with self.assertRaises((SDKError, OSError)):
                    OSReleaseBuilder(options).run()
                output = Path(options['destination'])
                self.assertFalse((output / 'build-success').exists())
                self.assertFalse((output / 'build-profile.json').exists())
                self.assertEqual(jsonio.read_json(output / OSReleaseBuilder.RECEIPT)['qualification'], 'incomplete')

    def test_amd64_dispatch_and_read_only_plan(self):
        options = release_fixture(self.root, 'amd64', '2.7+fixture')
        before = snapshot(self.root)
        plan = OSReleaseBuilder(options | {'plan': True}).run()
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(plan['plan'][1][-2:], ['tools', 'release'])
        self.assertIn('EMBER_HOST_BFD_CONFIG=' + options['host-binutils'] + '/host-bfd.mk', plan['plan'][1])
        self.assertEqual(plan['plan'][2][-1], 'install_image')
        self.assertEqual(plan['plan'][3][-1], 'iso-image')
        receipt = OSReleaseBuilder(options).run()
        p = jsonio.read_json(Path(options['destination']) / 'build-profile.json')
        validate_profile(p, receipt['sources']['os']['commit'], 'amd64')
        for flag in ('KERNEL_SETS=GENERIC', 'BUILD_KERNELS=INSTALL', 'MAKEFLAGS=KERNEL_SETS=GENERIC BUILD_KERNELS=INSTALL', 'MKKMOD=yes'):
            self.assertIn(flag, p['commands'][0])
        self.assertEqual(set(p['output_sha256']), {'release/images/' + n for n in kit.images('2.7+fixture')})

    def test_compatible_release_objects_reused_for_new_destination(self):
        options = release_fixture(self.root, 'aarch64')
        first = OSReleaseBuilder(options).run()
        second = OSReleaseBuilder(options | {'destination': str(Path(options['sdk']) / 'stages/next-release')}).run()
        self.assertEqual(first['signature'], second['signature'])
        self.assertIsNotNone(second['previous_contexts']['objects'])

    def test_os_commit_change_updates_kept_objects_incrementally(self):
        options = release_fixture(self.root, 'aarch64')
        first = OSReleaseBuilder(options).run()
        self.assertEqual(first['object_mode'], 'incremental')
        write(Path(options['os']) / 'usr.bin/config/main.c', 'int changed;\n')
        commit(options['os'])
        # Same profile, toolchain and configuration: build.sh -u updates the
        # kept objects although the commit metadata flags differ.
        second = OSReleaseBuilder(options | {'destination': str(Path(options['sdk']) / 'stages/next-release')}).run()
        self.assertNotEqual(first['signature']['flags'], second['signature']['flags'])
        self.assertEqual(second['previous_contexts']['objects']['sources']['os']['commit'],
                         first['sources']['os']['commit'])
        self.assertIn('-u', second['plan'][1])
        profile = jsonio.read_json(Path(options['sdk']) / 'stages/next-release/build-profile.json')
        self.assertEqual(profile['sdk_receipt']['object_mode'], 'incremental')
        # Only the commit metadata is left out; other flags are compared.
        signature = dict(flags=['MKREPRO_TIMESTAMP=1', 'EMBER_BUILD_ID=a', 'BUILDINFO=EmberBSD:a', 'MKFOO=yes'])
        selected = ProductionReleaseBuilder.__new__(ProductionReleaseBuilder)
        self.assertEqual(selected.reuse_signature(signature), dict(flags=['MKFOO=yes']))

    def test_fresh_object_mode_needs_new_trees_and_is_recorded(self):
        options = release_fixture(self.root, 'aarch64')
        OSReleaseBuilder(options).run()
        later = options | {'destination': str(Path(options['sdk']) / 'stages/fresh'), 'object-mode': 'fresh'}
        with self.assertRaisesRegex(SDKError, 'fresh object mode needs a new or empty objects'):
            OSReleaseBuilder(later).run()
        self.assertFalse(Path(later['destination'], 'build-success').exists())
        # The kept tools are refused too; the LLVM coordinator test covers the
        # tools that its own OS phases create in the same run.
        selected = OSReleaseBuilder(later | {'objects': str(Path(options['sdk']) / 'obj/fresh-objects')})
        with patch.object(OSReleaseBuilder, 'compatible_context', return_value=True):
            with self.assertRaisesRegex(SDKError, 'fresh object mode needs a new or empty tools'):
                selected.run()
        later['objects'] = str(Path(options['sdk']) / 'obj/fresh-objects')
        later['tools'] = str(Path(options['sdk']) / 'obj/fresh-tools')
        receipt = OSReleaseBuilder(later).run()
        self.assertEqual(receipt['object_mode'], 'fresh')
        self.assertEqual(receipt['previous_contexts'], {'objects': None, 'tools': None})
        profile = jsonio.read_json(Path(later['destination']) / 'build-profile.json')
        self.assertEqual(profile['sdk_receipt']['object_mode'], 'fresh')
        with self.assertRaisesRegex(SDKError, 'object mode must be'):
            OSReleaseBuilder(options | {'object-mode': 'reuse'})

    def test_wrong_compiler_and_dirty_source_before_output(self):
        options = release_fixture(self.root, 'aarch64')
        write(self.root / 'control.json', '{"version":"12.5.0"}')
        with self.assertRaisesRegex(SDKError, 'GCC 16.2.0'):
            OSReleaseBuilder(options).run()
        self.assertFalse(Path(options['destination']).exists())
        write(self.root / 'control.json', '{}')
        write(Path(options['os']) / 'build.sh', 'exit 99\n')
        with self.assertRaises(SDKError):
            OSReleaseBuilder(options).run()
        self.assertFalse(Path(options['destination']).exists())


class GitHubTests(TemporaryCase):
    def setUp(self):
        super().setUp()
        self.build = legacy_build(self.root)
        self.directory = self.root / 'kit'
        m = kit.prepare(self.build, self.root / 'unqualified', 'aarch64-sysroot')
        self.m = kit.prepare(self.build, self.directory, 'aarch64-sysroot', qualify(self.build, m, 'aarch64-sysroot'))
        self.select(self.directory)
        self.enabled = True
        self.status = 'ahead'

    def select(self, directory):
        self.directory = directory
        self.calls = []
        self.p = GitHub(self.directory, 'aarch64-sysroot', self.runner)
        self.release = dict(tag_name=self.p.tag, draft=True, immutable=False, prerelease=True, name=self.p.title,
                            body=(self.directory / 'release-notes.md').read_text(), assets=self.assets(), html_url=self.p.url,
                            target_commitish=self.p.sha)
        self.refs = [{'ref': 'refs/tags/' + self.p.tag, 'object': {'type': 'commit', 'sha': self.p.sha}}]

    def assets(self):
        return [dict(name=n, state='uploaded', size=(self.directory / n).stat().st_size,
                     digest='sha256:' + kit.sha256_file(self.directory / n)) for n in self.p.names]

    def runner(self, *args):
        self.calls.append(args)
        if args[0] == 'api':
            endpoint = args[-1]
            if endpoint.endswith('immutable-releases'): value = {'enabled': self.enabled}
            elif '/compare/' in endpoint: value = {'status': self.status}
            elif '/matching-refs/' in endpoint: value = self.refs
            elif '/releases/tags/' in endpoint:
                if self.release is None: return 'HTTP/2.0 404 Not Found\r\n\r\n{}'
                value = self.release
            elif '/releases?' in endpoint: value = []
            else: raise AssertionError(endpoint)
            return 'HTTP/2.0 200 OK\r\n\r\n' + json.dumps(value)
        if args[:2] == ('release', 'download'):
            destination = Path(args[args.index('--dir') + 1])
            for name in self.p.names: shutil.copyfile(self.directory / name, destination / name)
        elif args[:2] == ('release', 'edit'):
            self.release.update(draft=False, immutable=True)
        elif args[:2] == ('release', 'upload'):
            self.release['assets'] = self.assets()
        return ''

    def assert_no_write(self):
        self.assertFalse(any(a[:2] in (('release', 'edit'), ('release', 'upload'), ('release', 'create')) for a in self.calls))

    def test_identical_draft_and_immutable_retry(self):
        self.assertEqual(self.p.draft(), self.p.url)
        self.assert_no_write()
        self.assertEqual(self.p.publish(), self.p.url)
        edits = sum(a[:2] == ('release', 'edit') for a in self.calls)
        self.assertEqual(edits, 1)
        self.assertEqual(self.p.publish(), self.p.url)
        self.assertEqual(sum(a[:2] == ('release', 'edit') for a in self.calls), 1)
        self.assertTrue(any(a[:2] == ('release', 'verify-asset') for a in self.calls))

    def test_kit_without_qualification_publishes_on_request(self):
        self.select(self.root / 'unqualified')
        self.assertIsNone(kit.verify(self.directory, 'aarch64-sysroot')['qualification'])
        notes = (self.directory / 'release-notes.md').read_text()
        self.assertNotIn('Do not publish', notes)
        self.assertIn('No target qualification receipt is bound to this kit.', notes)
        self.assertEqual(self.p.publish(), self.p.url)
        self.assertEqual(sum(a[:2] == ('release', 'edit') for a in self.calls), 1)
        self.assertTrue(any(a[:2] == ('release', 'verify-asset') for a in self.calls))

    def test_foreign_tag_and_disabled_immutability(self):
        for state in ('tag', 'setting', 'source'):
            with self.subTest(state=state):
                self.refs[0]['object']['sha'] = 'b' * 40 if state == 'tag' else self.p.sha
                self.enabled = state != 'setting'
                self.status = 'behind' if state == 'source' else 'ahead'
                with self.assertRaises(SDKError): self.p.draft()
                self.assert_no_write()

    def test_foreign_metadata_or_asset_stops_publication(self):
        baseline = copy.deepcopy(self.release)
        for field in ('name', 'body', 'prerelease', 'digest', 'extra', 'duplicate', 'missing'):
            with self.subTest(field=field):
                self.release = copy.deepcopy(baseline)
                if field in ('name', 'body', 'prerelease'): self.release[field] = False if field == 'prerelease' else 'foreign'
                elif field == 'digest': self.release['assets'][0]['digest'] = 'sha256:' + '0' * 64
                elif field == 'extra': self.release['assets'].append(dict(name='foreign'))
                elif field == 'duplicate': self.release['assets'].append(self.release['assets'][0])
                else: self.release['assets'].pop()
                with self.assertRaises(SDKError): self.p.publish()
                self.assert_no_write()

    def test_published_release_never_accepts_asset_writes(self):
        self.release.update(draft=False, immutable=True)
        with self.assertRaisesRegex(SDKError, 'already published'): self.p.draft()
        self.assert_no_write()
        self.release['immutable'] = False
        with self.assertRaisesRegex(SDKError, 'not immutable'): self.p.verify_remote()
        self.assert_no_write()

    def test_resume_missing_asset_without_clobber(self):
        self.release['assets'].pop()
        self.p.draft()
        uploads = [a for a in self.calls if a[:2] == ('release', 'upload')]
        self.assertEqual(len(uploads), 1)
        self.assertNotIn('--clobber', uploads[0])
        self.assertFalse(any(a[:2] == ('release', 'edit') for a in self.calls))

    def test_hidden_draft_pagination(self):
        def runner(*args):
            endpoint = args[-1]
            if '/releases/tags/' in endpoint: return 'HTTP/2.0 404 Not Found\n\n{}'
            if endpoint.endswith('page=1'): value = [{'tag_name': 'unrelated'}] * 100
            elif endpoint.endswith('page=2'): value = [{'tag_name': self.p.tag, 'id': 200}]
            elif endpoint.endswith('/releases/200'): value = self.release
            else: return self.runner(*args)
            return 'HTTP/2.0 200 OK\n\n' + json.dumps(value)
        self.p.runner = runner
        self.refs = []
        self.assertEqual(self.p.preflight(), self.release)

    def test_changed_local_kit_stops_before_network(self):
        (self.directory / 'release-notes.md').write_text('changed')
        with self.assertRaises(SDKError): self.p.publish()
        self.assertEqual(self.calls, [])

class Amd64KitTests(TemporaryCase):
    def test_historical_kit_compatibility_and_mixed_components(self):
        directory = self.root / 'kit'
        directory.mkdir()
        sha = 'a' * 40
        names = kit.images('1.0') + ['release-notes.md'] + [n + '.tar.xz' for n in kit.SETS]
        for name in names: write(directory / name, b'abc')
        digest = hashlib.sha256(b'abc').hexdigest()
        components = {n: dict(source_commit=sha) for n in ('image', 'kernel', 'modules', 'base')}
        components['kernel'].update(buildinfo='EmberBSD:' + sha, sha256=digest)
        m = dict(schema=1, repository=kit.REPOSITORY, source_commit=sha, source_archive_sha256=digest,
                 release_tag=kit.tag(sha, 'amd64'), architecture='amd64', product_version='1.0', base_version='11.0',
                 profile=dict(MKX11='no', MKRUMP='no', MKGCCCMDS='no', KERNEL_SETS='GENERIC'), components=components,
                 files=[kit.asset(directory, n) for n in names], qualification=None)
        kit.seal(directory, m)
        self.assertEqual(kit.verify(directory, 'amd64'), m)
        before = snapshot(directory)
        kit.verify(directory, 'amd64')
        self.assertEqual(snapshot(directory), before)
        alternate = self.root / 'alternate'
        shutil.copytree(directory, alternate)
        dynamic = copy.deepcopy(m)
        dynamic['product_version'] = '2.7+fixture'
        for entry, old_name, new_name in zip(dynamic['files'][:2], kit.images('1.0'), kit.images('2.7+fixture')):
            (alternate / old_name).rename(alternate / new_name)
            entry['name'] = new_name
        (alternate / 'manifest.json').unlink()
        kit.seal(alternate, dynamic)
        self.assertEqual(kit.verify(alternate, 'amd64'), dynamic)
        self.assertIn('EmberBSD 2.7+fixture', GitHub(alternate, 'amd64').title)
        for field in ('image', 'kernel', 'modules', 'base'):
            broken = copy.deepcopy(m)
            broken['components'][field]['source_commit'] = 'b' * 40
            jsonio.write_json(directory / 'manifest.json', broken, exclusive=False)
            (directory / 'SHA256').write_text(kit.checksum_text(directory, names + ['manifest.json']))
            with self.assertRaisesRegex(SDKError, 'Mixed ' + field): kit.verify(directory, 'amd64')

    def test_prepare_actual_set_bytes_and_checker_provenance(self):
        build = self.root / 'build'
        build.mkdir()
        sha = 'a' * 40
        write(build / 'build-success', sha + '\n')
        write(build / 'source.tar', tar_bytes({'share/mk/bsd.own.mk': b'EMBER_VERSION?= 2.7+fixture\n'}, mode='w'))
        source = self.root / 'checker'
        write(source / 'ember/tests/installer-image.py',
              'import sys\nassert sys.argv[2:] == [' + repr(sha) + ", '2.7+fixture', 'github.com']\n")
        subprocess.run(['git', 'init', '-q', str(source)], check=True, env=git_environment())
        commit(source)
        kernel = header(62) + f'EmberBSD:{sha}\n\0'.encode()
        sets = {name + '.tar.xz': tar_bytes({'./netbsd': kernel} if name == 'kern-GENERIC' else {'./fixture': b'fixture'}) for name in kit.SETS}
        sums = ''.join(f'SHA512 ({name}) = {hashlib.sha512(data).hexdigest()}\n' for name, data in sets.items())
        files = {'amd64/binary/sets/' + n: data for n, data in sets.items()}
        files.update({'amd64/binary/sets/SHA512': sums.encode(), 'netbsd': gzip.compress(kernel)})
        iso = build / 'release/images' / kit.images('2.7+fixture')[0]
        write(iso, tar_bytes(files, mode='w'))
        write(build / 'release/images' / kit.images('2.7+fixture')[1], gzip.compress(b'USB fixture'))
        directory = self.root / 'kit'
        m = kit.prepare(build, directory, 'amd64', os_source=source)
        self.assertEqual(m['components']['kernel']['sha256'], hashlib.sha256(kernel).hexdigest())
        self.assertEqual(m['source_commit'], sha)
        self.assertNotEqual(m['image_checker']['source_commit'], sha)
        self.assertEqual(m['image_checker']['sha256'], kit.sha256_file(source / 'ember/tests/installer-image.py'))
        self.assertEqual((directory / 'base.tar.xz').read_bytes(), sets['base.tar.xz'])
        q = qualify(build, m, 'amd64')
        qualified = kit.prepare(build, self.root / 'qualified', 'amd64', q, source)
        kit.require_qualification(qualified, 'amd64')
        for name in kit.VM_CHECKS:
            broken = copy.deepcopy(qualified)
            broken['qualification']['checks'][name] = False
            with self.assertRaises(SDKError): kit.require_qualification(broken, 'amd64')
        files['netbsd'] = b'foreign kernel'
        iso.write_bytes(tar_bytes(files, mode='w'))
        with self.assertRaisesRegex(SDKError, 'boot kernel differs'):
            kit.prepare(build, self.root / 'mixed-kernel', 'amd64', os_source=source)
