# LLVM application compiler commands

`emberbsd_sdk.application_toolchain`, `APPLICATION_TOOLCHAIN_API == 2`, reads
the existing completed or imported sysroot record and checks the installed
LLVM runtime. It does not create another qualification record. Additional
packages outside `usr/pkg/llvm` are permitted; their contents are not verified
by runtime selection.

```python
from emberbsd_sdk import application_toolchain

runtime = application_toolchain.load_clang_runtime(sysroot, target="aarch64")
compile_commands = application_toolchain.clang_application_commands(
    compiler, sysroot, runtime)
c_link_flags = application_toolchain.clang_application_link_flags(
    compiler, sysroot, runtime, language="c")
cxx_link_flags = application_toolchain.clang_application_link_flags(
    compiler, sysroot, runtime, language="c++")
```

`compiler` is the live, selected Clang identity returned by the SDK target
selector. The version and target must match the runtime. Compile commands
contain the explicit target/sysroot, measured target resource directory,
matching native Clang builtin headers, and selected libc++ headers for C++.
Link flags select the LLVM linker, compiler-rt and libunwind, with measured
library directories and target runtime search paths. C++ link flags explicitly
select libc++; C link flags do not select a C++ library.

Pass these flags through the builder's language-specific link interface,
such as separate C and C++ link arguments. Alternatively, `link=True` on
`clang_application_commands` returns complete link commands. Never use those
commands for compilation: Clang diagnoses link-only arguments under
`-c -Werror`. With `-nostdinc++`, even `-stdlib=libc++` is a link-only input.

Builders accepting only executable `CC`/`CXX`/`CPP` values use the shared
operation-aware binding:

```python
tools = application_toolchain.bind_clang_application_commands(
    bindings, compiler, sysroot, runtime, python=native.tool("python"))
```

The returned paths preserve caller argv. They add the C or C++ runtime flags
only for link operations. Do not add the same runtime flags again through
global LDFLAGS. The target search path of libc++, libc++abi and libunwind
(`clang_application_runtime_rpath`) stays only in outputs that need one of
them: a C++ link gets it unless `-nostdlib`, `-nodefaultlibs`, `-nostdlib++`
or `-static` drops the C++ library, a C link does not. After a successful
link, the binding reads `DT_NEEDED` of the written ELF output and repeats the
same link once with the other choice if the prediction was wrong. An ELF
output it cannot inspect keeps the path. `-r`, `-###` and output to standard
output are not inspected. The split link flags and `link=True` commands keep
the search path; `runtime_rpath=False` omits it from
`clang_application_link_flags`. Selected bindings are immutable and reused
only for identical commands. The shared binding's POSIX response-file and
option-terminator limits are documented in [native checks](native-checks.md).

Set `include_auxiliary=True` to also bind executable `as` and `ld` paths.
The assembler uses the selected Clang target. The linker uses the selected
ELF linker with the application sysroot. Direct linker calls do not add
C or C++ runtime libraries; the caller selects them. These two bindings pass
tool arguments and response files unchanged, without compiler flags.
Compiler binding API 3 preserves the existing version 1 mapping format,
so unchanged compiler-only bindings remain reusable.

For a staging probe, an explicit `runtime_root` can select an already
validated separate runtime stage; no completed application receipt is needed
by the command constructors. The caller validates live inputs before using
these constructors. This interface does not establish target execution,
C++ ABI acceptance or package dependency closure.
