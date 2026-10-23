# Cross-build workflow

## Select independent host and target inputs

Choose a host environment that can run the required OS/Ports tools. Keep its
native compiler and generators separate from the target GCC16 compiler.
The current backend uses POSIX shell and make; CMake remains the build system
for dependencies which use it upstream. The workspace tools use Python's standard
library for paths, JSON and process invocation.
The current process adapters support Darwin and Linux. See
[host platforms](host-platforms.md) for qualification and WSL2/Windows boundaries.

Select the target through the actual producer. The current common GCC16 paths
use `aarch64--netbsd` or `x86_64--netbsd`. Preserve those fork ABI identifiers.
A target selector or a passing layout check does not qualify an architecture.

## Required tools

SDK control scripts use CPython 3.15 and its standard library. They require no
third-party Python packages, `pip` installation or virtual environment.

| Operation | Required tools |
| --- | --- |
| Create or inspect a workspace | CPython 3.15 with its standard library |
| Prepare libedit sources | Python with zlib support, Git and patch; see [libedit preparation](libedit.md) |
| Build the core sysroot | The qualified host process adapter, Python, Git, POSIX shell, the OS/Ports native build prerequisites, the common GCC16 and its verified runtime stage |
| Build an optional package profile | The selected Ports recipe's documented prerequisites |

The GCC16 sysroot profile uses Python for host control. Optional LLVM profiles
also use upstream Python build tools and `lit`. Installed upstream utilities
retain their own package dependencies. Record host and target Python roles
separately. A target device does not need the host controller to run prepared
acceptance programs.

## Cloud environment description

[`cloud-init.json`](../cloud-init.json) describes common SDK build requirements,
host capabilities and concrete reference configurations. It uses the custom
`emberbsd-cloud-environment` format, version `1`. It is declarative JSON, not
standard cloud-init user data. SDK commands do not install dependencies, grant
permissions or select a build profile from this file.

Always select both `common` and a `host_environments` entry. Read them with the
chosen concrete `profiles` entry. Requirements are additive; a profile does not
weaken the common contract. WSL2 also uses the `linux` host requirements. Map the common path roles
to caller-owned directories and prepare clean source inputs before the build.
No merge or host detection is performed by this file.

Linux x86_64 and ARM64, macOS, and WSL2 need native tools for their own OS and CPU.
The host CPU does not select the EmberBSD target. WSL2 runs the Linux tools inside
the guest; it does not establish a native Windows backend. Each environment
records the scope of available checks and the checks still required.
The WSL2 Linux filesystem is recommended for sources and build outputs. Other
mounts must pass the same case, link, mode and locking checks; a path prefix alone
does not establish compatibility.

The `debian13-x86_64-aarch64` profile retains the observed Debian package and
bootstrap choices. They are examples, not dependencies for every host. To add
a profile, choose a host environment, declare independent `host` and `target`
fields, and record that host's native tool providers and qualification evidence.
Keep package managers, versions and resource observations in that profile.
Resource figures are not minimums or proof of a complete build. Keep actual
build and target-execution receipts separate from the environment description.

Read the description with Python's standard library from the SDK root:

```sh
python3 - <<'PY'
import json
from pathlib import Path

description = json.loads(Path("cloud-init.json").read_text(encoding="utf-8"))
if description["format"] != "emberbsd-cloud-environment" or description["version"] != 1:
    raise SystemExit("Unsupported cloud environment description")
profile = description["profiles"]["debian13-x86_64-aarch64"]
host = description["host_environments"][profile["host_environment"]]
print(json.dumps(description["common"], indent=2))
if "uses_host_environment" in host:
    print(json.dumps(description["host_environments"][host["uses_host_environment"]], indent=2))
print(json.dumps(host, indent=2))
print(json.dumps(profile, indent=2))
PY
```

This checks the format identifier and selects the described profile. It does
not validate installed tools or enforce a sandbox policy. Follow the producer
CLIs below, record selected source and tool identities, and verify their outputs.

## Build the compiler with Ports

Use the [GCC16 cross profile](https://github.com/oxtech-ember/EmberBSD-Ports/blob/main/profiles/development-toolchain/cross/README.md).
It owns source pins, Binutils, compiler options and compiler checks.
Select its kernel-only mode only for artifacts which do not require a userland
sysroot. Application builds need the matching EmberBSD headers, CRT and runtime.

Use the [runtime bootstrap](https://github.com/oxtech-ember/EmberBSD-Ports/blob/main/profiles/development-toolchain/bootstrap/README.md)
for the target runtime and build-time C++ support. Its explicit
[existing-compiler mode](https://github.com/oxtech-ember/EmberBSD-Ports/blob/main/profiles/development-toolchain/bootstrap/external-compiler.md)
records the common POSIX GCC16 without rebuilding compiler frontends.
A recorded seed remains an input, not a new build result.

## Build the sysroot

Use the [SDK sysroot procedure](sysroot.md). It calls the fork's own build rules
and selects the verified Ports runtime stage and [prepared libedit source](libedit.md). Keep source revisions, compiler
hashes, runtime receipts and output hashes together.

The [OS external-runtime adapter](https://github.com/oxtech-ember/EmberBSD/blob/main/tools/docs/external-gcc-runtime.md)
provides build-time paths. It does not install a target runtime provider or
qualify a release. Follow the producer's current checks; SDK must not replace
EmberBSD headers or libraries with upstream NetBSD files.

## Build packages and applications

Use [Ports](https://github.com/oxtech-ember/EmberBSD-Ports) profiles with the
shared sysroot, object directory and package output. Supply real native
metadata tools when a target executable cannot run on the host. Keep target
ELF files out of the native tool prefix.

Public applications stay in their own repositories or
[Examples](https://github.com/oxtech-ember/EmberBSD-Examples).
Read the selected example's actual build interface. SDK does not define a
universal application CLI or device service.

## Record separate acceptance levels

Keep compiler preflight, actual target compilation, package checks, target
execution and physical hardware results distinct. Run the existing
[prebuilt compiler consumers](https://github.com/oxtech-ember/EmberBSD-Ports/blob/main/profiles/development-toolchain/cross/compile-tests.py)
on the selected target only after their cross build passes.

A release also needs loader/runtime closure, ownership, licenses and consumer
checks. See [distribution](distribution.md). Publish the SDK with the matching
binary package cohort only after those results exist.
