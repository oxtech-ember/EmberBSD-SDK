# Finite bootstrap make and shell probe

> Retired bootstrap admission interface. This document describes historical data.
> Normal native tools are Ports packages in `host-pkg-llvm`, built with Clang.
> GCC16 is an explicitly selected alternative.
> Use [host prefix selection](native-toolset.md); do not create these acceptance receipts for a normal build.

This probe checks an independently accepted bootstrap make input once.
It does not compile C or C++. It does not qualify an ordinary SDK or execute
package metadata, configure, or the full GCC16 recipe graph.
Use the existing [bootstrap seed](bootstrap-seed.md) admission contract.
Installed binaries retain their [installed-package history](bootstrap-installed-inputs.md).
Their known historical compiler is provenance, not a selected build compiler.

## Fixed Ports binding

`emberbsd_sdk.bootstrap_make` provides `BOOTSTRAP_MAKE_API = 1`.
The optional descriptor `make_binding` has exactly these fields:

| Field | Required value |
| --- | --- |
| `kind` | `ports-bootstrap-make-v1` |
| `ports_source` | Exact descriptor `sources.ports` |
| `template_relative` | `profiles/common-build-tools/bootstrap/sys.mk.in` |
| `template` | Exact `{path, sha256}` reference to the published Ports template |
| `original_inventory` | Reference to the full original 21-file mk inventory |
| `mk_root` | Real absolute in-bundle directory |
| `inventory` | Reference to its exact prepared 22-file inventory |
| `mode_policy` | `read-only` |
| `shell` | Exact selected `sh` spelling |
| `makeconf` | Reference to the explicit configuration file |
| `source_correlation` | Reference to preserved original mk source correlation |

The recognized template SHA256 is
`c8c05581827fa28185b061b1f8228a2c5554b06910aeee628c07d4d94c782b99`.
Only `@QUALIFIED_SH@` is substituted. Preserve every original inventory row.
Move the original `sys.mk` row to `sys-upstream.mk`. Add only the new `sys.mk`.
The explicit `read-only` mode policy clears only the original write bits:
each preserved row has `mode = original_mode & 0555`.
Keep the original inventory unchanged. The new entry has the same read-only
mode as the preserved original `sys.mk`.
The loader compares the complete prepared tree and exact instantiated entry.
It does not copy files or infer historical source-to-binary build provenance.
The independent seed receipt must repeat `make_binding` exactly.

Selected shell, make, configuration, and mk directory paths use the source-owned
ASCII subset `[A-Za-z0-9_./+@%=-]`. Each path is absolute.
The resulting selected tuples are:

- `make`: selected bmake, `-m`, mk directory, `SHELL=selected_sh`, `MAKECONF=configuration`.
- `gmake`: selected GNU make, `SHELL=selected_sh`, `MAKECONF=configuration`.

The Ports entry sets `.SHELL` before it includes the preserved upstream rules.
The explicit CLI `SHELL` also applies to each GNU make recursion.
`MAKECONF` is pinned before any makefile evaluation.
The preserved `bsd.own.mk` reads that variable. It must not reach its fallback
configuration paths. This finite probe supplies a fixture configuration only.
Production Darwin configuration and full recipe admission remain separate work.
The full recipe-plan loader currently refuses a descriptor with this binding.
It cannot accept a bare make invocation that drops the recognized tuple.

## Plan and execution

Use the `emberbsd-bootstrap-make-probe-plan` envelope, version 1,
scope `gcc16-bootstrap`, with purpose `make-and-shell-routing-probe`.
It has the same exact fields and serial resource agreement as the
[C/C++ pilot](bootstrap-pilot.md). Its `required_roles` includes `make`, `gmake`,
and `sh`. All declared tool, driver, ABI, and runtime inputs still pass current
bootstrap input checks. The full recipe graph and output bundle are not needed.

The fixture `makeconf` bytes are exported as `MAKECONF_SOURCE`.
They contain only the SPDX line, the finite-pilot comment, and
`EMBERBSD_BOOTSTRAP_MAKECONF=finite-bootstrap-make-v1`, with a final newline.
The selected shell must be mksh and report the MIRBSD shell family.
The fixed fixture text uses its `print -r --` builtin.
It does not require or infer a `printf` builtin or an external `printf`.
Generate exact commands with
`make_probe_steps(descriptor, selected_commands, abi_flags, work_root)`.
Do not append custom arguments or environment overrides.
The complete environment uses only selected tool directories and explicit
native selectors, ABI flags, `LC_ALL=C`, `TZ=UTC`, `TMPDIR=work_root`, and `MAKECONF`.
The runtime observation budget is at most 120 seconds for all six commands.
Input validation, startup, inspection, and drainage can add wall time.

The six source-owned commands check:

1. Shell builtin output and the selected shell family.
2. Bmake `.SHELL`, `.SYSPATH`, pinned configuration, `!=`, `:sh`, recursion,
   preserved `.c` and `.o` suffixes, and a no-C implicit suffix rule.
3. GNU make selected shell, configuration, and explicit recursion.
4. Shell failure with observed exit 7.
5. Bmake child failure with observed make exit 2.
6. GNU make child failure with observed make exit 2.

GNU make fixture output uses `:; print` to require the selected shell.
The leading no-op prevents direct execution of the shell builtin.
The final `print` command retains its exit status.

The last three commands must fail in the specified way. The controller retains
their exact nonzero exits and required output markers. Unexpected success fails
the probe. The custom implicit rule uses only shell builtins. It does not execute
or qualify the upstream C compiler suffix rules.

Use `scripts/bootstrap-seed.py run-make-probe` with the same six explicit
path/hash selectors and repeated `--required-role` arguments as `run-pilot`.
The Python entry point is `bootstrap_pilot.run_make_probe`.
Both routes use the existing shared-input, exclusive-work, timeout, drainage,
signal, and unproven-drain HOLD mechanism. No command retries are automatic.
Keep all inputs and SDK source immutable until all children drain.
A fresh `work_root` is required. Work receipts remain under its exclusive lease.
The returned result adds `all_leases_closed` only after actual closure.

## Evidence boundary

The result format is `emberbsd-bootstrap-make-probe-result`.
Completion remains `COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW`.
`ordinary_provider_qualified` is always false.
Receipts preserve source fixtures, exact commands, observed exits, combined logs,
the no-C implicit output, and input revalidation after drainage.

`.SHELL` reports the make-selected shell path. Shell-family markers and actual
exits support these finite observations. They are not kernel execution tracing.
This probe does not claim complete nested utility routing or production package
closure. Independently inspect the observations before the next common-recipe
metadata and native GCC16 producer phase.
