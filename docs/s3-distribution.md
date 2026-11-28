# One current S3 repository

`scripts/s3-release.py` publishes the current signed package set and retrieves
its authenticated metadata through HTTPS. It uses Python's standard library
and the selected Ports OpenSSL/pkg_tools. Installation is a separate layer.

Bodyless GET and HEAD requests make at most three connection attempts for
transient timeouts or disconnected peers before exposing a response. When a
package transfer stops after a partial body, verification can fetch the
remaining bytes in bounded HTTP ranges. It checks each range and the SHA256
of the complete artifact. Invalid certificates, HTTP responses and mismatched
signed bytes stop verification.
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

The CMS pointer uses channel schema v2 with the fixed name `current`,
issuance/expiry times and an exact manifest reference, without a generation.
The manifest keeps producer paths for local files; `artifact_key()` maps payloads to the platform directories
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
  --signer-directory /private/release-signer --expected-current absent
```

The example creates an initially empty repository. For an existing current,
use the verified snapshot hash as described below.

The publisher verifies local signatures and hashes, uploads missing packages
and new metadata, verifies new objects through public HTTPS, conditionally
switches `current.p7m`, then removes superseded files. Repeating the same
publication retains the current pointer and finishes any interrupted cleanup.
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

Publish that sparse release with both `--reuse-current` and the SHA256 of the
verified snapshot's `current.p7m` bytes:

```sh
CURRENT_SHA256=$(python3 -c 'import hashlib, pathlib; print(hashlib.sha256(pathlib.Path("/work/catalog/current.p7m").read_bytes()).hexdigest())')
python3 scripts/s3-release.py --host-prefix /work/host-pkg-llvm publish \
  /work/prepared-release/release.p7m --url https://emberbsd-pkg.oxteam.me \
  --trust-root trust/emberbsd-release-root.pem --signer-directory /private/release-signer \
  --expected-current "$CURRENT_SHA256" --reuse-current
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
merge the latest snapshot and retry with its current-envelope SHA256.

When extending an existing catalog, preserve its current packages and sets in
the candidate and pass `--expected-current SHA256` from the verified snapshot.
This is a compare-and-swap precondition for that publication, not a package or
catalog version. Under the publisher lock, a changed current envelope stops a
different candidate before any upload or cleanup; merge the new catalog and retry.

An interrupted publication may finish the exact authenticated release already
selected by an unexpired current pointer. Its release ID, path, size and envelope
SHA256 must all match. Recovery verifies objects and finishes cleanup without
signing or switching the pointer again. A refresh or an expired pointer requires
the actual current hash; expiry during verification also stops a stale retry.
Use `--expected-current absent` for an initially empty repository and retain that
precondition when recovering the same publication. The option does not merge
catalogs. `publish-additions.py` obtains the hash from its authenticated snapshot
automatically; users never maintain a counter.

A create-only `.publisher-lock` serializes publication **and cleanup** across
publishers. The SDK publication commands defer `SIGTERM`, `SIGHUP` and
`SIGINT` until publication and cleanup finish and the lock is released.
A host crash or `SIGKILL` can leave the lock in S3. Its
`release_sha256` identifies the signed `release.p7m` in the producer's work
directory. Compare this digest and the start time with the producer. Remove
that exact object with the storage administrator's tools only when no publisher
is running, then retry. Current uses ETag compare-and-swap. HTTP 409/412
refuses a competing write. ETags are concurrency tokens, not integrity checksums.

A failed upload does not switch current. A failure after switching, including
cleanup failure, can leave a usable new current and extra old files: retry the
same input. Deletion never happens before public verification of current.

## Check package names before a bulk build

After Ports writes its order, check the planned package names against one signed repository.
Run this maintainer command once for the batch. It needs permission to list the S3 package directory.

```sh
python3 scripts/s3-release.py --host-prefix "$HOST_PREFIX" check-package-names \
  --url "$PUBLIC_URL" --trust-root trust/emberbsd-release-root.pem \
  --repository netbsd-aarch64 --state "$STATE"
```

