# Minimal SDK client

The client connects the signed `current` catalog to pkg_tools and the existing
Ports package selector. It prefers a compatible installed package, then a
published binary, and builds missing roots with Ports. Network, integrity and
signature failures are errors, not reasons to compile another package.

One configuration describes the native prefix and optional target sysroots.
All tasks share these prefixes, package databases and a bounded SHA256 archive
cache. Selection remains versionless; host/target ABI comes from configuration.
The client downloads and verifies the entire binary selection before installing.
Target installation uses pkg_add's alternate root and no-script mode.
There is no separate dependency resolver, toolchain or build tree per task.

## Use the client

Use Python 3.15 and a native prefix containing OpenSSL and pkg_tools. No compiler
or bmake is loaded for binary-only operations. A client configuration is shared
by tasks using the same target/ABI; other targets can share its native prefix
and cache. Paths below are examples, not directories created automatically:

```json
{
  "schema": "emberbsd.sdk-client.v1",
  "host_prefix": "/work/host-pkg-llvm",
  "cache_dir": "/work/cache/packages",
  "state_dir": "/work/sdk-state",
  "url": "https://emberbsd-pkg.oxteam.me",
  "cache_limit": 536870912,
  "repositories": {
    "darwin-arm64": {
      "host": {"os": "Darwin", "arch": "arm64", "abi": "macos-27.0", "runtime": "libSystem"},
      "prefix": "/work/host-pkg-llvm",
      "pkgdb": "/work/host-pkg-llvm/pkgdb"
    }
  }
}
```

Platform identity must match the published repository exactly. Host OS/CPU must
also match the executing machine. Optional `https_ca` selects a public HTTPS CA
file; `trust_root` overrides the bundled EmberBSD signing root for private/test
repositories. They have different purposes. `url_prefix` selects a server
namespace. No S3 credentials are needed.

```sh
python3 scripts/packages.py --config /work/client.json list
python3 scripts/packages.py --config /work/client.json show --set host-fetch
python3 scripts/packages.py --config /work/client.json plan --set host-fetch
python3 scripts/packages.py --config /work/client.json install --set host-fetch
python3 scripts/packages.py --config /work/client.json install --package net/fetch@native
python3 scripts/packages.py --config /work/client.json cache-clean
```

Empty `plan`/`install` does nothing. Plans show roots, exact package names,
dependencies, `installed`/`cache`/`download` actions, bytes to download/unpack,
and roots requiring compilation. Successful installs remember the union of
explicit roots in `selection.json`; pkg_tools remains the version/file owner.
No packages are removed merely because a later task selects fewer roots.

## Missing binaries and source builds

Add `source` to the configuration when compilation is available:

```json
"source": {
  "ports_root": "/work/EmberBSD-Ports",
  "pkgsrc": "/work/prepared-pkgsrc",
  "makeconf": "/work/config/aarch64.mk.conf",
  "state": "/work/build/aarch64/package-state"
}
```

