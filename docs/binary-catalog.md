# Binary package catalogs

`scripts/binary-catalog.py` composes signed package repositories and lists or
plans their components. The producer uses the existing Ports graph; the reader
needs only the signed release, small catalogs and OpenSSL. It never runs bmake,
compiles a missing binary or installs a package.

The [release format](signed-releases.md) remains the trust and artifact format.
One release can include native and target repositories. One native tool archive
serves all targets on that host. Each repository has exactly one host or target
identity, including the declared ABI/runtime.

## Produce a catalog

Build selected packages with their common Ports recipes. Reuse existing
runtime/development partitions and shared stages, such as `lang/llvm` and
`lang/llvm-devel`; composition does not compile or split files again.

Export a graph using Ports `scripts/export-build-graph.py`. Its
`--dependency-phase runtime` mode visits only installed dependencies without
querying build tools or tests. Existing `all` or `build` graphs also work:
composition projects their runtime edges. Version patterns remain the
responsibility of pkgsrc and `pkg_admin pmatch`.

Pass actual source commits and an explicit package-file map:

```json
{
  "schema": "emberbsd.package-build.v1",
  "release_id": "preview-1",
  "created_at": 1793700000,
  "sources": {
    "os": "<full OS commit>",
    "ports": "<full Ports commit>",
    "sdk": "<full SDK commit>"
  },
  "repositories": [{
    "id": "darwin-arm64",
    "host": {"os": "Darwin", "arch": "arm64", "abi": "macos-27.0", "runtime": "libSystem"},
    "target": null,
    "graph": "/work/native-runtime-graph.json",
    "sets": ["host-fetch"],
    "packages": {
      "host:net/fetch": "/work/packages/fetch-1.9nb4.tgz",
      "host:security/openssl": "/work/packages/openssl-4.1.0nb1.tgz"
    }
  }]
}
```

These versions illustrate producer output; package sets do not pin versions.
Graph `provenance.revision` must match `sources.ports`. Use absolute local paths
and graph node IDs, including any context suffix. Sets come directly from the
Ports checkout's `profiles/package-sets/*.txt`. Repositories may also specify an optional
`"roots": ["devel/bmake@native"]` array to publish individual versionless
PKGPATHs. Roots union with selected sets and use the same runtime closure;
`"sets": []` is valid when roots are supplied. This does not add or change
named sets in the published catalog. A repository selects roots of
its own role: `@native` for host tools, unsuffixed roots for targets. Unknown
sets and missing or ambiguous roots fail. Unselected archive-map entries are
not read or copied.

For targets, set `host` to null and provide `target` with `arch`, `triplet`,
`abi` and `runtime`. Graph architecture/triplet and the archives' native pkgsrc
`OPSYS=NetBSD` and `MACHINE_ARCH` must agree. Architecture aliases
`amd64`/`x86_64` and `arm64`/`aarch64` are recognized; ABI/runtime stay exact.

```sh
python3 scripts/binary-catalog.py --host-prefix /work/host-pkg-llvm compose \
  package-build.json --ports-root /work/EmberBSD-Ports \
  --signer-directory /private/release-signer \
  --trust-root trust/emberbsd-release-root.pem --output /work/releases/preview-1
```

The producer selects OpenSSL, pkg_info and pkg_admin from that prefix. It:

1. Projects runtime closures; build, tool and test edges stay out.
2. Checks path/version/platform, licenses, sizes and archive DEPENDS against
   the resolved runtime providers using pkg_tools.
3. Signs unsigned packages with the existing X.509 mechanism. Existing signed
   inputs are authenticated and inspected, then copied without re-signing.
4. Runs standard `pkg_info -X` on the **signed** archives and writes
   `pkg_summary.gz`; FILE_SIZE describes the distributed bytes.
5. Writes the component catalog, seals the release with package/index hashes
   and atomically publishes the complete output directory.

Existing output is never overwritten. Failure removes only the operation's
staging directory, preserving source packages, objects and caches. Only selected
archives get signed copies; no whole SDK or unpacked tree is copied. Shared
directory publication currently supports Darwin and Linux.

