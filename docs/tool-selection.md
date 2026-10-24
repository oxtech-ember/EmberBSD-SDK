# Select SDK build tools

## Required build policy

Use the selected SDK tool set for OS, Ports, application, and test builds.
This includes bootstrap steps, configuration probes, and native generators.
Host describes where a program runs. It does not authorize a system compiler.

| Output | Required compiler and inputs |
| --- | --- |
| EmberBSD applications and libraries | The SDK GCC16 cross compiler, declared target triplet, EmberBSD sysroot, and matching runtime |
| Kernel-only or freestanding code | The SDK GCC16 cross compiler and the selected producer's target inputs; no application sysroot requirement |
| Native host code and generators | The SDK native GCC16 for the declared host OS and CPU, with its recorded host ABI inputs |

Do not use a target compiler to produce a native generator.
Keep native executables separate from target ELF files.
Use current shared Ports recipes for third-party tools and dependencies.
Keep native OS tool build rules in the OS repository.
SDK selects and records these producers; it must not copy their recipes.

Select the entire build tool set explicitly.
This includes preprocessors, assemblers, linkers, archivers, metadata tools,
make, CMake, Meson, Ninja, interpreters, and code generators.
Record resolved absolute paths, versions, hashes, and producer provenance.
Inspect the compiler driver's selected assembler, linker, headers, and libraries.
A version string or a symlink inside the SDK does not prove SDK provenance.

Do not select build tools from ambient `PATH`, Xcode, Command Line Tools,
Homebrew, MacPorts, or the host package manager.
Do not install a missing system package as a build workaround.
Host ABI headers and libraries must be explicit inputs of the qualified SDK host profile.
Keep them outside target include, library, and pkg-config searches.
Git, filesystem inspection, and OS process services do not replace build tool providers.
Documentation and skill-package validation are separate from EmberBSD artifact builds.

## Before compilation

1. Inspect the actual producer interface, plan, environment, and saved configuration.
2. Select native and target tools separately through that interface.
3. Verify compiler, binutils, interpreter, generator, and pkg-config identities.
4. Inspect CMake toolchain files, Meson native/cross files, and configure caches where applicable.
5. Check configuration probes and nested build commands for implicit system selections.
6. Refuse the dependent build if an SDK tool or explicit selector is missing.

An exported `HOST_CC` or `HOST_CXX` is insufficient if the producer overrides it.
Recording a system compiler in a receipt does not authorize its use.
Repair selection, provisioning, or refusal behavior in SDK, Ports, or OS.
Add a regression that rejects the system fallback.
A local wrapper, compiler rename, or temporary `PATH` change does not close that repair.
SDK bootstrap requires a separate producer with pinned, qualified inputs.
It does not exempt bootstrap code from the GCC16 requirement.

Invalidate incompatible configuration caches, objects, and generators after tool changes.
Verify actual commands and the resulting host and target architectures.
A passing plan, workspace manifest, or successful link does not establish SDK completeness.

## Diagnostic boundaries

Clang cannot compile ordinary host or target build outputs, including bootstrap tools.
Clang Static Analyzer remains an analysis tool.
The separate [diagnostic fuzz workflow](fuzz-check.md) permits Clang host fuzz targets.
Record diagnostic inputs and keep those outputs outside packages, sysroots, and releases.
Replay fuzz corpora with GCC16 as that procedure requires.
These exceptions do not authorize a Clang-built production generator.

## Implemented compiler selection and remaining limits

The sysroot and release builders require explicit native GCC16 compiler paths,
a provider root, and a [provider input manifest](native-compiler.md).
The shared `emberbsd_sdk.host_compiler` selector checks hashes, containment,
driver subprocesses, and native compiler identity. It rejects missing selectors, system provider roots, Clang,
older GCC, foreign host targets, and incompatible object contexts.
Both producers pass the selected paths through `HOST_CC` and `HOST_CXX`.

The manifest is a caller-trusted provenance declaration. Its hashes do not prove
source-to-binary correspondence or authenticate the producer. No accepted native
development-host GCC16 installation is established by this change.
Full provider provisioning and qualification remain open.

The [native toolset API](native-toolset.md) checks a complete declared bundle against a separately accepted producer receipt.
It does not provision or qualify that producer. The sysroot and release builders do not yet call this API.
Their compiler input declaration is not a substitute for accepted qualification evidence.

The selector does not enforce this policy for every build executable, dynamic
dependency, header search, or nested producer. Other tool selection gaps remain.
A successful plan or fixture test does not establish complete SDK compliance.
