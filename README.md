# EmberBSD SDK

Developer tools and build contracts for [EmberBSD](https://github.com/oxtech-ember/EmberBSD), Unix for intelligent devices.

## Prebuild SDK

Prebuild SDK is the shared development-host workspace for compiled objects,
the target sysroot, binary packages and build receipts. Reuse one compatible
workspace across tasks for one target. Use separate workspaces for `aarch64`
and `amd64`. Keep source checkouts separate from build outputs.

The first tools create and inspect that workspace. They use Python and its standard
library. They do not download a compiler or qualify an existing sysroot.

```sh
python3 scripts/create-prebuild-sdk.py /absolute/prebuild-sdk-aarch64 --target aarch64
python3 scripts/create-prebuild-sdk.py /absolute/prebuild-sdk-amd64 --target amd64
python3 scripts/inspect-prebuild-sdk.py /absolute/prebuild-sdk-aarch64
```

To retain an existing build tree, create a named alias. The existing tree must
already contain real `obj`, `sysroot` and `packages` directories. An alias to a
legacy tree supports inspection. It does not declare that tree's target.

```sh
python3 scripts/create-prebuild-sdk.py /absolute/prebuild-sdk --adopt /absolute/build
```

The canonical version 2 manifest declares the target and triplet. A directory
name, alias or build-host architecture does not declare them. The sysroot builder
refuses a legacy or version 1 workspace whose target is unknown. These tools do
not migrate an active build tree or change its existing producer commands.

The sysroot builder uses clean OS/Ports sources and the common Clang/LLVM
toolchain. It builds headers, the initial runtime, OS libc/CRT and the full C++
runtime in dependency order. GCC is an explicit platform alternative.
Start with its help and the documented preflight:

```sh
python3 scripts/build-sysroot.py --help
```

See the [workspace contract](docs/prebuild-sdk.md),
[build workflow](docs/build-workflow.md), [host platforms](docs/host-platforms.md),
[shared host tools](docs/host-tools.md), and [sysroot builder](docs/sysroot.md).
Use [sysroot export and import](docs/sysroot-transfer.md) to exchange verified
build outputs through a file share. Bundles preserve inventory and provenance;
import restores a new local tree without replacing an existing destination.
Use [Ports update plans](docs/ports-plan.md) to compare evaluated package graphs,
identify affected consumers, and verify explicit artifact receipts. These tools
leave builds and fixes to the coding agent and existing Ports producers.
Use the [libedit](docs/libedit.md) and [libelf](docs/libelf.md) source contracts
to prepare canonical Ports sources for native OS consumers.
Use [release preparation and tags](docs/release-tags.md) to set the source
product version before building and tag the checked OS, Ports, and SDK commits.
Use [OS release composition](docs/os-release.md) for complete OS producer orchestration,
kit verification and immutable GitHub publication.
The [distribution guide](docs/distribution.md) defines the additional boundary
for a future SDK release that accompanies a binary package profile.

A [source-built AArch64 check](docs/sysroot-transfer.md#source-built-aarch64-check)
on macOS arm64 passed SMB export/import and C/C++ consumer links at a new path.
Target execution and compiler distribution remain separate checks.

Use [diagnostic fuzz checks](docs/fuzz-check.md) to run prepared Clang host
fuzz targets, replay their corpus with the selected production compiler family
(Clang by default, explicit GCC alternative) and verify portable evidence.

## Repository structure

| Path | Purpose |
| --- | --- |
| `scripts/` | Host commands for workspace creation, inspection, sysroot builds, transfers and OS release composition |
| `src/emberbsd_sdk/` | Shared Python modules for SDK and Ports host commands |
| `tests/` | Executable contracts and refusal cases |
| `docs/` | Layout, compiler/sysroot workflow, reuse and distribution boundaries |
| [`SKILLS.md`](SKILLS.md) | AI assistant entry points and canonical skills |

Compiler and third-party recipes remain in
[EmberBSD-Ports](https://github.com/oxtech-ember/EmberBSD-Ports).
The [OS repository](https://github.com/oxtech-ember/EmberBSD) owns headers, libc,
CRT, drivers and system build rules. SDK connects those producers; it does not
maintain another compiler recipe or another libc.

## Build and validation boundaries

Follow the [SDK tool selection policy](docs/tool-selection.md).
The default native toolset is the `host-pkg-llvm` prefix built by Ports with Clang and LLVM.
Select it with `--host-prefix` for sysroot and OS release builds.
Use `--host-compiler-family gcc` only for an explicit platform exception.
No separate toolchain qualification, descriptor, acceptance receipt, or hash is required.
The [native prefix API](docs/native-toolset.md) checks contained executable paths,
host CPU, compiler driver routes, Clang resource files, and script interpreters.
The [OS command mapping](docs/native-os-routing.md) selects those tools for nested builds.
Select native and target compilers separately. Clang is the default family.
The [runtime boundary](docs/tool-selection.md#migration-boundary) describes the
phased LLVM producer and separates build records from target acceptance.
Reject implicit system build-tool selection.
The selected target architecture, ABI and runtime must agree. Build-host tools
remain host executables. Boards and VMs execute prepared acceptance programs.

Workspace checks, compiler checks, sysroot builds, package closure and target
execution are separate results. Record the exact sources, configuration,
compiler, runtime and output hashes for a build. Equal directory names or
package versions do not establish equivalent inputs.

The workspace tools use portable filesystem APIs. Producer control has Darwin,
Linux and a gated NetBSD 11 amd64 process adapter. NetBSD process control passes
29 native contracts with CPython 3.15 and the GCC16 host-header ABI probe.
This qualifies the logged-producer interface, not the complete SDK on NetBSD.
Exclusive directory publication and sysroot import remain unsupported there;
the complete native SDK suite does not pass. See [host platforms](docs/host-platforms.md).
Logged producers can select a finite positive `empty_snapshot_timeout` for
empty-session proof. The default remains 2 seconds. The
[host-tool API](docs/host-tools.md#run-a-logged-producer) defines this option
and its startup and drainage boundaries. Failed drainage also polls its owned
leader without relaxing the original ownership error or reuse refusal.
Other build hosts require their own process backend and checks.
Host and target platforms are independent inputs.
Actual platform checks are recorded with the tests;
portability intent does not establish an untested host or target.

SDK scripts use Python and its standard library. Related Ports controllers use
the same SDK modules. The control tools need no third-party Python packages.
Optional third-party Ports profiles retain their upstream tool requirements;
see the [dependency boundary](docs/build-workflow.md#required-tools).

The declarative [cloud-init.json](cloud-init.json) separates common build
requirements from Linux, macOS and WSL2 host capabilities and a concrete Debian
reference profile. Host CPU and target CPU are independent. See its
[format and usage](docs/build-workflow.md#cloud-environment-description).

A stable application API/ABI, application package format, device runtime and
redistributable binary SDK are still separate work. These workspace tools do
not implement those contracts.

## Check the tools

Use CPython 3.15 and its standard library for the complete tool set.
Run the contracts from this checkout:

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
```

Before a push, run the tests affected by your changes against `origin/main`:

```sh
python3 scripts/select-tests.py --run
```

The selector follows imports between SDK modules, scripts and test helpers,
and tests that name a changed file. It runs the whole suite when a change
touches the shared core (a module that at least half of the tests use) or
cannot be mapped. CI runs the whole suite on every push and pull request.

The tests use temporary filesystem trees and build-command fixtures. Their
results do not establish a real sysroot build or target execution.

## Related projects

- [EmberBSD](https://github.com/oxtech-ember/EmberBSD): OS and system builds.
- [Ports](https://github.com/oxtech-ember/EmberBSD-Ports): recipes, patches and dependency profiles.
- [Examples](https://github.com/oxtech-ember/EmberBSD-Examples): runnable applications and demonstrations.
- [Runtime](https://github.com/oxtech-ember/EmberBSD-Runtime): planned application execution and lifecycle contracts.
- [Agent Skills](https://github.com/oxtech-ember/Ember-Agent-Skills): portable developer instructions for AI assistants.

## License

New SDK tools use [BSD-2-Clause](LICENSE). Each imported component retains its
own license and provenance. AI-assisted migrations record their source baseline
and origin in the change commits.
