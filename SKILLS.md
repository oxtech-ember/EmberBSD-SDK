# SDK instructions for AI assistants

Use the same public commands and checks as a human developer.
Read [README.md](README.md) and the selected procedure before changing inputs.

| Task | Canonical skill or procedure |
| --- | --- |
| Create, inspect or reuse a Prebuild SDK workspace | [emberbsd-prebuild-sdk](https://github.com/oxtech-ember/Ember-Agent-Skills/blob/main/skills/emberbsd-prebuild-sdk/SKILL.md), [workspace contract](docs/prebuild-sdk.md) |
| Build the EmberBSD sysroot | [Sysroot procedure](docs/sysroot.md), `emberbsd-prebuild-sdk` |
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
Keep diagnostic fuzz objects out of target packages and sysroots. Reuse compatible shared objects through their producers'
checks. Record source, ABI, configuration and artifact provenance.

Treat workspace structure, completed sysroot, installed package closure and
target execution as separate evidence. Use the
[distribution boundary](docs/distribution.md) for future SDK releases.