The command reads `STATE/packages` and the signed package metadata. Exact matches for the same root count as `reusable`.
For the remaining names it lists the selected host or target directory once.
Withdrawal records identify exact artifact IDs and SHA-256 values. The current and earlier signed releases supply their archive names.
Add `--history /path/to/release.p7m` for each earlier signed release that identifies removed archives by ID and SHA-256.
The JSON report lists each conflicting root, package name and next free revision above the known revisions for that version.
Exit status 3 means the report contains conflicts. Status 0 means the checked names are clear. Publication checks concurrent changes again.

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
match successful `STATE/done` entries and graph versions. Completed packages
outside the supplied graph are ignored, so tasks can share one STATE directory.
For ready archives from a target build or a Ports `cross-bulk.sh package` run,
pass `--receipt packaged` to the publisher or to `publication-candidates`. This selects archive checksums from
`STATE/packaged` and excludes roots in `STATE/package-failed`. Use the same
producer graph and archive directory. The option also works with `--follow`.
For a rolling publication, export the typed runtime graph once for the producer
inputs. Reuse it for each batch from those inputs. Add one `--state-root PKGPATH`
per completed archive to the command above, for example
`--state-root devel/lib --state-root net/app`. These paths replace the input roots and sets for this
batch. The publisher keeps the sets in the signed current repository and reuses
its matching runtime providers. Include each new local provider in the batch.
Other workers can finish while the publisher retries the same batch.

For a running build, compare its completed receipts with a signed snapshot:

```sh
python3 scripts/binary-catalog.py --host-prefix /work/host-pkg-llvm \
  publication-candidates package-build.json --current /work/current \
  --state /work/aarch64/state --packages /work/aarch64/packages/All \
  --trust-root trust/emberbsd-release-root.pem
```

Use the graph and state from the same producer. The command reads metadata.
`missing` lists completed roots absent from the selected repository.
`changed` lists roots with a different published package name and gives both names.
Pass the selected roots and their new runtime providers to `publish-additions.py`
with `--state-root`. The publisher checks each new archive and the live catalog.

Install available binary providers before `cross-bulk.sh build` takes the prefix read lock.
Use a binary-only SDK configuration for the producer's prefix and target.
Match the catalog's repository, `root`, and `pkgname` to the entries in `STATE/packages`.
Set `config` to the client configuration file. The following commands request two providers.
Check the package names and the empty `build_roots` list in the plan before installing.

```sh
ember-sdk --config "$config" catalog
ember-sdk --config "$config" plan --package devel/zlib --package graphics/png
ember-sdk --config "$config" install --package devel/zlib --package graphics/png
```

Read the catalog again after a successful installation. For each exact target match with
`action=installed`, write one `PKGPATH PKGNAME` line to the file selected by
`CROSS_BULK_BINARY_INSTALLED`. Ports checks `@native` entries separately in the host prefix.
If the prefix is busy, retry after its owner releases the lock. If `pkg_add`
rejects an archive, keep its error and build the affected provider from source.
Start `publish-additions.py --follow` after the Ports build acquires `STATE/build.lock`.

Use `publication-candidates --ready` to group newly completed roots whose runtime
providers satisfy the graph dependency patterns in the signed catalog or in the same batch. The
`ready` list puts providers first. If another build published a newer version, the
publisher keeps it and evaluates consumers against its signed package name. The `waiting` map lists unavailable direct
providers. Use `--hold-root PKGPATH` to hold a known bad provider and its
consumers. Supply the resulting roots to the publisher. It checks the package
dependency patterns, runtime files and producer paths before signing.

For a live Ports build, pass `--follow` to `publish-additions.py` with its
`--state`, `--packages`, producer graph, private work directory and usual signing
options. For a target repository, the follower selects verified archives from
`STATE/packaged` while Ports installs or replaces them in the build sysroot.
For host repositories it selects `STATE/done` by default. Pass `--receipt`
to select a specific receipt. After publication, install the released packages
from the signed S3 catalog in a control prefix and run `pkg_admin check`.
The follower reads completion receipts when they change and emits one JSON
result per batch. Selection and composition reuse immutable release indexes by their
signed digests for the duration of the command. The publisher reads the current
pointer from S3 for each snapshot. It checks signed current again when missing providers can
arrive from another producer. It drains the final receipts when the build
releases `STATE/build.lock`. Start the follower after the producer takes the lock.
For an SDK source install, pass the configured source state directory. The
publisher reads completed packages from the active source wave and drains
the merged receipts when the SDK releases the lock.
For a finished build, it scans once and exits. Keep the state directory through
the final scan. Use a new invocation when the producer or its graph changes.
The input release ID is the prefix for each batch ID. Each batch is tied to
its signed parent and checksums. A displaced parent triggers fresh selection.
If another publisher holds the S3 lock, the follower retries at `--interval`.
It keeps the prepared batch and checks the signed parent after it obtains the lock.
An archive failure stops the follower. Fix the archive or restart with
`--hold-root PKGPATH` to release the independent packages.

