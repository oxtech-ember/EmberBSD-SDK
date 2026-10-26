# Select the native Ports prefix

The native toolset is the installed `host-pkg-g16` prefix.
Ports builds its native packages with the host GCC16 from Ports.
SDK uses that prefix to build the cross SDK and target outputs.
No independent qualification, descriptor, acceptance receipt, or tool hash is required.

## Python API

```python
from emberbsd_sdk.native_toolset import load_host_prefix

native = load_host_prefix(
    "/absolute/host-pkg-g16",
    required_roles=("cc", "cxx", "make", "sh", "python", "cmake", "ninja"),
)
cc = native.tool("cc")
command = native.command("make")
environment["PATH"] = native.path
```

`load_host_prefix(prefix, *, required_roles, host=None)` returns `HostPrefix`.
The optional `host` is an OS/CPU mapping; by default it uses the current host.
The result provides `tool(role)`, `command(role)`, `path`, `bundle_root`,
`host`, `compiler`, `tools`, `abi_flags`, `driver_components`, and `as_dict()`.
The mappings are immutable. `command(role)` returns a one-element argument tuple.
`as_dict()` returns ordinary data for plans and existing build records.
It is not a prerequisite file or a new acceptance record.

When a consumer requests `cc`, `cxx`, or `cpp`, the loader selects the complete GCC group:
`cc`, `cxx`, `cpp`, `as`, and `ld`. Other roles are selected only when requested.
A source export can select only `git` and `patch` before GCC exists.
That selection has empty `compiler` and `driver_components` mappings and runs no compiler queries.
Compiler lookup prefers `gcc16/bin` and then versioned GCC names in `bin`.
Make lookup prefers `bin/bmake`, then `bin/make`.
Shell lookup supports `sh`, `mksh`, `dash`, and `bash` inside `bin`.
Python lookup prefers `python3.15`, then `python3` and `python`.
The archiver, ranlib, nm, and strip roles also support their `llvm-` names.
Package queries prefer `sbin/pkg_info`, then `bin/pkg_info`.
The `otool` role also supports `bin/llvm-otool`.
The install role prefers `bsdinstall`; yacc also supports `byacc`.
Other role names select `bin/<role>` or `gnu/bin/<role>`; `pkg-config` also supports `pkgconf`.
The build search path includes `bin` and `gnu/bin` inside the prefix.
When `bin/as` or `bin/ld` is absent, the loader uses GCC's explicit component path.
No lookup uses ambient `PATH`.
An `env` shebang resolves its interpreter through the selected build PATH, in order.
The loader checks that resolved interpreter, including any earlier same-name entry.

The checks are limited to practical tool selection:

- Both the selected path and its real path must remain inside the prefix.
- Executables must match the host CPU. Scripts must use prefix interpreters.
- Native GCC must be version 16 and report the host triplet.
- GCC-selected compiler components, assembler, and linker must remain inside the prefix.
- Compiler queries and builds use prefix directories in `PATH`.

The loader queries the selected GCC drivers. It does not compile a test program.
Missing tools, external symlinks, target executables, and external interpreters fail directly.
Host ABI headers, libraries, loaders, and OS process services remain normal host inputs.
They are not external build-tool selection.

## Sysroot and release commands

Pass `--host-prefix /absolute/host-pkg-g16` to `build-sysroot.py` or `build-os-release.py`.
The commands load the required roles from the selected OS source contract.
They create the [OS command mapping](native-os-routing.md) under their locked tools directory.
`--host-cc`, `--host-cxx`, and `--python` are optional checked aliases.
An alias cannot replace the corresponding prefix tool.
The selected cross compiler and target runtime remain separate inputs.

## Historical records

`NativeToolset`, `load_native_toolset`, and the old seed readers remain for compatibility with historical data.
Their descriptor and receipt schemas are not the current native tool installation procedure.
Do not create old seed acceptance records to use an installed prefix.
The retired bootstrap documents describe their old data, not prerequisites for ordinary SDK builds.
Archive checksums and distribution integrity checks remain applicable to downloaded content.

The contracts are in `tests/test_host_prefix.py`, `tests/test_native_consumers.py`,
and `tests/test_native_os_routing.py`.
