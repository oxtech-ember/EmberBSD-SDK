# S3 publication and HTTPS metadata delivery

`scripts/s3-release.py` publishes an existing [signed release](signed-releases.md)
and retrieves a pinned metadata snapshot. It uses Python's standard library and
the selected Ports OpenSSL/pkg_tools. AWS CLI, boto3 and client S3 credentials
are not required. Package installation and the shared artifact cache remain
the next layer; this command does not change installed packages.

## Publisher

Provide these environment variables to the publishing process:

```text
EMBERBSD_S3_ENDPOINT
EMBERBSD_S3_REGION
EMBERBSD_S3_BUCKET
EMBERBSD_S3_ACCESS_KEY
EMBERBSD_S3_SECRET_KEY
```

Keep their values outside Git. Only the publisher gets write credentials.
Endpoint, region and bucket also have explicit CLI options; credentials do not
appear in command arguments. The transport implements path-style S3 URLs and
SigV4 header authentication. It does not follow redirects or log authorization.

```sh
python3 scripts/s3-release.py --host-prefix /work/host-pkg-llvm publish \
  /work/releases/preview-1/release.p7m \
  --url https://packages.example.org --channel development \
  --trust-root trust/emberbsd-release-root.pem \
  --signer-directory /private/release-signer
```

`--url` is the public HTTPS base. Optional `--prefix` selects the same relative
key prefix under both that base and the bucket. The command checks local package
signatures and hashes, then publishes:

```text
releases/<release_id>/<artifact paths>
releases/<release_id>/release.p7m
channels/<channel>.p7m
```

Payloads precede indexes; the signed manifest comes last. Immutable object
creation uses `If-None-Match: *`. Existing objects must have identical public
bytes. A previously sealed release ID cannot acquire a different manifest.
Unreferenced objects from an interrupted attempt can be reused by retrying the
same release; a different release needs a different ID.

Before making the channel current, the publisher streams every object from
the public HTTPS endpoint and checks Content-Length and SHA256. The channel is
one CMS object containing both pointer and signature. Updating it uses the ETag
read at the start, or create-only when absent. HTTP 409/412 means another writer
won: the command stops without an unconditional overwrite or hidden retry.
ETags are concurrency tokens, not checksums. S3 If-Match carries the ETag value
without the HTTP response's surrounding quotes.

Release objects use `public, max-age=31536000, immutable`; channels use
`no-cache, max-age=0, must-revalidate`. Existing bucket/CDN access policy must
allow public object GET; the command does not change bucket policies or ACLs.
A 403 on the bucket root can simply mean directory listing is disabled.

Repeat publication of the same current release is a no-op after verification.
`--refresh` advances the generation and renews expiry even for that release.
Default lifetime is seven days; `--ttl SECONDS` chooses another positive value.
The validity window starts after uploads and read-back verification finish.

### Certificate rotation and recovery

Rotate the working signer under the same root before it expires. Routine
publication verifies old and new signatures against the current clock.
If the previous channel's signer has already expired, a publisher can explicitly
select `--previous-channel-time UNIX_SECONDS`: a known historical instant at
or after that channel's issuance when its chain was valid. This authenticates
**only the previous channel** at that time, retaining its generation and ETag.
Its signature, trust chain and signer profile are still verified. New release
metadata, packages and the new signer must pass current-time verification.
Clients have no historical-time flag. Do not change the computer clock or
disable signature checks to recover publication.

## Anonymous reader

Use a private, persistent directory for replay state and a new output directory
for each snapshot. The output parent must already exist.

```sh
mkdir -m 700 /work/repository-state
python3 scripts/s3-release.py --host-prefix /work/host-pkg-llvm fetch \
  --url https://packages.example.org --channel development \
  --trust-root trust/emberbsd-release-root.pem \
  --state /work/repository-state/development.json --output /work/catalog-1 \
  --repository darwin-arm64
```

The reader verifies the channel at the current time, downloads the exact
referenced release, and streams selected package indexes into private staging.
Only after their signed sizes/hashes match does it advance the replay floor
and atomically publish the snapshot. A malformed signature, changed index,
missing repository or old generation fails before state acceptance. An output
publication failure may leave an advanced metadata floor; retrying the same
authenticated generation is permitted. No installed state has been changed.

`--repository` may be repeated; omitting it fetches all package indexes. Names
are the first path segment used by the [binary catalog producer](binary-catalog.md).
The snapshot includes `release.p7m` and the selected indexes, without package
archives, compilers or sysroots. Select its components using the existing reader:

```sh
python3 scripts/binary-catalog.py --host-prefix /work/host-pkg-llvm plan \
  --release /work/catalog-1/release.p7m --directory /work/catalog-1 \
  --trust-root trust/emberbsd-release-root.pem \
  --repository darwin-arm64 --set host-fetch
```

HTTPS server trust is separate from release signing trust. The TLS stack uses
its normal CA bundle. `--tls-ca-file FILE` explicitly selects another HTTPS CA
bundle, for example a host-provided bundle while preparing the first toolset.
It never replaces `--trust-root`, and there is no insecure TLS option.

## Size, retries and boundaries

Uploads and read-back verification stream data; downloads of indexes also
stream to disk. Only bounded CMS channel/release envelopes are kept in memory.
The publisher copies exact producer archives; choose gzip or another supported
package compression before signing. It does not keep another payload cache.

This first transport supports single PUT objects up to 5 GiB. Multipart upload
and automatic artifact-download resume belong to later work. The HTTPS layer
already validates exact Range/Content-Range reads for that downloader. A failed
upload leaves the current channel unchanged; a retry checks existing objects.
If the channel PUT succeeds but the following public read fails, publication
can already be current: inspect/retry rather than attempting a rollback.

On 2026-11-03, Timeweb `s3.twcstorage.ru` with the project's public HTTPS domain
passed create-only writes, ETag replacement, two competing writers (one success,
one 409), Content-Length, byte ranges and read-after-write. Temporary probes
were deleted. Provider behavior is verified, not inferred from an S3 label.

```sh
EMBERBSD_TEST_HOST_PREFIX=/work/host-pkg-llvm \
  python3 -B scripts/select-tests.py --run
```

Tests include the published AWS SigV4 vector, a local conditional object server,
real OpenSSL signatures, interrupted upload, same-ID conflict, competing writer,
metadata-only fetch, corrupt index/manifest, replay, large streamed index,
expiry during verification and explicit publisher certificate recovery.

Protocol references: [AWS conditional writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html),
[AWS SigV4](https://docs.aws.amazon.com/AmazonS3/latest/developerguide/sig-v4-header-based-auth.html),
[Timeweb S3 API](https://timeweb.cloud/docs/s3-storage/manage-storage/s3-guide).
