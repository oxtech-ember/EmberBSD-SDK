# Compose and publish OS releases

SDK owns release orchestration, kit composition and immutable publication.
The OS owns `build.sh`, distribution rules, media layout and installer checks.
Ports owns the common GCC16 compiler and its verified runtime.
Use [release tags](release-tags.md) for coordinated OS/Ports/SDK version preparation;
that command does not publish binary OS kits.

The release tools are Python standard-library commands. They use the shared
SDK compiler, runtime, source, object-context and process-ownership checks.
They do not define another toolchain recipe. The migration originates in
`EmberBSD@4a4c98bb99999836103d3fe63fd6f4c98013f0c5:ember/release/`
and its `ember/tests/release-*.rb` contracts. The source license is BSD-2-Clause.

See [libedit preparation](libedit.md) for the mandatory `--libedit-source` input.

## Build inputs and commands

Read [sysroot inputs](sysroot.md#required-inputs) and
[host requirements](host-platforms.md). Use CPython 3.15+, Git, the OS host
build prerequisites and an ISO-capable libarchive `tar`. Each source checkout
must be clean and fully materialized. Select exact OS, Ports and SDK commits.
The common compiler must be GCC 16.2.0 with POSIX threads for the selected ABI.
The normal Ports verifier must accept its runtime stage and source inputs.

Select `aarch64-sysroot` for complete base/comp sets and an EMBER64 test kernel.
Select `amd64` for the reduced UEFI ISO, USB image and installation sets.
The product version comes from canonical `EMBER_VERSION` in the pinned
`share/mk/bsd.own.mk`. Release tags set this source value before a build.
Images, manifests, notes and publication titles use that version. The compatibility
base remains 11.0. Verification uses each kit's recorded version and does not
rewrite historical receipts. Preparation reads the pinned source archive. Only
historical AArch64 receipts with agreeing explicit version overrides can be
prepared after that archive is removed.

Canonical version behavior follows OS commit
`70368a97ed5be79e3594d9f596e0e57b8a781593`.

```sh
python3 scripts/build-os-release.py --help
python3 scripts/build-os-release.py \
  --profile aarch64-sysroot \
  --python /absolute/python3 \
  --sdk-source /absolute/EmberBSD-SDK \
  --sdk /absolute/prebuild-sdk-aarch64 \
  --os-source /absolute/EmberBSD \
  --ports /absolute/EmberBSD-Ports \
  --libedit-source /absolute/prepared-libedit \
  --compiler /absolute/gcc16-cross \
  --runtime-work /absolute/gcc16-runtime-work \
  --gcc-archive /absolute/gcc-16.2.0.tar.xz \
  --pkgsrc /absolute/prepared-pkgsrc \
  --destination /absolute/prebuild-sdk-aarch64/stages/release-candidate \
  --jobs 4 --plan
```

Inspect the plan before removing `--plan`. The workspace target must match the
profile. The destination must be new or empty and must be outside the active
sysroot. Use paths without whitespace. `--objects` and `--tools` select shared
workspace object paths; reuse requires an identical compatible context.
Do not point this producer at a core-sysroot object's incompatible context.
No compilation or output writes occur in plan mode.

AArch64 runs `tools kernel=EMBER64`, then `distribution sets`.
The amd64 profile runs `tools release`, the OS UEFI `install_image` target,
then `iso-image`. It retains GENERIC kernel and modules, with the INSTALL boot
kernel profile selected through command variables. Both profiles omit native
bootstrap compiler commands, X11 and rump. The external runtime is a build-only
input. OS distribution rules maintain METALOG and strict set-list checks;
OS sets do not own the external SDK or GCC16 package payload. Select one matching
Ports runtime provider separately and check image dependency closure before
VM qualification. The adapter alone does not compose that runtime package.

The producer records the exact commands, source commits, compiler/runtime
hashes, source archive and output hashes. It sanitizes inherited make controls.
The common SDK log and incomplete receipt remain on failure. `build-success`
appears only after all producer operations and receipt checks succeed.
A build result is not a VM, hardware or publication qualification.

## Prepare a kit

```sh
python3 scripts/os-release.py --profile aarch64-sysroot \
  prepare /absolute/build /absolute/new-kit
python3 scripts/os-release.py --profile aarch64-sysroot verify /absolute/new-kit
python3 scripts/os-release.py --profile amd64 \
  prepare /absolute/build /absolute/new-amd64-kit --os-source /absolute/EmberBSD
```

Preparation requires a new output directory. A failed preparation leaves a
partial unqualified kit for inspection. Do not publish it.

For amd64, SDK calls the OS `ember/tests/installer-image.py` contract with the
actual image, source ID, version and GitHub host. `--os-source` selects that
checker independently from the build source. The checker must match its tracked
HEAD bytes. New manifests record its commit and SHA256 separately; they never
replace the producer's source identity. This permits a current Python checker
to inspect a preserved historical build. The tool defaults to the recorded OS
checkout for a schema 2 build. Historical exported trees need `--os-source`.

The amd64 kit takes sets from the actual ISO and verifies its embedded SHA512.
It compares the boot and installed kernel bytes and checks USB gzip integrity.
It includes the ISO, compressed USB image, required sets, release notes and
hash manifests. Optional omitted sets are named in the manifest.
The AArch64 kit checks required headers, CRT, libraries, loader architecture,
kernel build identity and the exact base/comp output receipts.

Verification rejects extra files, missing files, symlinks, changed bytes,
unsafe or duplicate names, mixed source IDs and assets of 2 GiB or more.
It uses the original file order for SHA256 receipt checks.

## Qualification and immutable publication

Run actual target checks before supplying a qualification receipt. The checker
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

```sh
python3 scripts/os-release.py --profile amd64 \
  prepare /absolute/build /absolute/qualified-kit \
  --os-source /absolute/EmberBSD --qualification /absolute/vm-receipt.json
python3 scripts/os-release.py --profile amd64 draft /absolute/qualified-kit
python3 scripts/os-release.py --profile amd64 publish /absolute/qualified-kit
python3 scripts/os-release.py --profile amd64 verify-remote /absolute/qualified-kit
```

Use the same operations with `--profile aarch64-sysroot` for development sets.
The GitHub CLI needs `release verify` and `release verify-asset`, release write
access and permission to read the immutable-release setting. The publisher
does not change that setting. Source must belong to the OS repository's `main`.

Tags remain `build-FULL_OS_COMMIT-amd64-no-x11` and
`build-FULL_OS_COMMIT-aarch64-sysroot`. A draft can resume only with identical
assets and metadata. Published releases forbid asset writes. Foreign tags,
extra files and different bytes stop publication. The publisher never clobbers.
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
stays explicit. It is not evidence for today's common GCC16 build profile.
New builds use schema 2 receipts with the common SDK source/compiler/runtime
identity and executed commands. Existing immutable tags must not be repurposed.

Run `python3 -B -m unittest discover -s tests -p 'test_*.py'`.
The release tests exercise archive composition, qualification binding, actual
fixture command dispatch, failure isolation and immutable remote-state refusals.
They do not build an OS, run a VM, inspect hardware or write GitHub releases.
The new GCC16 complete-release sequence needs an actual full build and target
acceptance before its binaries can be called qualified.

The automatic SDK workflow runs host contracts only. The manual
[OS release workflow](../.github/workflows/os-release.yml) requires a prepared
self-hosted runner labelled `emberbsd-release`. It requires explicit clean
source pins, workspace, GCC16, runtime, archive and pkgsrc inputs. Missing
inputs fail; the job does not substitute a GCC12 build or report a skipped
producer as a successful binary build. This migration does not establish that
such a runner exists or qualify its complete producer pipeline.
