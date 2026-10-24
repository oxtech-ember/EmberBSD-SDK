# Build an EmberBSD sysroot

Actual sysroot builds currently fail before preflight or producer execution.
The OS producer does not yet enforce complete native-tool routing.
There is no override flag. A qualified native toolset permits a read-only
`--plan`; it does not remove this execution gate.

The recorded producer sequence covers target headers, OS CRT and libraries,
the dynamic loader, and audited GCC16 developer support. It does not install
a system on a board.

## Required inputs

Use a qualified host process adapter with CPython 3.15 and the prerequisites of
the selected OS/Ports builders. Process adapters are available for Darwin and Linux.
See [host platforms](host-platforms.md).
Apply the [SDK tool selection policy](tool-selection.md).
Use SDK native GCC16 for host programs and SDK cross GCC16 for target code.
Keep host tools and target inputs separate.
Select an accepted [native toolset](native-toolset.md) with its descriptor and
independently accepted producer receipt, each pinned by SHA256.
The loader runs before compiler or runtime queries. It requires the direct
`cc`, `cxx`, `sh`, and `python` roles, in addition to its compiler-component checks.
`--host-cc`, `--host-cxx`, and `--python` remain required checked aliases.
They must resolve to the corresponding qualified role. Commands retain the
qualified path spelling, including a qualified in-bundle symlink.

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
  --native-toolset /absolute/evidence/native-toolset.json \
  --native-toolset-sha256 "$descriptor_hash" \
  --native-producer-receipt /absolute/evidence/qualification.json \
  --native-producer-receipt-sha256 "$accepted_receipt_hash" \
  --compiler /absolute/gcc16-cross \
  --runtime-work /absolute/gcc16-runtime-work \
  --gcc-archive /absolute/gcc-16.2.0.tar.xz \
  --pkgsrc /absolute/prepared-pkgsrc \
  --target aarch64 --jobs 2 --plan
```

The plan validates inputs and reports the recorded build commands. It does not
compile or create a successful-build receipt. It reports `build_admission`
as refused. Removing `--plan` still refuses execution.

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
The context includes the normalized native selection, descriptor hash, accepted
producer receipt reference, tool paths, and ABI flags. An older context without
that identity is incompatible. Native ABI flags map only to `HOST_CPPFLAGS`,
`HOST_CFLAGS`, `HOST_CXXFLAGS`, and `HOST_LDFLAGS`. They do not enter target flags.
The planned environment contains only directories of declared native tools in
`PATH`. Git is selected separately for source inspection.
This consumer refuses a native tool directory that contains the host `PATH`
separator. Such a directory cannot be represented as one `PATH` entry.

## Recorded producer sequence

Execution of this sequence remains blocked. At OS commit
[`51e33e73`](https://github.com/oxtech-ember/EmberBSD/tree/51e33e73bad73e84801a4c99adcaa898b32f9e79),
[`build.sh:1807`](https://github.com/oxtech-ember/EmberBSD/blob/51e33e73bad73e84801a4c99adcaa898b32f9e79/build.sh#L1807)
searches `PATH` for make. Its
[`bootstrap:1906`](https://github.com/oxtech-ember/EmberBSD/blob/51e33e73bad73e84801a4c99adcaa898b32f9e79/build.sh#L1906)
invokes bare `env` and retains `${HOST_CC-cc}`.
Its [shell selection](https://github.com/oxtech-ember/EmberBSD/blob/51e33e73bad73e84801a4c99adcaa898b32f9e79/build.sh#L408)
also has system fallbacks. Selecting four direct roles does not qualify these
nested utilities or their searches. The OS needs its own checked selector
contract before this SDK execution gate can be replaced.

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
