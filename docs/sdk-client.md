# Minimal SDK client

The client connects the signed `current` catalog to pkg_tools and the existing
Ports package selector. It prefers a compatible installed package, then a
published binary, and builds missing roots with Ports. Network, integrity and
signature failures are errors, not reasons to compile another package.

One configuration describes the native prefix, optional target sysroots, or a live target installation.
All tasks share these prefixes, package databases and a bounded SHA256 archive
cache. Selection remains versionless; host/target ABI comes from configuration.
The client downloads and verifies the entire binary selection before installing.
Target sysroots use pkg_add's alternate root and no-script mode; an explicit live target uses native package hooks.
There is no separate dependency resolver, toolchain or build tree per task.

## Use the client

For a machine without Python or SDK tools, use the [first-install bootstrap](bootstrap-client.md).
It provides an `ember-sdk` command and default configuration in one native prefix.

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
python3 scripts/packages.py --config /work/client.json installed
python3 scripts/packages.py --config /work/client.json plan --installed
python3 scripts/packages.py --config /work/client.json update
python3 scripts/packages.py --config /work/client.json cache-clean
```

Empty `plan`/`install` does nothing. Plans show roots, exact package names,
dependencies, `installed`/`cache`/`download` actions, bytes to download/unpack,
replaced package names, and roots requiring compilation. Installation reports
reuse, additions, replacements, cache hits, download bytes and source roots.
Successful installs remember the union of
explicit roots in `selection.json`; pkg_tools remains the version/file owner.
No packages are removed merely because a later task selects fewer roots.

## Installed versions and updates

`installed` reads each configured pkgdb with one `pkg_info -X -a` query.
It shows exact package versions, including `nbN`, and their versionless roots.
It needs no network, OpenSSL, compiler or Ports checkout, and runs no package hooks.
For an EmberBSD base, `os_release` shows the installed `/etc/release` header.
An absent header is reported as null. Configured ABI and the native kernel
release are separate fields; neither declares the EmberBSD product version.

`plan --installed` previews updates. `update` applies that selection through
the same download, signature, compatibility and pkg_tools installation path.
Both read the actual installed packages in each repository, including packages
installed outside this client. They never expand package sets or replay
`selection.json`. Removed packages stay removed. Adding another program to
current does not add it to an installation or invalidate an unchanged package.
Only installed roots and their published runtime dependencies are selected.
Two configured target roots retain separate package selections and databases.

Use `update --package category/package[@native]` to select one installed root.
The corresponding preview is `plan --installed --package ROOT`.
An uninstalled root is an error. Use `install` to add packages or a sysroot.
Installed packages absent from current, or lacking PKGPATH, are retained and
reported. Update does not compile them. A dependency cannot replace a retained
package by base name. Replacing packages with missing or different installed
PKGPATHs requires explicit migration, as do package renames.
Older catalog versions are refused before package mutation, using
pkg_tools version comparison. Compatible updates retain the safeguards below;
an incompatible ABI transition still stops before the first package changes.

The OS and each package have their own versions. The catalog is an authenticated
list, not a version of the installed system. There is no global generation or
persistent catalog replay state. Changing the catalog does not invalidate
unchanged installed packages. The current channel uses the single v2 contract;
old channel formats and command options are not supported. See the
[coordinated cutover](signed-releases.md#cutover-to-the-single-channel-contract).
Existing bootstrap archives retain their embedded client. Repackage the zipapp
from this SDK revision to use these commands with an existing configured prefix;
this does not rebuild installed packages. Source publication does not update
the client already installed on another host.

Use [`auto-update`](automatic-updates.md) for periodic installed-package updates.
It delegates each attempt to `update`, with no service installation or settings
changes. Starting the controller and delivering it to an existing bootstrap are
separate actions.

## Application compiler and sysroot

When the selected channel publishes the compiler set and a matching sysroot:

```sh
ember-sdk plan --set sdk-c --target aarch64
ember-sdk install --set sdk-c --target aarch64
/absolute/ember-sdk/bin/aarch64--netbsd-cc hello.c -o hello
```

`--target` adds one signed `sysroot` artifact to the selection. It uses the
matching target repository's configured `sysroot`, or `HOST_PREFIX/sysroots/TARGET`
when only a native repository is configured. An ambiguous target/ABI is refused;
no board or different compiler family is chosen implicitly. The native compiler
is shared by all targets. `--target` alone can acquire a sysroot before its compiler.

The archive contains the existing [sysroot transfer](sysroot-transfer.md) bundle.
All downloads are verified before installation. The base is restored at a new
path before target packages populate its pkgdb. An existing matching artifact
is reused without downloading or hashing the entire tree. A different or
unrecognized sysroot is preserved; replacing it requires the component update
path. The signed target runtime is part of the sysroot, with no external artifact
dependencies.

Once Clang is present, installation creates the existing SDK's operation-aware
`TRIPLET-cc`, `TRIPLET-c++`, `TRIPLET-cpp`, `TRIPLET-as` and `TRIPLET-ld` bindings
in the native `bin` directory. They select the target, sysroot and matching LLVM
runtime without consulting a source checkout on each compilation. Direct `ld`
calls still require the caller to select its libraries. An absent sysroot artifact
is reported explicitly: the Ports package fallback does not build an OS base;
use the [sysroot builder](sysroot.md) for that producer.

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

### Packages on a running EmberBSD device

The same signed target repository can install into a live matching system with
`"install_mode": "native"`, no `sysroot`, and the actual native `prefix`/`pkgdb`.
For example, a NetBSD 11.0 amd64 device using the published libc++ package graph:

```json
"netbsd-amd64": {
  "target": {"arch": "x86_64", "triplet": "x86_64--netbsd", "abi": "netbsd-11", "runtime": "libc++"},
  "install_mode": "native",
  "prefix": "/usr/pkg",
  "pkgdb": "/usr/pkg/pkgdb",
  "os_version": "11.0"
}
```

The target identity still must match the signed catalog exactly. Before accessing
its database, native mode checks the running NetBSD OS version, CPU, triplet and
major ABI. It does not change the package graph into host `@native` roots: use
`install --package www/blanc`, for example. Normal `pkg_add` executes installation
and deinstallation hooks; it receives no alternate root, foreign platform, or
no-script flag. Hooks can use the device's base OS utilities. Crypto and pkg_tools
remain explicitly selected executable paths from `host_prefix`; binary operation
needs no compiler. On devices with base pkg_tools under `/usr/sbin`, `/usr` can be
selected after the normal tool containment/architecture checks.

Live mode refuses an SDK sysroot and source-build fallback. Build missing packages
on the developer host through Ports and publish them before device installation.
The default target mode remains `sysroot`, with its non-root destination and
non-executing behavior; `install_mode` is not a platform or runtime override.

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
pkgdb and continues the missing packages. Qualified native package hooks are
deferred until all selected package payloads have been installed and relocated;
this lets one package's configuration use tools delivered by another selected
package. If a hook fails, its package remains registered and pending. The next
selection reads pkgdb without rerunning hooks, installs any missing packages,
then retries pending configuration. pkg_add does not provide an atomic
multi-package rollback, and the client does not claim one.
Re-running `install` selects the current versions of the requested components
and their dependencies. Exact installed versions are reused. Compatible
replacements use `pkg_add -U`, preserving the package database and reverse
dependencies. New transitive dependencies are marked automatic; new requested
roots are manual. Replacements preserve `automatic=yes`, `automatic=no`, or the
absence of that field, even for explicitly requested updates. The client records
an intent beside the package database before `pkg_add` and removes it only after
`pkg_admin` restores the field. A retry completes pending intents for installed
packages under the same database lock before further replacements. Before the first package changes, the client checks the installed
state under its database lock and checks every replacement against installed
consumers using pkg_tools dependency patterns. A library replacement must retain
its old providers while installed consumers exist; `REQUIRES` alone does not
describe all dynamically loaded libraries. Native provider paths are compared
relative to their package prefixes. A versioned regular ELF64 little-endian
implementation may replace a physical versioned filename when the actual old
and new payload bindings retain the same SONAME and ELF class, byte order,
machine, type, OS ABI, ABI version and flags. The retained SONAME must resolve
to the corresponding implementation. This proves neither complete C ABI nor
symbol or behavioral compatibility. Objects without SONAME, plugins and opaque,
unsafe or malformed entries retain the strict provider-path requirement.

The signed catalog pins exact package artifacts, while installed package
dependencies may allow a newer revision than the artifact selected by that
catalog. For a transitive dependency, the client reuses the newer installed
package when every selected direct consumer's authenticated `@pkgdep` pattern
accepts it and its installed providers satisfy the same provider retention or
proven ELF implementation substitution rule against the selected artifact.
Explicitly requested roots are not elided. If metadata
is missing, ambiguous, or incompatible, the normal replacement checks still
apply; the client does not infer ABI compatibility from version numbers alone.

This supports in-place component and package revision updates. It deliberately
refuses transitions which require removing library providers or replacing an
installed consumer before its dependency can change. Such coordinated ABI
transitions, sysroot replacement and updating tools during an active build remain
separate work. Do not update a prefix while a build uses it. A configured Ports
source build and binary installation share the existing build lock. Existing
Ports source builds retain their owner-defined replacement rules.
An exact runtime/devel dependency can require such a coordinated transition
even when both new packages appear in the selection. Checking only the final
dependency graph would leave the old consumer incompatible between individual
`pkg_add -U` calls and after a partial failure. Increasing either revision does
not resolve that ordering constraint. Preserve the accepted prefix and prepare
the changed stack alongside it through the owning producer until a coordinated
update contract is available.

## Client artifact and validation boundary

```sh
python3 scripts/package-client.py --output /work/ember-sdk.pyz
python3 /work/ember-sdk.pyz --config /work/client.json install --set host-fetch
```

The zipapp contains the SDK Python modules, license and public signing root.
It needs no SDK/Ports checkout for binary operations and contains no compiler,
sysroot, tests or private keys. It requires the selected Python/native runtime;
its archive size is **not** the size of a self-contained initial SDK.
A [self-contained initial runtime](bootstrap-client.md) supplies these tools
on Darwin arm64. pkg_add prefix relocation alone does not rewrite binary load
paths or producer paths in scripts. Only host packages explicitly qualified by
the publisher's signed prefix-independent contract may use another prefix;
unqualified packages still require the archive's declared destination.
The sysroot delivery and compiler-binding contracts do not establish availability
of a compiler/sysroot pair on current. Clean-host C/aarch64 execution remains
separate distribution acceptance work until a published pair passes that check.

The current self-contained archive is authenticated by the release referenced
by [`current.p7m`](https://emberbsd-pkg.oxteam.me/current.p7m). The Python client
itself is portable; the published bootstrap and native execution checks cover
Darwin arm64/macOS 27.

Checks cover shared dependencies, repeat installation, missing binaries versus
broken downloads, resumed transfers, insufficient disk, wrong platforms,
escaping archive links and target scripts. A native installation test uses real
pkg_tools and signed fixture archives. These contracts do not themselves prove
a complete portable compiler or execution on EmberBSD. The source-free zipapp
also reads the live public current and reuses installed fetch/OpenSSL on the
developer host without downloading or rebuilding them.
`tests/test_package_state.py` covers actual-pkgdb update selection, independent
repositories, removed packages, catalog additions, offline versions and CLI
dispatch. These fixture checks do not establish live update acceptance.
