# Transfer a sysroot between developers

Use `export-sysroot.py` and `import-sysroot.py` to transfer a completed SDK-built
sysroot through a shared directory. Use CPython 3.15 and its standard library.
The commands need no compiler, network client or third-party Python package.
Mount the selected file share before calling them.

Sysroot import requires native atomic directory publication without replacement.
That operation supports Darwin and Linux. It refuses NetBSD, even when the
NetBSD process-control contracts pass. The complete transfer/import workflow
is not qualified on NetBSD; see [host platforms](host-platforms.md).

This is a sysroot transfer format. It does not distribute the native compiler,
reuse another host's objects, qualify relocation, or publish a binary SDK release.
The normal [distribution acceptance](distribution.md) still applies.

## Export a frozen build

Stop all writers to the selected sysroot. Use the output of the normal
[sysroot builder](sysroot.md), including its successful build receipt.
Export refuses an incomplete marker, a missing receipt, a target mismatch,
or files that differ from the receipt. It rechecks required headers, target
ELF files, contained relative links and all output hashes.

The output parent must exist. The output directory must be new.
Use a unique build ID. Keep each target in its own directory.

```sh
python3 scripts/export-sysroot.py \
  --sysroot /absolute/prebuild-sdk-aarch64/sysroot \
  --output /absolute/share/EmberBSD/sysroots/aarch64/build-001 \
  --target aarch64
```

The JSON result contains `manifest_sha256`. Record this exact pin with the
consumer project. It binds the manifest, archive and installed files.
Do not obtain a trusted pin from an untrusted share at import time.
Hashes detect corruption; they do not authenticate the publisher.

The exporter creates three regular files:

| File | Contract |
| --- | --- |
| `payload.tar.gz` | Regular-file bytes in fixed ordinal tar entries |
| `manifest.json` | Target, provenance, inventory, modes, links and hashes |
| `READY` | Manifest SHA256 and a newline; written after read-back verification |

An interrupted export can leave an incomplete directory without a valid
`READY` record. A valid record binds files already checked by the exporter.
Readers still verify every hash. Inspect and remove only an incomplete,
caller-owned output before retrying,
or select a new ID. Never merge exports or overwrite an accepted version.
When copying a bundle with another tool, copy `READY` last.

## Import into a new local directory

Select the target explicitly. Supply the pin recorded at export.
The destination must not exist, including as an empty directory or symlink.
Its parent must exist and belong to the caller.

```sh
python3 scripts/import-sysroot.py \
  --bundle /absolute/share/EmberBSD/sysroots/aarch64/build-001 \
  --destination /absolute/local-sysroots/aarch64/build-001 \
  --target aarch64 \
  --manifest-sha256 RECORDED_64_CHARACTER_SHA256
```

Import checks metadata and copies the compressed payload into a private sibling
stage. It verifies its size and SHA256 before restoring any payload files.
It creates directories, regular files and relative symbolic links from the
validated inventory. It does not call `tar.extract` or `extractall`.
Archive names are fixed ordinals, never filesystem destinations.

The restored inventory must match the manifest, including permissions and ELF
architecture. The importer adds `.prebuild-sdk-import.json` with portable
provenance. It then uses the SDK's native no-replace directory publication.
A concurrent destination is never replaced. Caught failures and interrupts
remove only the private stage. SIGKILL or power loss can leave a
`.sysroot-import-*` sibling for manual inspection. No automatic stale cleanup
is performed. This protocol does not claim crash durability.

The destination filesystem must preserve the selected payload's names, modes
and links, and support no-replace publication. A local filesystem is
recommended. Builds must work on case-insensitive filesystems. SMB can store the bundle even when it cannot host the restored
tree. A successful file transfer does not establish network-filesystem support
for live build objects or distributed locks.

An existing workspace's empty `sysroot` directory is not an import destination.
Use a new versioned directory, including a new `stages/NAME` inside a workspace,
and select it explicitly in the consuming build. Do not change a path while
another build uses it. Re-export of imported trees is not implemented in v1;
retain the original bundle and pin.

## Format and limits

The library capability is `SYSROOT_TRANSFER_API = 1` in
`emberbsd_sdk.sysroot_transfer`. The manifest format is
`emberbsd-sysroot-transfer`, version `1` for historical GCC or `2` for Clang,
scope `sysroot-transfer`.

The archive is gzip-compressed POSIX ustar. It contains only regular files
named `files/00000000`, `files/00000001`, and so on. Their order is the sorted
inventory order after directories and links are excluded. Archive ownership
and modes are not used. Payload names, modes and link targets come from the
manifest. Use the importer; an ordinary tar extraction is not a sysroot.

The inventory records bytes, ordinary permission bits and relative links.
Hardlinks are transferred as independent files. Ownership, timestamps, ACLs,
extended attributes, sparse allocation and special files are not transferred.
Set-ID and sticky modes are refused. Dangling, cyclic and escaping links are
refused. Payload ancestors must be listed real directories, never links.
SDK build logs and private receipt paths are not copied.

