# SDK instructions for AI assistants

Use the same public commands and checks as a human developer.
Read [README.md](README.md) and the selected procedure before changing inputs.

| Task | Canonical skill or procedure |
| --- | --- |
| Create, inspect or reuse a Prebuild SDK workspace | [emberbsd-prebuild-sdk](https://github.com/oxtech-ember/Ember-Agent-Skills/blob/main/skills/emberbsd-prebuild-sdk/SKILL.md), [workspace contract](docs/prebuild-sdk.md) |
| Build the EmberBSD sysroot | [Sysroot procedure](docs/sysroot.md), `emberbsd-prebuild-sdk` |
| Select package roots for one SDK task | [Package selection CLI](docs/package-selection.md) |
| Export or import a sysroot through a shared store | [Sysroot transfer procedure](docs/sysroot-transfer.md) |
| Prepare the cross compiler or cross-build a kernel | [emberbsd-build-kernel](https://github.com/oxtech-ember/Ember-Agent-Skills/blob/main/skills/emberbsd-build-kernel/SKILL.md) |
| Run a diagnostic host fuzz campaign and check its evidence | [Diagnostic fuzz checks](docs/fuzz-check.md) |
| Plan a Ports update and verify result evidence | [Ports planning procedure](docs/ports-plan.md), `emberbsd-port-software` |
| Build, compose or publish an OS binary kit | [OS release procedure](docs/os-release.md); source checks and target qualification are separate |
| Set the product version and tag checked sources | [Release preparation and tags](docs/release-tags.md), `emberbsd-port-software` |
| Repair a shared dependency or cross recipe | [emberbsd-port-software](https://github.com/oxtech-ember/Ember-Agent-Skills/blob/main/skills/emberbsd-port-software/SKILL.md) |
| Develop an application | [emberbsd-develop-app](https://github.com/oxtech-ember/Ember-Agent-Skills/blob/main/skills/emberbsd-develop-app/SKILL.md) |

The [skill package](https://github.com/oxtech-ember/Ember-Agent-Skills)
provides standalone references and client adapters. It does not supply a
compiler or device runtime. Manual use of the SDK procedures does not require
an installed skill package.

Keep build-host tools and target output separate. Clang/LLVM is the default
compiler family; select GCC only through an explicit platform profile.
Keep the SDK minimal for the requested task: select only its required native roles and target/ABI sysroot.
Do not build every target or optional package group by default. Kernel-only work does not require an application sysroot.
Keep diagnostic fuzz objects out of target packages and sysroots. Reuse shared
objects by their [input key](docs/input-keys.md): Git tree hashes and declared
compiler, configuration, ABI and target. A matching key means reuse without
rereading or rehashing. Do not add checks that reread or rehash inputs that a
git hash or input key already identifies.

Treat workspace structure, completed sysroot, installed package closure and
target execution as separate evidence. Use the
[distribution boundary](docs/distribution.md) for future SDK releases.

Keep operation recovery separate from package identity. Retrying a build,
upload or cleanup after a transport error does not itself require a version,
package revision or release ID change. Inspect the committed state first and
reuse the exact signed archives and release envelope for publication recovery.
Change package identity when installed payload or package metadata changes;
derive exact dependencies and revision checks from the owning declarations.
Keep ABI/API compatibility floors explicit; they need not equal today's revision.

Pin the SDK, OS and Ports commits for one build and its recovery. Updating those
inputs starts a changed-input operation, with compatible reuse decided by the
existing input-key contracts. Do not update tools used by an active build.
Before an expensive build, evaluate the selected dependency plan and check
unsatisfied version patterns together through the owning Ports checks.

For changes to resumable operations, test first use, unchanged repeat,
compatible changed inputs, interruption before and after commit, recovery with
the same arguments, and a competing incompatible operation. Use representative
metadata, including read-only hooks, supported schemas and exact dependencies when
relevant. Test explicit rejection of retired schemas in normal operations; do not retain a legacy reader
only to keep an old test passing. Update affected consumers and tests with the
current contract. Keep state-machine regressions runnable without a production SDK;
native signing, package lifecycle and target tests remain separate evidence.
Report executed and skipped checks, including missing integration prerequisites.
