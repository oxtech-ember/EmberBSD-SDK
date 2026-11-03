import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import test_core  # Adds the SDK source to sys.path.
from emberbsd_sdk import host_services
from emberbsd_sdk.errors import SDKError


class Native:
    def __init__(self, root):
        self.bundle_root = str(root)
        self.path = str(root / 'bin')
        self.tools = {role: str(root / 'bin' / name) for role, name in
                      (('sh', 'mksh'), ('awk', 'nawk'), ('sed', 'nbsed'))}

    def tool(self, role):
        return self.tools[role]


class HostServicesTests(unittest.TestCase):
    def test_selected_prefix_tools_and_bounded_os_services(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prefix = root / 'prefix'
            (prefix / 'bin').mkdir(parents=True)
            for name in ('mksh', 'nawk', 'nbsed', 'clang', 'ninja', 'python3'):
                path = prefix / 'bin' / name
                path.write_text('#!/bin/sh\nexit 0\n')
                path.chmod(0o755)
            shadow_sed = prefix / 'bin' / 'sed'
            shadow_sed.write_text('#!/bin/sh\nexit 0\n')
            shadow_sed.chmod(0o755)
            services = root / 'os-bin'
            services.mkdir()
            for name in host_services.SERVICE_COMMANDS:
                path = services / name
                path.write_text('#!/bin/sh\nexit 0\n')
                path.chmod(0o755)
            work = root / 'work'
            work.mkdir()
            native = Native(prefix)

            with patch.object(host_services.os, 'defpath', str(services)):
                build_path = host_services.build_path(native, work)
                shim = work / '.native' / 'toolshim'

                self.assertEqual(build_path.split(os.pathsep), [str(shim), native.path])
                self.assertEqual([native.tool(role) for role in ('sh', 'awk', 'sed')],
                                 [str(prefix / 'bin' / name) for name in ('mksh', 'nawk', 'nbsed')])
                self.assertEqual(set(path.name for path in shim.iterdir()),
                                 set(host_services.SERVICE_COMMANDS) | {'sh', 'awk', 'sed'})
                self.assertFalse({'cc', 'clang', 'ninja', 'python', 'python3'} &
                                 set(path.name for path in shim.iterdir()))
                for name in host_services.SERVICE_COMMANDS:
                    self.assertEqual(os.readlink(shim / name), str(services / name))
                for role, name in host_services.PREFIX_COMMANDS.items():
                    self.assertEqual(os.readlink(shim / name), native.tool(role))
                    self.assertEqual(os.path.realpath(shutil.which(name, path=build_path)),
                                     os.path.realpath(native.tool(role)))
                self.assertNotEqual(os.path.realpath(shutil.which('sed', path=build_path)),
                                    os.path.realpath(shadow_sed))
                for name in ('mkdir', 'uname'):
                    self.assertEqual(shutil.which(name, path=build_path), str(shim / name))

                before = {name: (os.lstat(shim / name).st_ino,
                                 os.lstat(shim / name).st_mtime_ns)
                          for name in set(host_services.SERVICE_COMMANDS) | {'sh', 'awk', 'sed'}}
                self.assertEqual(host_services.build_path(native, work), build_path)
                after = {name: (os.lstat(shim / name).st_ino,
                                os.lstat(shim / name).st_mtime_ns)
                         for name in set(host_services.SERVICE_COMMANDS) | {'sh', 'awk', 'sed'}}
                self.assertEqual(after, before)

    def test_refuses_nonmatching_existing_entry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prefix = root / 'prefix'
            (prefix / 'bin').mkdir(parents=True)
            for name in ('mksh', 'nawk', 'nbsed'):
                path = prefix / 'bin' / name
                path.write_text('selected')
            services = root / 'os-bin'
            services.mkdir()
            for name in host_services.SERVICE_COMMANDS:
                path = services / name
                path.write_text('service')
                path.chmod(0o755)
            work = root / 'work'
            (work / '.native' / 'toolshim').mkdir(parents=True)
            (work / '.native' / 'toolshim' / 'mkdir').write_text('unexpected')

            with patch.object(host_services.os, 'defpath', str(services)):
                with self.assertRaisesRegex(SDKError, 'not a reusable symlink'):
                    host_services.build_path(Native(prefix), work)


if __name__ == '__main__':
    unittest.main()
