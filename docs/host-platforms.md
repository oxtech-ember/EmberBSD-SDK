# Host platforms and target platforms

The SDK host runs the compiler, generators and build commands. The target runs
the resulting EmberBSD programs. Select and record them independently.

## Common control layer

Workspace creation and inspection use Python's standard filesystem and JSON APIs.
They do not depend on `diskutil`, `xcrun`, Linux `/proc` or platform-specific
`stat` options. Adoption requires symbolic-link support and host permissions.
Paths must satisfy the selected backend's transport restrictions; the current
OS/Ports workflow uses build paths without whitespace.

The initial workspace contracts run on macOS arm64. The filesystem, process and
fixture-producer contracts also run on Linux x86_64 with CPython 3.15. BSD and
native Windows filesystem behavior still need their own real checks. A
conditional code path or successful syntax check is not platform acceptance.

## Current build backend

The OS and Ports producers use POSIX shell, make and their documented native
tools. SDK calls those producers and records their actual inputs. Upstream
CMake projects keep their CMake build interfaces. All target compilation uses
the common GCC16 cross compiler.

The Darwin process backend uses `ps`, Python's session APIs and the public
`proc_pid_rusage` interface through standard-library `ctypes`. The Linux backend
uses `/proc` process records, `CLOCK_BOOTTIME`, `pidfd_open` and
`pidfd_send_signal`. It starts one private Python supervisor for each producer.
The supervisor enables `PR_SET_CHILD_SUBREAPER` in its own process. It reaps the
producer's orphan descendants and confirms that no children remain. It does not
change the controller's subreaper setting or consume unrelated children.

Darwin `ps` prints numeric UIDs with a signed format, although `uid_t` is
unsigned 32-bit. The adapter normalizes values from -2147483648 through -1
to their unsigned identities before ownership checks. It retains unsigned
UIDs through 4294967295. PID and PPID parsing remains unsigned and bounded.
See Apple's [ps UID format](https://github.com/apple-oss-distributions/adv_cmds/blob/main/ps/keyword.c)
and [uid_t definition](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/sys/_types.h).
The actual-parser contracts cover signed UIDs, malformed rows and foreign
ownership refusal. These checks do not qualify a compiler or target build.

Both backends check their native prerequisites before a producer starts. The
producer must retain its effective UID and session. Linux needs readable procfs
for the current PID namespace and a kernel with pidfd support. Restricted
procfs access fails closed. An unqualified host receives an error before a
producer starts. Workspace creation and inspection remain separate operations.

The Linux contracts were checked on Linux 6.18 x86_64 with CPython 3.15. These
checks include real process exits, changed process groups, interrupted
controllers, orphan writers, zombies and foreign process churn. Build-command
fixtures do not qualify a real compiler, sysroot or target program.

Do not run Darwin tools on Linux, or Linux tools on native Windows. Build and
distribute host tools for the declared host environment. Target ELF libraries
remain separate from those native tools.

## NetBSD 11 amd64 qualification

The NetBSD adapter uses standard-library `select.kqueue`, `os.waitid` and
`ctypes` for libc `sysctl`. It reads a caller-sized `KERN_PROC2` prefix for
PID, PPID, SID, effective UID and state. It never treats `p_ustart` wall-clock
seconds or microseconds as a birth identity. The initial ABI profile is
NetBSD 11 amd64. Other versions and architectures fail before producer start.
`security.curtain` must be zero for the all-process census profile.

A private Python launcher waits on a pipe before exec. The controller installs
`EVFILT_PROC` with `NOTE_TRACK`, `NOTE_FORK` and `NOTE_EXIT` before releasing it.
Kernel-generated `NOTE_CHILD` and terminal `NOTE_EXIT` events define owned
lifetimes. `NOTE_TRACKERR`, an unknown event or a failed event read makes
shutdown unproven. The backend never repairs that error with a ps fallback.
A full event batch is drained before an empty result is accepted.

The root PID is kept waitable with `waitid(WNOWAIT)` until every tracked
lifetime has exited. Only then does the controller reap that exact child.
This reserves the session identifier while descendants change process groups
or outlive their parent. Exit notes establish that tracked writers have exited;
this controller does not adopt orphan zombies from the system init process.

Signals check the current tracked lifetime, native effective UID and session
before `kill`. This retains the Darwin check-then-kill contract. It does not
provide Linux pidfd atomic signalling. A producer must retain its effective
UID and session. `setsid()` escapes remain outside the supported build profile;
a visible credential or session change is an ownership error. Do not run setuid
producers, debuggers that consume the controller's child status, or another
waiter for the retained root PID.

