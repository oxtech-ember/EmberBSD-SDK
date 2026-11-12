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
        view = root/'view'; view.mkdir()
        options['runtime_view'] = str(view)
        output = Path(write_native_stage_makeconf(**options))
        self.assertIn('EMBERBSD_DARWIN_LD64_LLD= ' + str(selected_tool), output.read_text())
        for variable, role in (('CCPATH', 'cc'), ('CXXPATH', 'cxx'), ('CPPPATH', 'cpp')):
            self.assertIn(variable+'= '+str(binding/role)+'\n', output.read_text())
        with_rpcgen = options | dict(output=str(root/'staged-rpcgen.mk'),
                                     tools=options['tools'] + ['rpcgen=' + str(selected_tool)])
        self.assertIn('TOOLS_PATH.rpcgen= ' + str(selected_tool),
                      Path(write_native_stage_makeconf(**with_rpcgen)).read_text())
        self.assertIn('EMBERBSD_NATIVE_RUNTIME_VIEW= ' + str(view), output.read_text())
        with self.assertRaisesRegex(SDKError, 'No such file or directory'):
            write_native_stage_makeconf(**(options | dict(runtime_view=str(root/'missing'))))
        with self.assertRaisesRegex(SDKError, 'expected directory'):
            write_native_stage_makeconf(**(options | dict(runtime_view=str(self.base))))
        unsafe = root/'bad:colon'; unsafe.mkdir()
        with self.assertRaisesRegex(SDKError, 'without make metacharacters'):
            write_native_stage_makeconf(**(options | dict(runtime_view=str(unsafe))))
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

    def test_linker_runtime_binding(self):
        root = self.base.parent
        ld = self.staged/'bin/ld64.lld'; ld.parent.mkdir(parents=True)
        ld.write_text('fixture'); ld.chmod(0o755)
        view = root/'view'; view.mkdir()
        options = self.options | dict(tools=['ld='+str(ld)], runtime_view=str(view),
                                      linker_runtime_view=str(view))
        config = Path(write_native_stage_makeconf(**options))
        adapter = Path(str(config)+'.ld')
        text = config.read_text()
        self.assertIn('EMBERBSD_DARWIN_LD64_LLD= '+str(adapter)+'\n', text)
        self.assertIn('LDFLAGS+= --ld-path='+str(adapter)+'\n', text)
        self.assertIn('EMBERBSD_NATIVE_RUNTIME_VIEW= '+str(view)+'\n', text)
        script = adapter.read_text()
        for value in (str(ld), str(view), str(self.python), 'run-darwin-with-runtime.py'):
            self.assertIn(value, script)
        self.assertEqual(write_native_stage_makeconf(**options), str(config))
        with self.assertRaisesRegex(SDKError, 'requires a selected staged linker'):
            write_native_stage_makeconf(**(options | dict(output=str(root/'missing.mk'), tools=[])))
        with self.assertRaises(SDKError):
            write_native_stage_makeconf(**(options | dict(output=str(root/'view-missing.mk'), linker_runtime_view=str(root/'missing-view'))))
        from emberbsd_sdk import runtime_loader
        with self.assertRaisesRegex(SDKError, 'Python cannot contain whitespace'):
            runtime_loader.bind_command(str(root/'bad-python'), python=str(root/'bad python'),
                                        view=str(view), command=str(ld))
        alias = root/'alias'; alias.symlink_to(adapter)
        with self.assertRaises(SDKError):
            runtime_loader.bind_command(str(alias), python=str(self.python),
                                        view=str(view), command=str(ld))
        adapter.chmod(0o755); adapter.write_text('changed')
        with self.assertRaisesRegex(SDKError, 'runtime command binding differs'):
            write_native_stage_makeconf(**options)

    def test_explicit_stage_and_refusals(self):
        output = Path(write_native_stage_makeconf(**self.options))
        text = output.read_text()
        self.assertIn('PKG_DBDIR= '+str(self.staged/'pkgdb')+'\n', text)
        self.assertIn('EMBERBSD_NATIVE_STAGE_ROOT= '+str(self.stage)+'\n', text)
        self.assertIn('EMBERBSD_NATIVE_STAGE_PREFIX= '+str(self.prefix)+'\n', text)
        self.assertIn('DEPENDS_TARGET= emberbsd-stage-missing-dependency\n', text)
        self.assertIn('MACHO_USE_LOAD_COMMANDS= yes\n', text)
        self.assertIn('CWRAPPERS_SRC_DIR= '+str(self.staged/'libexec/cwrappers')+'\n', text)
        self.assertIn('TOOLS_PATH.rpcgen= '+str(self.staged/'bin/rpcgen')+'\n', text)
        self.assertIn('EMBERBSD_NATIVE_STAGE_PYTHON= '+str(self.python)+'\n', text)
        for variable, role in (('CCPATH', 'cc'), ('CXXPATH', 'cxx'), ('CPPPATH', 'cpp')):
            self.assertIn(variable+'= '+str(self.record.parent/role)+'\n', text)
        self.assertIn('EMBERBSD_NATIVE_CXX_ABI_NAMESPACE= __ember1\n', text)
        self.assertIn('BUILDLINK_PREFIX.llvm= '+str(self.staged)+'\n', text)
        self.assertEqual(write_native_stage_makeconf(**self.options), str(output))
        rpcgen = self.staged/'bin/rpcgen'
        rpcgen.parent.mkdir(parents=True, exist_ok=True)
        rpcgen.write_text('fixture'); rpcgen.chmod(0o755)
        selected = dict(self.options, output=str(self.base.parent/'rpcgen.mk'),
                        tools=['rpcgen='+str(rpcgen)])
        self.assertIn('TOOLS_PATH.rpcgen= '+str(rpcgen)+'\n',
                      Path(write_native_stage_makeconf(**selected)).read_text())
        rpcgen.unlink(); rpcgen.symlink_to(self.python)
        with self.assertRaisesRegex(SDKError, 'stage tool must be executable inside'):
            write_native_stage_makeconf(**(selected | dict(output=str(self.base.parent/'escape.mk'))))
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


    def host_binding(self, name, commands=None, runtime=None):
        root = self.base.parent
        binding = root/name
        if commands is None:
            commands = {role: [str(self.python)] for role in ('cc', 'cxx', 'cpp')}
        compiler_bindings.bind_compilers(binding, commands=commands, flags=self.flags,
                                         python=str(self.python), native_cxx_runtime=runtime)
        return binding/'commands.json'

    def test_host_toolchain_generic_binding_and_repeat(self):
        import json
        record = self.host_binding('host-generic')
        self.assertEqual(json.loads(record.read_text())['version'], 1)
        opts = self.options | dict(binding_record=str(record), host_toolchain=True)
        out = Path(write_native_stage_makeconf(**opts))
        text = out.read_text()
        self.assertIn('CWRAPPERS_SRC_DIR= '+str(self.prefix/'libexec/cwrappers')+'\n', text)
        self.assertIn('TOOLS_PATH.rpcgen= '+str(self.prefix/'bin/rpcgen')+'\n', text)
        self.assertIn('.undef EMBERBSD_NATIVE_CXX_ABI_NAMESPACE\n', text)
        self.assertIn('PKG_DBDIR= '+str(self.staged/'pkgdb')+'\n', text)
        for variable, role in (('CCPATH', 'cc'), ('CXXPATH', 'cxx'), ('CPPPATH', 'cpp')):
            self.assertIn(variable+'= '+str(record.parent/role)+'\n', text)
        self.assertIn('BUILDLINK_PREFIX.llvm= '+str(self.staged)+'\n', text)
        self.assertEqual(write_native_stage_makeconf(**opts), str(out))
        self.assertEqual(out.read_text(), text)
        with self.assertRaises(SDKError):
            write_native_stage_makeconf(**(opts | dict(host_toolchain=False)))
        adapter = record.parent/'cc'
        adapter.chmod(0o755); adapter.write_text('tampered')
        with self.assertRaises(SDKError):
            write_native_stage_makeconf(**opts)

    def test_host_toolchain_explicit_prefix_runtime(self):
        include = self.prefix/'include/libc++/v1'; include.mkdir(parents=True)
        lib = self.prefix/'lib/libc++'; lib.mkdir(parents=True)
        (include/'__config_site').write_text('#define _LIBCPP_ABI_NAMESPACE __ember1\n')
        (lib/'libc++_ember1.1.0.dylib').write_text('fixture')
        (lib/'libc++_ember1.1.dylib').symlink_to('libc++_ember1.1.0.dylib')
        (lib/'libc++.dylib').symlink_to('libc++_ember1.1.dylib')
        runtime = self.runtime | dict(include_dir=str(include), library_dir=str(lib))
        record = self.host_binding('host-v4', runtime=runtime)
        opts = self.options | dict(binding_record=str(record), host_toolchain=True)
        text = Path(write_native_stage_makeconf(**opts)).read_text()
        self.assertIn('EMBERBSD_NATIVE_CXX_ABI_NAMESPACE= __ember1\n', text)
        self.assertIn('CWRAPPERS_SRC_DIR= '+str(self.prefix/'libexec/cwrappers')+'\n', text)
        self.assertEqual(write_native_stage_makeconf(**opts), opts['output'])

    def test_host_toolchain_rejects_wrong_inputs(self):
        import json
        root = self.base.parent
        record = self.host_binding('host-invalid')
        opts = self.options | dict(binding_record=str(record), host_toolchain=True)
        with self.assertRaises(SDKError):
            write_native_stage_makeconf(**(opts | dict(tools=['ld='+str(self.python)])))
        with self.assertRaises(SDKError):
            write_native_stage_makeconf(**(opts | dict(linker_runtime_view=str(root))))
        # The staged v4 runtime remains invalid for a host compiler selection.
        with self.assertRaises(SDKError):
            write_native_stage_makeconf(**(opts | dict(binding_record=str(self.record))))
        stage_cc = self.staged/'bin/cc'; stage_cc.parent.mkdir(parents=True)
        stage_cc.write_text('fixture'); stage_cc.chmod(0o755)
        foreign = root/'foreign'; foreign.write_text('fixture'); foreign.chmod(0o755)
        for name, commands in (
            ('stage', {role:[str(stage_cc)] for role in ('cc', 'cxx', 'cpp')}),
            ('mixed', {'cc':[str(stage_cc)], 'cxx':[str(self.python)], 'cpp':[str(self.python)]}),
            ('foreign', {role:[str(foreign)] for role in ('cc', 'cxx', 'cpp')}),
        ):
            candidate = self.host_binding('binding-'+name, commands)
            with self.subTest(name=name), self.assertRaises(SDKError):
                write_native_stage_makeconf(**(opts | dict(binding_record=str(candidate))))
        data = json.loads(record.read_text()); data['format'] = 'invalid'
        record.chmod(0o644)
        record.write_text(json.dumps(data))
        with self.assertRaises(SDKError):
            write_native_stage_makeconf(**opts)

if __name__ == '__main__':
    unittest.main()