Use Ports with the binary selection interface (first revision
[`c7cedf8b`](https://github.com/oxtech-ember/EmberBSD-Ports/commit/c7cedf8b)) and
a current export, the existing native compiler/tool selection and
the same shared cross-bulk state as ordinary source builds. The client checks
effective Ports destinations and platform against binary destinations before
running the producer. It does not silently invent another build configuration.
If missing binaries need this configuration and it is absent, installation
stops before changing packages and names the roots that require Ports.

Ports package sets supplement the published sets when `source` is configured.
For missing roots, the existing `selected-order` controller supplies the closure
and exact names in `STATE/packages`. The client downloads matching binaries
within that closure, then invokes the existing selected build. Ports rechecks
exact installed target names before accepting this authenticated binary
selection; ordinary explicit source builds keep their existing behavior.
Version mismatches remain source work. Runtime dependency selection in the
binary catalog and build dependency resolution in Ports remain separate.

Compilation requires its usual prepared compiler/sysroot inputs. It may download
upstream sources, but it does not upload locally built packages or need a release
signing key. Use the existing source `packages.py build` command for an explicit
source build even when a published binary exists.

## Target packages

A target repository configuration has `target` instead of `host`, plus the
absolute `sysroot` and pkg_tools `os_version`. For example:

```json
"aarch64": {
  "target": {"arch": "aarch64", "triplet": "aarch64--netbsd", "abi": "netbsd-11", "runtime": "libc++"},
  "sysroot": "/work/aarch64/sysroot",
  "prefix": "/usr/pkg",
  "pkgdb": "/usr/pkg/pkgdb",
  "os_version": "11.0"
}
```

These identity values illustrate configuration, not an available target release.
pkg_add receives `-P SYSROOT -I` and the explicit foreign platform. Its package
database lives inside that root; scripts, packing-list commands and replacement
deinstallation scripts are not run on the host. The client does not unpack
packages itself. Paths and links are inspected before installation, including
existing paths; escaping and absolute payload links are refused. Installed files
are ordinary package files, never mutable hardlinks into the archive cache.
This installs package payloads into an existing target environment; base headers,
CRT/libc and producer sysroot bundles retain their existing SDK/OS ownership.

## Cache and interruption

Archives are stored once as `SHA256.tgz`, with partial downloads as `SHA256.part`.
The complete selection is downloaded and verified before the first pkg_add.
Sizes and SHA256 come from the signed release; pkg_admin also authenticates each
package. A displaced current archive causes one metadata reread/replan, only
when the current release changed. Corruption and an unchanged missing archive
are errors, never source fallbacks.

The default cache budget is 512 MiB. The active selection may temporarily exceed
it; older archives are removed first and the budget is restored after success.
`cache-clean` removes cached archives/partials only, preserving installed files,
pkgdb, incremental build objects and remembered roots. Writer locks fail quickly
instead of waiting indefinitely; after a crash, remove a named lock only after
confirming its owner stopped. Re-running a partially completed install reads
pkgdb and continues the missing packages. pkg_add does not provide an atomic
multi-package rollback, and the client does not claim one.
This first installer adds missing components and reuses exact installed versions.
Replacing a different installed binary version is refused before mutation;
coherent updates including installed reverse dependencies belong to the update
path. Existing Ports source builds retain their owner-defined replacement rules.

## Client artifact and validation boundary

```sh
python3 scripts/package-client.py --output /work/ember-sdk.pyz
python3 /work/ember-sdk.pyz --config /work/client.json install --set host-fetch
```

The zipapp contains the SDK Python modules, license and public signing root.
It needs no SDK/Ports checkout for binary operations and contains no compiler,
sysroot, tests or private keys. It requires the selected Python/native runtime;
its archive size is **not** the size of a self-contained initial SDK.
The complete portable bootstrap runtime, relocation of native package payloads,
base-sysroot import from current and clean-host C/aarch64 execution remain
separate distribution acceptance work. pkg_add prefix relocation alone does
not rewrite Mach-O/ELF load paths or producer paths in scripts. This client
therefore requires the archive's declared prefix to match its destination.

The first published [client artifact](https://emberbsd-pkg.oxteam.me/current/host/darwin-arm64/ember-sdk-0.1.pyz)
is 479,369 bytes. Its hash is authenticated by the release referenced by
[`current.p7m`](https://emberbsd-pkg.oxteam.me/current.p7m); it is a client program,
not a self-contained Python/pkg_tools bootstrap. The Python program is portable;
the presently published repository and native verification cover Darwin arm64.

Checks cover shared dependencies, repeat installation, missing binaries versus
broken downloads, resumed transfers, insufficient disk, wrong platforms,
escaping archive links and target scripts. A native installation test uses real
pkg_tools and signed fixture archives. These contracts do not themselves prove
a complete portable compiler or execution on EmberBSD. The source-free zipapp
also reads the live public current and reuses installed fetch/OpenSSL on the
developer host without downloading or rebuilding them.
