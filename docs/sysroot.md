# Build an EmberBSD sysroot

The SDK builder orchestrates the existing EmberBSD and Ports producers. It
builds target headers, OS CRT and libraries, the dynamic loader, and the audited
GCC16 developer support. It does not install a system on a board.

## Required inputs

Use a qualified host process adapter with CPython 3.15 and the prerequisites of
the selected OS/Ports builders. Process adapters are available for Darwin and Linux.
See [host platforms](host-platforms.md).
Apply the [SDK tool selection policy](tool-selection.md).
Use SDK native GCC16 for host programs and SDK cross GCC16 for target code.
Keep host tools and target inputs separate.
The current automatic `cc`/`c++` selection is an enforcement gap, not an approved default.
Inspect that gap before treating a successful plan as permission to build.

Provide:

- A version 2 Prebuild SDK workspace with the selected target and a new or empty destination.
- Clean, fully materialized EmberBSD and Ports Git superprojects at recorded commits.
- The common GCC16.2.0 application cross compiler with the selected triplet and POSIX threads.
- An accepted Ports runtime bootstrap work tree and its original GCC archive.
- The exact prepared pkgsrc input used by that runtime build.

The normal
[Ports runtime verifier](https://github.com/oxtech-ember/EmberBSD-Ports/blob/main/profiles/development-toolchain/runtime/README.md)
must accept that stage. A kernel-only compiler or an arbitrary copy of GCC
libraries cannot replace those inputs. Record the seed provenance separately
from the newly produced sysroot.

Sparse checkout, skip-worktree and assume-unchanged entries are rejected.
Tracked files and links must be present with their recorded types.
The Ports `upstream/pkgsrc` gitlink is recorded as an external pin without
claiming its payload is qualified. The selected runtime verifier uses the
explicit `--pkgsrc` export instead. No recursive pkgsrc checkout is required
for that unused gitlink. Other gitlinks are rejected by this backend.

See [libedit preparation](libedit.md) for the mandatory `--libedit-source` input.

## Preflight and plan

Use the implemented script's help for the exact options:

```sh
python3 scripts/build-sysroot.py --help
python3 scripts/build-sysroot.py \
  --python /absolute/native/python3 \
  --sdk-source /absolute/EmberBSD-SDK \
  --sdk /absolute/prebuild-sdk-aarch64 \
  --os /absolute/EmberBSD \
  --ports /absolute/EmberBSD-Ports \
  --libedit-source /absolute/prepared-libedit \
  --host-cc /absolute/native-gcc16/bin/gcc \
  --host-cxx /absolute/native-gcc16/bin/g++ \
  --compiler /absolute/gcc16-cross \
  --runtime-work /absolute/gcc16-runtime-work \
  --gcc-archive /absolute/gcc-16.2.0.tar.xz \
  --pkgsrc /absolute/prepared-pkgsrc \
  --target aarch64 --jobs 2 --plan
```

The plan validates inputs and reports the exact build commands. It does not
compile or create a successful-build receipt. Remove `--plan` for the actual
build only after the preflight succeeds.

The builder first compares `--target` with the canonical layout declaration.
An unknown or different target fails before compiler, runtime or producer
queries and before output writes. A version 1 or manifestless tree remains
available to inspection and existing external producers. This builder does not
infer its target or migrate it. Create a separate version 2 workspace for new
builds; keep active build trees unchanged.

The default destination is the workspace's `sysroot`. A nonempty destination
is rejected before build writes. For an update, select an explicit new stage
such as `--destination /absolute/prebuild-sdk-aarch64/stages/sysroot-next` inside the
same shared workspace. A destination inside the active `sysroot` is rejected.
Keep the active sysroot unchanged while other consumers use it.

`--objects` and `--tools` can select existing compatible component paths under
`obj`. Defaults use stable host/target names. Object reuse requires the recorded
compiler, runtime, host, target and configuration context. The tool does not
clear an incompatible context to make a build pass.

## Producer sequence

The selected OS source provides these operations:

1. `build.sh -U -u` selects the common external GCC16 and the audited runtime.
2. `tools libs` builds native tools, creates object directories and installs
   target headers, CRT and libraries through the fork's own rules.
3. The same generated make wrapper builds and installs `libexec/ld.elf_so`.
4. The builder copies the verified GCC16 developer stage into `usr/pkg/gcc16`.
5. It verifies the copied inventory and runs the normal runtime verifier again.

The `libs` operation exists in the fork's
[`build.sh`](https://github.com/oxtech-ember/EmberBSD/blob/main/build.sh).
Its `buildlibs` implementation invokes `do-distrib-dirs`, `includes` and
`do-lib`. The [OS runtime adapter](https://github.com/oxtech-ember/EmberBSD/blob/main/tools/docs/external-gcc-runtime.md)
selects external GCC headers, CRT and libraries during compilation.
The SDK does not duplicate their source recipes.

The copied developer stage has no fabricated target package registration.
Select actual runtime and developer package ownership when composing a Ports
sysroot or device image. That package composition is a separate operation.

## Output and acceptance

A completed build records source pins, tool/runtime identities, actual commands,
file/link inventory and SHA256 values. It checks required headers, CRT,
libraries and loader, target ELF architecture, and contained relative links.
An error retains an explicit incomplete state and its evidence; a previous
artifact does not become the result of that failed build.

The sysroot builder waits up to 30 seconds for a complete empty producer
session snapshot on Darwin. Unrelated host process churn can make a shorter
snapshot inconclusive. The builder still quarantines the object contexts if
it cannot prove drainage; it never treats an uncertain snapshot as empty.

A successful builder result qualifies a build artifact. Target execution,
loader/runtime behavior, package closure and physical hardware remain separate
checks. Run the producer's prebuilt consumers on the selected EmberBSD target.
Do not claim those checks from command dispatch fixtures or `--plan` output.

Use [sysroot transfer](sysroot-transfer.md) to export this completed output and
restore a pinned copy into a new local directory on a developer host.
A releasable SDK also needs the [distribution checks](distribution.md), including
host-platform and relocation acceptance. The initial workspace tests do not
establish a complete sysroot build on every advertised future platform.
