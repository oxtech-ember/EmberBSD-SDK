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
`metadata`, and `commands`. This page describes `stage: metadata`.
The separate [direct-source program profile](bootstrap-program.md) supports
`stage: grep-bootstrap`, `cmp-bootstrap`, `hostname-bootstrap`,
`basename-bootstrap`, `dirname-bootstrap`, `cat-bootstrap`, `head-bootstrap`,
`uniq-bootstrap`, `wc-bootstrap`, `tr-bootstrap`, or `printf-bootstrap` with its own
finite input fields and commands.
The [fixed initial GNU profiles](bootstrap-gnu-program.md) add
`diffutils-bootstrap` and `findutils-bootstrap` through the same producer entry.
The separate [initial utility relocation profile](bootstrap-relocation.md)
supports `stage: utility-relocation`. The same `run-producer` entry point
dispatches its 42 fixed transforms. It keeps original inputs shared and work
exclusive through per-output checks and the final deterministic comparison.
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
| `recipes` | One through 256 distinct, case-preserved `category/package` names, including `lang/gcc16` |

The complete prepared export inventory must match current bytes.
It must contain each requested recipe Makefile with the exact same case.
Traversal components, duplicate names and names that differ only by case are refused.
The recognized profile source is
`profiles/development-toolchain/darwin-bootstrap.mk.conf`.
The recognized SHA256 values are:

- Original serial configuration: `3b663ca1e6183f500dcc143c160d916da6088541f8a01b7376983b090d7c8298`.
- Parallel link route: `6adbb69112056a867b463fd825a0842294b1eb7e67331433184c1f31d5de518a`.
- Parallel link route with selected-shell builtins: `73209380dcfeeb36abe13f0637a87bc1fd441817c98458ffb92751605c7b543b`.
- Selected-shell route with Darwin libtool linker classification: `ec0354fe315fb02605be2e08299b7495590fc1086d511f67093f4fbb8f4775e0`.
- Darwin libtool route with the selected ranlib in configure: `5b48cba0ea1bd64b883631028d94db1aca574d87f235299a3fc9e768253907db`.
- Ports bmake for every recipe outside the `devel/bmake` closure: `92dc223512de9427030e7a2af85d7673cf658d6f8ba68564a8ca62dd5dfaf4a8`.
- Ports bmake and Ports mksh for every recipe outside that closure: `caef7465f704a40a481eb082d56dc3d5b9434350b9ebd1735c12a4408332f4eb`.
- Builtin helper from `${LOCALBASE}/libexec`, installed by `pkgtools/bootstrap-mk-files`, for those recipes: `d601e1332d820b922c43cbf19b9150ad3527fd00eea6d67615850960ed320833`.
- Native trust store from the common CA package under `${LOCALBASE}/etc`, with the same native-provider routes: `3366a715faa4c6271d3020f484b969d552164cdfe23bf8dc08e266234d82501f`.

- Metadata keeps the initial tools; full builds reapply the Ports handoff on each MAKECONF read: `87b856b28ff8757bd9c3c62cefaeb032d95be12da79e4fff7415b6b7b8a4a9b9`.
- The same metadata/full handoff with the native CA store under `${LOCALBASE}/etc`: `06009d0687d0553bcb2de546415caf185b9bdff372006df9147b1e92bc8a9052`.

`bootstrap_producer.py` lists the native-provider revisions between these two.

The exported profile and accepted Ports source manifest must match the same revision.
All parallel revisions receive the CPU budget and launcher assignments.
Job and linker assignments remain scoped to parallel profiles.
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
For a complete bound-recipe plan, initialize these arguments only in
the pkgsrc `BSD_PKG_MK` context. A nested BSD make inherits the complete
pkgsrc `LDFLAGS`, including dependency library paths. Reading its `MAKECONF`
must not replace those flags. Metadata-only configuration bytes stay unchanged.
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
The GCC configure diagnostic selector `EMBERBSD_GCC16_UNAME` uses that same
selected native `uname` role. It accepts one executable path without arguments.
The common Ports recipe forwards it to its explicit SDK diagnostic mode.
The full-plan environment retains that mode for direct make-driven reconfiguration.
A named source provider adds no runnable binary or qualification claim.
Missing active utilities or runtime dependencies still refuse the dependent step.
Some recipe builds run a bare command that pkgsrc does not create as a tool.
A closure with `devel/ninja-build` needs a native `od` role, because the
ninja bootstrap runs `od`. A closure with `devel/meson` needs a native `lipo`
role, because meson's Darwin compiler checks run `lipo -info`. Select the
in-bundle coreutils `od` and LLVM `llvm-lipo`. Each role must be in
`native_tools`, `required_roles` and the descriptor; otherwise the plan is
refused before any child runs. `RECIPE_UTILITIES` exports this map.

