# Signed releases and channels

`scripts/release.py` produces and verifies the current distribution format for
host packages, target packages, sysroots, OS sets, images and the minimal SDK
bootstrap. It uses Python's standard library and the explicitly selected native
Ports OpenSSL and pkg_tools. It neither builds packages nor installs them.
Existing OS/sysroot producer records remain payload provenance; they are not
alternative authenticated distribution formats.

## Trust and files

Provision the root through a trusted bootstrap, OS image or administrator.
The initial public root is [bundled in this repository](../trust/README.md).
Never import a root offered by a package repository. A self-signed X.509 root
directly issues a separate release certificate. New RSA keys have 3072 bits;
certificates and CMS metadata use SHA256. The root has critical
`CA:TRUE,pathlen:0` and `keyCertSign,cRLSign`. The leaf has critical `CA:FALSE`
and `digitalSignature`, with exactly `codeSigning,emailProtection` EKUs as
required by pkg_tools.

Metadata is an attached binary CMS SignedData object in DER (`.p7m`), with one
signer. Verification authenticates the exact embedded UTF-8 JSON bytes before
parsing. JSON is never re-encoded to verify a signature. OpenSSL checks the
explicit trust root, certificate validity, purpose and security level 2,
without default CA files, directories or stores. The SDK checks the leaf
profile. There is no unsigned fallback.

Packages retain the standard pkg_tools archive and X.509 signature from
`pkg_admin x509-sign-package`. `pkg_admin check-signature` verifies the entire
package with explicit `CERTIFICATE_ANCHOR_PKGS`. A valid manifest does not
replace package verification. Installer integration must also configure
`VERIFIED_INSTALLATION=always`; pkgin alone does not verify channel metadata.

Example layout, relative to the repository HTTPS base:

```text
current.p7m
current/host/darwin-arm64/example-1.0.tgz
current/target/aarch64/example-1.0.tgz
current/metadata/<manifest-sha256>/release.p7m
current/metadata/<manifest-sha256>/darwin-arm64/pkg_summary.gz
```

There is one current package set, without historical release retention. Upload
new payloads and metadata, then atomically replace the **single** current object.
After verification, remove obsolete packages and metadata. CMS combines content
and signature, avoiding a new pointer paired with an old detached signature.
The local sealer refuses existing output paths; remote publication and writer
coordination are separate transport responsibilities.
The [S3 publisher](s3-distribution.md) implements those conditional writes and
verifies public delivery before switching the channel.

## Release: `emberbsd.release.v1`

Every field below is required. Unknown fields, other schemas, duplicate keys,
invalid UTF-8 and non-finite numbers are rejected. Timestamps are positive
integer Unix seconds; numeric fields do not accept JSON booleans.

```json
{
  "schema": "emberbsd.release.v1",
  "release_id": "2026.11.03-1",
  "created_at": 1793664000,
  "sources": {"os": "<40 lowercase hex>", "ports": "<40 lowercase hex>", "sdk": "<40 lowercase hex>"},
  "artifacts": [{
    "id": "example-aarch64",
    "path": "target/aarch64/All/example-1.0.tgz",
    "kind": "target-package",
    "host": null,
    "target": {"arch": "aarch64", "triplet": "aarch64--netbsd", "abi": "netbsd11", "runtime": "libcxx"},
    "size": 1234,
    "unpacked_size": 5678,
    "sha256": "<64 lowercase hex>",
    "depends": [],
    "licenses": ["bsd-2-clause"]
  }]
}
```

| Kind | Platform | Purpose |
| --- | --- | --- |
| `host-package` | host only | Native developer tool, signed pkg_tools archive |
| `target-package` | target only | Target runtime/development package, signed archive |
| `sysroot` | target only | Headers, libraries and development metadata |
| `os-set` | target only | System set owned by the OS producer |
| `image` | target only | Complete installation image |
| `bootstrap` | host only | Minimal developer installer/client payload |
| `package-index` | Exactly one of host/target | Ordinary pkg_summary, optionally compressed |

