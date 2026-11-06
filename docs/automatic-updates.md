# Automatic installed-package updates

Run the SDK client's explicit foreground controller:

```sh
python3 scripts/packages.py --config /work/client.json auto-update --interval 3600
```

The first attempt runs immediately. Each later attempt starts after the previous
attempt and the interval. The default interval is 3600 seconds. Optional repeated
`--package ROOT` limits updates to explicitly named installed roots.
Use `plan --installed` to inspect an update before starting the controller.

Each attempt calls the existing `update` operation. It rereads the configuration
and actual pkgdb, selects published versions and their runtime dependencies,
verifies downloads, and applies compatible replacements through pkg_tools.
It never expands package sets, compiles missing binaries or restores removed
packages. An explicitly named root removed by the user causes an error until
the controller is restarted with an updated selection.

No client, cache, build or pkgdb lock is held during the interval. Busy writers
and failed updates are reported. The next attempt waits the same interval and
reads pkgdb again; it does not spin or assume the previous batch was atomic.
Signal handlers request a stop between attempts. Existing subprocess interruption
and package recovery rules still apply. There is no multi-package rollback.
Existing build/pkgdb locks remain the coordination boundary. The controller does
not detect unrelated builds that use a prefix outside that lock protocol.

This command does not install a service, edit configuration or enable itself.
Starting it, arranging persistence through the user's service manager, and
delivering an updated bootstrap are separate actions. It updates configured
SDK host prefixes and target package roots, not a running device's OS base.
Coordinated ABI transitions and sysroot replacement retain their existing refusal.

## Metadata and component identity

Every attempt reads the signed current catalog and the local installed set.
An unchanged catalog does not suppress a check: the local installed set can
change independently. Adding another published package does not invalidate
installed packages or their SHA256 archive cache.

There is no catalog generation or persistent replay floor. The client verifies
metadata signatures and validity, then compares individual package versions.
It refuses package downgrades. Metadata expiry bounds freshness; it does not
prove that an unexpired catalog is the newest publication. See the
[signed channel contract](signed-releases.md#channel-emberbsdchannelv2).

Within one operation the client reuses the authenticated release returned by
`fetch_release`. It keeps catalog hash checks before parsing and package checks
before installation, without verifying that release's CMS twice. Independent
offline readers retain their own release verification.

`tests/test_package_auto_update.py` covers cadence, retry spacing, signals,
lock release and changed pkgdb under an unchanged channel. These fixture checks
do not establish native pkg_tools acceptance or activate automatic updates.
The three `test_sdk_client_install.py` native fixtures need compatible Ports
OpenSSL and full pkg_tools through `EMBERBSD_TEST_HOST_PREFIX`; no compiler or
sysroot is required. Bootstrap-mode pkg_tools without signatures is insufficient.
