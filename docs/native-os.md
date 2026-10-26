# Native tools for OS producers

`scripts/run-native-os.py` runs OS build commands with the installed native
Ports prefix selected by the caller. It reads `tools/native-tools/contract`
from the selected OS source, loads every declared role plus Python, and uses
the existing native OS routing directory. An existing directory is reused
only when its contract and complete set of selected symlinks match.

```sh
"$HOST_PREFIX/bin/python3.15" -B "$SDK_SOURCE/scripts/run-native-os.py" \
  --host-prefix "$HOST_PREFIX" --compiler-family clang \
  --source "$OS_SOURCE" --routing "$OBJECTS/native-routing" \
  --cwd "$OS_SOURCE" --log "$OBJECTS/build.log" --build-sh -- \
  -m evbarm -a aarch64 -T "$TOOLS" -O "$OBJECTS/os" \
  -V "EXTERNAL_TOOLCHAIN=$TARGET_PREFIX" \
  -V EMBER_COMPILER_FAMILY=clang tools
```

The routing parent must already exist. The caller owns its tools and outputs
for the command's lifetime. `--compiler-family` selects the host family; target
family and target inputs remain explicit OS arguments. There is no fallback
to another compiler family or ambient host tools.

`--build-sh` accepts build.sh arguments without the script path. It invokes the
selected shell and records native ABI `HOST_*` flags and host compiler family
through build.sh `-V`, preserving them in generated make wrappers. Caller
arguments cannot override these selected native variables. The environment
contains native routing and `HOST_*` flags, without setting target `CC`, `CXX`,
`CPP`, `CFLAGS`, `CXXFLAGS`, `CPPFLAGS` or `LDFLAGS`.

Use `--shell -- /absolute/nbmake ...` for generated make wrappers. Without a
command mode, the first argument must be an explicit executable path, for
example a selected target preprocessor or an OS-built tool. All modes use the
routing directory followed by the selected prefix's tool directories as PATH.

Select `--log FILE` for build commands; add `--append-log` to continue the
same caller-owned log. A `build.sh` producer must use this logged mode so the
runner can drain its owned process. `--capture` instead forwards exact
stdout/stderr bytes for metadata queries and short explicit commands such as
preprocessor output; it cannot be combined with `--build-sh`. Exit failures
are preserved.
Source checks that must compile native test programs use
[`run-native-check.py`](native-checks.md); their `CC` argument binding must not
be applied to target OS producers.

Python callers use `emberbsd_sdk.native_os`, `NATIVE_OS_API == 1`:
`select_native_os_tools(prefix, source, compiler_family=...)`, then
`run_native_os(native, source=..., routing=..., cwd=..., arguments=[...],
log=..., build_sh=True)`. `shell=True`, `capture=True` and `append=True`
correspond to the command-line modes above; `capture=True` is rejected for
`build_sh=True` because producers require logged process drainage.
