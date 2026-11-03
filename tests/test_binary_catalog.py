# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Binary selection follows producer-resolved runtime edges, never build edges."""

from copy import deepcopy
import gzip
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk import binary_catalog as catalog
from emberbsd_sdk.jsonio import json_bytes


def graph():
    def node(name, dependencies=()):
        return dict(id=name, version='1.0', input_sha256='a' * 64,
                    dependencies=[dict(id=child, kind=kind) for child, kind in dependencies])
    return dict(schema='ports-graph-v1',
                context=dict(target='aarch64', triplet='aarch64--netbsd',
                             toolchain_sha256='a' * 64, base_sha256='b' * 64, shared_inputs={}),
                provenance=dict(source='Ports', revision='a' * 40), nodes=[
                    node('target:net/app', [('target:devel/lib', 'runtime'),
                                          ('target:devel/lib-devel', 'build'),
                                          ('host:devel/generator', 'tool')]),
                    node('target:net/other', [('target:devel/lib', 'runtime')]),
                    node('target:devel/lib'),
                    node('target:devel/lib-devel', [('target:devel/lib', 'runtime')]),
                    node('host:devel/generator')])


class ProjectionTests(unittest.TestCase):
    def test_empty_selection_is_empty(self):
        self.assertEqual(catalog.runtime_projection(graph(), [], 'target'), {})

    def test_only_runtime_dependencies_are_delivered(self):
        nodes = catalog.runtime_projection(graph(), ['target:net/app', 'target:net/other'], 'target')
        self.assertEqual(set(nodes), {'target:net/app', 'target:net/other', 'target:devel/lib'})
        self.assertEqual(nodes['target:net/app']['dependencies'], ['target:devel/lib'])

    def test_development_payload_is_explicit(self):
        nodes = catalog.runtime_projection(graph(), ['target:devel/lib-devel'], 'target')
        self.assertEqual(set(nodes), {'target:devel/lib-devel', 'target:devel/lib'})

    def test_kernel_tools_need_no_target_sysroot(self):
        self.assertEqual(set(catalog.runtime_projection(graph(), ['host:devel/generator'], 'host')),
                         {'host:devel/generator'})

    def test_wrong_role_and_missing_root_are_rejected(self):
        for root in ('host:devel/generator', 'target:missing/path'):
            with self.subTest(root=root), self.assertRaises(SDKError):
                catalog.runtime_projection(graph(), [root], 'target')

    def test_runtime_edge_may_not_cross_platform_roles(self):
        value = graph()
        value['nodes'][0]['dependencies'][2]['kind'] = 'runtime'
        with self.assertRaises(SDKError):
            catalog.runtime_projection(value, ['target:net/app'], 'target')

    def test_ambiguous_host_variants_are_rejected(self):
        value = graph()
        other = deepcopy(value['nodes'][-1])
        other['id'] += '@0123456789abcdef'
        value['nodes'].append(other)
        with self.assertRaises(SDKError):
            catalog.runtime_projection(value, [value['nodes'][-2]['id'], other['id']], 'host')

    def test_pkg_summary_repeated_fields_preserved(self):
        value = b'PKGNAME=app-1.0\nDEPENDS=lib>=1\nDEPENDS=tls>=2\nDESCRIPTION=\n\n'
        self.assertEqual(catalog.parse_summary(value)[0]['DEPENDS'], ['lib>=1', 'tls>=2'])

    def test_pkg_summary_rejects_duplicate_identity(self):
        with self.assertRaises(SDKError):
            catalog.parse_summary(b'PKGNAME=a-1\nPKGNAME=b-1\n\n')