The other platform field is null. `host` is `{os,arch,abi,runtime}`, for example
`Darwin`, `arm64`, `macos-15.0`, `libSystem-libcxx`. `target` is
`{arch,triplet,abi,runtime}`. These strings name the producer's compatibility
contract; the installer must match its consumer. Verification authenticates
declarations, not binary ABI compatibility.

IDs use lowercase letters, digits, `.`, `_`, `-`, starting with a letter/digit.
Paths use nonempty ASCII segments of letters, digits, `.`, `_`, `+`, `-`.
Absolute paths, `.`/`..` segments, URL escapes, query/fragment syntax and
backslashes are forbidden. Paths are case-insensitively unique and cannot be
ancestors of each other. Local artifact components must be real directories
and regular files, without symlinks below the explicitly selected root.

`size` is a nonnegative byte count. Required `unpacked_size` is the producer's
nonnegative count of expanded regular-file payload bytes (expanded bytes for
an index, raw bytes for an uncompressed artifact). The sealer authenticates
this declaration; the extraction consumer must enforce it. It is not a claim
about filesystem allocation: inode/block overhead and staging add to it.
SHA256 covers the distributed bytes,
including the package signature. `depends` lists unique artifact IDs in the
same release; cycles are rejected. This is a projection of the Ports graph,
not another dependency solver. `licenses` is a nonempty list of identifiers.
Generate package indexes after signing packages; include them as artifacts
so their exact bytes are bound to this release.

## Channel: `emberbsd.channel.v2`

```json
{
  "schema": "emberbsd.channel.v2",
  "channel": "current",
  "issued_at": 1793664000,
  "expires_at": 1794268800,
  "release": {
    "id": "2026.11.03-1",
    "path": "current/metadata/<64 lowercase hex>/release.p7m",
    "size": 4321,
    "sha256": "<64 lowercase hex>"
  }
}
```

The channel has no version counter. `issued_at` and `expires_at` are Unix
seconds, with validity `issued_at <= now < expires_at`; they bound validity,
not package identity. The expected channel name is a client input to the
generic verifier; S3 delivery fixes it to `current`. The reference hashes the
complete release CMS envelope, whose ID must match the authenticated release.
Envelopes are limited to 16 MiB, embedded content to 15 MiB. Payloads are
streamed for hashing.

`verify-channel` verifies channel and release signatures and their exact binding.
It does not keep a local channel history or replay floor. Installers verify
hashes and signatures for selected components and refuse downgrades relative
to their installed package database. Package and OS versions are independent;
a catalog change does not require reinstalling unchanged components.

Signatures prove authenticity, not that a catalog is the newest one. An attacker
holding an older, still-valid signed catalog and archives can hide a newer
publication until expiry. Deleting old files on the origin does not erase such
copies. The client does not claim remembered catalog freshness; it still rejects
expired metadata, corrupted content and unsigned packages. No CLI option disables
signatures or time checks. Fix the clock or obtain fresh metadata when those
checks fail.

`verify-release` verifies a locally supplied signed release and all payloads,
including current certificate validity. Offline verification is not installation.

### Cutover to the single channel contract

Update publisher and client tooling together before switching the public channel
to v2. Repackage and deliver the client zipapp/bootstrap as part of that separate
release operation; publishing source code does not update installed clients.
Old v1 channels are rejected, not translated. Old `--expected-generation` and
channel `--state` options have no aliases. Existing installed package databases
and valid package caches are retained: they are not channel state.

The new client does not read or create former channel-state files. Those files
are obsolete. Migrate through the single v2 current contract with the intended
complete inventory and the existing publication lock/conditional-write rules.
Preserve installed packages and signature verification throughout the cutover.

## Keys and commands

Use an existing compatible Ports prefix; signing needs no compiler or sysroot.
Start with `python3 scripts/release.py --help`. Below, `release` abbreviates that
invocation using the selected Ports Python, with
`--host-prefix /absolute/native-prefix` before the subcommand.

