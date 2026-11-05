# SPDX-License-Identifier: BSD-2-Clause
"""Private root preparation contracts; no device or release publication."""
import base64
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
SDK_SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SDK_SOURCE / 'src'))

from emberbsd_sdk.errors import SDKError
from emberbsd_sdk.private_root import copy_layer, personalize

class Native:
    bundle_root = '/fixture/native'
    def tool(self, name):
        return '/fixture/native/bin/' + name

class PrivateRootTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        root = self.base / 'base'
        for name in ('etc/ssh', 'root/.ssh', 'var/db', 'bin', 'dev'):
            (root / name).mkdir(parents=True, exist_ok=True)
        (root / 'etc/passwd').write_text('root:*:0:0::/root:/bin/sh\n')
        (root / 'bin/tool').write_bytes(b'base executable')
        (root / 'bin/tool').chmod(0o755)
        os.link(root / 'bin/tool', root / 'bin/alias')
        overlay = self.base / 'overlay'; overlay.mkdir()
        (overlay / 'etc').mkdir()
        for directory in (root, *root.rglob('*'), overlay / 'etc'):
            if directory.is_dir():
                directory.chmod(0o755)
        (overlay / 'etc/private.json').write_text('opaque-private-token')
        (overlay / 'etc/private.json').chmod(0o600)
        public = base64.b64encode(b'\0\0\0\x0bssh-ed25519\0\0\0\x20' + b'a' * 32).decode()
        key = self.base / 'authorized.pub'; key.write_text('ssh-ed25519 ' + public + '\n')
        boot = self.base / 'boot'; boot.write_bytes(b'producer boot artifact')
        version = self.base / 'version'; version.write_text('fixture kernel\n')
        makedev = self.base / 'MAKEDEV'; makedev.write_text('# fixture')
        tools = self.base / 'tools/bin'; tools.mkdir(parents=True)
        for path in (tools / 'nbmakefs', self.base / 'ssh-keygen'):
            path.write_text('# fixture'); path.chmod(0o700)
        self.options = SimpleNamespace(root_tree=root, overlay=overlay,
            authorized_key=key, device_id='fixture-device', boot_image=boot,
            kernel_version_file=version, host_prefix=self.base / 'native',
            ssh_keygen=self.base / 'ssh-keygen', tools=tools.parent,
            makedev=makedev, os_source=self.base, machine='evbarm',
            machine_arch='aarch64', device=['std'], byte_order='little',
            size=16777216, output=self.base / 'private')
        self.calls = []
        self.run = patch('emberbsd_sdk.private_root.run_logged', self.producer)
        self.run.start(); self.addCleanup(self.run.stop)
        self.routing = patch('emberbsd_sdk.private_root.build_path', return_value='/fixture/native/bin')
        self.routing.start(); self.addCleanup(self.routing.stop)

    def producer(self, argv, log, **kw):
        self.calls.append(argv)
        log = Path(log)
        if argv[0].endswith('ssh-keygen'):
            key = Path(argv[argv.index('-f') + 1])
            key.write_bytes(os.urandom(64)); key.chmod(0o600)
            wire = b'\0\0\0\x0bssh-ed25519\0\0\0\x20' + os.urandom(32)
            Path(str(key) + '.pub').write_text('ssh-ed25519 ' + base64.b64encode(wire).decode() + '\n')
            log.write_text('')
        elif argv[0].endswith('nbmakefs'):
            with Path(argv[-2]).open('wb') as image:
                image.truncate(int(argv[argv.index('-s') + 1]))
            log.write_text('')
        else:
            log.write_text('. type=dir uid=0 gid=0\n./console type=char device=netbsd,240,0 uid=0 gid=0 mode=600\n')
        log.chmod(0o600)

    def test_private_identity_ffs_and_source_unchanged(self):
        old_ns = (946684800123456789, 946684801234567890)
        for name in ('bin/tool', 'bin'):
            os.utime(self.options.root_tree / name, ns=old_ns)
        result = personalize(self.options, native=Native())
        out = self.options.output
        self.assertEqual(result, {'status': 'private_root_prepared', 'installation_ready': False})
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o700)
        for name in ('private.json', 'root.ffs', 'root.mtree'):
            self.assertEqual(stat.S_IMODE((out / name).stat().st_mode), 0o600)
        data = json.loads((out / 'private.json').read_text())
        self.assertEqual(data['device_id'], 'fixture-device')
        self.assertEqual(data['boot_sha256'], hashlib.sha256(b'producer boot artifact').hexdigest())
        self.assertEqual(data['kernel_version'], 'fixture kernel')
        self.assertRegex(data['nonce'], r'^[0-9a-f]{48}$')
        tree = out / 'root'
        for name in ('bin/tool', 'bin'):
            self.assertEqual((tree / name).stat().st_mtime_ns, old_ns[1])
        self.assertEqual((tree / 'bin/tool').stat().st_atime_ns, old_ns[0])
        self.assertEqual((tree / 'etc/ember/firstboot-pending').read_text(), data['nonce'] + '\n')
        entropy = (tree / 'var/db/entropy-file').read_bytes()
        self.assertEqual(len(entropy), 536)
        self.assertEqual(entropy[-20:], hashlib.sha1(entropy[:-20]).digest())
        self.assertEqual((tree / 'bin/tool').stat().st_ino, (tree / 'bin/alias').stat().st_ino)
        spec = (out / 'root.mtree').read_text()
        self.assertIn('./bin/tool type=file uid=0 gid=0 mode=755', spec)
        self.assertIn('./dev/console type=char', spec)
        self.assertIn('./etc/ember/firstboot-pending type=file uid=0 gid=0 mode=600', spec)
        self.assertFalse((self.options.root_tree / 'etc/ember').exists())
        self.assertNotIn('opaque-private-token', json.dumps(result))
        self.assertFalse((out / 'release.p7m').exists())

    def test_each_output_has_new_keys_nonce_and_entropy(self):
        personalize(self.options, native=Native())
        first = self.options.output
        self.options.output = self.base / 'second'
        personalize(self.options, native=Native())
        second = self.options.output
        for name in ('etc/ssh/ssh_host_ed25519_key', 'etc/ember/firstboot-pending', 'var/db/entropy-file'):
            self.assertNotEqual((first / 'root' / name).read_bytes(), (second / 'root' / name).read_bytes())

    def test_symlink_parent_cannot_redirect_private_writes(self):
        outside = self.base / 'outside'; outside.mkdir()
        os.symlink(outside, self.options.overlay / 'root')
        with self.assertRaises(SDKError):
            personalize(self.options, native=Native())
        self.assertEqual(list(outside.iterdir()), [])

    def test_existing_output_and_nested_output_are_refused(self):
        self.options.output.mkdir()
        (self.options.output / 'keep').write_text('keep')
        with self.assertRaises(SDKError):
            personalize(self.options, native=Native())
        self.assertEqual((self.options.output / 'keep').read_text(), 'keep')
        self.options.output = self.options.root_tree / 'nested'
        with self.assertRaises(SDKError):
            personalize(self.options, native=Native())
        self.assertFalse(self.options.output.exists())
        self.assertEqual(self.calls, [])

    def test_target_ownership_is_an_explicit_caller_choice(self):
        entries = {}
        copy_layer(self.options.root_tree, self.base / 'copy', entries, preserve_ownership=True)
        info = (self.options.root_tree / 'bin/tool').stat()
        self.assertEqual(entries['bin/tool']['uid'], info.st_uid)
        self.assertEqual(entries['bin/tool']['gid'], info.st_gid)

    def test_absolute_identity_directory_symlink_is_refused(self):
        outside = self.base / 'outside'; outside.mkdir()
        old = self.options.root_tree / 'var/db'
        old.rmdir(); old.symlink_to(outside)
        with self.assertRaises(SDKError):
            personalize(self.options, native=Native())
        self.assertEqual(list(outside.iterdir()), [])

    def test_inherited_host_keys_are_removed_only_in_output(self):
        old = self.options.root_tree / 'etc/ssh/ssh_host_rsa_key'
        old.write_text('template-identity')
        personalize(self.options, native=Native())
        self.assertEqual(old.read_text(), 'template-identity')
        self.assertFalse((self.options.output / 'root/etc/ssh/ssh_host_rsa_key').exists())


    def test_truncated_ffs_is_not_prepared(self):
        original = self.producer
        def truncated(argv, log, **kw):
            original(argv, log, **kw)
            if argv[0].endswith('nbmakefs'):
                Path(argv[-2]).write_bytes(b'truncated')
        with patch('emberbsd_sdk.private_root.run_logged', truncated):
            with self.assertRaises(SDKError):
                personalize(self.options, native=Native())
        self.assertFalse((self.options.output / 'private.json').exists())

    def test_identity_parent_target_write_permissions_are_refused(self):
        for index, name in enumerate(('.', 'etc', 'etc/ssh', 'root', 'root/.ssh', 'var', 'var/db')):
            with self.subTest(parent=name):
                parent = (self.options.overlay if name == 'etc' else self.options.root_tree) / name
                self.calls.clear()
                mode = stat.S_IMODE(parent.stat().st_mode)
                parent.chmod(mode | 0o020)
                self.options.output = self.base / ('unsafe-' + str(index))
                try:
                    with self.assertRaises(SDKError):
                        personalize(self.options, native=Native())
                    self.assertEqual(self.calls, [])
                    self.assertFalse((self.options.output / 'root/etc/ssh/ssh_host_ed25519_key').exists())
                finally:
                    parent.chmod(mode)

    def test_nonroot_identity_parent_metadata_is_refused(self):
        def nonroot(*args, **kwargs):
            copy_layer(*args, **kwargs)
            args[2]['etc/ssh']['uid'] = 123
        with patch('emberbsd_sdk.private_root.copy_layer', nonroot):
            with self.assertRaises(SDKError):
                personalize(self.options, native=Native())
        self.assertEqual(self.calls, [])

    def test_makedev_option_is_refused_before_tools(self):
        self.options.device = ['-s']
        with self.assertRaises(SDKError):
            personalize(self.options, native=Native())
        self.assertEqual(self.calls, [])
        self.assertFalse(self.options.output.exists())

    def test_device_spec_escape_is_refused(self):
        original = self.producer
        def escaped(argv, log, **kw):
            original(argv, log, **kw)
            if not argv[0].endswith(('ssh-keygen', 'nbmakefs')):
                Path(log).write_text('../escape type=char device=netbsd,240,0\n')
        with patch('emberbsd_sdk.private_root.run_logged', escaped):
            with self.assertRaises(SDKError):
                personalize(self.options, native=Native())
        self.assertFalse((self.options.output / 'private.json').exists())

if __name__ == '__main__':
    unittest.main()
