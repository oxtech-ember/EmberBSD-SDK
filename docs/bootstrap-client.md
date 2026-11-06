# First SDK installation

For Linux, use the [initial native bootstrap](linux-bootstrap.md) when no
compatible host bootstrap is published. This shell installer selects Darwin arm64;
target NetBSD packages cannot supply native Linux tools.

The Darwin arm64 bootstrap runs on macOS 27 or newer. It contains the client,
Python 3.15 without test modules, OpenSSL, pkg_tools, public Mozilla CA data and
their runtime packages. It has no compiler, application sysroot, GUI or source
checkout. All files retain ordinary Ports package ownership; the tar archive is
a delivery container, not another package database or toolchain.

Download the installer from this repository and select a new absolute directory:

```sh
curl -fL https://raw.githubusercontent.com/oxtech-ember/EmberBSD-SDK/main/scripts/install-sdk.sh -o install-sdk.sh
sh install-sdk.sh "$HOME/Downloads/ember-sdk"
"$HOME/Downloads/ember-sdk/bin/ember-sdk" list
"$HOME/Downloads/ember-sdk/bin/ember-sdk" plan --set host-fetch
"$HOME/Downloads/ember-sdk/bin/ember-sdk" install --set host-fetch
"$HOME/Downloads/ember-sdk/bin/ember-sdk" install --set sdk-source-tools
```

The first installer uses macOS curl, LibreSSL, plutil and tar. It pins the signing
root fingerprint, verifies both CMS envelopes and the selected archive hash
before running downloaded code. HTTPS acquisition uses finite connection and
transfer timeouts and up to three retries for transient network failures; a
failed transfer leaves no installed prefix. The first RSA-3072/SHA256 certificate
profile is explicit; changing that profile requires updating this installer. No Python,
Homebrew, compiler or SDK is required beforehand. Further operations use the
installed Python/OpenSSL and the [normal client](sdk-client.md), including its
bounded shared cache and authenticated current metadata.

`sdk-source-tools` adds digest, awk, sed, patch and the Ports mktools/cwrappers
helpers, reusing fetch and OpenSSL when present. Its roots live in Ports, without
duplicated version pins. This prepares source handling; a compiler, make and
target sysroot remain separate selections.

Choose a path without spaces or shell metacharacters, as required by Ports.
An archive can be installed at any such path outside its producer prefix.
Once initialized, that installation stays at the selected path: adding source
builds and other packages can introduce fixed paths not owned by the bootstrap.
To choose a different location, install a fresh archive there. Initialization
is resumable and registers normal package configuration actions only once.

