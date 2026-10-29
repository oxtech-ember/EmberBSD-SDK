# Run native source checks

Use `scripts/run-native-check.py` for owner checks that compile host fixtures.
The selected native prefix supplies compilers, interpreters and other build
tools. The check script remains in its owning repository.

```sh
"$HOST_PREFIX/bin/python3.15" "$SDK_SOURCE/scripts/run-native-check.py" \
  --host-prefix "$HOST_PREFIX" --compiler-family clang \
  --required-role clang --required-role cppcheck --required-role ruby \
  --tool sh --cwd "$OS_SOURCE" \
  --bindings "$CHECK_WORK/native-commands" --log "$CHECK_WORK/check.log" \
  --cflag=-O1 --cflag=-fsanitize=address,undefined \
  -- owner/check.sh
```

Create the check-work parent first. `--bindings` is a generated command
directory, not a qualification record. An existing directory is reused only
when its command arguments and selected tools match; a different configuration
uses another directory. The log records the executed check's output. Failures
retain the actual command status, and the existing SDK process backend drains
owned descendants. The caller keeps selected tools and inputs stable until
the check finishes.

The default compiler family is Clang; `--compiler-family gcc` explicitly
selects the GCC alternative. The baseline roles are `cc`, `cxx`, `cpp`, `sh`
and `python`; request other roles used by the check with `--required-role`.
`--tool` selects the entry-point role. Arguments after `--` go to that tool
unchanged. No target compiler or application runtime is selected by this command.

`CC`, `CXX` and `CPP` name executable adapters. They bind the selected compiler's
exact native ABI argument words and explicit extra flags. A source contract
that ignores standard flag variables still receives those arguments. Per-test
arguments, including spaces and quotes inside a word, are preserved without
shell evaluation. Link flags follow input arguments and are omitted for
compile-only, preprocess, syntax-only and analyzer operations. POSIX compiler
response files are inspected for these operation switches and passed unchanged;
cyclic response files, more than 32 nested files and files over 1 MiB are refused.
Link commands with a `--` option terminator and bound linker flags are refused,
including when the terminator is in a response file: appending flags after it
would turn them into filenames, while moving libraries earlier can change
resolution. Spell a leading-dash input as `./-filename` instead.
Compiler identity queries do not receive build flags. Native Python's shebang
path must contain no whitespace; source, binding and argument paths may contain
spaces when the calling script quotes its executable variables.

`--cppflag`, `--cflag`, `--cxxflag` and `--ldflag` each add one argument; repeat
them as needed. Standard flag environment variables are empty inside the
check because the adapters already bind their values. This avoids double
addition by contracts that honor them. An ambient target `CC`, `SDKROOT`,
include path, library path or Python import path is discarded.

`PATH` contains the generated command directory followed by the selected
prefix directories. Command aliases such as `sh`, `make`, `python3` and `c++`
resolve only to selected tools or the bound compiler adapter. The selected
native Python is also available as `PYTHON`, and the shell as `SHELL` and
`CONFIG_SHELL`. Requested archive, make and interpreter roles populate their
standard tool variables. Requesting `clang`, `cppcheck` or `valgrind` additionally
sets `CSA`, `CPPCHECK` or `VALGRIND`. `CSA` binds native C flags and never adds
link flags; an analyzer caller can add its own C++ language options explicitly.

The Python API is `emberbsd_sdk.native_checks`, with `NATIVE_CHECKS_API == 1`:

```python
environment = native_checks.check_environment(
    native, bindings=command_directory, extra_flags={"cflags": ["-O1"]})
native_checks.run_check(
    native, "python", ["-B", owner_test], cwd=owner_source, log=log_file,
    bindings=command_directory, extra_flags={"cflags": ["-O1"]})
```

`native` comes from [host prefix selection](native-toolset.md). This API does
not replace the source owner's checks, provide target execution evidence or
make an untested host supported. Tests of argv/environment and process cleanup
are in `test_native_checks.py` and `test_compiler_bindings.py`.

The underlying `compiler_bindings.bind_compilers` also accepts optional
`link_flags={"cc": [...], "cxx": [...]}` for language-specific link options.
These follow common LDFLAGS only during the corresponding link operation.
Native checks need no extra map. [Application commands](application-toolchain.md)
use it to select libc++ at C++ link without passing that option to compilation.
Optional `runtime_rpath={"flags": [...], "libraries": [...]}` names a runtime
search path that stays only in link outputs that need one of the named
libraries; see application commands.

