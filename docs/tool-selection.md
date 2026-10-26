# Select SDK build tools

Use the native `host-pkg-llvm` prefix built by Ports with Clang and LLVM.
Build the cross SDK from this prefix. Pass it through `--host-prefix` or the
shared [load_host_prefix API](native-toolset.md).
Clang is the default compiler family. Select GCC explicitly for a platform
that requires it. Host and target compiler families are independent inputs.
Do not retry a failed LLVM selection with GCC or a system compiler.
No qualification report, acceptance receipt, or tool hash is an admission requirement.
Existing control Python, Git, and patch can download and prepare source before
the native prefix exists. Source acquisition does not require an installed compiler.

## Host and target tools

| Output | Compiler and inputs |
| --- | --- |
| Native programs and generators | Native Clang/LLVM, or explicit GCC, and tools from the selected Ports prefix |
| EmberBSD applications and libraries | Selected compiler family, explicit target triplet, EmberBSD sysroot, and matching runtime |
| Kernel-only or freestanding code | Selected compiler family and the producer's target inputs |

Use shared Ports recipes for compilers, tools, and third-party dependencies.
Keep native OS tool rules in the OS repository.
SDK connects these producers. It does not maintain another compiler recipe.

The native prefix supplies compilers, preprocessors, assembler, linker,
archiver, make, CMake, Meson, Ninja, interpreters, and generators as required.
Each selected executable and its real path must remain inside the prefix.
Check the native CPU, compiler host triplet, compiler-selected components,
and script interpreter paths. Set a prefix-only build `PATH`.
Reject a missing tool or an external build-tool path instead of substituting one.

Do not select build tools from ambient `PATH`, Xcode, Homebrew, MacPorts,
or host package directories. Host ABI libraries and headers, loaders,
and OS process services are normal host inputs. Keep native ABI paths out of
target include, library, and pkg-config searches.
Git source inspection and documentation checks are separate from artifact builds.

## Producer routing

Select native and target tools separately through the producer interface.
Check configuration files and nested commands for external tool selection.
An exported `HOST_CC` is insufficient if the producer overrides it.
Fix a concrete selection defect in SDK, Ports, or OS and add its regression.
Do not add a qualification pipeline to establish that an installed tool is usable.

Sysroot and release builders read the selected OS `tools/native-tools/contract`.
They load its roles from the prefix and publish the OS command mapping.
`EMBER_NATIVE_TOOLS` selects that mapping. The OS binds compiler and shell
variables itself; SDK passes native ABI flags without conflicting overrides.
`--plan` inspects inputs and lists commands. Removing it runs the producer.
There is no unconditional native-tool qualification gate.

After tools or configuration change, use compatible objects or rebuild affected outputs.
Keep host executables separate from target binaries.
An actual build result and target execution are distinct checks.

## Diagnostic boundaries

Clang compiles ordinary host and target outputs.
Clang Static Analyzer remains a separate analysis operation.
The separate [fuzz workflow](fuzz-check.md) permits diagnostic host fuzz targets.
Keep diagnostic outputs outside packages, sysroots, and releases.
Replay the corpus with the selected production compiler and runtime.

Checksums for downloaded archives, packages, and distributed outputs remain useful.
Removing native admission receipts does not remove content integrity checks.
Legacy bootstrap admission interfaces are retained only for historical data.
They are not part of the normal `host-pkg-llvm` workflow.

## Migration boundary

Native prefix selection and the Darwin configuration support LLVM and explicit GCC.
The sysroot and release CLIs default `--compiler-family` to `clang`, independently
of `--host-compiler-family`. Until the common LLVM runtime producer is connected,
they refuse a Clang application build before creating outputs. They never import
the old GCC runtime or choose GCC automatically. Use `--compiler-family gcc`
only when intentionally building the supported GCC alternative.
The current target runtime import and release schemas still describe GCC16 artifacts.
They do not establish LLVM target runtime support. Update these producers and
their matching Ports/OS runtime interfaces before a Clang SDK release.
Changing the compiler does not by itself change the C++ library or target ABI.
The [common LLVM runtime stage contract](llvm-runtime.md) defines the initial
builtins/static-unwind inputs and the subsequent full C++ runtime.
