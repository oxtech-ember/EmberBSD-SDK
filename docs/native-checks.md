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
default, and later caller targets are still checked. Only the C++ compiler is
routed through the adapter; initial
bootstrap and target compiler selections remain separate.
