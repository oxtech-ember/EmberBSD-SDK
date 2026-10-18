# Shared host tools

SDK host commands and related Ports controllers use CPython 3.15 and its
standard library. CPython 3.15 is the current host profile. A minimum-version
check in a command does not qualify a later interpreter. Keep one implementation
of each operation. Put reusable
mechanisms in `src/emberbsd_sdk`. Keep compiler recipes and package policy in
their owning Ports profiles.

Use small CLI entry points. Put behavior in named modules with explicit inputs
and results. Imports must not download, build, install, change the working
directory or modify the process environment.

## Select the source and interpreter

Ports consumers receive the SDK source through `--sdk-source` or the recipe's
`EMBERBSD_SDK_SOURCE`. They load modules from that source's `src` directory and
check the library API version. The initial API version is `1`.

Select the build-host Python executable explicitly for a build. Record its
resolved path, version and identity with the SDK/Ports source commits. Do not
substitute the target Python executable for a host controller.

The control modules require no third-party Python packages, `pip` installation
or virtual environment. Upstream components retain their documented tools and
package dependencies. Those tools do not become hidden imports in SDK control
modules.

## Check inputs and side effects

Use explicit validation for required fields, types and numeric ranges. Reject
duplicate JSON keys and nonfinite values. Keep text encodings explicit. Read
binary artifacts as bytes and hash the original bytes.

Use argument arrays to start programs. Supply the selected environment and
working directory. Check exit status and retain diagnostic output. Keep
platform process control behind the declared backend; POSIX process groups
do not establish native Windows support.

The current producer backend uses Darwin's base-system `ps` and public
`proc_pid_rusage` interface. Python's standard-library `ctypes` calls that
interface. No additional package is required. Birth identities distinguish PID
reuse from processes that disappeared during inspection. The backend checks its
native prerequisites before it starts a producer.

The backend must stop all process groups in its owned session before it
releases object locks. Commands must retain their effective UID and must not
start a different session. This contract does not contain arbitrary detached
daemons. Other hosts require a separately qualified process backend.

`CommandError` retains the completed command's exit status or signal.
`ProcessOwnershipError` means that producer shutdown could not be confirmed.
It contains the session ID, reason and optional log path. A consumer must keep
the work and reject its reuse after this error. Keep a persistent refusal
marker outside immutable input inventories. Do not treat an uncertain shutdown
as an ordinary failed command or remove its work automatically.

Resolve source and destination identities before writes. Refuse overlapping
input/output trees and unexpected links. Keep exclusive creation, locks,
incomplete markers and receipts where the operation requires them. A failed
producer cannot authenticate an earlier artifact at the same path.

Use `unittest` for executable contracts and refusal cases. Keep tests of real
filesystem and process behavior separate from command fixtures. Record the
interpreter and platform for actual results. Type annotations document module
interfaces; they do not replace input validation or runtime checks.

## Partition a package stage

`emberbsd_sdk.stage` provides `STAGE_PARTITION_API = 1` and
`partition_stage(stage, full, partitions, selected, *, dependencies=None,
keep_empty=())`. The package producer must own the stage exclusively.
`full` lists every installed file and symbolic link. `partitions` maps each
package role to its members. The roles must form a disjoint, exhaustive union.
Directories are implicit. Package recipes parse their own manifests and define
the role policy; SDK does not parse pkgsrc PLIST syntax.

The operation validates the actual tree and every link before it removes
anything. A link may traverse another role only through an explicit dependency.
The dependency map permits transitive dependencies. Links must terminate in
listed regular files. For example, a developer package may link into its exact
runtime dependency; the runtime must not link back into the developer package.
`keep_empty` lists existing empty directories that the selected role must retain.

A second inventory must match before pruning starts. Validation failures leave
the stage unchanged. An I/O failure during deletion fails the producer and may
leave a partial stage. Do not package that stage. This API is not a concurrent
filesystem transaction or a security boundary against another stage writer.
The real filesystem contracts run on Linux with CPython 3.15. They do not qualify
a target package, a package transaction, or a native Windows build host.

## Keep acceptance separate

Platform qualification needs actual filesystem, process and producer checks
on the declared host. Start the Windows build path in WSL2 as described in
[host platforms](host-platforms.md). Qualify a native Windows backend separately.

Historical receipts keep their original inputs and hashes. A replacement
verifier must authenticate that history explicitly. A language migration does
not establish a new compiler build, package transaction or hardware result.
