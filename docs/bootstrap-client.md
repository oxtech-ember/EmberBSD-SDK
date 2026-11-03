# First SDK installation

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
```

The first installer uses macOS curl, LibreSSL, plutil and tar. It pins the signing
root fingerprint, verifies both CMS envelopes and the selected archive hash
before running downloaded code. The first RSA-3072/SHA256 certificate profile
is explicit; changing that profile requires updating this installer. No Python,
Homebrew, compiler or SDK is required beforehand. Further operations use the
installed Python/OpenSSL and the [normal client](sdk-client.md), including its
bounded shared cache and persistent channel generation.

Choose a path without spaces or shell metacharacters, as required by Ports.
An archive can be installed at any such path outside its producer prefix.
Once initialized, that installation stays at the selected path: adding source
builds and other packages can introduce fixed paths not owned by the bootstrap.
To choose a different location, install a fresh archive there. Initialization
is resumable and registers normal package configuration actions only once.

The default state and cache live in `var/sdk` and `var/cache/packages` under the
SDK. `--config /absolute/client.json` selects an explicit configuration, including
the common Ports source-build inputs. A missing binary without those inputs
reports what needs to be prepared; this bootstrap does not yet acquire a compiler
or an application sysroot automatically. Coherent installed-component updates
and additional host platforms remain separate work.

## Producing the archive

`scripts/package-bootstrap.py --help` accepts complete Ports package archives,
a selected native prefix and explicit Mach-O editing/ad-hoc signing tools.
Use the `sdk-bootstrap` Ports roots with Python's `no-test-modules -x11` options
and their actual runtime closure. Include the native mksh and bootstrap-mk-files
providers required by generated package installation scripts. The Mozilla CA
package supplies `etc/openssl/certs/ca-certificates.crt`; private HTTPS trust and
release signing keys are never included. The public EmberBSD signing root is
part of the client.

Mach-O load commands are made relative before the release is signed. OpenSSL's
common Ports recipe resolves standard configuration, certificate and module
paths relative to its loaded library. First initialization adjusts textual
prefixes and package checksums, runs package hooks, then rebuilds pkgdb.
Historical `+BUILD_INFO` and `+BUILD_VERSION` remain original producer evidence;
only active `REQUIRES`/`PROVIDES` paths are adjusted in the installed database.

Publish the archive as a `bootstrap` artifact in the same signed release as its
component catalog; [S3 publication](s3-distribution.md) replaces the previous
current. The bootstrap archive currently occupies approximately 39 MB.

## Prefix-independent component packages

`scripts/package-portable.py --help` qualifies simple Darwin packages such as
fetch before signing. It adjusts Mach-O references, validates their dependency
closure against the selected prefix, rejects package hooks/commands and rejects
payloads still containing the producer prefix. Prefix-local `REQUIRES` are replaced
by the existing package dependencies only after checking library ownership;
system requirements stay unchanged. Packages with prefix-local `PROVIDES` still
need their owner's relocation support. It then uses native pkg_create to
preserve metadata and regenerate checksums. A packaging change requires a new
Ports package revision.

The signed `@comment emberbsd-prefix-independent-v1` packing-list marker permits
host installation at another prefix. Unqualified packages retain the exact-prefix
check; target packages retain their fixed target prefix and no-script install.
This deliberately narrow contract is not a generic relocator for arbitrary
applications, scripts or configuration files.

Validated on Darwin arm64: isolated bootstrap startup and HTTPS with all reads
from the producer prefix denied; Python/OpenSSL default trust paths; dynamic
legacy-provider and CT configuration loading; package registration/checksums;
signed component installation and repeated-install reuse. These results do not
establish a complete compiler SDK, another host OS, or an EmberBSD device install.
