# Build an EmberBSD sysroot

Use the native Ports tool prefix with `--host-prefix`.
The builder checks prefix paths and host architecture, then uses the OS command mapping.
No independent native-tool qualification or admission receipt is required.
Host and target compiler families are independent. Clang is the default for
both; `--compiler-family gcc` selects the GCC target runtime route explicitly.
The [common LLVM runtime contract](llvm-runtime.md) describes the Clang phases.

The recorded producer sequence covers target headers, OS CRT and libraries,
the dynamic loader, and the selected compiler's developer runtime. It does not install
a system on a board.

`--plan` also runs an optional prepared-source graph diagnostic before listing
commands. A normal build skips that additional graph walk; the selected source
identities, target ABI, tools and build contexts are still checked, and the
actual OS makefiles retain their `.error` guards for sources the build reaches.
The graph diagnostic is not a guarantee that a build will succeed or that it
will discover every conditional source guard.

## Required inputs

Use a qualified host process adapter with CPython 3.15 and the prerequisites of
the selected OS/Ports builders. Process adapters are available for Darwin and Linux.
See [host platforms](host-platforms.md).
Apply the [SDK tool selection policy](tool-selection.md).
Use SDK native Clang/LLVM for host programs, or GCC through an explicit platform profile.
The implemented GCC target route uses the matching SDK cross GCC16 and runtime.
Keep host tools and target inputs separate.
Select the [native Ports prefix](native-toolset.md).
The loader selects the roles required by the OS contract plus Python.
`--host-cc`, `--host-cxx`, and `--python` are optional checked aliases.
They must resolve to their corresponding prefix tools.

Provide:

