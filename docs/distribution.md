# SDK and binary package distribution

A [signed release](signed-releases.md) binds the inventory, package
signatures and replay-protected channel pointer. Maintainers publish it to
[S3](s3-distribution.md). The [bootstrap installer](bootstrap-client.md)
installs the SDK client on a supported host. The [SDK client](sdk-client.md)
reads the signed catalog over HTTPS, installs compatible packages and target
sysroots, and delegates missing package roots to Ports when source inputs are
configured. The [binary catalog](binary-catalog.md) supplies package runtime
closures for binary installation without a source checkout or bmake.

The [sysroot transfer commands](sysroot-transfer.md) export and import a
completed builder output with pinned hashes and portable provenance. For binary distribution,
the signed inventory identifies the target sysroot. Qualify the compiler
and sysroot together for each declared host and target.

## Keep the workspace and release separate

The shared workspace includes incremental object files and native build trees.
They can contain absolute source paths and host configuration. They are useful
for development reuse but are not automatically portable release inputs.

A redistributable SDK should contain the selected target headers, CRT,
development libraries and metadata; its supported host tools and selected
Clang/LLVM compiler, or explicit GCC platform compiler; licenses; and
authenticated build and compatibility receipts.
Package the required target runtime through its declared owner. Keep target
compiler executables out of a runtime-only package.

Do not distribute local credentials, private application files, installed
package databases from unrelated systems or arbitrary build caches as SDK data.

## Bind an SDK to a package cohort

Use the [source version and release-tag procedure](release-tags.md) to prepare
`EMBER_VERSION` before building. After the selected checks, create matching
`emberbsd-<version>` tags in OS, Ports, and SDK. Tags bind source identities.
They do not themselves publish or qualify a binary SDK.

Before publication, record:

- SDK format/version, target architecture and exact GNU triplet.
- Build-host operating system and architecture, with actual validation results.
- Full OS, Ports and SDK source commits and recipe/configuration hashes.
- Compiler family and compiler, linker and binary-tool identities, hashes
  and configuration.
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

A development compiler prefix can link to shared LLVM or Binutils components.
Record those resolved providers in build receipts. Include their required host
payload in a release and validate its links after relocation. A reference to a
developer's shared object tree is not a portable compiler distribution.

Record unsupported hosts and target architectures explicitly. A portable
orchestration language does not make every upstream build backend native to
Windows or prove ABI compatibility between architectures.

Publish the accepted SDK artifact through the signed release inventory alongside
the matching binary package profile. Use its host/target, ABI/runtime, artifact
hashes and dependencies. Updates and installation must reuse that contract.
