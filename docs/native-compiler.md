# Select a native SDK compiler

The sysroot and OS release builders require four explicit inputs:

- `--host-toolchain`: the real SDK native compiler provider root.
- `--host-cc` and `--host-cxx`: absolute compiler paths inside that provider.
- `--host-toolchain-manifest`: the provider's native compiler input declaration.

The Python API requires the same keys, with hyphens, in its options dictionary.
Both producers use the shared `emberbsd_sdk.host_compiler` selector.
Missing inputs fail before tool queries or output writes. The builders never
select native `cc` or `c++` from `PATH`. The target `--compiler` remains separate.
System tool directories cannot serve as native SDK provider roots. This includes
the general `/usr` and `/usr/local` roots, their system tool directories,
`/bin`, `/sbin`, Homebrew, MacPorts, and Apple developer tool trees.
A dedicated provider such as `/usr/local/emberbsd-sdk/native` remains permitted.
Host ABI headers and libraries can be declared inputs outside the provider.

## Provider declaration and trust

Obtain the manifest from your verified SDK producer. Do not create one by
adopting whichever compiler happens to be installed. SDK does not provide an
automatic adoption command, compiler recipe, or bootstrap exemption.
Shared compiler recipes remain in Ports.

This is a caller-owned declaration. The repository URL, revision and recipe
identify the claimed producer. They do not authenticate the supplier or prove
that the binaries were built from that source. File hashes bind selected bytes.
They are not signatures, reproducible-build evidence, or complete SDK acceptance.
Referenced build receipts retain their own evidence and qualification limits.

This selector change does not provision or qualify a native development-host
provider. Cross-compiler receipts and target-native GCC package checks do not
qualify one. No actual native SDK GCC16 installation or full build is established
by the selector's fixture tests.

## Manifest version 1

The JSON object has exactly these fields:

| Field | Required value |
| --- | --- |
| `format` | `emberbsd-native-gcc-inputs` |
| `version` | Integer `1` |
| `gcc_version` | `16.2.0` |
| `host` | `system`, `machine`, `pointer_bits`, `byteorder`, and `triplet` |
| `producer` | HTTPS `repository` URL, full hexadecimal `revision`, and repository-relative `recipe` |
| `tools` | Entries for `cc`, `cxx`, `cc1`, `cc1plus`, `collect2`, `lto1`, `lto-wrapper`, `as`, and `ld` |
| `abi_inputs` | Nonempty array of explicit host ABI file references |
| `provenance` | Array of existing producer receipt references; empty when unavailable |

Each `tools` entry contains `path` and `sha256`. The path is relative to the
provider root, without traversal. The hash covers the resolved executable.
The two compiler selectors must exactly equal the root plus their declared paths.
Contained symlinks are permitted. A link to an external system compiler fails.
The builder invokes each resolved canonical path. A contained C++ alias must
resolve to the provider's qualified C++ driver. A `gcc` alias does not establish
the default C++ link behavior. The provider must check that behavior separately;
the selector's preprocessing query does not perform a C++ runtime link.

Each `abi_inputs` or `provenance` entry contains an absolute regular-file `path`
and its `sha256`. Duplicate references are rejected. Declare the ABI inputs used
by the producer, such as its SDK identity, host headers, and runtime libraries.
The selector checks the listed bytes. It cannot prove that this list is complete.
Keep these host inputs outside target include and library searches.

Use `Darwin`, `Linux`, or `NetBSD` for `host.system`. Use the native machine name,
with `arm64` normalized to `aarch64` and `amd64` to `x86_64`.
`pointer_bits` is `32` or `64`; `byteorder` is `little` or `big`.
`triplet` is the exact compiler `-dumpmachine` result for that host.
Host process backend qualification remains a separate requirement.

## Checks and producer binding

The selector checks the running host against the declaration. It checks GCC
version, predefined GCC/OS/CPU macros, pointer width, byte order, and C/C++ roles.
It records both version-banner streams and rejects Clang, LLVM, or Apple compiler vendor markers.
An `apple` triplet in a native Darwin GCC driver name remains permitted.
Clang and Apple compiler marker macros also fail, even with GCC compatibility macros.
Both drivers preprocess the same supplied `.c` probe without a forced language.
The probe contains only a comment; its exact bytes are checked before and after
each query. A C driver cannot pass the C++ role by forcing `-x c++`.
Clang, older GCC, and a compiler for another host fail before producer execution.
It does not compile or execute a test program during plan inspection.

Both drivers must report absolute subprocess paths that resolve to the declared
provider files. An implicit `as` or `ld`, a different provider, and changed bytes
are errors. Inspection uses the producer environment; a temporary `PATH` cannot
conceal a different driver selection. This does not audit all dynamic dependencies
or every nested build tool. Apply the [full tool policy](tool-selection.md).

The builders pass the selected native compilers as `HOST_CC` and `HOST_CXX` to
`build.sh`. Its bootstrap make and native tool rules use these variables.
The target GCC remains selected through `EXTERNAL_TOOLCHAIN`.
The complete identity and manifest hashes enter object contexts and receipts.
Old contexts without this identity fail compatibility checks and remain unchanged.
Detected compiler drift after context preparation quarantines both object trees.
Restoring the original compiler bytes does not authorize their reuse.
The builder rechecks inputs before output creation, before each producer dispatch,
and before successful completion. These checks do not lock external input files
against a concurrent writer; keep the selected provider immutable during a build.

New release profiles and SDK release receipts use schema 3. Their verification
checks the declaration, tool identities, and arguments without reading mutable
local compiler paths. Historical schema 2 receipts retain their original checks.
They do not acquire native SDK qualification from this new input contract.
New sysroot build receipts use version 2 and enforce the same native bindings
during export. Historical version 1 receipts retain their previous transfer
checks without gaining native compiler qualification. The transfer bundle
format remains version 1; its context hash binds the original build identity.