The default state and cache live in `var/sdk` and `var/cache/packages` under the
SDK. `--config /absolute/client.json` selects an explicit configuration, including
the common Ports source-build inputs. A missing binary without those inputs
reports what needs to be prepared. With a published compiler set and target
sysroot, `ember-sdk install --set sdk-c --target aarch64` acquires both and
creates the target compiler commands. Repeating `install` also applies compatible
component updates; see the [client update boundary](sdk-client.md#cache-and-interruption).
Coordinated ABI transitions and additional host platforms remain separate work.

## Producing the archive

`scripts/package-bootstrap.py --help` accepts complete Ports package archives,
a selected native prefix and explicit Mach-O editing/ad-hoc signing tools.
Use the `sdk-bootstrap` Ports roots with Python's `no-test-modules -x11` options
and their actual runtime closure. Include the native mksh and bootstrap-mk-files
providers required by generated package installation scripts. The Mozilla CA
package supplies `etc/openssl/certs/ca-certificates.crt`; private HTTPS trust and
release signing keys are never included. The public EmberBSD signing root is
part of the client.

Mach-O dependency load commands are made relative before the release is signed.
Dylib IDs use `@rpath/<basename>` so newly linked consumers resolve the provider
through their declared library-directory RPATH, including programs in `bin`.
Existing runtime loads retain image-relative paths. Ports buildlink supplies
the provider RPATH; a nested library requires its own library-directory contract. OpenSSL's
common Ports recipe resolves standard configuration, certificate and module
paths relative to its loaded library. First initialization adjusts textual
prefixes and package checksums, runs package hooks, then rebuilds pkgdb.
Historical `+BUILD_INFO` and `+BUILD_VERSION` remain original producer evidence;
only active `LOCALBASE`, `REQUIRES` and `PROVIDES` paths are adjusted in the
installed database. `LOCALBASE` must name the consumer prefix because Ports
buildlink reads it to find installed headers and libraries.

The archive preserves every exact payload name from the package packing lists,
including names that differ only by case on the producer filesystem. Each file
has independent archive data; aliases do not become self-hardlinks during
extraction. Text relocation includes every spelling, so an archive prepared on
a case-insensitive filesystem can initialize on a case-sensitive destination.
Package file timestamps survive staging. Initialization refreshes owned Python
bytecode only when stale or when its source was relocated, preserving hash and
optimization modes before updating package checksums. Ordinary Python imports
do not then rewrite the installed cache files; `-B` is not an installation rule.

Publish the archive as a `bootstrap` artifact in the same signed release as its
component catalog; [S3 publication](s3-distribution.md) replaces the previous
current. The bootstrap archive currently occupies approximately 39 MB.

## Prefix-independent component packages

`scripts/package-portable.py --help` qualifies simple Darwin packages such as
fetch before signing. It adjusts Mach-O references, validates their dependency
closure against the selected prefix, rejects unqualified hooks and packing-list commands, and rejects
payloads still containing the producer prefix by default. Prefix-local `REQUIRES` are replaced
by the existing package dependencies only after checking library ownership;
system requirements stay unchanged. It then uses native pkg_create to
preserve metadata (including protection and display notices) and regenerate
checksums. Changing already-published package bytes requires a new Ports package
revision; the first publication can qualify an existing recipe's package.

The signed `@comment emberbsd-prefix-independent-v1` packing-list marker permits
host installation at another prefix. Unqualified packages retain the exact-prefix
check; target packages retain their fixed target prefix. Sysroot installs suppress
scripts, while native target installs run them.
This deliberately narrow contract is not a generic relocator for arbitrary
applications, scripts or configuration files.

For compiler packages, `--relocate-text` records the owned text files that need
prefix replacement and permits prefix-local library `PROVIDES`. The signed
`emberbsd-prefix-relocation-v1` marker declares this contract. After successful
`pkg_add`, the client adjusts those text files and active provider metadata,
then refreshes checksums for the changed files. Both portable package contracts
update active `LOCALBASE` metadata, including recovery after an interrupted
metadata update, without changing payload checksums. An interrupted relocation can
resume; a failed package installation must be repaired first.
Overlapping old and new prefixes are refused when a replacement
would not be safe to repeat.

`--package-revision N` increases a package's pkgsrc `nb` revision while
qualifying it, without changing its upstream version or compiled provenance.
First bump the matching Ports recipe; use this only for a metadata or packaging
change that does not require recompilation. Existing `+BUILD_VERSION` remains
the record of the source used to build the package.
Choose that revision for the intended package change, not for each packaging
attempt. After a successful qualification/signing, retain and reuse the exact
archive for publication retries. Rebuilding or resigning an archive is not
guaranteed to reproduce its bytes; immutable-name conflicts must be investigated,
not repaired by automatically increasing `N`. If an earlier packaging attempt
failed, inspect its output and retry from the same original input and revision
into a new output path. Feeding the revised result back into this command is
another transformation, not an idempotent publication retry.
For an installation-script fix, `--install-script` and `--deinstall-script`
accept replacements generated by the current Ports recipe. They require the
revision bump and `--defer-hooks`; generate the scripts for the archive's prefix.
An already qualified package can be repacked with one new relocation declaration.
This preserves compiled payloads when only package metadata changed. Check the
new hooks at a fresh prefix before publication.

`--allow-binary-prefix` additionally permits binary build-path strings that
cannot be rewritten as text. It requires `--relocate-text` and an actual
compiler check at a different prefix before publication. Mach-O load commands
and their transitive package-provided libraries are still checked and made
relative; this option does not prove that embedded paths are inactive.

Packages with configuration scripts additionally need `--defer-hooks`. This
explicit qualification requires their PRE-INSTALL and POST-INSTALL actions to
work after extraction and to be repeatable. The client uses `pkg_add -I`,
relocates payload text and both hook scripts, then runs the same package
configuration helper as the bootstrap. A hook failure leaves pending state;
the next client operation retries configuration before accepting the package
as installed. This contract applies only to signed native packages; target
scripts are never executed on the development host. The producer must test
these hooks and the package at a different prefix before publication.

Validated on Darwin arm64: isolated bootstrap startup and HTTPS with all reads
from the producer prefix denied; Python/OpenSSL default trust paths; dynamic
legacy-provider and CT configuration loading; package registration/checksums;
signed component installation and repeated-install reuse. These results do not
establish a complete compiler SDK, another host OS, or an EmberBSD device install.

`tests/source-tools-runtime.py` checks signed source-tool installation, SHA256,
text processing, patch application, buildlink symlinks and wrapper argument
forwarding in a disposable bootstrap. It also checks package ownership,
preservation markers and nbpatch's original redistribution notices.