@unittest.skipUnless(os.environ.get('EMBERBSD_TEST_HOST_PREFIX'), 'select native Ports pkg_tools')
class BinaryReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from emberbsd_sdk.host_prefix import load_host_prefix
        from emberbsd_sdk.release_signing import ReleaseCrypto
        cls.temporary = tempfile.TemporaryDirectory(prefix='binary-release-')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        cls.crypto = ReleaseCrypto(load_host_prefix(os.environ['EMBERBSD_TEST_HOST_PREFIX'],
            required_roles=('openssl', 'pkg_admin', 'pkg_info', 'pkg_create')))
        cls.ca = cls.crypto.create_root(cls.root / 'root')
        cls.leaf = cls.crypto.issue_signer(cls.ca, cls.root / 'leaf')

    def setUp(self):
        self.work = self.root / self._testMethodName
        self.work.mkdir()
        self.ports = self.work / 'ports'
        self.sets = self.ports / 'profiles/package-sets'
        self.sets.mkdir(parents=True)
        (self.sets / 'app.txt').write_text('net/app\n')
        (self.sets / 'other.txt').write_text('net/other\n')
        (self.sets / 'devel.txt').write_text('devel/lib-devel\n')
        (self.sets / 'kernel.txt').write_text('devel/generator@native\n')
        self.graph_path = self.work / 'graph.json'
        self.graph_path.write_bytes(json_bytes(graph()))
        self.spec = dict(schema='emberbsd.package-build.v1', release_id='test', created_at=1,
            sources={name: 'a' * 40 for name in ('os', 'ports', 'sdk')}, repositories=[])

    def package(self, root, *, opsys='NetBSD', arch='aarch64', depends=(), payload=b'fixture data\n'):
        name = root.rsplit('/', 1)[-1] + '-1.0'
        work = self.work / (name + '-' + opsys + '-' + arch)
        work.mkdir()
        (work / 'payload').write_bytes(payload)
        (work / 'PLIST').write_text(f'@name {name}\npayload\n')
        (work / 'BUILD_INFO').write_text(f'PKGPATH={root}\nOPSYS={opsys}\nMACHINE_ARCH={arch}\n'
                                       'OS_VERSION=11.0\nLICENSE=2-clause-bsd\n')
        (work / 'SIZE').write_text(str(len(payload)))
        output = work / (name + '.tgz')
        args = ['-c', '-fixture', '-d', '-fixture', '-f', work / 'PLIST', '-p', work,
                '-B', work / 'BUILD_INFO', '-s', work / 'SIZE']
        if depends:
            args += ['-P', ' '.join(depends)]
        self.crypto.run_package('pkg_create', [*args, output])
        return str(output)

    def repository(self, name='arm64', *, arch='aarch64'):
        repo = dict(id=name, host=None, target=dict(arch=arch, triplet=arch + '--netbsd',
                    abi='netbsd-11', runtime='libc++'), graph=str(self.graph_path),
                    sets=['app', 'other', 'devel'], packages={})
        for root in ('net/app', 'net/other', 'devel/lib', 'devel/lib-devel'):
            repo['packages']['target:' + root] = self.package(root, arch=arch,
                depends=('lib>=1',) if root != 'devel/lib' else ())
        self.spec['repositories'].append(repo)
        return repo

    def compose(self):
        from emberbsd_sdk.binary_release import compose
        return compose(self.crypto, self.spec, self.ports, self.work / 'release',
                       self.leaf, self.ca.certificate)

    def test_unchanged_signed_packages_keep_exact_bytes_in_next_catalog(self):
        from emberbsd_sdk.binary_release import compose
        repo = self.repository()
        self.compose()
        for root in repo['packages']:
            repo['packages'][root] = str(self.work / 'release/arm64' /
                                        (root.rsplit('/', 1)[-1] + '-1.0.tgz'))
        expected = {Path(path).name: Path(path).read_bytes() for path in repo['packages'].values()}
        self.spec['release_id'] = 'next'
        compose(self.crypto, self.spec, self.ports, self.work / 'next', self.leaf, self.ca.certificate)
        for name, data in expected.items():
            self.assertEqual((self.work / 'next/arm64' / name).read_bytes(), data)

    def test_signed_foreign_binary_does_not_bypass_target_inspection(self):
        repo = self.repository()
        unsigned = self.package('net/foreign', payload=b'\xcf\xfa\xed\xfe' + b'x' * 60)
        signed = self.work / 'foreign-1.0.tgz'
        self.crypto.sign_package(unsigned, signed, self.leaf, self.ca.certificate)
        from emberbsd_sdk.binary_release import inspect_package
        with self.assertRaisesRegex(SDKError, 'foreign host binary'):
            inspect_package(signed, repo['target'])

    def test_signed_extended_ar_filename_is_inspected_without_extraction(self):
        source = self.package('net/package-with-a-long-name', payload=b'odd payload')
        signed = self.work / 'package-with-a-long-name-1.0.tgz'
        self.crypto.sign_package(source, signed, self.leaf, self.ca.certificate)
        from emberbsd_sdk.binary_release import inspect_package
        self.assertEqual(inspect_package(signed, None), len(b'odd payload'))

    def test_signed_catalog_and_standard_summary_select_without_payloads(self):
        from emberbsd_sdk.binary_release import load_catalogs
        self.repository()
        release = self.compose()
        directory = self.work / 'release'
        summary = catalog.parse_summary(gzip.decompress((directory / 'arm64/pkg_summary.gz').read_bytes()))
        self.assertEqual(len(summary), 4)
        for record in summary:
            package = directory / 'arm64' / catalog.scalar(record, 'FILE_NAME')
            self.assertEqual(package.stat().st_size, int(catalog.scalar(record, 'FILE_SIZE')))
            package.unlink()  # Catalog selection cannot depend on cached archives.
        verified, catalogs = load_catalogs(self.crypto, directory / 'release.p7m', directory,
                                           self.ca.certificate, ['arm64'])
        self.assertEqual(release, verified)
        plan = catalog.select_packages(verified, catalogs, sets=['app', 'other'])
        self.assertEqual(len(plan['packages']), 3)
        self.assertFalse(any('devel-1' in item['path'] for item in plan['packages']))
        self.assertEqual(plan['unpacked_size'], 3 * len(b'fixture data\n'))
        self.assertEqual(catalog.select_packages(verified, catalogs)['download_size'], 0)
        self.assertEqual(len(catalog.select_packages(verified, catalogs, sets=['devel'])['packages']), 2)
        with self.assertRaises(SDKError):
            catalog.select_packages(verified, catalogs, sets=['missing'])
        path = directory / 'arm64/catalog.json'
        path.write_bytes(path.read_bytes() + b' ')
        with self.assertRaises(SDKError):
            load_catalogs(self.crypto, directory / 'release.p7m', directory, self.ca.certificate, ['arm64'])

    def test_wrong_architecture_rejected_before_publication(self):
        self.repository(arch='amd64')
        with self.assertRaises(SDKError):
            self.compose()
        self.assertFalse((self.work / 'release/release.p7m').exists())

    def test_foreign_host_package_rejected(self):
        repo = self.repository()
        repo['packages']['target:net/app'] = self.package('net/app', opsys='Darwin')
        with self.assertRaises(SDKError):
            self.compose()

    def test_foreign_host_library_in_target_payload_rejected(self):
        repo = self.repository()
        # Correct declared target metadata cannot hide a Mach-O payload.
        path = Path(repo['packages']['target:devel/lib'])
        path.unlink()
        import shutil
        shutil.rmtree(path.parent)
        repo['packages']['target:devel/lib'] = self.package('devel/lib', payload=b'\xcf\xfa\xed\xfe' + bytes(64))
        with self.assertRaises(SDKError):
            self.compose()

    def test_missing_runtime_binary_rejected(self):
        repo = self.repository()
        del repo['packages']['target:devel/lib']
        with self.assertRaises(SDKError):
            self.compose()

    def test_archive_dependency_missing_from_graph_rejected(self):
        self.repository()
        value = graph()
        value['nodes'][0]['dependencies'] = []
        self.graph_path.write_bytes(json_bytes(value))
        with self.assertRaises(SDKError):
            self.compose()

    def test_no_overwrite_of_existing_release(self):
        self.repository()
        (self.work / 'release').mkdir()
        (self.work / 'release/keep').write_text('existing')
        with self.assertRaises((OSError, SDKError)):
            self.compose()
        self.assertEqual((self.work / 'release/keep').read_text(), 'existing')

    def test_second_target_reuses_one_host_repository(self):
        from emberbsd_sdk.binary_release import load_catalogs
        self.repository()
        other = self.repository('amd64', arch='amd64')
        other_graph = graph()
        other_graph['context'].update(target='amd64', triplet='amd64--netbsd')
        other_path = self.work / 'amd64-graph.json'
        other_path.write_bytes(json_bytes(other_graph))
        other['graph'] = str(other_path)
        self.spec['repositories'].append(dict(id='host',
            host=dict(os='Darwin', arch='arm64', abi='macos-27', runtime='libSystem'), target=None,
            graph=str(self.graph_path), sets=['kernel'],
            packages={'host:devel/generator': self.package('devel/generator', opsys='Darwin')}))
        self.compose()
        directory = self.work / 'release'
        release, catalogs = load_catalogs(self.crypto, directory / 'release.p7m', directory,
                                          self.ca.certificate, ['host', 'arm64', 'amd64'])
        plan = catalog.select_packages(release, catalogs, sets=['kernel', 'app'])
        self.assertEqual(len(plan['packages']), 5)
        self.assertEqual(sum(item['kind'] == 'host-package' for item in plan['packages']), 1)
        kernel = catalog.select_packages(release, catalogs, sets=['kernel'])
        self.assertEqual(len(kernel['packages']), 1)
        self.assertIsNone(kernel['packages'][0]['target'])

    def test_standard_pkg_tools_version_constraints_are_checked(self):
        repo = self.repository()
        original = Path(repo['packages']['target:net/app'])
        import shutil
        shutil.rmtree(original.parent)
        repo['packages']['target:net/app'] = self.package('net/app', depends=('lib>=2',))
        with self.assertRaises(SDKError):
            self.compose()

    def test_cli_show_rejects_unknown_set_and_unsupported_package(self):
        self.repository()
        self.compose()
        directory = self.work / 'release'
        command = [sys.executable, str(Path(__file__).resolve().parents[1] / 'scripts/binary-catalog.py'),
            '--host-prefix', os.environ['EMBERBSD_TEST_HOST_PREFIX'], 'show',
            '--release', str(directory / 'release.p7m'), '--directory', str(directory),
            '--trust-root', str(self.ca.certificate)]
        for arguments in (['--set', 'typo'], ['--package', 'net/app']):
            with self.subTest(arguments=arguments):
                result = subprocess.run(command + arguments, capture_output=True)
                self.assertNotEqual(result.returncode, 0, result.stdout)
        result = subprocess.run(command + ['--set', 'app'], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(__import__('json').loads(result.stdout), {'arm64': {'app': ['net/app']}})


if __name__ == '__main__':
    unittest.main()