Limits are 200,000 entries, 32 MiB of manifest JSON, and less than 8 GiB per
regular file. The default total regular-file limit is 64 GiB. Import can set
`--max-bytes` explicitly. JSON, archive records, decompression and trailer reads
are bounded. Import requires local free space for the compressed archive and
the restored tree at the same time.

Provenance records full OS, Ports and SDK source commits, build-host identity,
selected compiler binary hashes, and hashes of the runtime, build context and
receipt. Version 2 explicitly records the Clang family and version; version 1
retains the historical GCC16 contract. Import supports both formats.
Version 2 also preserves the existing portable runtime manifest and its
inventory in `runtime`, bound to `provenance.runtime_sha256`. Its complete
prefix subtree must equal the payload subtree, including directories and
links; unrecorded additions within the runtime are refused. The imported
record retains measured runtime paths for [application tool selection](application-toolchain.md).
It omits the original host paths, environment and command logs. Files themselves
can still contain build paths or private content. The producer must control
which files it installs. Export is not a secret scanner or a relocation fix.

## Shared store for a team

Use this directory convention on a mounted share:

```text
EmberBSD/
  sysroots/aarch64/BUILD-ID/
  sysroots/amd64/BUILD-ID/
  packages/aarch64/BUILD-ID/
  packages/amd64/BUILD-ID/
  prebuild/HOST-ID/TARGET/CONTEXT-ID/
  incoming/DEVELOPER/JOB-ID/
```

Give each publication directory one writer. Readers use a fixed manifest pin.
Keep published bundles unchanged. Each developer keeps sources and active
build objects locally, with host tools separate from target files. Reuse the
common compiler recipe for the selected host OS/CPU and target.

Packages require their producer's own checks and compatibility records.
Prebuild archives are stopped host-specific workspace backups. They are not
portable SDK bundles. This command pair does not create or restore package
sets or prebuild archives. Separate accounts and share permissions remain the
storage administrator's policy. The tools do not change router settings.

## Checks and acceptance boundary

Run `python3 -m unittest discover -s tests -p 'test_sysroot_transfer.py'`.
The suite uses build-command fixtures with real SDK receipts and ELF headers.
It checks both targets, file/link/mode restoration, CLI use, corruption,
unsafe metadata, failed publication and preservation of existing destinations.
It does not build or execute an EmberBSD consumer.

A macOS arm64 run also transfers a fixture through an SMB 3.1.1 share,
disconnects and reconnects the share, and imports it at a new local path.
This qualifies that byte-store round trip, not live SMB builds, a second Mac,
Linux/WSL2, target execution, package closure or a relocatable SDK release.

## Source-built AArch64 check

On 2026-10-21, macOS 27.0.1 arm64 built an AArch64 sysroot on case-sensitive
APFS with CPython 3.15.0, GCC 16.2.0 and Ports Binutils 2.47.
The normal SDK builder accepted headers, OS CRT, base libraries, the loader,
and the verified GCC runtime with POSIX threads, libgomp and libatomic.

| Input | Source commit |
| --- | --- |
| EmberBSD | `32afa976de8d28799739978f80cc3fca08928145` |
| EmberBSD-Ports | `95b66026c82d0405aa6d691c80b443a3f2a81d1c` |
| EmberBSD-SDK | `5c6e8f5189044886051c2c0a069465e060730fd6` |
| OS bootstrap seed | `7d4cb79185717c8ed37d3630e058c91fa1f566a3` |

Export and import through SMB passed after disconnecting and reconnecting the
share. The restored inventory matched all 4,501 entries and 139,088,986 bytes
of regular files. The compressed payload is 38,329,498 bytes.
The accepted manifest SHA256 is:

```text
20c63b43f9db788ac3f3d80366844c0e436e0e92fb6faf39df244fcbf346f17b
```

The original and restored trees passed the Ports
[cross-compile tests](https://github.com/oxtech-ember/EmberBSD-Ports/blob/95b66026c82d0405aa6d691c80b443a3f2a81d1c/profiles/development-toolchain/cross/README.md).
Coverage includes C11 atomics/TLS, C++20 constexpr evaluation and a C++ shared
library boundary. The existing OpenMP and 256-byte C++ atomic consumers also
passed plain and LTO links against `libgomp.so.1` and `libatomic.so.3`.

Additional restored-path checks selected C++ headers with `-nostdinc++` and
explicit sysroot include directories. Dependency files and linker maps selected
that tree's headers, CRT and runtime libraries. They showed no fallback to the
compiler seed or original sysroot. Target ELF files had no host dynamic paths.
These checks used the same host compiler at its original path.

Freeze the producer tree before export. Keep file-manager metadata out of the
payload and close file-manager views during transfer checks. Finder can update
`.DS_Store` files while a tree is being inventoried. The integrity checks remain
strict; do not rewrite a build receipt to accept changed files.

This run did not execute the target programs, test a second Mac or Intel Mac,
or qualify compiler relocation, package closure or a complete binary SDK.
This historical bundle contains target files for the GCC16 runtime. Each
developer supplies a matching host-executable GCC16 cross compiler for its
application builds; it does not qualify the newer LLVM runtime.
