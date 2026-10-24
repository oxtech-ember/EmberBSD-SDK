# First-native GCC16 metadata producer

This interface admits one finite metadata stage with accepted bootstrap inputs.
It runs no build target and issues no provider qualification.
It needs no completed dependency graph or future compiler output.
The existing [seed loader](bootstrap-seed.md) validates current tools and evidence.
The [make binding](bootstrap-make.md) remains mandatory.
Use `BOOTSTRAP_PRODUCER_API = 1` from `emberbsd_sdk.bootstrap_producer`.

## Plan and source chain

The plan format is `emberbsd-native-gcc16-producer-plan`, version 1,
scope `gcc16-bootstrap`. It has exactly `format`, `version`, `scope`, `stage`,
`seed_descriptor`, `host`, `sources`, `work_root`, `required_roles`, `resources`,
`metadata`, and `commands`. Only `stage: metadata` is supported.
This interface does not execute an export or recipe build.
Provider graduation remains a separate check.
The source owner retains the canonical exporter and all recipes in Ports.

`metadata` has exactly these fields:

| Field | Required input |
| --- | --- |
| `export` | Exact `root`, `inventory`, and `source_proof` |
| `profile` | Exact reference to exported `EMBERBSD-DARWIN-BOOTSTRAP-MK.CONF` |
| `makeconf` | Exact reference repeated by the descriptor's make binding |
| `host_facts` | Current `os`, `cpu`, `kernel_version`, `product_version`, `pkgsrc_opsys_version` |
| `utility_bin` | Real in-bundle directory with only accepted utility invocations |
| `native_tools` | Sorted, distinct native utility role names |
| `source_tools` | Sorted, distinct stock Ports source-provider tool names |
| `recipes` | One through 16 distinct `category/package` names, including `lang/gcc16` |

The complete prepared export inventory must match current bytes.
It must contain each requested recipe Makefile.
The recognized profile source is
`profiles/development-toolchain/darwin-bootstrap.mk.conf`.
Its SHA256 is
`3b663ca1e6183f500dcc143c160d916da6088541f8a01b7376983b090d7c8298`.
The exported profile and the accepted Ports source manifest must have those bytes.
All declared sources repeat the seed's exact source pins.

`source_proof` references a data-only JSON object with exactly `format`, `version`,
`ports_source`, `pkgsrc_source`, `ports_manifest`, `exporter`, `export_config`,
and `inventory`. Its format is `emberbsd-ports-export-source-proof`, version 1.
It has no status or executable hook. The independent gate establishes its
source-to-export provenance before it accepts the seed receipt hash.

`ports_source` repeats the selected Ports source. `pkgsrc_source` is its pinned
upstream source. `ports_manifest` references the accepted complete source roster.
Its `upstream/pkgsrc` row has exactly `kind: gitlink` and the upstream `commit`.
Other relevant rows have file `sha256` values.
`exporter` and `export_config` each contain exactly `relative` and `file`.
The latter is a regular `{path, sha256}` reference.
The canonical exporter path is `scripts/prepare-pkgsrc.sh` or
`scripts/prepare-pkgsrc.py`. The config path is `pkgsrc/export.conf`.
Their bytes must match their source-roster hashes.
`inventory` repeats the prepared export inventory reference.
Only complete source and export inventories use the existing Ports 64 MiB limit.
The ordinary metadata size limit remains unchanged.
The SDK parser does not execute the exporter, Git, or other validation hooks.

## Selected configuration and utilities

Use `metadata_makeconf(plan, descriptor, selected_commands, abi_flags)` to prepare
exact configuration bytes before admission. This helper issues no acceptance.
The loader reconstructs and compares those bytes.
Every command tuple and native ABI argument comes from the accepted selection.
Compiler arguments appear once in the selected compiler command.
Link-only arguments appear in `LDFLAGS`.
The file includes only the pinned source-owner profile after its input assignments.
Paths exclude whitespace and make syntax. No arbitrary configuration suffix is accepted.

The host must be Darwin AArch64. The SDK compares actual OS, CPU, kernel release,
and macOS product metadata. It computes the pkgsrc numeric product version.
A host SDK version is not a substitute for the running OS version.

`native_tools` includes the initial 19 utility names exported as `EARLY_TOOLS`.
The required selection also includes `pkg_info`, `pkg_admin`, and every compiler,
make, shell, archive, and debug role used by the configuration.
Each invocation is the exact selected file or a relative in-bundle link to its
selected spelling. Extra PATH entries and members are refused.
Compiler and make tuples cannot be replaced by raw utility aliases.
Native and source tool selections cannot overlap.
A named source provider adds no runnable binary or qualification claim.
Missing active utilities or runtime dependencies still refuse the dependent step.

The metadata stage has fresh work children `obj`, `distfiles`, `packages`,
`prefix`, and `pkgdb`. The configuration binds its isolated local prefix and
database to those paths. It does not use a historical installation prefix.
The resource agreement has the pilot's exact five fields, with integer CPU jobs
from 1 through 4, one linker slot, and runtime observation budget from 1 through
1200 seconds. Metadata runs no links. Future nested builds need their separate
shared heavy-link agreement.

## Execution and evidence

`producer_steps` constructs six fixed queries for each explicit recipe:
`show-vars`, `show-tools`, then `show-depends` for each of `TOOL_DEPENDS`,
`BUILD_DEPENDS`, `DEPENDS`, and `TEST_DEPENDS`.
`VARIABLES` is the fixed variable roster for host, compiler/wrapper/configure
routes, dependency inputs, tools, and consumed makefiles.
Each command starts with the complete selected bmake tuple.
`EMBERBSD_DARWIN_BOOTSTRAP=yes` precedes `-C`. The job count is explicit.
No custom target, argument, or environment suffix is supported.

Use `scripts/bootstrap-seed.py run-producer` with the existing six path/hash
selectors and repeated `--required-role` arguments.
The Python entry point is `bootstrap_pilot.run_producer`.
It reuses the finite controller, shared input/source leases, exclusive work
lease, `run_logged`, deadline observation, and undrained-child HOLD policy.
Input-and-route-only acceptance binds the exact descriptor, stage plan,
commands, tools, native ABI and DSO inputs. It does not assert future execution.

Raw result format is `emberbsd-native-gcc16-producer-result`.
Each attempted command retains its exact actual exit and combined log reference.
Completion remains `COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW`.
All inputs are revalidated after child drainage.
`ordinary_provider_qualified` is always false.
Inspect actual metadata, failures, selected tool routes and the dependency
results before accepting the next build stage. No successful output is invented.
