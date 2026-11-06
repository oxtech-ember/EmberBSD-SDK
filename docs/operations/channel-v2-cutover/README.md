# Channel v2 one-time cutover handoff

This branch preserves the reviewed, standalone transition artifact and its local
fixture tests. It is not an installed SDK command, a permanent compatibility
adapter, a deployment authorization, or evidence of production execution.

Base: SDK PR #33, commit `bebda45`.

## Recovery and verification

The temporary workspace was lost. These two files were reconstructed from the
recorded creation commands and exact recorded patches, not rewritten from a
summary. The cutover script SHA256 matches the independent review record:

```
2085747829a050f106865ec0aede469cad877ce33b8249bb71f65cad93822f90  emberbsd-channel-v2-cutover.py
eb89d1a0a2ebb880951cf9e21ad1e0ece63e1bfbf4e3f6565592680d6ed956d0  test_channel_v2_cutover.py
```

The test file retains the original local fixture paths. For reproduction, place
this SDK checkout at `/tmp/kiss-sdk` and copy the two files to `/tmp`, then run:

```sh
PYTHONPATH=/tmp/kiss-sdk/src:/tmp/kiss-sdk/tests \
  python /tmp/test_channel_v2_cutover.py CutoverTests -q
```

The original dedicated run passed 18 tests. A post-recovery run is reported in
the handoff message. These tests use a mocked CMS/package verifier and a real
local conditional-HTTP fixture. They do not prove native pkg_tools/OpenSSL
verification or the production storage backend's CAS semantics.

## Operational boundary

The operator must verify the complete candidate, actual bootstrap v2-reader
readiness and the real backend's conditional-write behavior before use. A flag
or this document is not that evidence. Consumer and repository transitions may
be sequential; the owner accepts a temporary mismatch. Do not add a legacy
reader to hide that mismatch.

The artifact checks an exact old signed-current hash, authenticates the initial
v1 snapshot and its referenced release, checks complete candidate coverage and
package signatures, and uses the existing publisher lock and ETag conditional
switch. It does not install or update any user packages. A failure after the
switch can mean publication committed; an exact authenticated v2 retry verifies
and completes cleanup without depending on the retired v1 signer or expiry.
An expired current v2 is not silently refreshed.

No production cutover, bootstrap deployment, source merge or native integration
acceptance is performed by publishing this handoff branch.