The publisher excludes roots in the current `failed`, `blocked` and `skipped`
files. Cross-bulk archives prior failures on resume. A target repository
selects target entries from the state.
For native publication, select a host repository and its native archive directory.
The state can mix target archives and native tools installed by cross-bulk.
Two-column `STATE/done` rows omit the archive checksum. Host rows ending in
the exact `native` marker record local tool installations. Each supersedes
earlier rows for that root, and the publisher skips it. Only rows with a valid
archive SHA256 are publishable, including host rows. Other marker values are
rejected.
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
with its original expected-current hash. Keep that batch unchanged until it finishes, then use a new `release_id`
for additional packages. If another publisher changed current,
the current-hash precondition refuses to overwrite it; coordinate the writer before
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
python3 scripts/s3-release.py --host-prefix /work/host-pkg-llvm fetch \
  --url https://emberbsd-pkg.oxteam.me \
  --trust-root trust/emberbsd-release-root.pem \
  --output /work/catalog \
  --repository darwin-arm64
```

For an explicit HTTP CONNECT proxy, place `--https-proxy http://host:port`
before the subcommand. TLS authentication still verifies the final HTTPS server;
proxy environment variables are not selected automatically.

The output parent must exist and the output directory must be new. The reader
verifies current, its exact manifest and selected index sizes/hashes, then
publishes the local snapshot without persistent channel replay state. Indexes
stream to disk; package archives are not downloaded. If superseded metadata
disappears during reading, it retries once only when current has changed.
An ordinary missing file, invalid signature or corrupt bytes is an error. Public
snapshot export uses atomic no-replace directory publication; client reads and
artifact downloads use private temporary metadata cleaned up after the operation.

Download one artifact by its exact ID from authenticated current. Use a
directory owned by the calling user. Only its owner may write to it. For a
privileged download, select a path controlled by root. On EmberBSD, a private
work directory on FFS is suitable.

```sh
mkdir -m 700 "$HOME/client-download"
python3 scripts/s3-release.py --host-prefix /work/host-pkg-llvm fetch-artifact \
  --url https://emberbsd-pkg.oxteam.me \
  --trust-root trust/emberbsd-release-root.pem \
  --artifact bootstrap-netbsd-amd64-client --output "$HOME/client-download/ember-sdk.pyz"
```

For an EmberBSD aarch64 device, select `bootstrap-netbsd-aarch64-client`.
This portable Python zipapp requires Python 3.15 and native pkg_tools already
installed on the device; it does not contain a compiler or a Python runtime.

The command verifies the signed channel and release and the selected artifact's
size and SHA-256 before atomically creating or replacing the regular output file.
A sibling `.sdk-lock` serializes writers; download or verification failure leaves
the previous file intact. After an interruption the verified replacement may have
committed; rerunning is safe. It does not install packages or choose a platform. A standalone client can be
run with the selected Python and an explicit `--config` JSON file; deployment
to another host is an administrator operation.

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
renews its validity without changing package bytes. Expiry
blocks accepting stale online metadata; it does not delete packages or stop
installed programs. Renewal is not automatic.

Rotate the working certificate under the same root before expiry. If the
previous pointer's signer has expired, explicit publisher-only
`--previous-channel-time UNIX_SECONDS` authenticates that pointer at a known
valid historical time, retaining its exact envelope and ETag. New packages,
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
`sdk-source-tools` to reuse installed bootstrap dependencies. In the validation
run with 16 bootstrap packages, source tools added seven packages with a
402,470-byte download and 1,513,876 unpacked bytes. Repeating the installation
downloaded nothing. That bootstrap archive was 38,862,758 bytes and contained
117,540,506 bytes of regular files. The Darwin arm64 bootstrap requires macOS 27+,
with no preinstalled Python, compiler or SDK. First installation, signed fetch
addition and HTTPS execution are checked with producer-prefix reads denied.
Compiler acquisition, target sysroot delivery and EmberBSD execution remain
separate acceptance work. The [current pointer](https://emberbsd-pkg.oxteam.me/current.p7m)
selects the active release. Its signed artifact records give the sizes. The publisher removes
displaced packages and previous metadata.

Tests cover conditional publication, interrupted upload and cleanup, version
replacement, unchanged archive reuse, competing writers, current changing
during a read, bad signatures/hashes, validity and streamed large indexes. Timeweb
S3 listing may omit Content-Length; listing responses are bounded independently
of it, while signed artifact downloads still require the exact size.

Protocol references: [AWS conditional writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html),
[AWS ListObjectsV2](https://docs.aws.amazon.com/AmazonS3/latest/API/API_ListObjectsV2.html),
[Timeweb S3 API](https://timeweb.cloud/docs/s3-storage/manage-storage/s3-guide).
