# AArch64 target runtime acceptance

The shared target suite is compiled by Ports and executed by the SDK in a
disposable EmberBSD FFS root under full-system QEMU TCG. The guest has no
compiler, package manager or Python. The selected SDK native prefix supplies
the controller Python, archive utility, shell and `qemu-system-aarch64`; the
target compiler family defaults to Clang and GCC remains an explicit option.

The runner consumes one completed AArch64 OS build, its matching OS source
revision, the shared Ports source, the selected sysroot and target compiler,
and one native prefix. It extracts the build's base set, adds only the test
programs and runtime libraries required by the selected family, builds the
root with that build's `nbmakefs`, `nbmtree` and `DESTDIR/etc/MAKEDEV`, then
boots the build's `netbsd-EMBER64.gz`. The root device is mandatory; the runner
does not assume a QEMU unit number. It verifies the mounted root is read-only
and uses tmpfs for test scratch files.

```sh
/absolute/native-ports-prefix/bin/python3.15 -B scripts/target-acceptance.py \
    --os-build /absolute/os-build-destination \
    --os-source /absolute/EmberBSD \
    --ports-source /absolute/EmberBSD-Ports \
    --sysroot /absolute/aarch64-sysroot \
    --target-prefix /absolute/aarch64-clang \
    --host-prefix /absolute/native-ports-prefix \
    --compiler-family clang \
    --root-device ld0a \
    --output /absolute/new-acceptance-output \
    --sdk-source /absolute/EmberBSD-SDK
```

Select `--compiler-family gcc` explicitly for the GCC alternative. Do not pass
`--host-prefix` to the Ports GCC consumer; the SDK still requires its selected
native prefix for the host controller and QEMU. The runner checks each source
revision and the OS build output hashes before starting. Ports records its
compile source inventory separately from the OS build revision, so adding or
changing acceptance tests does not claim a new OS build.

The guest serial log uses a per-run nonce. Acceptance requires one read-only
root event, all seven named check events, one completion event, and a clean
QEMU exit. Timeout, partial output, duplicate or unknown events, and process
failure do not produce an acceptance receipt. A successful run writes
`target-acceptance.json` and the raw `target-acceptance.log`. It also writes the
release input `qualification.json` only when the selected consumer family and
version match the OS build's compiler. A different-family run remains useful
as a separately recorded target result, but cannot qualify that build. The
result does not publish a release.
