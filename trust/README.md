# EmberBSD release trust anchor

[`emberbsd-release-root.pem`](emberbsd-release-root.pem) is the initial public
X.509 root for EmberBSD release metadata and binary packages, issued 2026-11-03.
It is valid until 2036-10-31 09:33:49 UTC. Its SHA256 certificate fingerprint is:

```text
D5:BF:B3:64:48:44:A5:3E:B8:37:92:98:52:8C:B4:9C:4A:81:0B:E3:B4:B7:E2:6B:54:2A:BA:72:BE:46:1B:0C
```

Bootstrap and OS image producers bundle this public certificate as a trust input.
Provision this root independently of the release channel.
The SDK release commands take its path through `--trust-root`.
The OS etc set installs it at `/etc/openssl/emberbsd-release-root.pem`.
The image builder checks it before enabling the native update service.
The device uses this root to verify the signed channel and release.
[Installed-system update architecture](https://github.com/oxtech-ember/EmberBSD-Docs/blob/main/modules/updates.md)

The root issues separate short-lived signing certificates; the CMS envelopes
and package signatures carry their leaf certificates. Renewing a leaf under
this root does not change installed trust. See the
[signing format and key procedure](../docs/signed-releases.md).