- A version 2 Prebuild SDK workspace with the selected target and a new or empty destination.
- Clean, fully materialized EmberBSD and Ports Git superprojects at recorded commits.
- A Ports Clang/LLVM prefix with the selected target backend and LLD.
- The prepared common Ports LLVM source, passed as `--llvm-source`.
- The native [common Binutils build](https://github.com/oxtech-ember/EmberBSD-Ports/blob/main/profiles/development-toolchain/cross/binutils.md)
  selected with `--host-binutils`, including its matching `src`, `build`,
  `prefix` and `host-bfd.mk`. The OS host tools consume BFD and `elfedit`
  from this work tree through `EMBER_HOST_BFD_CONFIG`. Keep it unchanged
  while the tools are built or reused.
- A new runtime work path below the SDK object directory, or an explicitly
  selected completed full `--llvm-runtime-stage`. When selecting a full stage directly, also pass `--llvm-bootstrap-stage` for the exact bootstrap stage recorded by that full runtime. For a separate recorded foundation, select `--llvm-runtime-base` as described in [the runtime guide](llvm-runtime.md). The OS embeds its checked static libunwind archive into libc; the full runtime remains the application runtime.

The prepared common LLVM source is also required with GCC: libc and kernel
builtins use that source independently of the application runtime choice.
For the GCC exception, select `--compiler-family gcc`, the common GCC16.2.0
application cross compiler with the selected triplet and POSIX threads, its
Ports runtime work tree, original `--gcc-archive` and prepared `--pkgsrc` export.
The [GCC runtime verifier](https://github.com/oxtech-ember/EmberBSD-Ports/blob/main/profiles/development-toolchain/runtime/README.md)
checks that route. Neither family accepts arbitrary copied runtime libraries.

Sparse checkout, skip-worktree and assume-unchanged entries are rejected.
Tracked files and links must be present with their recorded types.
The Ports `upstream/pkgsrc` gitlink is recorded as an external pin without
claiming its payload is qualified. The selected runtime verifier uses the
explicit `--pkgsrc` export on the GCC route instead. The common libarchive and
libevent producers require the pinned pkgsrc checkout to prepare and verify
their export inputs. Other gitlinks are rejected by this backend.

See [libedit preparation](libedit.md) for the mandatory `--libedit-source` input.
For an OS producer that consumes common OpenSSL, prepare the current Ports
`security/openssl` source using `scripts/source-inputs/openssl/source.py` and
select it with `--openssl-source`. The owner verifies its archive and complete
patch set when it prepares the tree; the SDK reads the tree's preparation
receipt. This optional selector is forwarded as `EMBER_OPENSSL_SOURCE`;
older OS producers remain usable without it. For an OS producer using the
common `security/libfido2` source, prepare it with the Ports
`scripts/source-inputs/libfido2/source.py` owner and pass `--libfido2-source`;
the SDK verifies its current recipe identity and forwards `EMBER_LIBFIDO2_SOURCE`.

For OS builds using the common `devel/libcbor` source, prepare it using the
Ports `scripts/source-inputs/libcbor/source.py` owner and select it with
`--libcbor-source`; SDK forwards `EMBER_LIBCBOR_SOURCE`. Source preparation
needs no application sysroot.

For OS builds using common `net/libfetch`, prepare its source using the Ports
`scripts/source-inputs/libfetch/source.py` owner and select `--libfetch-source`.
The SDK forwards `EMBER_LIBFETCH_SOURCE` to the OS build.

For OS builds using common `databases/openldap`, prepare its source with the
Ports `scripts/source-inputs/openldap/source.py` owner and select
`--openldap-source`. The SDK forwards `EMBER_OPENLDAP_SOURCE`.

For OS builds using common `net/unbound`, prepare its source with the Ports
`scripts/source-inputs/unbound/source.py` owner and select `--unbound-source`.
The SDK forwards `EMBER_UNBOUND_SOURCE`.

For OS builds using common `net/wpa_supplicant`, prepare its source with the
Ports `scripts/source-inputs/wpa/source.py` owner and select `--wpa-source`.
The SDK forwards `EMBER_WPA_SOURCE`.
Select prepared Ports `scripts/source-inputs/mdns/source.py` and `scripts/source-inputs/dhcpcd/source.py` outputs with `--mdns-source` and `--dhcpcd-source`. The SDK forwards `EMBER_MDNS_SOURCE` and `EMBER_DHCPCD_SOURCE`.

For OS builds using common `security/heimdal`, prepare its source with the
Ports `scripts/source-inputs/heimdal/source.py` owner and select `--heimdal-source`.
The SDK forwards `EMBER_HEIMDAL_SOURCE`.
For OS builds using common `security/openssh`, prepare the source with the
Ports `scripts/source-inputs/openssh/source.py` owner and select
`--openssh-source`. The SDK forwards `EMBER_OPENSSH_SOURCE`.

For OS builds using common zlib, prepare the Ports `devel/zlib` source and pass
`--zlib-source`; the SDK forwards `EMBER_ZLIB_SOURCE`, including during header
installation.

Prepare libarchive with the Ports `scripts/libarchive-source.py prepare` command
and libevent with `scripts/source-inputs/libevent/source.py prepare`; both use
the selected SDK source and canonical Ports export. Pass the resulting trees
as `--libarchive-source` and `--libevent-source` for the base build. The
bootstrap headers phase does not consume these two sources.

## Preflight and plan

Use the implemented script's help for the exact options:

```sh
/absolute/host-pkg-llvm/bin/python3.15 -B scripts/build-sysroot.py --help
/absolute/host-pkg-llvm/bin/python3.15 -B scripts/build-sysroot.py \
  --sdk-source /absolute/EmberBSD-SDK \
  --sdk /absolute/prebuild-sdk-aarch64 \
  --os /absolute/EmberBSD \
  --ports /absolute/EmberBSD-Ports \
  --libedit-source /absolute/prepared-libedit \
  --libarchive-source /absolute/prepared-libarchive \
  --libevent-source /absolute/prepared-libevent \
  --host-prefix /absolute/host-pkg-llvm \
  --host-binutils /absolute/binutils-aarch64 \
  --compiler /absolute/host-pkg-llvm \
  --llvm-source /absolute/prepared-llvm \
  --runtime-work /absolute/prebuild-sdk-aarch64/obj/llvm-runtime \
  --destination /absolute/prebuild-sdk-aarch64/stages/sysroot-next \
  --target aarch64 --plan
```

The plan checks inputs and lists the build commands without creating outputs.
Remove `--plan` to run those commands.
By default, the builder uses the CPUs available to its process, respecting
process affinity when the host exposes it. Set `--jobs` to override that count;
`--jobs 1` selects a serial build. This choice does not sample CPU load or free
memory.

The final Clang build enables the common libc++ base layout by default. Use
`--no-base-cxx` to retain the historical build-only layout and `MKLIBCXX=no`;
`--base-cxx` explicitly enables it. GCC remains disabled by default, and
`--base-cxx` with `--compiler-family gcc` is refused. `build-os-release.py`
uses the same default and opt-out for its final Clang producer profile.

The builder first compares `--target` with the canonical layout declaration.
An unknown or different target fails before compiler, runtime or producer
queries and before output writes. A version 1 or manifestless tree remains
available to inspection and existing external producers. This builder does not
infer its target or migrate it. Create a separate version 2 workspace for new
builds; keep active build trees unchanged.

Select an explicit new destination stage. A nonempty destination
is rejected before build writes. For an update, select a new stage
such as `--destination /absolute/prebuild-sdk-aarch64/stages/sysroot-next` inside the
same shared workspace. A destination inside the active `sysroot` is rejected.
For an exclusively owned, completed SDK work stage, `--update-stage` reuses
its original destination and compatible objects. The builder marks the stage
incomplete during the update; a later invocation with the same option resumes
an interrupted update. Do not use the stage as a consumer input while updating.
Keep the active sysroot unchanged while other consumers use it.

`--objects` and `--tools` can select existing compatible component paths under
`obj`. Defaults use stable host/target names. Object reuse requires the recorded
compiler, runtime, host, target and configuration context. The tool does not
clear an incompatible context to make a build pass. When the selected OS provides a prepared-source refresh
contract, the SDK can selectively clean the affected objects under the existing
object-tree lock before recording the new source context. Other input changes
remain subject to the normal compatibility check.

For an otherwise identical bootstrap runtime, OS and Ports commit pins may
advance while every recorded base input, recipe file and runtime payload digest
stays unchanged. Moving that runtime still refreshes affected header consumers.

A full runtime rebuilt against updated base libraries can also reuse objects
when its entire recorded payload, modes, links, LLVM source, recipe files and
target identity are unchanged. Base provenance and the bootstrap manifest pin
may advance; the bootstrap archive digests and selected unwind binding must
still match. Base headers and libraries keep their normal OS and prepared-source
refresh paths. A moved runtime refreshes its header and C++ link consumers.

For a Clang runtime update confined to C++ headers and the shared libc++ and
libc++abi libraries, the same planner refreshes their header and link consumers.
The LLVM release, target C ABI, artifact names, symlinks, builtins, unwinder and
static runtime archives must stay unchanged. A relocated runtime prefix also
refreshes these consumers so their dependency files use the new paths. Source
and runtime changes share one planning pass; unrelated C objects and native
tools remain available. Runtime differences are read from existing manifests,
without hashing the installed trees again.

The context includes the selected native prefix, tool paths, host and target, and flags.
SDK controller module updates do not by themselves invalidate objects: the
selected Python and API, computed build flags, compiler, runtime, and native ABI
still must match. When the loaded SDK package is the selected clean SDK
checkout, the build record names its modules by that checkout's Git commit
and does not hash them. Another SDK copy is compared with the loaded modules
file by file. An OS contract
may add an explicit utility role while every existing command, binding, tool
selection, and the relative order of existing search directories stays intact.
Removing or replacing a tool, changing a compiler binding, or changing ABI
inputs refuses reuse. Published source ancestry is checked as before.
Completed LLVM header/base stages use the same significant-input comparison
and retain their original build records when reused. Their complete output
inventory and the OS files recorded in the stage set's `inputs.json` (the
whole OS source tree without a record) must remain identical; an ancestor OS revision
alone cannot prove that a completed stage contains newer source changes.
Unrelated Ports commits and SDK controller changes do not require rebuilding
these stages when the measured common recipes and all compiled inputs match.
Changed recorded OS inputs need a new stage, reusing compatible objects incrementally.
Native-tool and headers-only contexts do not use the target linker and may
reuse their outputs after its binary changes at the same selected path.
Compiler identities, selected command routes and ABI inputs still must match.
Base/final object contexts retain the strict target linker comparison; linked
target outputs cannot silently bypass a required relink. Fingerprints remain
in each original record, including records for reused headers.
Native ABI flags use `EMBER_HOST_ABI_CPPFLAGS`, `EMBER_HOST_ABI_CFLAGS`,
`EMBER_HOST_ABI_CXXFLAGS`, and `EMBER_HOST_ABI_LDFLAGS`, separate from target flags.
The build environment uses prefix directories in `PATH`; Git source inspection remains separate.
Git is selected before narrowing that path. Repeated phase preflight checks use
the same executable and keep the build environment restricted to the native prefix.
The builder loads the OS contract roles and publishes `EMBER_NATIVE_TOOLS` in a
contract-keyed directory under the locked tools tree. New contracts leave earlier
mappings unchanged; the former fixed `native-routing` name remains reusable.
Reuse compares the old contract kept in that directory with the selected one.
Only the former fixed name reads it from the recorded OS commit, using local Git
objects: a partial OS checkout that lacks it stops with an error instead of
fetching it.
The selected toolset is fixed at preflight for the run.
New build records bind selected paths and commands. Historical records remain readable.

### Repeated input checks within one run

A run is one `build-sysroot.py` or `build-os-release.py` invocation, including
all of its LLVM phases. A common prepared source is identified by the receipt
its owner wrote when it prepared it; the SDK reads the receipt and neither runs
the owner again nor reads the tree. Once per run, before the build, Ports
`scripts/prepared-source-current.py` compares every selected receipt with the
effective recipe of the selected Ports checkout, from Git object ids and the
small recipe files only; a source prepared from another recipe (a stale Ports
pin) fails the run and must be prepared again. The Ports owner verifier of the common LLVM
source and of each LLVM runtime stage runs the first time the run needs it.
Later checks of the same input compare an in-memory metadata fingerprint
instead of running the verifier again.

The fingerprint covers only input-key files: the stage manifest
(`runtime.json`), the `llvm-source.json` manifest of a prepared LLVM source,
and the files named by the verifier command (Python, the owner script, patch,
lzip and Git). For each file it records the device, inode, mode, size,
modification time and change time. A stage or source tree is never walked:
its manifest is its [input key](input-keys.md). A changed manifest runs the
verifier again. A directory or a missing input gets no fingerprint and is
checked every time.

The LLVM or GCC runtime is verified once, at preflight. The build does not
verify it again before or after the copy into the sysroot or release, and does
not inventory the copied files again. The headers/base completion path also
returns its recorded outputs without another inventory.

The OS, Ports and SDK checkouts must be clean Git roots. Each checkout is
checked with Git (status, index entries and commit) once per run, by the first
phase that needs it. Later phases of the same run (the LLVM stage selection,
the headers and base stages, the final build and the libedit recipe check)
reuse that record without asking Git or walking the tree again, whether the
Git directory is inside the checkout or elsewhere (a linked worktree). The
checkouts are not checked again after the build: the record names the commit
the run builds, and the outputs are identified by their own hashes. Keep the
checkouts unchanged while a build runs. Release finalization publishes the
inspected outputs and does not inspect the checkouts.

Receipts are never written to disk. A new run starts with an empty set and
selects its inputs at preflight. Dispatch and completion reuse the selected
native toolset, target compiler and Python identity. The builder inspects newly
produced outputs before recording a successful build.

## Recorded producer sequence

The [OS command mapping](native-os-routing.md) supplies native make, shell, compilers, and utilities.
The builder does not use the old implicit make bootstrap.

The Clang path first uses `tools`, then the generated OS make wrapper's `obj`,
`do-distrib-dirs` and `includes` goals. Ports builds compiler-rt builtins and PIC
static libunwind against those headers. A C-only OS stage then supplies libc,
the remaining C libraries, the loader and fork-owned CRT. That stage selects
the initial LLVM runtime for C links; it never asks for `libgcc`. Ports can then
link libc++, libc++abi and shared libunwind against the actual base.

The C-base phase may consume bootstrap archives from an ancestor OS revision
when the preserved header files and symlinks still match their measured runtime
inputs. It compares those inputs again with the newly installed base headers
before qualifying the result. A changed header leaves that stage incomplete;
it requires rebuilding the affected runtime. The explicitly selected full
runtime may originate from a different OS revision; select one compatible
with the final OS ABI, including libc and C++ runtime interfaces.

With the full selected runtime, the OS source provides these operations:

1. `build.sh -U -u` selects the external compiler family and its matching runtime.
2. `tools libs` builds native tools, creates object directories and installs
   target headers, CRT and libraries through the fork's own rules.
3. The same generated make wrapper builds and installs `libexec/ld.elf_so`.
4. The builder copies the verified developer stage into `usr/pkg/llvm` for
   Clang, or `usr/pkg/gcc16` for the explicit GCC alternative.
5. It records the runtime by its preflight record; it does not verify it again.

The `libs` operation exists in the fork's
[`build.sh`](https://github.com/oxtech-ember/EmberBSD/blob/main/build.sh).
Its `buildlibs` implementation invokes `do-distrib-dirs`, `includes` and
`do-lib`. The OS runtime adapters select the matching compiler runtime during
compilation. Clang keeps the fork's CRT and uses the common Ports LLVM source
for the builtins incorporated into libc.
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

Sysroot builds select `MKREPRO=yes` and pass `MKREPRO_TIMESTAMP` from the
selected OS commit time. The OS build maps the source and object roots to
`/usr/src` and `/usr/obj`. It maps the prepared libedit, libevent, libarchive
and LLVM sources to `/usr/src/external/NAME`, and the staged runtime to
`/usr/pkg/llvm` or `/usr/pkg/gcc16`. Ports maps the LLVM runtime phases to the
same roots. Shipped files then contain no build-host paths, and the output
does not depend on the directory layout of the build host. See the OS
[reproducible path rules](https://github.com/oxtech-ember/EmberBSD/blob/main/tools/docs/cross-build.md#reproducible-debug-paths).

The sysroot builder waits up to 120 seconds for a complete empty producer
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

Select the common Ports `textproc/expat` prepared source with `--expat-source`
when the OS producer requires `EMBER_EXPAT_SOURCE`. Prepare it with the Ports
`scripts/source-inputs/expat/source.py` owner.

For a reviewed, compatible change to target ELF object tools, an experienced
developer can bind an explicit one-run transition to the saved object context
and the selected tool, for example `--compatible-target-tool
objcopy:OLD_SHA256:NEW_SHA256`. The role must be `objcopy` or `strip`; repeat
the option for both when needed. The SDK compares both digests and keeps every
other compiler, path, ABI and object input strict. This is an explicit
developer assertion, not automatic proof that tool outputs are equivalent.