```sh
release create-root /private/signing/root
release issue-signer --root-directory /private/signing/root /private/signing/release-2026
release sign-package --trust-root /private/signing/root/certificate.pem \
  --signer-directory /private/signing/release-2026 --output signed.tgz unsigned.tgz
release seal-release --trust-root /private/signing/root/certificate.pem \
  --signer-directory /private/signing/release-2026 --directory /repository/releases/example \
  --output /repository/releases/example/release.p7m inventory.json
release seal-channel --trust-root /private/signing/root/certificate.pem \
  --signer-directory /private/signing/release-2026 --directory /repository \
  --output /staging/current.p7m channel.json
release verify-channel --trust-root /installed/trust/root.pem --directory /repository \
  --channel current /staging/current.p7m
release verify-release --trust-root /installed/trust/root.pem \
  --directory /repository/releases/example /repository/releases/example/release.p7m
```

Inventories use the schemas above. Sealers fill/recompute `size` and `sha256`,
so those two fields may be omitted in producer inputs. Other fields are checked
before accessing payloads. Sealing a channel verifies the referenced release
metadata; release sealing already checked its payloads. Sign native packages
explicitly before generating the index.

### Developer disk usage

The format separates download bytes from expanded payload bytes and identifies
identical compressed objects by SHA256. A binary client can therefore share one
content-addressed cache across releases and target profiles, download only
missing objects and keep one compatible installation of shared host tools.
Separate runtime, development, documentation and debug producer artifacts
where useful. Package the selected bootstrap components and metadata; keep source checkouts and build trees outside the bootstrap payload. Kernel-only work needs no application sysroot.

The installer must account for cache misses, new expanded components and peak
staging/rollback space before writing. Reflinks or hardlinks are optional local
optimizations only where mutation semantics allow them. Never share a writable
sysroot file with the immutable cache through a hardlink. Cleanup must preserve
components referenced by other projects, active tools and any explicitly retained
recovery artifacts. Cache limits and explicit garbage collection belong to the binary
client stage, not this signing command.

Key directories must be new (0700); private keys use 0600. Generated keys are
unencrypted PEM for the native pkg_tools signer. After issuing the leaf, keep
the root on protected offline storage with an encrypted administrator-controlled
backup. Keep only the leaf key on the signing machine. Neither private key
belongs in Git, S3, packages, images, logs or bootstrap. S3 credentials are separate.
Roots last 3650 days; leaves at most 365 days. Rotate the leaf into a new directory
under the same root; installed clients retain their root. CRL/OCSP and trusted
timestamping are not implemented. Root replacement needs trusted provisioning.

## Checks and integration boundary

`tests/test_release_catalog.py` covers the strict contract and concurrent atomic
state without external tools. `tests/test_release_signing.py` uses real OpenSSL
and pkg_tools, disposable roots, negative signature/time cases, leaf rotation,
complete release/channel verification and offline recovery. Run with
`EMBERBSD_TEST_HOST_PREFIX=/absolute/native-prefix`; without this input crypto
tests are skipped, not executed acceptance.

On 2026-11-03 the 32 catalog contracts and 10 signing/path contracts passed on
macOS arm64 with CPython 3.15.2, Ports OpenSSL 4.1.0 and pkg_install-20260227nb21.
The [Ports correction](https://github.com/oxtech-ember/EmberBSD-Ports/commit/1cafb55f)
also passes its native full-block and missing-input regression. This establishes
host protocol interoperability, not an on-device updater or a published channel.

S3 transport, the minimal bootstrap installer, pkgin integration and on-device
OS update/rollback are subsequent consumers of this contract. They must reuse
the provisioned root and format; this command does not implement them.
The protocol uses standard [OpenSSL CMS](https://docs.openssl.org/3.5/man1/openssl-cms/)
and [pkg_tools X.509](https://man.netbsd.org/pkg_admin.1) signatures.
