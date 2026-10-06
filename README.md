# EmberBSD SDK

Application interfaces, package contracts and developer tools for
[EmberBSD](https://github.com/apovalixin/EmberBSD) — Unix for intelligent devices.

## Purpose

SDK is the planned developer-facing contract for EmberBSD applications.
It will define supported API/ABI boundaries, application package metadata,
target profiles, build tools, templates and compatibility checks. Developers
and AI assistants should use the same documented tools and tests.

SDK owns what an application may depend on and how developers build and validate
it. Runtime will implement execution, lifecycle and device operations on the
target. Ports owns third-party build recipes; Examples owns runnable scenarios.
The operating system remains in the central EmberBSD repository.

## Current state

**Design stage.** This repository currently documents its purpose and boundaries.
No stable application API/ABI, package format, scaffolding CLI or released SDK
toolchain is available here. There is no SDK installation command yet.
Use current public Examples and Ports instructions for existing workflows.

The first implementation must pair a versioned contract with Runtime behavior
and an example that validates it. Wasm portability or compatibility between
ARM64 and RISC-V must be demonstrated; it is not implied by this repository.

## Related EmberBSD projects

[EmberBSD](https://github.com/apovalixin/EmberBSD#emberbsd-ecosystem) is the
central project and the entry point for the ecosystem.

- [EmberBSD](https://github.com/apovalixin/EmberBSD) — OS, drivers, boards and system builds.
- [EmberBSD-Runtime](https://github.com/neonix20b/EmberBSD-Runtime) — application execution, lifecycle and shared device operations; design stage.
- [EmberBSD-Ports](https://github.com/neonix20b/EmberBSD-Ports) — third-party recipes, patches and native dependencies.
- [EmberBSD-Examples](https://github.com/neonix20b/EmberBSD-Examples) — standalone applications and reproducible demonstrations.
- [Ember-Agent-Skills](https://github.com/neonix20b/Ember-Agent-Skills) — instructions for AI coding assistants and tested contributions.

## Connect developer skills

Install the developer instructions in a Codex CLI with plugin support:

```sh
codex plugin marketplace add neonix20b/Ember-Agent-Skills --ref main
codex plugin add emberbsd-development@ember-agent-skills
```

Start a new conversation and ask `$emberbsd-repository-guide` to identify real
interfaces and validation steps for your application. This installs assistant
skills; it does not create missing SDK commands. See the
[installation, verification and update guide](https://github.com/neonix20b/Ember-Agent-Skills#install-in-codex)
for the complete procedure and other assistant environments.