For an explicitly selected Darwin native C++ runtime,
`compiler_bindings.bind_compilers` accepts `native_cxx_runtime` with canonical
`include_dir`, `library_dir`, `sysroot`, exact allowed `targets`, and `deployment`
(the runtime's minimum macOS version, for example `26.0`). The caller selects
these inputs together from the same runtime; this API does not discover them.
`NATIVE_CXX_RUNTIME_BINDING_API == 1` advertises this optional capability.
The binding uses record format 2, whose command objects are rejected by older
readers; existing format 1 records retain their previous semantics.

The C++ binding selects both runtime paths, preserves explicit standard-library
and no-standard-include options, and rejects incompatible target, architecture,
sysroot, standard-header directory, and lower deployment overrides. If no
macOS deployment option is supplied, it sets the selected minimum explicitly.
Response-file options are inspected too. Target builds use their own application
binding; this native selection is not a target sysroot or target runtime.

The same explicit `native_cxx_runtime` selection can be passed to
`check_environment` or `run_check`; their existing calls without it are
unchanged. The Darwin Ports configuration accepts the same selection with a
caller-owned binding directory and selected native Python. Its CLI exposes
`--native-cxx-include`, `--native-cxx-library`, repeatable
`--native-cxx-target`, `--native-cxx-deployment`, `--native-cxx-bindings`,
and `--native-cxx-python`; supply them together. The selected macOS SDK is the
runtime sysroot. The first explicitly selected native target is bound as the C++
default, and later caller targets are still checked. The MAKECONF exposes its
`commands.json` as `EMBERBSD_NATIVE_CXX_BINDING_RECORD` for consumers that need
the same full argv. Only the C++ compiler is
routed through the adapter; initial
bootstrap and target compiler selections remain separate.

For a Darwin runtime that re-exports the platform C++ ABI, select
`platform_cxxabi=True` in `native_cxx_runtime` (or pass
`--native-cxx-platform-abi` to the Darwin MAKECONF CLI). The SDK creates an
owned include view with only `cxxabi.h` and `__cxxabi_config.h` symlinks from
the selected macOS SDK. Its paths and content hashes are part of the binding
record; it is searched after the selected libc++ headers. This uses native
binding record format 3, which earlier SDK readers reject; format 1 and 2
remain supported. `NATIVE_CXX_PLATFORM_ABI_API == 1` advertises the option.
The public `validate_native_compiler_context(record)` API, advertised by
`NATIVE_COMPILER_CONTEXT_API == 1`, checks that CC, CXX and CPP explicitly
select the same macOS SDK and deployment without duplicating their flags.

For a namespace-specific native runtime, also select `abi_identity` with its
`namespace` and the immediate `linker_target` of `libc++.dylib`. The SDK checks
these against the selected `__config_site` and the library symlink chain; the
producer validates the dylib's actual install name. This uses binding format 4,
which older readers reject. `NATIVE_CXX_ABI_IDENTITY_API == 1` advertises it.
The Darwin MAKECONF CLI accepts the same values as `--native-cxx-abi-namespace`
and `--native-cxx-linker-target` together with `--native-cxx-platform-abi`.
Choose a new binding directory when the ABI changes so compile commands and
their build-system caches are invalidated. Existing format 2 and 3 bindings reject a
selected config that explicitly names a namespace other than `__1`.

For a native replacement stage, `prepare-darwin-stage.py` writes a separate
pkgsrc MAKECONF from an existing Darwin base configuration and an explicit v4
binding for that stage. Pass `--stage-root`, the final `--prefix`, distinct
`--object-root` and `--packages` directories, and repeat `--buildlink` for
providers that must resolve from the stage. Select the binding's host Python
with `--python`; the SDK verifies the existing adapters. The overlay selects the stage
package database and binding after including the base; selected compiler
executables must belong to the host prefix. Other providers and host tools retain
the base configuration. Missing dependencies fail instead of recursively installing
into the active prefix; prepare them in the stage before retrying. The helper
does not install packages or select a runtime loader environment.
