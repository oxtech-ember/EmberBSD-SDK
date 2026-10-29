from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

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
        self.runtime = runtime
        self.flags = flags
        self.python = compiler
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

    def test_staged_compiler_and_escape(self):
        root = Path(self.tmp.name).resolve()
        staged_compiler = self.staged/'bin/clang'
        staged_compiler.parent.mkdir(parents=True)
        staged_compiler.write_text('fixture'); staged_compiler.chmod(0o755)
        binding = root/'staged-binding'
        commands = {role:[str(staged_compiler)] for role in ('cc', 'cxx', 'cpp')}
        compiler_bindings.bind_compilers(binding, commands=commands, flags=self.flags,
                                         python=str(self.python), native_cxx_runtime=self.runtime)
        options = self.options | dict(binding_record=str(binding/'commands.json'))
        selected_tool = self.staged/'bin/ld64.lld'
        selected_tool.write_text('fixture'); selected_tool.chmod(0o755)
        roles = ('ld', 'ar', 'ranlib', 'nm', 'strip', 'dsymutil', 'llvm_mc', 'libtool_darwin', 'lipo', 'otool', 'install_name_tool', 'llvm_readtapi')
        options['tools'] = [role + '=' + str(selected_tool) for role in roles]
        output = Path(write_native_stage_makeconf(**options))
        self.assertIn('EMBERBSD_DARWIN_LD64_LLD= ' + str(selected_tool), output.read_text())
        with self.assertRaisesRegex(SDKError, 'stage tool must be executable inside'):
            write_native_stage_makeconf(**(options | dict(tools=['ld=' + str(self.python)])))
        with self.assertRaisesRegex(SDKError, 'invalid or repeated staged tool role'):
            write_native_stage_makeconf(**(options | dict(tools=options['tools'] * 2)))
        with self.assertRaisesRegex(SDKError, 'staged compiler requires the complete'):
            write_native_stage_makeconf(**(options | dict(tools=options['tools'][:-1])))
        mixed_binding = root/'mixed-binding'
        compiler_bindings.bind_compilers(mixed_binding,
            commands={'cc':[str(staged_compiler)], 'cxx':[str(self.python)],
                      'cpp':[str(staged_compiler)]}, flags=self.flags,
            python=str(self.python), native_cxx_runtime=self.runtime)
        with self.assertRaisesRegex(SDKError, 'stage binding cannot mix compiler roots'):
            write_native_stage_makeconf(**(options | dict(binding_record=str(mixed_binding/'commands.json'))))
        foreign = root/'foreign'; foreign.write_text('fixture'); foreign.chmod(0o755)
        escaped = self.staged/'bin/escape'; escaped.symlink_to(foreign)
        with self.assertRaisesRegex(SDKError, 'stage tool must be executable inside'):
            write_native_stage_makeconf(**(options | dict(tools=['ld=' + str(escaped)])))
        bad_binding = root/'bad-binding'
        compiler_bindings.bind_compilers(bad_binding,
            commands={role:[str(escaped)] for role in ('cc', 'cxx', 'cpp')},
            flags=self.flags, python=str(self.python), native_cxx_runtime=self.runtime)
        with self.assertRaisesRegex(SDKError, 'stage binding must use selected'):
            write_native_stage_makeconf(**(options | dict(binding_record=str(bad_binding/'commands.json'))))

    def test_explicit_stage_and_refusals(self):
        output = Path(write_native_stage_makeconf(**self.options))
        text = output.read_text()
        self.assertIn('PKG_DBDIR= '+str(self.staged/'pkgdb')+'\n', text)
        self.assertIn('EMBERBSD_NATIVE_STAGE_ROOT= '+str(self.stage)+'\n', text)
        self.assertIn('EMBERBSD_NATIVE_STAGE_PREFIX= '+str(self.prefix)+'\n', text)
        self.assertIn('DEPENDS_TARGET= emberbsd-stage-missing-dependency\n', text)
        self.assertIn('MACHO_USE_LOAD_COMMANDS= yes\n', text)
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
