# Prebuild SDK workspace

A workspace groups build outputs on the development host. It is not an
application ABI, installed target system or released SDK archive.

## Layout

```text
prebuild-sdk/
  .prebuild-sdk.json    Managed layout version and scope
  obj/                 Shared target objects and native build tools
  sysroot/             Target headers, CRT, libraries and dependency metadata
  packages/            Binary package output, grouped by target/profile
  receipts/            Source, configuration, tool and result records
```

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
python3 scripts/create-prebuild-sdk.py /absolute/prebuild-sdk
python3 scripts/inspect-prebuild-sdk.py /absolute/prebuild-sdk
```

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
tree. It is not converted into a qualified SDK. If a manifest exists, its
schema and mandatory directories must be valid.

Symbolic-link creation requires host support and the relevant permissions.
An unsupported operation fails; it does not copy a potentially large tree.
Keep existing build commands and receipts on their canonical paths until their
producers support relocation. Tools that validate `realpath` can reject aliases.

## Reuse contract

Before reusing a component, compare its target/triplet, host tools, compiler,
recipe, configuration, dependency ABI and source state. Preserve the producer's
receipts and normal incremental rules. Do not accept a previous artifact after
a failed build merely because its output path still exists.

Build independent package roles from the same accepted objects when their
actual compilation inputs agree. Give each role a separate clean install stage
and its ordinary package checks. A runtime package and developer package can
share compilation without sharing installed-file ownership.

Use one declared GCC runtime provider in each target closure. Build-only SDK
headers and static libraries do not create a target compiler package identity.
