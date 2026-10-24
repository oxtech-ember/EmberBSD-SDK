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

from test_core import TemporaryCase, header, SDK_SOURCE
from test_builder import fixture, write, commit, snapshot
from libedit_fixture import installation_source
from emberbsd_sdk import os_release as kit, jsonio
from emberbsd_sdk.os_release_builder import OSReleaseBuilder, validate_profile
from emberbsd_sdk.os_release_github import GitHub
from emberbsd_sdk.errors import SDKError


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

    def test_qualification_binding_and_local_publication_refusal(self):
        calls = []
        with self.assertRaisesRegex(SDKError, 'qualification is required'):
            GitHub(self.directory, 'aarch64-sysroot', lambda *a: calls.append(a)).publish()
        self.assertEqual(calls, [])
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
    base, comp = set_files()
    # This producer emits archives and kernel identity bytes. It never compiles.
    producer = '''import json, os, sys, gzip, hashlib, io, tarfile
from pathlib import Path
args = sys.argv[1:]
def value(k): return args[args.index(k)+1]
control = json.loads(Path(CONTROL).read_text())
if any(os.environ.get(k) for k in ('NOCHECKFLIST', 'NOGCCERROR', 'MAKEFLAGS', 'CFLAGS', 'EMBER_VERSION')): raise SystemExit('inherited control')
release = Path(value('-R')); output = release.parent
version = Path(__file__).with_name('share').joinpath('mk/bsd.own.mk').read_text().split('=',1)[1].strip()
with (output / 'observed-argv.jsonl').open('a') as handle: handle.write(json.dumps(args)+'\\n')
if control.get('fail') == args[-1]: raise SystemExit(77)
sha = next(a.split('=',1)[1] for a in args if a.startswith('EMBER_BUILD_ID='))
kernel = ELF + ('EmberBSD:'+sha+'\\n\\0').encode()
if args[-1] == 'kernel=EMBER64':
    path = Path(value('-O'))/'kernels/EMBER64/netbsd'; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(kernel)
elif args[-1] == 'sets':
    path = release/'evbarm-aarch64/binary/sets'; path.mkdir(parents=True)
    (path/'base.tar.xz').write_bytes(BASE)
    if not control.get('omit_comp'): (path/'comp.tar.xz').write_bytes(COMP)
elif args[-1] == 'release':
    path = release/'images'; path.mkdir(parents=True)
    (path/f'EmberBSD-{version}-amd64-install.img.gz').write_bytes(gzip.compress(b'USB fixture'))
    wrapper = Path(value('-T'))/'bin/nbmake-amd64'; wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_text('#!/bin/sh\\nexit 0\\n'); wrapper.chmod(0o755)
elif args[-1] == 'iso-image':
    path=release/f'images/EmberBSD-{version}-amd64.iso'; path.write_bytes(b'ISO orchestration fixture')
else: raise SystemExit('unexpected operation')
'''
    producer = ('CONTROL=' + repr(str(root / 'control.json')) + '\nELF=' + repr(header(183 if target == 'aarch64' else 62)) +
                '\nBASE=' + repr(tar_bytes(base)) + '\nCOMP=' + repr(tar_bytes(comp)) + '\n') + producer
    producer += "dest = Path(value('-D'))\n" + installation_source(183 if target == 'aarch64' else 62, arguments='args', control='control')
    write(source / 'fixture-producer.py', producer)
    commit(source)
    options['destination'] = str(Path(options['sdk']) / 'stages/release')
    return options


class BuilderReleaseTests(TemporaryCase):
    def test_native_schema3_identity_and_historical_schema2(self):
        from emberbsd_sdk import native_compiler
        options = release_fixture(self.root, 'aarch64')
        receipt = OSReleaseBuilder(options).run()
        output = Path(options['destination'])
        profile = jsonio.read_json(output / 'build-profile.json')
        sha = receipt['sources']['os']['commit']
        self.assertEqual(profile['schema'], 3)
        self.assertEqual(profile['sdk_receipt']['version'], 3)
        for mutation in ('missing', 'host-toolchain', 'host-cc', 'host-cxx', 'host-toolchain-manifest', 'cxx-role'):
            changed = copy.deepcopy(profile)
            signature = changed['sdk_receipt']['signature']
            if mutation == 'missing':
                del signature['native_compiler']
            elif mutation == 'cxx-role':
                del signature['native_compiler']['compilers']['cxx']['macros']['__cplusplus']
                signature['host_tools'] = native_compiler.host_tools(signature['native_compiler'])
            else:
                changed['sdk_receipt']['paths'][mutation] = '/different/provider'
            with self.subTest(mutation=mutation), self.assertRaises((SDKError, KeyError)):
                validate_profile(changed, sha, 'aarch64')
        # Historical verification does not manufacture the new native identity.
        historical = copy.deepcopy(profile)
        historical['schema'] = historical['sdk_receipt']['version'] = 2
        del historical['sdk_receipt']['signature']['native_compiler']
        for key in native_compiler.OPTIONS:
            del historical['sdk_receipt']['paths'][key]
        validate_profile(historical, sha, 'aarch64')
        jsonio.write_json(output / 'build-profile.json', historical, exclusive=False)
        result = kit.prepare(output, self.root / 'historical-schema2-kit', 'aarch64-sysroot')
        self.assertEqual(result['build_profile']['schema'], 2)
        self.assertNotIn('native_compiler', result['build_profile']['sdk_receipt']['signature'])
        kit.verify(self.root / 'historical-schema2-kit', 'aarch64-sysroot')

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
                    signature['libedit']['recipe']['ports_commit'] = 'f' * 40
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
        self.assertEqual(p['schema'], 3)
        self.assertEqual(p['product_version'], '2.7+fixture')
        self.assertFalse(any(a.startswith('EMBER_VERSION=') for c in p['commands'] for a in c))
        self.assertFalse(any('HAVE_GCC=' in a for c in p['commands'] for a in c))
        self.assertTrue(all('TOOLCHAIN_MISSING=yes' in c for c in p['commands']))
        manifest = kit.prepare(output, self.root / 'kit', 'aarch64-sysroot')
        self.assertEqual(manifest['schema'], 'emberbsd.aarch64.sysroot.v2')
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
        self.calls = []
        self.p = GitHub(self.directory, 'aarch64-sysroot', self.runner)
        self.release = dict(tag_name=self.p.tag, draft=True, immutable=False, prerelease=True, name=self.p.title,
                            body=(self.directory / 'release-notes.md').read_text(), assets=self.assets(), html_url=self.p.url,
                            target_commitish=self.p.sha)
        self.refs = [{'ref': 'refs/tags/' + self.p.tag, 'object': {'type': 'commit', 'sha': self.p.sha}}]
        self.enabled = True
        self.status = 'ahead'

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
        subprocess.run(['git', 'init', '-q', str(source)], check=True)
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
