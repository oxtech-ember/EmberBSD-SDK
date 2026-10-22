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

The sysroot builder uses clean OS/Ports sources and the accepted GCC16 runtime
stage. Start with its help and the documented preflight:

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
Use [release preparation and tags](docs/release-tags.md) to set the source
product version before building and tag the checked OS, Ports, and SDK commits.
The [distribution guide](docs/distribution.md) defines the additional boundary
for a future SDK release that accompanies a binary package profile.

A [source-built AArch64 check](docs/sysroot-transfer.md#source-built-aarch64-check)
on macOS arm64 passed SMB export/import and C/C++ consumer links at a new path.
Target execution and compiler distribution remain separate checks.

## Repository structure

| Path | Purpose |
| --- | --- |
| `scripts/` | Host commands for workspace creation, inspection, sysroot builds and transfers |
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

Build target code with the common GCC16 cross compiler on the development host.
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
