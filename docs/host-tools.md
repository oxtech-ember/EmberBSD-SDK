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

The Darwin producer backend uses base-system `ps` and the public
`proc_pid_rusage` interface through Python's standard-library `ctypes`. Linux
reads coherent session and birth fields from `/proc/PID/stat`. It pins each
process directory while it reads the effective UID from `/proc/PID/status`.
Linux sends signals through pidfds so PID reuse cannot redirect a signal.
No additional Python package is required.

Linux starts a private Python supervisor as each owned session's leader. The
producer inherits that session. Its ID is known before the supervisor can start
writable work. The supervisor acts as a child subreaper and reports the
producer's actual exit status. It waits for all its children before it confirms
shutdown. This contains orphan reaping without changing the controller's process-wide state
or waiting for unrelated children. Zombies remain owned until reaped.
Supervisor failure makes shutdown unproven and raises `ProcessOwnershipError`.

For an unsupervised Linux snapshot, vanished PIDs and births in or after the
snapshot's clock tick make an empty result uncertain. An owned session becomes
empty only after its supervisor proves that no descendants remain. Unrelated
process churn cannot substitute for this proof. The Linux effective-UID check
applies only to a session with a supervisor record, which exists only after a
build is launched. Before launch, the controller session can contain processes
of another user, such as `sudo` or a CI runner. They are not producers and do
not count as an ownership change. The backends check their native
prerequisites before they start a producer.

The backend must stop all process groups in its owned session before it
releases object locks. Commands must retain their effective UID and must not
start a different session. This contract does not contain arbitrary detached
daemons. Hosts other than Darwin and Linux require a separately qualified
process backend.

`process.ContextLocks(paths, lock_root, *, shared=False)` keeps nonblocking
`flock` handles open until the caller closes the context. The default remains
exclusive and is required for producers or installers. `shared=True` allows
concurrent read-only context users and refuses while an exclusive holder owns
the same lock. It does not authorize writes or installation; callers must
acquire the default exclusive mode before mutation. Lock names, supplied path
identity, lock root, no-follow regular-file checks and busy refusal are the
same in both modes. Every user of a context must pass the same canonical path
value and lock root.

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

## Inspect an installed tree

`inventory.INSTALLED_TREE_INVENTORY_API = 1` provides
`snapshot_installed_tree(root, exclude=())`. It uses the common inventory walker.
It records real file bytes, hashes, modes and directory entries. It records each
symbolic link's literal target text. Absolute, dangling, escaping and cyclic
links are recorded without resolving or reading their destinations.
Paths and link targets must contain valid UTF-8 and no control characters.
Special files and changes during inspection cause refusal. The root must have
real directory ancestors. Exclusions use the same relative entry names as
`snapshot_tree`.

The result describes an existing installation. It does not authenticate link
destinations or qualify an archive. `snapshot_tree` retains its strict payload
link checks and its existing ELF inspection arguments. Use that operation for
prepared payloads and archive acceptance. The library API remains `1`.

## Inspect one process identity

`emberbsd_sdk.process_identity` provides `PROCESS_IDENTITY_API = 1`.
`process_identity(pid)` reads a native process birth identity, executable path,
and exact argument tuple. It returns `None` for an absent process or a Darwin
process with a confirmed exit record, including an unreaped child. It sends no
signal. A live access refusal does not prove absence. The caller must validate
a positive integer PID and recheck the full identity before an owned operation. PID alone does not prove
ownership. This snapshot does not make later signals process-bound.

Darwin uses the existing public `proc_pid_rusage` birth identity, `proc_pidpath`,
and `KERN_PROCARGS2`. It requires conventional nonempty `argv[0]` because the
native record does not distinguish an empty first argument from alignment NULs.
Later empty arguments, whitespace, quotes and shell characters remain distinct.
Linux pins the process directory and reads `stat`, `cmdline` and `exe`.
Repeated birth, executable and argv observations must agree. The native APIs do
not provide an atomic snapshot of all three fields.

Run `python3 -m unittest discover -s tests -p 'test_process_identity.py'`.
Native Darwin checks exercised exact argv, live/unreaped/reaped exit, and
birth preservation across `exec`. Refusal tests preserve live access errors
and reject a recycled birth identity. Parser fixtures cover malformed records.
The Linux implementation has not been executed in that acceptance; it needs a native Linux run before Linux
qualification.