The metadata stage has fresh work children `obj`, `distfiles`, `packages`,
`prefix`, and `pkgdb`. The configuration binds its isolated local prefix and
database to those paths. It does not use a historical installation prefix.
The resource agreement has the pilot's exact five fields, with integer CPU jobs
from 1 through 4, one linker slot, and runtime observation budget from 1 through
1200 seconds. Metadata runs no links. The separate
[complete bound-make plan](bootstrap-seed.md#complete-plan-with-the-fixed-make-binding)
adds recipe execution and its shared heavy-link agreement.

## Execution and evidence

`producer_steps` constructs six fixed queries for each explicit recipe:
repeated `-V NAME=${NAME}` expressions for the fixed variable roster,
`show-tools`, then `show-depends` for each of `TOOL_DEPENDS`,
`BUILD_DEPENDS`, `DEPENDS`, and `TEST_DEPENDS`.
`VARIABLES` is the fixed variable roster for host, compiler/wrapper/configure
routes, dependency inputs, tools, and consumed makefiles. It also includes
`WRKDIR`, `WRKSRC`, `OBJDIR`, `CONFIGURE_DIRS`, `BUILD_DIRS`, `MAKE_DIRS`,
`DISTFILES`, `EXTRACT_ONLY`, `MASTER_SITES`, `DIST_SUBDIR`, `PATCHFILES`, and `PATCH_SITES`.
`MACHINE_GNU_PLATFORM` and `GCC_TARGET_MACHINE` pin the output compiler triplet.
The current 56-name roster also observes `BOOTSTRAP_DEPENDS` directly. Keep
these prerequisites in the complete build graph. The common `show-depends`
target does not support that variable; do not use it as an extra query target.
These exact fields support the later object-directory and distfile plan.
The caller cannot add an arbitrary variable query.
The variables command has no target. bmake expands each expression directly.
It does not use recipe echo commands or job-output blank-line filtering.
Use `metadata_values(log_reference)` to read a pinned variables log.
The helper accepts exactly the current 56-name roster or the previous fixed
55-name `LEGACY_VARIABLES` roster. It requires all names once, in their order.
It returns only observed keys. A legacy record has no `BOOTSTRAP_DEPENDS` key;
the helper does not invent an empty value. A complete new dependency graph
must require that observation. Keep each old raw log linked to its accepted
command, result and exact SDK source before using it as local-source proof.
It partitions each line at the first `=` and preserves the remaining value.
It does not execute shell code or remove quoting and backslashes.
Diagnostics, missing fields, extra lines, embedded newlines, CR and NUL are refused.
An empty value does not distinguish an undefined make variable from a defined empty variable.
The helper reads observations. It does not issue qualification or accept a dependency graph.
New producer plans always use the 56-name command. Replay a historical
55-name producer plan with its exact retained SDK source. New-source plan
validation refuses the old command tuple. Reader compatibility does not
admit an old live query or relabel old observations as a complete build graph.
Each recognized parallel Ports profile also needs `EMBERBSD_DARWIN_CPU_BUDGET`.
SDK sets it to the accepted job count. Metadata with that profile uses one job
and an empty linker prefix. A parallel recipe build needs the separate full-plan
`link_control` service.
Metadata MAKECONF sets `EMBERBSD_DARWIN_METADATA=yes` before the Ports profile include.
The flag marks graph observation before the final prefix exists.
SDK emits it only for the producer format with `stage: metadata`.
Full bootstrap plans and other stages omit it.
Older recognized profiles may ignore the phase flag.
The existing command-line `MAKE` and post-profile `MAKE`/`_MAKE` bindings remain.
The Ports metadata policy must preserve source-owned dependency edges.
It must keep the selected initial make, shell and helpers during observation.
This flag does not admit a build or qualify a provider.
Each command starts with the complete selected bmake tuple.
Each read-only metadata query also sets command-line `MAKE` to that full tuple.
Nested `show-var` queries must use the initial make before the recipe closure exists.
Metadata MAKECONF restores both `_MAKE` and `MAKE` after the profile include.
Parse-time queries can run before bmake exports its command-line overrides in `MAKEFLAGS`.
This restoration applies only to the metadata stage of the producer format.
This override keeps the source-owned `BOOTSTRAP_DEPENDS` edges.
Complete recipe builds keep the separate Ports make handoff.
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

## Common native GCC tool providers

The native-provider Darwin profile requires the common `lang/llvm` and `devel/lld`
recipes in a full GCC plan. Their installed tools supply GCC's configured defaults.
CMake recipe metadata also requires explicit native `libtool_darwin`,
`install_name_tool` and `llvm_readtapi` roles in `required_roles`.
SDK emits their existing Ports selectors and fixes the TAPI kind to `llvm-readtapi`.
These roles do not replace the raw `libtool` utility or create ordinary providers.

With `link_control`, SDK emits the same resource command as a CMake command list.
Ports forwards it to C/C++ final links, including nested `try_compile` work.
The source profile adds static GCC runtime link options separately from ABI flags.
Its default-runpath variant also adds the supported GCC `-nodefaultrpaths` option.
This suppresses seed compiler-library runpaths without changing explicit package paths.
Historical profile hashes retain their previous MAKECONF contents.
The unprivileged native-provider profile is
`34648560a0c6fd66f74b134d6a832ff28a7a1bc74ba06cf554a18bbf6203a8a8`.
It adds the standard pkgsrc `UNPRIVILEGED=yes` policy for the owned output prefix
and package database. The common rules use the selected shell and current user
for installation. SDK command construction and native-provider guards stay the
same. The earlier native-provider and default-runpath profile hashes remain valid
for their pinned historical inputs.
Native execution, source-built provider receipts and full graduation remain required.
