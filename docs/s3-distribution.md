# One current S3 repository

`scripts/s3-release.py` publishes the current signed package set and retrieves
its authenticated metadata through HTTPS. It uses Python's standard library
and the selected Ports OpenSSL/pkg_tools. Installation is a separate layer.

Bodyless GET and HEAD requests make at most three connection attempts for
transient timeouts or disconnected peers before exposing a response. TLS trust,
HTTP status, signed-byte failures and interrupted response bodies remain errors.
S3 writes and deletes are never automatically retried; publication recovery uses
the existing lock, conditional operations and reconciliation.

## Layout and retention

There is one `current`, with one package version per platform:

```text
current.p7m
current/host/darwin-arm64/openssl-4.1.0nb1.tgz
current/target/aarch64/openssl-4.1.0nb1.tgz
current/metadata/<manifest-sha256>/release.p7m
current/metadata/<manifest-sha256>/<repository>/catalog.json
current/metadata/<manifest-sha256>/<repository>/pkg_summary.gz
```

Packages retain pkgsrc names: `name-version[nbN].tgz`. The platform belongs in
the directory, and SHA256 belongs in the signed inventory. A changed package
must have a new version or package revision; the publisher refuses different
bytes under the same current filename. An unchanged signed package is reused
byte for byte. The [catalog composer](binary-catalog.md) accepts existing signed
archives for this purpose after verifying their signatures and payloads.

The small metadata directory lets the publisher prepare the new catalog while
the old one is readable. It is temporary coexistence, not release history.
After switching `current.p7m`, the publisher deletes obsolete packages, previous
metadata and abandoned upload files under its `current/` prefix. Only the
current set remains. Objects outside that prefix are never swept.

There are no dated release directories or separate S3 content-addressed package
pool. Local client caches can use SHA256 to avoid repeated downloads across
projects; that does not require keeping historical server versions.

The CMS pointer retains the shared channel schema with the fixed name `current`,
a generation, expiry and exact manifest reference. The manifest keeps producer
paths for local files; `artifact_key()` maps payloads to the platform directories
above. Repository IDs in metadata remain those selected by the producer.

## Publish

Provide credentials only to the publishing process, outside Git:

```text
EMBERBSD_S3_ENDPOINT
EMBERBSD_S3_REGION
EMBERBSD_S3_BUCKET
EMBERBSD_S3_ACCESS_KEY
EMBERBSD_S3_SECRET_KEY
```

```sh
python3 scripts/s3-release.py --host-prefix /work/host-pkg-llvm publish \
  /work/prepared-release/release.p7m --url https://emberbsd-pkg.oxteam.me \
  --trust-root trust/emberbsd-release-root.pem \
  --signer-directory /private/release-signer
```

The publisher verifies local signatures and hashes, uploads missing packages
and new metadata, verifies new objects through public HTTPS, conditionally
switches `current.p7m`, then removes superseded files. Repeating the same
publication retains the generation and finishes any interrupted cleanup.
Keep the exact signed release envelope and archives after a failed attempt;
resealing equivalent metadata creates different bytes and is not recovery of
that publication. A transport, verification or cleanup failure alone requires
no package revision or release ID change.

For an add-only catalog update, `release.py seal-release --reuse-current
CURRENT_RELEASE.p7m` may fill a missing **non-index** artifact from an exact
record in that signed snapshot. The new draft still supplies the complete
merged inventory; every new or changed artifact must be local, and package
archives are verified with pkg_tools before signing. Indexes remain local so
the publisher can prepare the new catalog beside the release.

Publish that sparse release with both `--reuse-current` and the generation from
the snapshot:

```sh
python3 scripts/s3-release.py --host-prefix /work/host-pkg-llvm publish \
  /work/prepared-release/release.p7m --url https://emberbsd-pkg.oxteam.me \
  --trust-root trust/emberbsd-release-root.pem --signer-directory /private/release-signer \
  --expected-generation 12 --reuse-current
```

Under the publisher lock, the tool authenticates current and its release
inventory, requires every prior artifact ID/platform to remain in the candidate,
and accepts a missing non-index payload only when its complete signed record
matches current. New and changed local objects receive normal size/SHA checks
and package signature checks; their public bytes are read back before switching.
For a reused object, HTTPS HEAD confirms that it is present with the signed
size. Its body is not downloaded or rehashed: the authenticated current record
is the reuse evidence, keeping large unchanged archives off the publisher's
disk and avoiding their transfer. Consumers verify downloaded bytes against the
signed SHA before installation. A missing reused object stops publication;
merge the latest snapshot and retry with its generation.

