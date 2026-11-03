# EmberBSD release trust anchor

[`emberbsd-release-root.pem`](emberbsd-release-root.pem) is the initial public
X.509 root for EmberBSD release metadata and binary packages, issued 2026-11-03.
It is valid until 2036-10-31 09:33:49 UTC. Its SHA256 certificate fingerprint is:

```text
D5:BF:B3:64:48:44:A5:3E:B8:37:92:98:52:8C:B4:9C:4A:81:0B:E3:B4:B7:E2:6B:54:2A:BA:72:BE:46:1B:0C
```

This file contains no private key. Bootstrap and OS image producers must bundle
this public certificate as an immutable trust input. A channel cannot replace
it or provision its own trust. Current release commands take its installed path
explicitly through `--trust-root`; installer/image integration is a later stage.

The root issues separate short-lived signing certificates; the CMS envelopes
and package signatures carry their leaf certificates. Renewing a leaf under
this root does not change installed trust. See the
[signing format and key procedure](../docs/signed-releases.md).