## Publish an owned directory

`emberbsd_sdk.publication` provides `DIRECTORY_PUBLICATION_API = 1`.
`require_directory_publication()` checks the host and native symbol without
filesystem writes. Call it before starting a producer. An old API1 source can
lack this capability; check the constant explicitly before using the module.

`publish_directory_noreplace(source, destination)` moves a real directory to a
new destination entry. It returns `None` on success. Parent aliases are resolved;
the source leaf must not be a symlink. An existing file, directory or symlink at
the destination is never replaced. A destination created after path validation
is also protected by the native operation.

Darwin uses `renamex_np` with `RENAME_EXCL`. Linux uses libc `renameat2` with
`AT_FDCWD` and `RENAME_NOREPLACE`. A missing symbol or another host is unsupported.
Native errors retain their errno. A filesystem that refuses the operation, or a
cross-filesystem move, does not cause an ordinary rename or copy fallback.
The availability check cannot qualify each destination filesystem.

The caller must own the source and both parent directories throughout the call.
The API does not lock those paths against another writer. It does not validate
packages, drain producers, delete work, or establish crash durability. Those
contracts remain with the caller and the existing process APIs.

The wrapper derives from the BSD-licensed Ports
[`bundle-gcc16.py`](https://github.com/oxtech-ember/EmberBSD-Ports/blob/2313b5f8d9b124369310f2b9f4f9a94c4852861f/profiles/installer-desktop/bundle-gcc16.py).
The fixed native ABI follows Darwin `sys/stdio.h`, Linux UAPI
[`fs.h`](https://github.com/torvalds/linux/blob/v6.17/include/uapi/linux/fs.h) and
[`fcntl.h`](https://github.com/torvalds/linux/blob/v6.17/include/uapi/linux/fcntl.h),
and the libc `renameat2` declaration.

Run `python3 -m unittest discover -s tests -p 'test_publication.py'`.
The filesystem cases execute the current host's native operation. Separate ABI
fixtures check Darwin and Linux arguments and injected failures. Darwin results
do not establish an actual Linux filesystem run or native Windows support.

## Read a binary producer stream

`emberbsd_sdk.stream` provides `PRODUCER_STREAM_API = 1`.
`run_streamed(argv, consume, *, cwd=None, env=None, stderr_limit=1048576,
timeout=None, input=None, empty_snapshot_timeout=2)` calls `consume(reader)`
with binary stdout. The reader accepts explicit read sizes from zero through one MiB. Short reads are valid. The
callback must not retain the reader after it returns. Unread output is drained.
The returned `StreamResult` contains the callback value, stderr bytes and a
`stderr_truncated` flag. Stderr is drained concurrently. Only its first
`stderr_limit` bytes are retained. The limit is at most one MiB.

`capture_streamed(argv, *, stdout_limit=1048576, input=None, **options)` uses the same
producer contract and returns captured stdout as `StreamResult.value`.
It rejects stdout above the explicit limit. An output/parser error still drains
the pipes and checks the producer's completed status. A nonzero producer status
raises `CommandError` with that exact status and bounded stderr. It takes
precedence over a parser error. This includes status 97 and negative signal
statuses. The error also has a `stderr_truncated` flag. For `capture_streamed`,
the error contains the stdout prefix in `stdout`. This prefix is at most
`stdout_limit` bytes. Its `stdout_truncated` flag is true if the output exceeded
the limit. The extra byte used to detect overflow is not exposed.

The additive `PRODUCER_INPUT_API = 1` capability accepts `input=None` or bytes.
`None` keeps standard input at `/dev/null`. Bytes, including an empty value,
are staged in a private `tempfile.TemporaryFile` and rewound before the producer
starts. The producer receives a regular file as standard input. The controller
does not write a pipe while it waits to read stdout or stderr. This supports
input larger than a pipe buffer and producers that write output before reading
input. The temporary file closes on success, refusal, startup failure, timeout
and interruption, after any required session drainage. Invalid input types fail
before temporary-file allocation or producer startup. Staging uses the host's
configured temporary directory and may fail before a producer starts. The read
deadline begins after producer startup; it does not bound input staging.
Consumers that pass input must check `PRODUCER_INPUT_API` before startup.

The additive `PRODUCER_INSPECTION_API = 1` capability accepts a finite positive
`empty_snapshot_timeout`, in seconds. The default is 2. It bounds each attempt
to prove an empty session when process rows change during inspection. A caller
can select a larger budget for concurrent build activity. Unknown rows still
block empty-session proof. The polling interval and ownership checks do not
change. Persistent uncertainty still fails after this budget. This budget is
separate from the producer read timeout and the stop grace periods. It also
applies to the inspector's startup probes, before the read timeout starts.
Invalid budgets fail before input staging or producer startup. Consumers that
set this option must check `PRODUCER_INSPECTION_API` before startup.

`SessionInspector(env=None, empty_snapshot_timeout=2)` owns this policy.
`OwnedProcessGroup(..., empty_snapshot_timeout=2)` forwards the option when it
creates an inspector. An explicit inspector retains its own policy.

Both functions reuse `OwnedProcessGroup`. They do not create another process
backend. A producer that leaves a live descendant is rejected. Session drainage
finishes before return or exception. A signal handler can interrupt a read;
the CLI owns those handlers. A second signal cannot interrupt session drainage.
An optional finite positive timeout bounds waiting for output. A session
inspection can use its own budget before the next read-timeout check. The
existing session-stop grace periods apply after interruption. The callback must
also bound its own processing; the SDK cannot preempt arbitrary Python code.

`ProcessOwnershipError` remains distinct and takes precedence over other
failures. A consumer must retain and quarantine its writable work. Neither
function publishes, deletes or authenticates that work. An old API1 source may
lack this additive module. Consumers must check `PRODUCER_STREAM_API` explicitly
and report the missing capability before starting a producer.

## Inspect raw tar records

`emberbsd_sdk.tarstream` provides `TAR_STREAM_API = 1`.
`tar_records(stream)` yields `TarRecord(name, linkname, typeflag, size, payload)`.
Names, links and type flags remain bytes. `record.read(size)` reads at most one
MiB. Advancing the iterator drains unread payload and padding. A prior record
cannot be read after the iterator advances. A zero header ends traversal; the
producer wrapper drains remaining output. The reader never extracts a file.

`TAR_HEADER_FACTS_API = 1` adds raw ownership facts to each yielded record:
integer `uid` and `gid`, and byte strings `uname` and `gname`. Owner names stop at
the first NUL byte; they are not decoded or normalized. These are fixed-header
facts, without PAX overrides. The consumer still owns effective PAX policy.
The existing five-argument `TarRecord` constructor remains valid. Its omitted
ownership fields are `None`, which means unknown, not root ownership.

The standard-library `TarInfo.frombuf` validates each 512-byte header and its
numeric fields. The SDK retains the raw name, prefix, link and type fields.
It does not use `tarfile` stream traversal: that traversal applies PAX size
replacements and sparse transforms before a consumer can inspect them.
`x` and `g` records remain visible, including their original payload lengths.
Unknown and sparse member types also remain visible to consumer policy.

`pax_records(data, *, limit=1048576)` parses byte-counted records. It returns
byte keys and values, including empty values and sparse keys. The last duplicate
value wins. It rejects malformed, truncated and oversized record data. Consumers
must check an extension's declared size before reading its body, enforce their
member-type and path rules, reject unsupported sparse metadata, and apply their
own global/local PAX policy. Package manifests, ELF limits and package trust
remain Ports policy. These modules do not qualify an archive or a package set.

Run the focused contracts from the SDK checkout:

```sh
python3 -m unittest discover -s tests -p 'test_stream.py'
python3 -m unittest discover -s tests -p 'test_tarstream.py'
```

The process tests use real host subprocesses and synthetic parser failures.
They do not install packages or establish target execution.

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
The dependency map permits transitive dependencies. Links terminate in listed
files or nonempty payload directories. Every member below a directory target
must have a permitted owner. For example, a developer package may link into its exact
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

## Plan Ports updates

`emberbsd_sdk.ports_plan` provides `PORTS_PLAN_API = 1`. It validates evaluated
package graphs, creates immutable locks and update plans, and checks explicit
result receipts against current artifact bytes. It never executes producers.
See [Ports update plans](ports-plan.md) for schemas, CLI commands, and limits.

## Prepare and tag a release

`emberbsd_sdk.ports_release` provides `PORTS_RELEASE_API = 1`. It prepares the
canonical source version and a source manifest before building. It validates
the selected build receipts and optional checks before creating release tags.
See [release preparation](release-tags.md) for the two-phase procedure and limits.