When extending an existing catalog, preserve its current packages and sets in
the candidate and pass `--expected-generation N` from the verified snapshot.
Under the publisher lock, a changed generation stops a different candidate
before any artifact upload or cleanup; merge the newer catalog and retry.
There is one recovery exception: an older expected generation may finish the
exact authenticated release already selected by an unexpired current pointer.
Its release ID, path, size and envelope SHA256 must all match. It verifies the
objects and finishes cleanup without signing or advancing the pointer. This
covers a lost switch acknowledgement or a failure after switching. A future
expected generation, `--refresh`, or an expired pointer still requires the
actual current generation; expiry during verification also stops a stale retry.
Use `0` for an initially empty repository and retain it for recovery of that
same publication. The option does not merge catalogs.

A create-only `.publisher-lock` serializes publication **and cleanup** across
publishers. Normal exit removes it. After a process crash, inspect the publisher
and remove that exact object with the storage administrator's tools only when
no publisher is running, then retry. No lease service or background worker is
needed. Current still uses ETag compare-and-swap; HTTP 409/412 refuses a competing
write. ETags are concurrency tokens, not integrity checksums.

A failed upload does not switch current. A failure after switching, including
cleanup failure, can leave a usable new current and extra old files: retry the
same input. Deletion never happens before public verification of current.

## Optional maintainer command: publish built additions

`scripts/publish-additions.py` automates fetch, catalog composition, sealing and
publication for an **existing** current repository. It is an explicit maintainer
operation. Ordinary `ember-sdk install` and Ports builds never invoke it, inspect
publisher credentials or warn about missing keys. Setting S3 environment variables
does not enable publication. Only this command needs the S3 credentials above and
the release signing identity.

