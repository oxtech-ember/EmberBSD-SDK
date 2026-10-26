# Configure the first Darwin Ports prefix

`scripts/prepare-darwin-bootstrap.py` writes an ordinary MAKECONF for Darwin arm64.
It selects explicit initial tools before the final Ports prefix exists.
It needs no tool descriptor, producer receipt, inventory hash, or independent admission.
The current exported Ports profile owns package options, dependencies, and tool handoff.

First export Ports with its normal `scripts/prepare-pkgsrc.py` command.
Read the Ports [Darwin profile](https://github.com/oxtech-ember/EmberBSD-Ports/blob/main/profiles/development-toolchain/darwin-bootstrap.md).
Use the common GCC16 recipe and its complete Darwin patches.

## Explicit inputs

Pass every tool as `--tool ROLE=/absolute/executable`.
Repeated `--tool-root` options name the initial installations that contain these tools.
Tools and their script interpreters must resolve inside those roots and match the host CPU.
Executable aliases retain their invocation spelling.
No missing command is found through the ambient PATH.

Required command roles are `cc`, `cxx`, `cpp`, `make`, `gmake`, `sh`, `as`, `ld`,
`dsymutil`, `ar`, `ranlib`, `nm`, `strip`, `uname`, `pkg_info`, `pkg_admin`,
`pkg_add`, and `pkg_create`.
The selected Ports profile supplies its additional early utility roster.
The result prints these required roles and every selected role.
Later recipes can require more tools. Their normal pkgsrc checks remain active.
Declare those tools explicitly, or select their shared source providers with `--source-tool ROLE`.
Only the profile's missing `gzip`, `gunzip`, and `gzcat` providers default to source builds.

Use repeatable `--cc-arg`, `--cxx-arg`, and `--cpp-arg` options for complete compiler arguments.
Use `--cc-arg=-OPTION` when an argument starts with a dash.
Keep the actual `-B`, specs, native ABI, and deployment arguments in their original order.
`--ldflag` adds a selected link-only argument. It does not change compiler arguments.
The renderer checks executable paths and CPU. It does not query GCC or compile probes.
The caller must select the real native GCC16 commands, including their driver routes.

`--makefiles` selects the complete initial system-makefile tree.
Its `sys.mk` must select the same shell with `.SHELL: name=sh path=/absolute/shell`.
Use the common Ports [make binding](https://github.com/oxtech-ember/EmberBSD-Ports/blob/main/profiles/common-build-tools/bootstrap/README.md).
No historical binding receipt is required. Keep the upstream rules and include tree intact.
The emitted bmake command retains `-m`, `SHELL`, and `MAKECONF`.
The emitted GNU make tuple retains its command-line `SHELL` and `MAKECONF`.

`--macos-sdk` supplies native ABI headers and libraries, including `SDKSettings.plist`
and `usr/lib/libiconv.tbd`. It supplies no build executable.
The renderer reads host OS facts and that SDK version directly.

## Write and use the configuration

Start with the explicit tool selections in shell arguments. For example:

```sh
set -- --tool cc=/absolute/initial/gcc16/bin/gcc \
       --tool cxx=/absolute/initial/gcc16/bin/g++ \
       --tool cpp=/absolute/initial/gcc16/bin/cpp
# Append --tool ROLE=/absolute/path for each remaining required and selected role.
# Append the actual compiler argument words; do not replace a bound command with its first word.

/absolute/control/python3.15 -B scripts/prepare-darwin-bootstrap.py \
  --sdk-source /absolute/EmberBSD-SDK \
  --tool-root /absolute/initial --tool-root /absolute/initial-utilities \
  --pkgsrc /absolute/pkgsrc --makefiles /absolute/initial-mk \
  --macos-sdk /absolute/MacOSX.sdk --deployment 15.0 \
  --prefix /absolute/host-pkg-g16 --pkgdb /absolute/host-pkg-g16/pkgdb \
  --work-root /absolute/bootstrap-work \
  --distfiles /absolute/distfiles --packages /absolute/packages \
  --output /absolute/bootstrap-work/mk.conf \
  --utility-bin /absolute/bootstrap-work/tools --jobs 1 "$@"
```

Create the work directory and output parents first. Paths must exclude whitespace and make metacharacters.
The MAKECONF and utility mapping stay inside the explicit work directory.
The prefix, package database, distfiles, and packages retain their separately selected paths.
The renderer writes only the configuration and utility mapping.
It does not install packages, alter the selected tools, or execute make.
Rerunning with the same mapping replaces the configuration atomically.
A different existing utility map fails without changing it.

The command prints the exact `initial_command` and its environment as JSON data.
This is an invocation description, not a receipt.
Run that command with the returned environment and ordinary process failure handling.
It builds and installs `devel/bmake` through the shared recipe and its dependencies.
The utility mapping contains only explicit selected utility links.
It excludes raw compiler and make names whose plain links would lose command arguments.

After that closure installs bmake, its makefiles, and mksh, use the resulting tools:

```sh
/absolute/host-pkg-g16/bin/bmake \
  -m /absolute/host-pkg-g16/share/mk \
  SHELL=/absolute/host-pkg-g16/bin/mksh \
  MAKECONF=/absolute/bootstrap-work/mk.conf \
  EMBERBSD_DARWIN_BOOTSTRAP=yes -C /absolute/pkgsrc/lang/gcc16 build install
```

Retain the returned utility PATH and selected environment for this command.
The Ports profile owns the later make, shell, and gzip transitions.
The first renderer supports serial builds only. `--tool-phase` forwards the profile's
`initial` or `full` choice; the default is `full`. It does not change package checks.
The normal recipe still performs compiler configuration, build tests, and installation checks.

Once the native Ports tools are installed, use [host prefix selection](native-toolset.md).
Configuration success does not claim a compiler build, runtime check, or complete package graph.
