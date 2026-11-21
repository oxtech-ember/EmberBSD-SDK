# SPDX-License-Identifier: BSD-2-Clause
import hashlib
import io
from pathlib import Path, PurePosixPath
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.package_runtime import RuntimeOverlay, selected


class RuntimePackageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.lib = 'usr/pkg/llvm/lib/libunwind.so.1.0'
        self.link = 'usr/pkg/llvm/lib/libunwind.so.1'
        self.builtins = 'usr/pkg/llvm/lib/clang/23/lib/netbsd/libclang_rt.builtins-aarch64.a'
        self.record = dict(prefix='usr/pkg/llvm', artifacts={'builtins': self.builtins,
            'libunwind': {'static': 'usr/pkg/llvm/lib/libunwind.a'}},
            files={self.lib: hashlib.sha256(b'original').hexdigest()}, symlinks={self.link: 'libunwind.so.1.0'})
        path = self.root / self.lib
        path.parent.mkdir(parents=True)
        path.write_bytes(b'original')
        (self.root / self.link).symlink_to(path.name)
        self.config = dict(sysroot=str(self.root), prefix='/usr/pkg', pkgdb='/usr/pkg/pkgdb',
                           target=dict(arch='aarch64', triplet='aarch64--netbsd'))
        self.overlay = RuntimeOverlay(self.config, self.root, self.record, False)

    def archive(self, package, entries):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w') as tar:
            for path, contents in entries:
                member = tarfile.TarInfo(path)
                if isinstance(contents, str):
                    member.type = tarfile.SYMTYPE
                    member.linkname = contents
                    tar.addfile(member)
                else:
                    member.size = len(contents)
                    tar.addfile(member, io.BytesIO(contents))
        stream.seek(0)
        visit = self.overlay.visitor(package)
        with tarfile.open(fileobj=stream, mode='r|') as tar:
            for member in tar:
                visit(member, PurePosixPath(member.name), tar, '/usr/pkg')
        return dict(pkgname=package, action='download')

    def test_projected_destination_and_signed_runtime_links(self):
        (self.root / 'usr/pkg/alternate').symlink_to('llvm/lib', target_is_directory=True)
        package = self.archive('changed-1', [('alternate/libunwind.so.1.0', b'new')])
        with self.assertRaisesRegex(SDKError, self.lib):
            self.overlay.check([package])
        self.overlay.check([dict(package, action='installed')])
        links = self.archive('links-1', [('llvm/lib/libunwind.so.1', 'different')])
        with self.assertRaisesRegex(SDKError, self.link):
            self.overlay.check([links])
        matching = self.archive('matching-1', [('alternate/libunwind.so.1.0', b'original'),
            ('llvm/lib/libunwind.so.1', 'libunwind.so.1.0'),
            ('llvm/lib/clang/23/include/cpu.h', b'new resource header'), ('share/other', b'extra')])
        self.overlay.check([matching])
        unexpected = self.archive('extra-1', [('llvm/lib/new-library', b'other')])
        with self.assertRaisesRegex(SDKError, 'new-library'):
            self.overlay.check([unexpected])

    def test_only_writes_beneath_a_new_or_changed_link_are_ambiguous(self):
        link_owner = self.archive('layout-1', [('library', 'llvm/lib')])
        other = self.archive('data-1', [('share/data', b'unrelated')])
        change = self.archive('writer-1', [('library/libunwind.so.1.0', b'new')])
        self.overlay.check([link_owner, other])
        with self.assertRaisesRegex(SDKError, 'directory link.*library'):
            self.overlay.check([link_owner, other, change])
        self.overlay.check([dict(link_owner, action='installed'), change])
        (self.root / 'usr/pkg/library').symlink_to('llvm/lib', target_is_directory=True)
        self.overlay.parent.cache_clear()
        same_link = self.archive('layout-2', [('library', 'llvm/lib')])
        matching = self.archive('writer-2', [('library/libunwind.so.1.0', b'original')])
        self.overlay.check([same_link, matching])

    def test_case_alias_uses_the_destination_filesystem(self):
        name = 'LLVM/LIB/LIBUNWIND.SO.1.0'
        package = self.archive('case-1', [(name, b'wrong')])
        if (self.root / 'usr/pkg' / name).exists():
            with self.assertRaisesRegex(SDKError, self.lib):
                self.overlay.check([package])
        else:
            self.overlay.check([package])

    def test_removal_is_checked_at_each_owning_operation(self):
        database = self.root / 'usr/pkg/pkgdb/old-1'
        database.mkdir(parents=True)
        (database / '+CONTENTS').write_text('@cwd /usr/pkg\nllvm/lib/libunwind.so.1.0\n')
        successor = self.archive('old-2', [('llvm/lib/libunwind.so.1.0', b'original')])
        successor['replaces'] = ['old-1']
        conflict = self.archive('split-1', [])
        conflict['removes'] = ['old-1']
        self.overlay.check([successor, conflict])
        with self.assertRaisesRegex(SDKError, 'old-1.*' + self.lib):
            self.overlay.check([conflict, successor])
        omitted = self.archive('incomplete-2', [])
        omitted['replaces'] = ['old-1']
        with self.assertRaisesRegex(SDKError, 'incomplete-2.*' + self.lib):
            self.overlay.check([omitted, successor])

    def test_live_target_native_and_unrecorded_sysroot_keep_their_policies(self):
        self.assertIsNone(selected(self.config))
        self.assertIsNone(selected(dict(install_mode='native', target=self.config['target'])))
        self.assertIsNone(selected(dict(install_mode='native-stage', host={'arch': 'arm64'})))
