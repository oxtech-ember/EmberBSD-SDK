# Compose and publish OS releases

Use the native Ports tool prefix with `--host-prefix`.
The builder checks prefix paths and host architecture, then uses the OS command mapping.
No independent native-tool qualification or admission receipt is required.

SDK owns release orchestration, kit composition and immutable publication.
The OS owns `build.sh`, distribution rules, media layout and installer checks.
Ports owns the common Clang/LLVM compiler and runtime, and the explicit GCC alternative.
Use [release tags](release-tags.md) for coordinated OS/Ports/SDK version preparation;
that command does not publish binary OS kits.

The release tools are Python standard-library commands. They use the shared
SDK compiler, runtime, source, object-context and process-ownership checks.
They do not define another toolchain recipe. The migration originates in
`EmberBSD@4a4c98bb99999836103d3fe63fd6f4c98013f0c5:ember/release/`
and its `ember/tests/release-*.rb` contracts. The source license is BSD-2-Clause.

See [libedit preparation](libedit.md) for the mandatory `--libedit-source` input.
Prepare and select the common Ports libarchive and libevent sources as
described in [sysroot preparation](sysroot.md); release builds require both.
The `--host-binutils` work directory selects the matching common BFD and
`elfedit` build used by OS host tools; see the [sysroot inputs](sysroot.md#required-inputs).

## Build inputs and commands

Read [sysroot inputs](sysroot.md#required-inputs) and
[host requirements](host-platforms.md). Use CPython 3.15+, Git, the OS host
build prerequisites and an ISO-capable libarchive `tar`. Each source checkout
must be clean and fully materialized. Select exact OS, Ports and SDK commits.
Clang is the default target family and installs the common C++ runtime in the
base sets by default. Use `--no-base-cxx` for the historical build-only Clang
layout; explicit GCC builds keep base C++ disabled. Native compilation uses
the independently selected host prefix. Prepare `--llvm-source` with the common Ports source
producer. Without `--llvm-runtime-stage`, the [LLVM coordinator](llvm-runtime.md)
builds headers, the initial runtime, C base and full C++ runtime before the
release. An explicitly supplied full stage is checked against these same inputs. When selecting a full stage directly, also pass `--llvm-bootstrap-stage` for the exact bootstrap stage recorded by that full runtime. For a separate recorded foundation, select `--llvm-runtime-base` as described in [the runtime guide](llvm-runtime.md). The OS embeds its checked static libunwind archive into libc; the full runtime remains the application runtime.

When the selected OS consumes the common Ports OpenSSL source, pass the
prepared tree with `--openssl-source`. The optional input is identified by its
preparation receipt and recorded with the other common source inputs; it is forwarded
to the OS as `EMBER_OPENSSL_SOURCE`. An older OS build can omit it. For an OS
that consumes the common `security/libfido2` source, prepare it with the Ports
owner and select it with `--libfido2-source`; this forwards
`EMBER_LIBFIDO2_SOURCE`.

For OS builds using the common `devel/libcbor` source, prepare it using the
Ports `scripts/source-inputs/libcbor/source.py` owner and select it with
`--libcbor-source`; SDK forwards `EMBER_LIBCBOR_SOURCE`.

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

Select `aarch64-sysroot` for complete base/comp sets and an EMBER64 test kernel.
Select `amd64` for the reduced UEFI ISO, USB image and installation sets.
The product version comes from canonical `EMBER_VERSION` in the pinned
`share/mk/bsd.own.mk`. Release tags set this source value before a build.
Images, manifests, notes and publication titles use that version. The compatibility
base remains 11.0. Verification uses each kit's recorded version and does not
rewrite historical receipts. A build reads the version with
`git show COMMIT:share/mk/bsd.own.mk`; preparation reads it again from the
recorded OS checkout and checks that commit's Git tree. Profiles before schema 6
read it from their git-archive export instead. Of those, only historical AArch64
receipts with agreeing explicit version overrides can be prepared after that
archive is removed.

The OS source of a build is its commit and the commit's Git tree
(`git rev-parse COMMIT^{tree}`), recorded as `source_tree` in build profile
schemas 6, 7 and 8 and in the version 4 kit manifests. The builder writes no
`source.tar.gz`, and a kit has no `source-export.sha256`; the published outputs
keep their SHA256 hashes. Older kits keep their `source_archive_sha256` and
`source-export.sha256` and remain verifiable.

Canonical version behavior follows OS commit
`70368a97ed5be79e3594d9f596e0e57b8a781593`.

```sh
/absolute/host-pkg-llvm/bin/python3.15 -B scripts/build-os-release.py --help
/absolute/host-pkg-llvm/bin/python3.15 -B scripts/build-os-release.py \
  --profile aarch64-sysroot \
  --sdk-source /absolute/EmberBSD-SDK \
  --sdk /absolute/prebuild-sdk-aarch64 \
  --os-source /absolute/EmberBSD \
  --ports /absolute/EmberBSD-Ports \
  --libedit-source /absolute/prepared-libedit \
  --libarchive-source /absolute/prepared-libarchive \
  --libevent-source /absolute/prepared-libevent \
  --host-prefix /absolute/host-pkg-llvm \
  --host-binutils /absolute/binutils-aarch64 \
  --compiler /absolute/host-pkg-llvm \
  --llvm-source /absolute/prepared-llvm \
  --runtime-work /absolute/prebuild-sdk-aarch64/obj/llvm-runtime \
  --destination /absolute/prebuild-sdk-aarch64/stages/release-candidate \
  --jobs 4 --plan
```

Remove `--plan` to run the build. The workspace target must match the
profile. The destination must be new or empty and must be outside the active
sysroot. Use paths without whitespace. `--objects` and `--tools` select shared
workspace object paths; reuse requires an identical compatible context.
Do not point this producer at a core-sysroot object's incompatible context.

`--object-mode incremental` (the default) keeps the objects and tools of the
same profile between builds. The kept trees are reused only when their recorded
context matches the profile, the toolchain (compiler, LLVM runtime and stage
records, native tools), the SDK build configuration and every compiled input;
any difference refuses them, and the build needs new directories. Only the
flags that name the OS commit (`MKREPRO_TIMESTAMP`, `EMBER_BUILD_ID`,
`BUILDINFO`) are left out of that comparison, so a new OS commit updates the
kept trees with `build.sh -u`. The source ancestry check still requires the
kept trees' commits to be ancestors of the selected ones.
`--object-mode fresh` requires new or empty `--objects` and `--tools`
directories at the start of the run, so the OS phases of this run are the only
producers of the tools; use it for a published release. The selected mode is
recorded as `object_mode` in the build receipt and in `build-profile.json`.
Completed LLVM bootstrap stages are keyed by their recorded inputs (see
`native-os-routing.md`) and are reused in both modes.

A profile with `--gettext-source` builds the common cross-libtool from a
pkgsrc export. When the selected Ports checkout has
`scripts/pkgsrc-export-stage.py`, preflight takes that export's Git fingerprint
and selects `stages/pkgsrc-export/FINGERPRINT` under the SDK work root. The
stage is created or verified as its own step before the first OS build
command, so it never runs alongside the compile;
later builds with the same pkgsrc inputs reuse it, and the receipt records the
fingerprint and whether the stage was reused as `pkgsrc_export`. The libtool
producer only reads the stage. Older Ports checkouts keep the per-run export.
No compilation or output writes occur in plan mode.
The three optional driver/interpreter path options are checked aliases for the selected
native prefix roles. The context binds the complete normalized native selection.
Only native `HOST_*` flags receive its ABI arguments. Saved profile verification
checks command and selection consistency; it does not requalify local tools.

The Clang schema 7 (formerly 4) AArch64 sequence uses `tools kernel=EMBER64`,
then `distribution sets`; amd64 runs `tools release`, the OS UEFI
`install_image` target, then `iso-image`. Schema 8 (formerly 5) is selected only when the
Clang release enables the common base C++ runtime. It separates distribution,
imports that runtime and its derived base aliases into `dest`, runs the normal
sets check, then builds sets from the SDK-derived overlay. On amd64 the
equivalent sequence keeps release media generation after the imported runtime
and sets check. The OS's native unprivileged installer records the imported
files in METALOG. The fixtures verify orchestration only; OS-owner tests cover
the real installer and sets overlay. Neither schema establishes target
execution or runtime acceptance.

Both Clang profiles omit native bootstrap compiler commands, X11 and rump.
The `aarch64-sysroot` release build can add the X11 sets with `--x11-source`
(see `native-os-routing.md`); published sysroot manifests still require
`MKX11=no`.
New AArch64 sysroot and amd64 release manifests are version 4 for every
build-profile schema that names its source by Git tree. The explicit GCC
alternative uses build profile schema 6. For GCC and Clang schema 7 (or 4),
select one matching Ports runtime provider separately and check image
dependency closure before VM qualification; those release kits do not compose
that runtime package. Clang schema 8 (or 5) includes the selected common C++
runtime in the base set.

The producer records exact commands, source commits, compiler/runtime
hashes, the OS source tree and output hashes. It sanitizes inherited make controls.
The common SDK log and incomplete receipt remain on failure. `build-success`
appears only after all producer operations and receipt checks succeed.
A build result is not a VM, hardware or publication qualification.

Clang release profiles use schema 7 for the build-only runtime command
sequence, or schema 8 when the common runtime is installed into the base set
through the SDK-derived overlay. The explicit GCC alternative uses schema 6.
All three use version 4 outer kit manifests. Historical Clang schemas 4 and 5
(version 3 manifests) and GCC schemas 2 and 3 (version 2 manifests) differ only
in their git-archive source identity and remain verifiable. Portable verification binds the native selection,
input pins, tool paths, ABI flags, and environment. It does not read the original compiler files.
Historical schema 2 profiles retain their original command contract and acquire no native-toolset qualification.
For GCC, add `--compiler-family gcc`, select the GCC16 cross prefix and runtime
work tree, and supply its `--gcc-archive` and `--pkgsrc` inputs. That path still
uses `--llvm-source` for libc and kernel builtins; it does not consume an LLVM
application runtime stage or require the GCC and LLVM version numbers to match.

## Prepare a kit

```sh
python3 scripts/os-release.py --profile aarch64-sysroot \
  prepare /absolute/build /absolute/new-kit
python3 scripts/os-release.py --profile aarch64-sysroot verify /absolute/new-kit
python3 scripts/os-release.py --profile amd64 \
  prepare /absolute/build /absolute/new-amd64-kit --os-source /absolute/EmberBSD
```

Preparation requires a new output directory. A failed preparation leaves a
partial kit for inspection. Do not publish it.

For amd64, SDK calls the OS `ember/tests/installer-image.py` contract with the
actual image, source ID, version and GitHub host. `--os-source` selects that
checker independently from the build source. The checker must match its tracked
HEAD bytes. New manifests record its commit and SHA256 separately; they never
replace the producer's source identity. This permits a current Python checker
to inspect a preserved historical build. The tool defaults to the recorded OS
checkout for a build with a schema 2 or later profile. Historical exported trees need `--os-source`.

The amd64 kit takes sets from the actual ISO and verifies its embedded SHA512.
It compares the boot and installed kernel bytes and checks USB gzip integrity.
It includes the ISO, compressed USB image, required sets, release notes and
hash manifests. Optional omitted sets are named in the manifest.
The AArch64 kit checks required headers, CRT, libraries, loader architecture,
kernel build identity and the exact base/comp output receipts.

Verification rejects extra files, missing files, symlinks, changed bytes,
unsafe or duplicate names, mixed source IDs and assets of 2 GiB or more.
It uses the original file order for SHA256 receipt checks.
Each asset is hashed once per verification. The `SHA256` file and the
build-output receipts are compared with those digests. A kit that `prepare`
has just sealed is not hashed again; only its sizes are checked.

## Optional qualification receipt

A person starts publication deliberately and does the release checks.
`publish` needs no qualification receipt. To record target results in the kit,
prepare it with `--qualification`. Run actual target checks first. The checker
validates receipt bindings; it cannot establish that claimed execution occurred.
For amd64, follow the OS
[VM installation guide](https://github.com/oxtech-ember/EmberBSD/blob/main/doc/guide/amd64-vm-install.md).
The receipt binds `source_commit`, `image_sha256`, `usb_image_sha256` and
`installed_kernel_sha256`. Its `checks` object must record true for:

- `local_install`, `https_install`, `first_boot_without_media`, `reboot_persistence`.
- `tls_reject_unknown_ca`, `tls_reject_wrong_name`, `tls_reject_missing_anchors`.
- `http_404_rejected`, `github_redirect`.

AArch64 uses the [target receipt](aarch64-sysroot-release.md).
Prepare another new kit with `--qualification /absolute/receipt.json`.
Without that option, the release notes state that no receipt is bound.

## Immutable publication

```sh
python3 scripts/os-release.py --profile amd64 draft /absolute/new-amd64-kit
python3 scripts/os-release.py --profile amd64 publish /absolute/new-amd64-kit
python3 scripts/os-release.py --profile amd64 verify-remote /absolute/new-amd64-kit
```

Use the same operations with `--profile aarch64-sysroot` for development sets.
The GitHub CLI needs `release verify` and `release verify-asset`, release write
access and permission to read the immutable-release setting. The publisher
does not change that setting. Source must belong to the OS repository's `main`.

Tags remain `build-FULL_OS_COMMIT-amd64-no-x11` and
`build-FULL_OS_COMMIT-aarch64-sysroot`. A draft can resume only with identical
assets and metadata. Published releases forbid asset writes. Foreign tags,
extra files and different bytes stop publication. The publisher never clobbers.
The local kit is verified and hashed once, when the publisher starts. Later
steps compare remote digests and downloads with those digests, and detect a
changed local kit by its file identity (size, inode and times). They do not
hash the local kit again.
It downloads and checks every asset, publishes an immutable prerelease without
changing `latest`, and verifies release and per-asset attestations.
If attestation verification fails after publication, retain the published
state and retry `verify-remote`; do not delete or replace its tag or assets.

For amd64, perform a separate GitHub-native installation and download check
before adding an accepted kit to download navigation. A custom download domain
needs its own accepted HTTPS frontend; a DNS alias alone is insufficient.

## Compatibility and validation limits

Existing schema 1 amd64 and `emberbsd.aarch64.sysroot.v1` manifests remain
verifiable without rewriting their receipts. Their bootstrap GCC12 provenance
stays explicit. It is not evidence for the current compiler/runtime profiles.
Historical schema 2 receipts retain their source, compiler, runtime and command
checks. They do not establish native toolset qualification. New Clang plans use
schema 7 or 8 and bind the selected native prefix. Builds use the OS native command mapping. Existing immutable tags must not
be repurposed.

Run `python3 -B -m unittest discover -s tests -p 'test_*.py'`.
The release tests exercise archive composition, optional qualification binding, actual
fixture command dispatch, failure isolation and immutable remote-state refusals.
They do not build an OS, run a VM, inspect hardware or write GitHub releases.
The Clang complete-release sequence needs an actual full build and target
acceptance before its binaries can be called qualified.

The automatic SDK workflow runs host contracts only. The manual
[OS release workflow](../.github/workflows/os-release.yml) requires a prepared
self-hosted runner labelled `emberbsd-release`. It requires explicit clean
source pins, workspace, independent host/target compiler families, and the
matching LLVM source or GCC runtime/archive/pkgsrc inputs. Missing
inputs fail; the job does not substitute another compiler or report a skipped
producer as a successful binary build. This migration does not establish that
such a runner exists or qualify its complete producer pipeline.
