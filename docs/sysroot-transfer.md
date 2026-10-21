# Transfer a sysroot between developers

Use `export-sysroot.py` and `import-sysroot.py` to transfer a completed SDK-built
sysroot through a shared directory. Use CPython 3.15 and its standard library.
The commands need no compiler, network client or third-party Python package.
Mount the selected file share before calling them.

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
and links, and support no-replace publication. A case-sensitive local filesystem
is recommended. SMB can store the bundle even when it cannot host the restored
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
`emberbsd-sysroot-transfer`, version `1`, scope `sysroot-transfer`.

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
GCC16 binary hashes, and hashes of the runtime, build context and receipt.
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
