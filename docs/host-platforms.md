# Host platforms and target platforms

The SDK host runs the compiler, generators and build commands. The target runs
the resulting EmberBSD programs. Select and record them independently.

## Common control layer

Workspace creation and inspection use Python's standard filesystem and JSON APIs.
They do not depend on `diskutil`, `xcrun`, Linux `/proc` or platform-specific
`stat` options. Adoption requires symbolic-link support and host permissions.
Paths must satisfy the selected backend's transport restrictions; the current
OS/Ports workflow uses build paths without whitespace.

The initial workspace contracts run on macOS arm64. Linux, BSD and native
Windows filesystem behavior require their own real checks. A conditional code
path or successful syntax check is not platform acceptance.

## Current build backend

The OS and Ports producers use POSIX shell, make and their documented native
tools. SDK calls those producers and records their actual inputs. Upstream
CMake projects keep their CMake build interfaces. All target compilation uses
the common GCC16 cross compiler.

The current process backend uses Darwin `ps`, Python's session APIs and the
public `proc_pid_rusage` interface through standard-library `ctypes`. It checks
these prerequisites before it starts a producer. The producer must retain its
effective UID and session. An unqualified host receives an error before a
producer starts. Workspace creation and inspection remain separate operations.

Do not run Darwin tools on Linux, or Linux tools on native Windows. Build and
distribute host tools for the declared host environment. Target ELF libraries
remain separate from those native tools.

## Windows through WSL2

The first Windows build path uses a Linux distribution under WSL2. Run the SDK,
Python, Git, the common GCC16 cross compiler and OS/Ports build tools inside that
distribution. This path needs a Linux process backend and actual SDK build
checks. A WSL2 SDK build has not been accepted by the macOS workspace tests.

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