For an unchanged package, pass its existing signed archive in `packages`.
This preserves exact bytes and avoids uploading it again to `current`; signing
the same source again can change the signature container. A rebuilt or changed
package needs the appropriate version/revision before replacing its predecessor.
Both unsigned TARs and signed BSD-ar containers undergo the same payload checks.

A missing or empty LICENSE is represented in the signed release as SPDX
[`NOASSERTION`](https://spdx.github.io/spdx-spec/v2.3/package-information/#713-concluded-license-field): no license terms are asserted. This identifier does not grant
redistribution rights. Declared nonempty LICENSE values are preserved. Duplicate
or malformed metadata, nonempty NO_BIN_ON_FTP/RESTRICTED and payload size
exceeding SIZE_PKG are rejected. For target packages, each `/usr/pkg/` REQUIRES path must be present in
PROVIDES of the package itself or its reachable runtime dependency closure.
Requirements outside `/usr/pkg/` belong to the target base and are not verified
by this package-only check. Declared redistribution checks do not replace
license review. Target payloads are inspected without extraction: direct little-endian
ELF headers must match the CPU; ordinary Mach-O/PE host files are rejected.
Native package composition requires exactly one qualified prefix-independent or
text relocation declaration in the packing list. This declaration does not
prove that binaries work after relocation; producers still run relocated runtime
checks. Target packages are unaffected.
Nested static archives, universal Mach-O, foreign ELF OS ABI, linkage and
runtime behavior remain package-producer checks, not catalog guarantees.

## Read and select

```sh
python3 scripts/binary-catalog.py --host-prefix /work/host-pkg-llvm list \
  --release /mirror/preview-1/release.p7m --directory /mirror/preview-1 \
  --trust-root trust/emberbsd-release-root.pem

python3 scripts/binary-catalog.py --host-prefix /work/host-pkg-llvm plan \
  --release /mirror/preview-1/release.p7m --directory /mirror/preview-1 \
  --trust-root trust/emberbsd-release-root.pem \
  --repository darwin-arm64 --set host-fetch
```

`list` shows repositories and sets; `show --set NAME` shows versionless roots.
`plan` requires explicit `--repository` selections and accepts repeated `--set`
and `--package category/package[@native]`. Empty selection does no work.
Two sets union their dependencies. A second target does not repeat host tools.

Plans show exact artifacts, roles, dependencies, licenses, download bytes and
logical unpacked bytes. Totals precede cache/installed-component subtraction;
filesystem overhead, staging and rollback space are additional. Cache-aware
accounting is provided by the [SDK client](sdk-client.md).

Reading verifies CMS and selected catalog hashes. Package archives and
pkg_summary need not be downloaded merely to list, show or plan. A direct
release is a pinned/offline operation; online clients must first use existing
channel verification and replay state. Before pkgin imports pkg_summary, its
consumer must verify that artifact's hash; installation must verify selected
package hashes and signatures. This command does not modify installed state.

## Metadata and checks

Each `<repository>/catalog.json` is a `package-index` release artifact with
schema `emberbsd.packages.v1`. It contains `repository`, the `summary` artifact
ID, `packages` entries (`root`, `pkgname`, `artifact`), and `sets` mapping names
to versionless roots. Hashes, sizes, dependencies and platforms stay in the
release inventory. One root/base name has one chosen package. Runtime edges
must remain within its platform repository. Build graphs, source paths and
producer receipts are not shipped to readers.

```sh
EMBERBSD_TEST_HOST_PREFIX=/work/host-pkg-llvm \
  python3 -B -m unittest discover -s tests -p test_binary_catalog.py
```

Tests use real pkg_create/pkg_admin/pkg_info and OpenSSL with test keys:
signed summary sizes, missing/incompatible dependencies, corrupt catalog,
wrong role/architecture, foreign host payload, shared libraries, development
roots, second target, kernel tools without sysroot, CLI selection and no overwrite.
Fixture packages establish the distribution contract, not a built compiler or
an application running on EmberBSD. [S3 delivery](s3-distribution.md) publishes
these catalogs and fetches authenticated metadata. The [SDK client](sdk-client.md)
adds installation, shared caching and source builds for missing binaries.
A complete portable C/C++ SDK remains separate acceptance work.
