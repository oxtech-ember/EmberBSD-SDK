# Future SDK and binary package distribution

This document defines the release boundary. A binary SDK export command and a
published SDK channel are not implemented by the workspace initializer.

## Keep the workspace and release separate

The shared workspace includes incremental object files and native build trees.
They can contain absolute source paths and host configuration. They are useful
for development reuse but are not automatically portable release inputs.

A redistributable SDK should contain the selected target headers, CRT,
development libraries and metadata; its supported host tools and GCC16
compiler; licenses; and authenticated build and compatibility receipts.
Package the required target runtime through its declared owner. Keep target
compiler executables out of a runtime-only package.

Do not distribute local credentials, private application files, installed
package databases from unrelated systems or arbitrary build caches as SDK data.

## Bind an SDK to a package cohort

Before publication, record:

- SDK format/version, target architecture and exact GNU triplet.
- Build-host operating system and architecture, with actual validation results.
- Full OS, Ports and SDK source commits and recipe/configuration hashes.
- GCC16 and Binutils identities, hashes and configuration.
- EmberBSD seed/sysroot provenance and the selected runtime provider.
- Package-profile identity and hashes of the matching binary packages.
- Output inventory, file/link hashes, licenses and relocation rules.
- Link, loader, runtime and representative consumer checks.

A package version alone is insufficient. The SDK and package cohort must agree
on headers, symbols, SONAMEs, target ABI and the supported build configuration.
Runtime and developer payloads need separate ownership even when they reuse
one compilation.

## Qualify relocation and host platforms

Test a copied SDK at a different absolute path on each declared host platform.
Check compiler subprocesses, linker scripts, CMake/pkg-config metadata, native
generators and runtime searches. Build and execute the resulting consumers on
the selected EmberBSD target. Compile success does not establish execution.

A development compiler prefix can link to shared Binutils components.
Record those resolved providers in build receipts. Include their required host
payload in a release and validate its links after relocation. A reference to a
developer's shared object tree is not a portable compiler distribution.

Record unsupported hosts and target architectures explicitly. A portable
orchestration language does not make every upstream build backend native to
Windows or prove ABI compatibility between architectures.

Publish the accepted SDK artifact and its checksums alongside the matching
binary package profile. Distribution signing, update policy and artifact
format require their own implemented and tested contracts.
