# AArch64 target runtime acceptance

The shared target suite is compiled by Ports and executed by the SDK in a
disposable EmberBSD FFS root under full-system QEMU TCG. The guest has no
compiler, package manager or Python. The selected SDK native prefix supplies
the controller Python, archive utility, shell and `qemu-system-aarch64`; the
image helper's utility roles are also selected from that prefix. The target
compiler family defaults to Clang and GCC remains an explicit option.

The runner consumes one completed AArch64 OS build, its matching core OS source
revision, an OS test/helper source snapshot, the shared Ports source, the
selected sysroot and target compiler, and one native prefix. It extracts the
build's base set, adds the test programs, selected runtime libraries, and the
diagnostic `/etc/rc`, builds the root with that build's `nbmakefs`, `nbmtree`
and `DESTDIR/dev/MAKEDEV`, then boots the build's `netbsd-EMBER64.gz`. The root
device is mandatory; the runner does not assume a QEMU unit number. It verifies
the mounted root is read-only and uses tmpfs for test scratch files.

```sh
/absolute/native-ports-prefix/bin/python3.15 -B scripts/target-acceptance.py \
    --os-build /absolute/os-build-destination \
    --os-source /absolute/EmberBSD \
    --os-test-source /absolute/EmberBSD-test-snapshot \
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
native prefix for the host controller and QEMU. The runner requires clean
committed source snapshots. `--os-source` must match the completed OS build and
supplies same-build `DESTDIR/dev/MAKEDEV` and OS metadata to the root-image
helper. `--os-test-source` supplies the C regressions and guest/root-helper
scripts; it defaults to `--os-source`. Its revision and file inventory are
recorded separately from the OS build revision, so adding acceptance tests or
root-helper logic does not claim a new OS build. The image helper runs in the
selected native environment, with SDK-generated utility aliases and a PATH
limited to the native prefix; target compilation uses a separate environment.
Both source snapshots should be published inputs.

The guest serial log uses a per-run nonce. Acceptance requires one read-only
root event, all nine named check events, one completion event, and a clean
QEMU exit. Timeout, partial output, duplicate or unknown events, and process
failure do not produce an acceptance receipt. A successful run writes
`target-acceptance.json` and the raw `target-acceptance.log`. It also writes the
release input `qualification.json` only when the selected consumer family and
version match the OS build's compiler. A different-family run remains useful
as a separately recorded target result, but cannot qualify that build. The
result does not publish a release. The current `target-runtime-v2` corpus
adds ordinary finite libm calls and a valid setjmp/longjmp check. Its
qualification records the OS test source commit separately from the OS build,
the Ports source commit, and the compiled source inventory hash. Older
qualifications without a corpus marker retain their original v1 coverage and
do not establish these two checks.

## Ports protocol regression

The SDK consumer regression runs the selected published Ports guest runner and
passes its stdout through the SDK parser, including a negative case where a
host-only compile marker is inserted into the guest stream:

```sh
PYTHONPATH=src:tests /absolute/native-ports-prefix/bin/python3.15 -B \
    tests/target_acceptance_ports_protocol.py \
    --ports-source /absolute/EmberBSD-Ports
```
