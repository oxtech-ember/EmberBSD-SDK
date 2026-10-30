# Select the native Ports prefix

The default native toolset is the installed `host-pkg-llvm` prefix.
Use the [initial Darwin configuration](darwin-bootstrap.md) before that prefix exists.
Ports builds its native packages with Clang and LLVM from the common recipes.
Select GCC explicitly only for a platform that requires it.
SDK uses that prefix to build the cross SDK and target outputs.
No independent qualification, descriptor, acceptance receipt, or tool hash is required.

## Python API

```python
from emberbsd_sdk.native_toolset import load_host_prefix

native = load_host_prefix(
    "/absolute/host-pkg-llvm",
    required_roles=("cc", "cxx", "make", "sh", "python", "cmake", "ninja"),
)
cc = native.tool("cc")
command = native.command("make")
environment["PATH"] = native.path
```

`load_host_prefix(prefix, *, required_roles, host=None, compiler_family="clang")`
returns `HostPrefix`. `compiler_family="gcc"` selects the GCC16 alternative.
The loader never changes families when a selected tool is missing.
The optional `host` is an OS/CPU mapping; by default it uses the current host.
The result provides `tool(role)`, `command(role)`, `path`, `bundle_root`,
`host`, `compiler`, `compiler_family`, `tools`, `abi_flags`, `driver_components`, and `as_dict()`.
The mappings are immutable. Use `command(role)` to retain required arguments.
For LLVM, `as` selects the Ports `clang-as` frontend for the integrated assembler.
Current roles return a one-element argument tuple.
`as_dict()` returns ordinary data for plans and existing build records.
It is not a prerequisite file or a new acceptance record.

When a consumer requests `cc`, `cxx`, or `cpp`, the loader selects the complete compiler group:
`cc`, `cxx`, `cpp`, `as`, and `ld`. Other roles are selected only when requested.
A source export can select only `git` and `patch` before a compiler exists.
That selection has empty `compiler` and `driver_components` mappings and runs no compiler queries.
The default selects `bin/clang`, `bin/clang++`, and `bin/clang-cpp`.
Explicit GCC lookup prefers `gcc16/bin` and then versioned GCC names in `bin`.
Make lookup prefers `bin/bmake`, then `bin/make`.
Shell lookup supports `sh`, `mksh`, `dash`, and `bash` inside `bin`.
Python lookup prefers `python3.15`, then `python3` and `python`.
LLVM selects the `llvm-` archiver, ranlib, nm, strip, and object tools.
The `elf-ld` role explicitly selects `bin/ld.lld` for ELF firmware and cross
targets, including on Darwin. The ordinary `ld` role still follows the native
host ABI (`ld64.lld` on Darwin); `lld` names the generic dispatcher.
The GCC alternative supports generic and `llvm-` archive and symbol names.
Package queries prefer `sbin/pkg_info`, then `bin/pkg_info`.
The `otool` role also supports `bin/llvm-otool`.
The install role prefers `bsdinstall`; yacc also supports `byacc`.
The `hostname` role prefers `bin/hostname`, then `gnu/bin/hostname`, then
`libexec/gettext/hostname` installed by the common Ports `gettext-tools` package.
That provider supports the read-only full and `-s` short-name queries used by
OS build metadata; it needs no copied binary or compatibility symlink.
Other role names select `bin/<role>` or `gnu/bin/<role>`; `pkg-config` also supports `pkgconf`.
The build search path includes `bin` and `gnu/bin` inside the prefix.
Clang uses its integrated assembler and the LLVM linker reported by its driver.
For GCC, missing `bin/as` or `bin/ld` uses the driver's explicit component path.
No lookup uses ambient `PATH`.
An `env` shebang resolves its interpreter through the selected build PATH, in order.
The loader checks that resolved interpreter, including any earlier same-name entry.

The checks are limited to practical tool selection:

- Both the selected path and its real path must remain inside the prefix.
- Executables must match the host CPU. Scripts must use prefix interpreters.
- The compiler family, C/C++ versions, and host triplet must agree.
- Clang's resource directory and reported compiler/linker routes remain inside the prefix.
- GCC16-selected compiler components, assembler, and linker remain inside the prefix.
- Compiler queries and builds use prefix directories in `PATH`.

Darwin executables can be thin or universal Mach-O images.
Universal selection checks table bounds, alignment, nonoverlapping slices, and the selected image header and file type.
It accepts generic x86_64, generic arm64, or versioned arm64e ABI 0 on the matching native host.
Unknown CPU subtypes and feature combinations do not establish host compatibility.
The fat formats and subtype values follow Apple's
[Mach-O definitions](https://github.com/apple-oss-distributions/cctools/blob/main/include/mach-o/fat.h)
and [CPU definitions](https://github.com/apple-oss-distributions/xnu/blob/main/osfmk/mach/machine.h).
This does not change prefix containment or the separate historical shared-library reader.

The loader queries the selected drivers. Clang `-###` reports C and C++ routes without compilation.
The final Clang recipe must select the prefix LLVM linker by default.
The loader does not compile a test program or repair an external linker selection.
Missing tools, external symlinks, target executables, and external interpreters fail directly.
Host ABI headers, libraries, loaders, and OS process services remain normal host inputs.
They are not external build-tool selection.

## Sysroot and release commands

Pass `--host-prefix /absolute/host-pkg-llvm` to `build-sysroot.py` or `build-os-release.py`.
Use `--host-compiler-family gcc` for an explicit native GCC exception.
The commands load the required roles from the selected OS source contract.
They create the [OS command mapping](native-os-routing.md) under their locked tools directory.
`--host-cc`, `--host-cxx`, and `--python` are optional checked aliases.
An alias cannot replace the corresponding prefix tool.
The selected cross compiler and target runtime remain separate inputs.

Use the [shared native-check runner](native-checks.md) for source checks and CI.
It binds exact native ABI arguments even for existing scripts that accept only
an executable in `CC` or `CXX`.
OS producers use [run-native-os.py](native-os.md), which keeps native `HOST_*`
arguments separate from the target compiler and persists them in build.sh wrappers.

## Historical records

`NativeToolset`, `load_native_toolset`, and the old seed readers remain for compatibility with historical data.
Their descriptor and receipt schemas are not the current native tool installation procedure.
Do not create old seed acceptance records to use an installed prefix.
The retired bootstrap documents describe their old data, not prerequisites for ordinary SDK builds.
Archive checksums and distribution integrity checks remain applicable to downloaded content.

The contracts are in `tests/test_host_prefix.py`, `tests/test_native_consumers.py`,
and `tests/test_native_os_routing.py`.