The source mechanisms are pinned to NetBSD netbsd-11
[`4decc303f104deb1589104682b60bfb13cfb6a92`](https://github.com/NetBSD/src/tree/4decc303f104deb1589104682b60bfb13cfb6a92):
[`sysctl.h`](https://github.com/NetBSD/src/blob/4decc303f104deb1589104682b60bfb13cfb6a92/sys/sys/sysctl.h),
[`kern_event.c`](https://github.com/NetBSD/src/blob/4decc303f104deb1589104682b60bfb13cfb6a92/sys/kern/kern_event.c),
and [`wait.h`](https://github.com/NetBSD/src/blob/4decc303f104deb1589104682b60bfb13cfb6a92/sys/sys/wait.h).
The fork hook attaches child tracking before the child becomes runnable.
The kernel creates separate CHILD and EXIT notes, so an immediate child exit
does not overwrite the child-creation record.
A CHILD note can precede census visibility: [`kern_fork.c`](https://github.com/NetBSD/src/blob/4decc303f104deb1589104682b60bfb13cfb6a92/sys/kern/kern_fork.c#L531-L565)
queues tracking before SACTIVE, while [`kern_proc.c`](https://github.com/NetBSD/src/blob/4decc303f104deb1589104682b60bfb13cfb6a92/sys/kern/kern_proc.c#L2029-L2033)
omits SIDL embryos. A missing census row retains the active kernel lifetime.
Cleanup defers its signal and retries within the existing drain deadline.
It cannot establish emptiness or permit a signal without a matching UID/session.

Before native acceptance, use host GCC16 to compile and run
`tests/netbsd_process_abi.c` against the actual host headers. Then run
`PYTHONPATH=src:tests python3.15 -m unittest discover -s tests -p 'test_netbsd*.py'`.
Tests cover gated launch, process-group changes, orphan writers, rapid
fork/exit, ignored TERM, KILL drainage, cancellation and an unrelated sentinel.
Injected event tests cover PID reuse, lost tracking and census-delayed embryos.
macOS model checks do not qualify NetBSD.

Native NetBSD 11 amd64 process control passes all 29 contracts with CPython 3.15,
including the six kernel scenarios. The GCC16 probe against actual host headers
confirms PID/UID/state offsets 116/136/360, prefix 368 and full record size 680.
These checks qualify `process.run_logged` and its owned-session mechanism.
They do not qualify a real SDK compiler producer or target cross-build.

The complete native SDK suite does not pass: 310 tests report one failure,
16 errors and 28 skips. Fifteen errors and the failure reach unsupported
directory publication or sysroot import. The remaining stream test expects
`OSError` for a missing executable; the gated launcher instead exits 127,
which the stream reports as `CommandError`. The complete stream interface
is not qualified on NetBSD. These tests remain enabled and failing there.

Exclusive directory publication has no qualified NetBSD implementation. `publication._native_rename` supports Darwin `renamex_np` and
Linux `renameat2` only. It refuses NetBSD; no check-then-rename fallback replaces
its atomic no-replace contract. Sysroot import requires that operation and is
unsupported on NetBSD. A successful process backend does not qualify sysroot
publication, transfer/import or the complete SDK host profile.

## Windows through WSL2

The first Windows build path uses a Linux distribution under WSL2. Run the SDK,
Python, Git, the common GCC16 cross compiler and OS/Ports build tools inside that
distribution. The Linux process backend is available. This path still needs
actual SDK build checks on the selected WSL2 distribution. Linux process tests
do not establish a complete WSL2 SDK build.

Microsoft documents installation with `wsl --install` from an administrator
PowerShell session. Check the current
[WSL installation requirements](https://learn.microsoft.com/en-us/windows/wsl/install)
before changing a Windows system.

Keep sources and Prebuild SDK in the Linux filesystem, such as
`~/src` and `~/prebuild-sdk`. Microsoft recommends the WSL filesystem for the
performance of Linux command-line tools. See its
[filesystem guide](https://learn.microsoft.com/en-us/windows/wsl/filesystems).
Case distinctions, symbolic links and executable modes also belong in the
filesystem validation for this build profile.

A future Windows/WSL2 release selects Linux-host executables matching that
WSL distribution's architecture. It is not a package of native Windows `.exe`
tools. Verify the complete build, links, relocation and target execution on
that actual host before declaring support.

## Native Windows backend

A native Windows SDK is a separate future host profile. It needs Windows-native
compiler and generator executables, a supported OS/Ports build environment,
path/response-file handling, link semantics and actual end-to-end tests.
The portable control layer should permit that adapter without changing the
target ABI or duplicating the compiler and libc recipes.

No native Windows cross-build backend or binary SDK is currently qualified.
An initializer running under Windows Python would establish only its filesystem
contract. It would not establish that the current POSIX build backend works.