Use the [existing package-build input and Ports graph](binary-catalog.md#produce-a-catalog)
to declare the selected repository, host or target ABI, source revisions and a new
`release_id`. The graph belongs to the producer; `--dependency-phase runtime`
can export just its runtime edges. No second resolver or build graph is inferred
from the untyped bulk-build edges. Existing `packages` maps work directly;
archive filenames must be the producer's `PKGNAME.tgz` and match their metadata.

To collect a completed Ports batch without writing hundreds of archive paths,
select one repository in the input and supply its state and archive directory:

```sh
python3 scripts/publish-additions.py package-build.json \
  --host-prefix /work/host-pkg-llvm --ports-root /work/EmberBSD-Ports \
  --state /work/aarch64/state --packages /work/aarch64/packages/All \
  --workdir /work/package-publication \
  --url https://emberbsd-pkg.oxteam.me \
  --trust-root trust/emberbsd-release-root.pem \
  --signer-directory /private/release-signer
```

The input can leave `packages` empty in this mode. `STATE/packages` names must
match successful `STATE/done` entries and graph versions. Failed, blocked and
skipped entries stay out; native entries stay out of a target repository.
For native publication, select a host repository and its native archive directory.
The command adds successful roots to the explicit selection; selected sets retain
their normal complete runtime requirements. It verifies the producer's recorded
SHA256 for each archive it actually uses. State records do not replace the
producer's runtime or hardware acceptance checks.

The command authenticates live current, preserves all unrelated repositories,
packages, sets, bootstrap, sysroots and OS sets, and prepares only changed
packages and small metadata. Same root and full package name already published are reused from
current without opening or signing a local duplicate. A changed build must bump
its package version or revision. Downgrades and incompatible retained consumers
are refused. Old payload bodies are not downloaded to the publishing machine.
No compilation is performed.

JSON output includes the channel and counts for added, replaced and reused
packages, plus upload candidate count/bytes and `built: 0`. The same counts are
summarized on stderr. Upload candidates describe the batch, not network traffic:
a retry can reuse objects uploaded before the interruption. A fully unchanged
selection returns current without signing, switching its pointer or cleanup.

Keep the private workdir outside source packages and graphs. After an interrupted
operation, rerun the same command: a prepared signed batch is frozen and resumes
with its original expected generation. Do not edit that batch to add more packages;
finish it, then use a new `release_id`. If another publisher changed current,
the generation guard refuses to overwrite it; coordinate the writer before
starting. This tool does not bypass the existing publication lock.

The next invocation after success removes only the previous operation's snapshot
and signed output, then reads fresh current. Producer archives, build objects and
shared caches remain intact. Only one batch's additional copies are retained for
retry; there is no local release archive. Initial channel creation and explicit
expiry refresh remain operations of `s3-release.py publish`.

`--prefix` optionally selects an owned repository namespace shared by S3 and
HTTPS. It must not contain unrelated files under `current/`. Endpoint, region
and bucket have explicit options; keys come only from environment variables.
S3 uses path-style URLs and SigV4, including paginated ListObjectsV2 for cleanup.
No redirects, external S3 library or client write credentials are required.

Package and immutable metadata objects use long immutable caching. Current uses
`no-cache, max-age=0, must-revalidate`. Single PUT supports objects up to 5 GiB;
multipart remains unsupported. The public endpoint must permit object GET;
403 on its root can simply mean directory listing is disabled.

## Read

```sh
mkdir -m 700 /work/repository-state
python3 scripts/s3-release.py --host-prefix /work/host-pkg-llvm fetch \
  --url https://emberbsd-pkg.oxteam.me \
  --trust-root trust/emberbsd-release-root.pem \
  --state /work/repository-state/current.json --output /work/catalog \
  --repository darwin-arm64
```

The output parent must exist and the output directory must be new. The reader
verifies current, its exact manifest and selected index sizes/hashes, then
advances persistent replay state and publishes the local snapshot. Indexes
stream to disk; package archives are not downloaded. If superseded metadata
disappears during reading, it retries once only when current has changed.
An ordinary missing file, invalid signature or corrupt bytes is an error.

Choose package sets from the snapshot with `scripts/binary-catalog.py plan`.
The [SDK client](sdk-client.md) finishes downloading and verifying its whole selection
before modifying installed state; if the server has removed a superseded
package, it re-reads current and re-plans first. These installation operations
remain separate from the metadata-only fetch command.

The `pkg_summary` format is unchanged. Direct stock pkgin does not authenticate
our manifest or select its metadata snapshot: its verified repository adapter
remains a separate integration. This tool does not claim a working pkgin install.

## Trust and lifetime

The default current lifetime is seven days. `--ttl SECONDS` chooses another
positive lifetime; it starts after upload and read-back complete. `--refresh`
renews it and advances the generation without changing package bytes. Expiry
blocks accepting stale online metadata; it does not delete packages or stop
installed programs. Renewal is not automatic.

Rotate the working certificate under the same root before expiry. If the
previous pointer's signer has expired, explicit publisher-only
`--previous-channel-time UNIX_SECONDS` authenticates that pointer at a known
valid historical time, retaining its generation and ETag. New packages,
metadata and signer still require current validity. Readers have no such flag.

HTTPS CA trust is separate from the release root. `--tls-ca-file FILE` selects
the HTTPS CA bundle when needed, for example a host CA bundle while preparing
the first toolset. There is no insecure TLS option. Bodyless GET/HEAD requests reuse HTTPS
connections only after successfully consuming the complete known-length response
(or a bodyless HEAD response). Chunked and unknown-length replies remain
supported but are not pooled. Partial reads and failed checks close the connection. Mutations use fresh
connections and are never retried; safe reads retain their bounded retries.
Callers close the transport after its requests complete.

## Validation boundary

The current native catalog contains fetch, OpenSSL, mksh, digest, nawk, nbsed,
nbpatch, mktools and cwrappers for Darwin arm64, alongside the
[self-contained initial SDK](bootstrap-client.md). Select `host-fetch` or
`sdk-source-tools`; existing bootstrap dependencies are reused. On a fresh
bootstrap, source tools add seven packages with a 402,470-byte download and
1,513,876 unpacked bytes. Repeating the installation downloads nothing.
The bootstrap archive is 38,862,758 bytes (117,540,506 bytes of regular files),
including 16 whole runtime packages and the client. It requires macOS 27+,
with no preinstalled Python, compiler or SDK. First installation, signed fetch
addition and HTTPS execution are checked with producer-prefix reads denied.
Compiler acquisition, target sysroot delivery and EmberBSD execution remain
separate acceptance work. The [current pointer](https://emberbsd-pkg.oxteam.me/current.p7m)
selects this one release; displaced packages and previous metadata are removed.

Tests cover conditional publication, interrupted upload and cleanup, version
replacement, unchanged archive reuse, competing writers, current changing
during a read, bad signatures/hashes, replay and streamed large indexes. Timeweb
S3 listing may omit Content-Length; listing responses are bounded independently
of it, while signed artifact downloads still require the exact size.

Protocol references: [AWS conditional writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html),
[AWS ListObjectsV2](https://docs.aws.amazon.com/AmazonS3/latest/API/API_ListObjectsV2.html),
[Timeweb S3 API](https://timeweb.cloud/docs/s3-storage/manage-storage/s3-guide).
