# Prebuild SDK workspace

A workspace groups build outputs on the development host. It is not an
application ABI, installed target system or released SDK archive.

## Layout

```text
prebuild-sdk-aarch64/
  .prebuild-sdk.json    Version 2 layout, declared target and triplet
  obj/                 Shared target objects and native build tools
  sysroot/             Target headers, CRT, libraries and dependency metadata
  packages/            Binary package output, grouped by profile
  receipts/            Source, configuration, tool and result records
```

Use a separate canonical root for each target. Recommended names are
`prebuild-sdk-aarch64` and `prebuild-sdk-amd64`. Names are labels, not evidence.
Only the canonical root's manifest declares its target. `aarch64` uses the
`aarch64--netbsd` triplet. `amd64` uses `x86_64--netbsd`.
The layout scope is `workspace-layout`; it does not qualify the ABI or payload.

Use stable component paths inside `obj`, such as `os`, `modules` and
`host-tools`. Reuse the selected component paths across tasks. Keep incompatible
host, target, ABI or compiler configurations separate. A source change uses the
producer's dependency tracking; a changed ABI or tool configuration requires
its own validated build context.

The sysroot contains target files. Native generators and compilers run on the
build host and belong outside the target prefix. An AArch64 macOS executable
and an AArch64 EmberBSD executable have different operating-system ABIs.

## Create

Run from this repository with Python and its standard library:

```sh
python3 scripts/create-prebuild-sdk.py /absolute/prebuild-sdk-aarch64 --target aarch64
python3 scripts/inspect-prebuild-sdk.py /absolute/prebuild-sdk-aarch64
```

New creation requires `--target aarch64` or `--target amd64`.
The build host does not select the target implicitly.

The parent directory must exist. Use build paths without whitespace or control characters. The destination must be new. Creation leaves
an incomplete marker until the layout is complete. A failed creation does not
remove an existing directory or recursively roll back files.

Inspection checks the layout and reports logical and canonical paths. Its scope is `workspace-layout`; payload qualification is `not-assessed`. Empty directories do not establish a compiler,
EmberBSD provenance, dependency closure or target execution.

## Adopt existing outputs

```sh
python3 scripts/create-prebuild-sdk.py /absolute/prebuild-sdk --adopt /absolute/build
python3 scripts/inspect-prebuild-sdk.py /absolute/prebuild-sdk
```

The source must contain real `obj`, `sysroot` and `packages` directories.
Adoption creates an alias; it does not copy, relocate or rewrite their contents.
A repeated adoption of the same alias is read-only. A destination collision,
dangling link or symlinked mandatory child fails.

A legacy tree without a layout manifest is reported as an existing adoptable
tree. A version 1 manifest remains readable. Both report `target: null`,
`triplet: null` and `target_status: "unknown"`. Inspection never upgrades them.
If a manifest exists, its schema and mandatory directories must be valid.

To check a known target during adoption, add `--target aarch64` or
`--target amd64`. This compares the existing canonical version 2 manifest.
A mismatch or an unknown target fails before alias creation. Adoption never
writes a sidecar declaration or changes the source manifest.

The sysroot builder accepts only a matching version 2 target. An inspection
alias to an older tree does not enable a build. Migration of existing metadata,
objects or payload requires a separate explicit operation. These commands
provide no automatic migration. Keep active source pins and producer paths.

Symbolic-link creation requires host support and the relevant permissions.
An unsupported operation fails; it does not copy a potentially large tree.
Keep existing build commands and receipts on their canonical paths until their
producers support relocation. Tools that validate `realpath` can reject aliases.

## Reuse contract

Before reusing a component, compare its target/triplet, host tools, compiler,
recipe, configuration, dependency ABI and source state. Preserve the producer's
receipts and normal incremental rules. Do not accept a previous artifact after
a failed build merely because its output path still exists.

The builder records all loaded SDK module hashes in its object signature.
A changed SDK implementation can make an old context incompatible. This
layout update does not rewrite such a context or clear its objects.
The shared host API version remains `1`; the layout schema version is `2`.

Build independent package roles from the same accepted objects when their
actual compilation inputs agree. Give each role a separate clean install stage
and its ordinary package checks. A runtime package and developer package can
share compilation without sharing installed-file ownership.

Use one coherent runtime selection for the chosen compiler family in each
target closure. Build-only SDK headers and static libraries do not create a
target compiler package identity.
