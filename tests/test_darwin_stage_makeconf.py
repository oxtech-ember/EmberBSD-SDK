from pathlib import Path
import tempfile
import unittest

from emberbsd_sdk import compiler_bindings
from emberbsd_sdk.darwin_makeconf import write_native_stage_makeconf
from emberbsd_sdk.errors import SDKError


class NativeStageMakeconfTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        self.prefix = root/'host'; self.prefix.mkdir()
        self.stage = root/'stage'; self.stage.mkdir()
        self.staged = self.stage/str(self.prefix).lstrip('/')
        self.include = self.staged/'include/libc++/v1'; self.include.mkdir(parents=True)
        self.lib = self.staged/'lib/libc++'; self.lib.mkdir(parents=True)
        (self.staged/'pkgdb').mkdir()
        (self.include/'__config_site').write_text('#define _LIBCPP_ABI_NAMESPACE __ember1\n')
        (self.lib/'libc++_ember1.1.0.dylib').write_text('fixture')
        (self.lib/'libc++_ember1.1.dylib').symlink_to('libc++_ember1.1.0.dylib')
        (self.lib/'libc++.dylib').symlink_to('libc++_ember1.1.dylib')
        sdk = root/'sdk'; sdk.mkdir()
        abi = sdk/'usr/include/c++/v1'; abi.mkdir(parents=True)
        for name in ('cxxabi.h', '__cxxabi_config.h'):
            (abi/name).write_text('fixture')
        runtime = dict(include_dir=str(self.include), library_dir=str(self.lib), sysroot=str(sdk),
                       targets=['arm64-apple-macos'], deployment='26.0', platform_cxxabi=True,
                       abi_identity=dict(namespace='__ember1', linker_target='libc++_ember1.1.dylib'))
        compiler = self.prefix/'bin/compiler'; compiler.parent.mkdir()
        compiler.write_text('fixture'); compiler.chmod(0o755)
        command = {role:[str(compiler)] for role in ('cc', 'cxx', 'cpp')}
        flags = dict(cppflags=[], cflags=[], cxxflags=[], ldflags=[])
        binding = root/'binding'
        compiler_bindings.bind_compilers(binding, commands=command, flags=flags,
                                         python=str(compiler), native_cxx_runtime=runtime)
        self.record = binding/'commands.json'
        self.base = root/'base.mk'
        self.base.write_text('LOCALBASE= '+str(self.prefix)+'\n'
                             'PKG_DBDIR= '+str(self.staged/'pkgdb')+'\n'
                             'EMBERBSD_NATIVE_CXX_ABI_NAMESPACE= __ember1\n'
                             'EMBERBSD_DARWIN_SEED_CXX= '+str(binding/'cxx')+'\n'
                             'EMBERBSD_NATIVE_CXX_BINDING_RECORD= '+str(self.record)+'\n')
        self.options = dict(base=str(self.base), output=str(root/'stage.mk'),
            stage_root=str(self.stage), prefix=str(self.prefix), object_root=str(root/'obj'),
            packages=str(root/'packages'), binding_record=str(self.record), python=str(compiler),
            buildlinks=['llvm', 'clang'])

    def test_explicit_stage_and_refusals(self):
        output = Path(write_native_stage_makeconf(**self.options))
        text = output.read_text()
        self.assertIn('PKG_DBDIR= '+str(self.staged/'pkgdb')+'\n', text)
        self.assertIn('DEPENDS_TARGET= emberbsd-stage-missing-dependency\n', text)
        self.assertIn('EMBERBSD_NATIVE_CXX_ABI_NAMESPACE= __ember1\n', text)
        self.assertIn('BUILDLINK_PREFIX.llvm= '+str(self.staged)+'\n', text)
        self.assertEqual(write_native_stage_makeconf(**self.options), str(output))
        for change in (dict(buildlinks=['llvm', 'llvm']), dict(stage_root=str(self.prefix)),
                       dict(object_root=str(self.staged/'obj')),
                       dict(packages=self.options['object_root'])):
            with self.assertRaises(SDKError):
                write_native_stage_makeconf(**(self.options | change))
        adapter = self.record.parent/'cxx'
        original = adapter.read_bytes()
        adapter.chmod(0o755)
        adapter.write_text('foreign adapter')
        with self.assertRaises(SDKError):
            write_native_stage_makeconf(**self.options)
        adapter.write_bytes(original)
        adapter.unlink()
        with self.assertRaises(SDKError):
            write_native_stage_makeconf(**self.options)
        adapter.write_bytes(original); adapter.chmod(0o755)
        (self.include/'__config_site').write_text('#define _LIBCPP_ABI_NAMESPACE __1\n')
        with self.assertRaises(SDKError):
            write_native_stage_makeconf(**self.options)


if __name__ == '__main__':
    unittest.main()
