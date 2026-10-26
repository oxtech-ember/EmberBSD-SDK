# First native GCC16 bootstrap

> Retired bootstrap admission interface. This document describes historical data.
> Normal native tools are Ports packages in `host-pkg-llvm`, built with Clang.
> GCC16 is an explicitly selected alternative.
> Use [host prefix selection](native-toolset.md); do not create these acceptance receipts for a normal build.

This interface admits a seed for one first-native-GCC16 plan.
It does not admit that seed for ordinary builds or issue acceptance.
The resulting GCC16 still needs [NativeToolset qualification](native-toolset.md).
For a host that already has accepted GCC16 but no initial make tool, use the
separate [first bmake bootstrap contract](bootstrap-bmake-seed.md).
That route requires its own full-prefix audit and independently issued
compiler-seed receipt; the original record's self-hash alone is insufficient.
The [finite first C/C++ pilot](bootstrap-pilot.md) can run before the full
recipe closure is available. It needs independent input and route acceptance.
The separate [finite make and shell probe](bootstrap-make.md) checks the accepted
make binding before package metadata or recipe execution.
The fixed [NetBSD 11 first-native GCC16 route](bootstrap-netbsd-gcc16.md) uses
the same plan and graduation envelope with independently accepted pkgsrc tools
and a target-side source-transfer receipt. A `bmake-bootstrap` compiler record
is not a complete GCC16 seed and cannot be relabeled for this route.
The [stage-aware metadata producer](bootstrap-producer.md) can then query a
prepared canonical export. It needs no future build output or accepted DAG.
The [direct-source grep profile](bootstrap-program.md) can provide the missing
initial utility from pinned OS sources. It does not need a package build graph.
Use common `lang/gcc16` and its named dependencies in a pinned prepared Ports export.
Keep recipes and patches in Ports. SDK owns orchestration and evidence checks.

## Admission and execution

Obtain the acceptance hash from an independent gate.
Do not trust a hash that an untrusted bundle supplies for itself.
A file digest or version string does not establish acceptance.
Inspect the seed binary, source, patch, native ABI, driver, and DSO closure.
Explicitly accept native assembler and linker roles.
External historical compiler text is provenance. It never selects a new process.
This contract does not automatically approve an external package manager or SDK.
Do not compile with an unaccepted seed.

```python
from emberbsd_sdk.bootstrap_seed import BOOTSTRAP_SEED_API, load_bootstrap_seed

assert BOOTSTRAP_SEED_API == 1
seed = load_bootstrap_seed(
    "/absolute/evidence/seed.json",
    descriptor_sha256=descriptor_hash,
    accepted_seed_receipt={"path": acceptance_path, "sha256": accepted_hash},
    bootstrap_plan={"path": plan_path, "sha256": accepted_plan_hash},
    required_roles=("cc", "cxx", "sh", "make"),
)
cc_prefix = seed.command("cc")
commands = seed.as_dict()["plan"]["commands"]
```

The API constant is 1. `BootstrapSeed` is immutable.
`command(role)` returns `(selected, "-B" + driver_prefix + "/")` for `cc`, `cxx`, and `cpp`.
Other roles return `(selected,)`. `abi_flags` has the NativeToolset four-array interface.
`as_dict()` returns fresh JSON data. Missing roles raise `SDKError`.
No general driver options or response files are accepted.
The fixed Ports binding below is the only supported specs selector.
Use the same tuple for metadata queries and compiler commands.
All seven driver members, including `lto-wrapper`, must be inside the pinned prefix.
Bare, default-search, and external driver selections fail closed.

`scripts/bootstrap-seed.py check` requires these explicit options:
`--descriptor`, `--descriptor-sha256`, `--accepted-seed-receipt`,
`--accepted-seed-receipt-sha256`, `--bootstrap-plan`, `--bootstrap-plan-sha256`,
and repeated `--required-role`.
It emits selection JSON. It does not execute the plan.
The Python accessor also runs no tools, probes, downloads, or installs.

An execution controller must use SDK process and lease interfaces.
It must execute exact plan `argv`, `cwd`, and complete `env` without ambient additions.
Keep selected inputs immutable under read leases through command drain.
Preserve actual child results on failure. Never infer successful child exit from a plan.
The check command does not execute commands. `run-pilot` and `run-make-probe`
provide their finite controllers. `run-bootstrap` executes only the complete
fixed bound-make plan described below. It cannot execute the legacy unbound route.

### Fixed Darwin seed bindings

An optional `driver_binding` recognizes two fixed Ports bindings on Darwin arm64.
The original binding is `ports-gcc16-darwin-llvm-mc-seed-v1`.
It has exactly `kind`, `ports_source`, `template_relative`, `template`,
`instantiated`, `deployment`, and `reported_programs`.
`ports_source` must equal `sources.ports`.
The template-relative path is
`profiles/development-toolchain/bootstrap/darwin-llvm-mc-seed.specs.in`.
The exact published template SHA256 is
`f6cd27cdc2bf3926de2816e6aaf2dc964e744d7cd42da298f2b073b3fb04d929`.
`template` and `instantiated` are exact file references.
The in-bundle instantiation replaces only `@DEPLOYMENT@` with `26.0`.
Its SHA256 is `c8e99fe7a77bd22b1e91af6f1bffa3d6a0dd2636c0ae9173e2ece2fdb9ced663`.
`deployment` is exactly `26.0`.
The compiler tuple becomes `(selected, -Bprefix/, -specs=instantiated,
-mmacosx-version-min=26.0)` before the separate qualified ABI flags.
All metadata queries must use the complete same tuple.
The Ports template owns assembler arguments and the fixed linker selector.
The SDK does not copy or reinterpret a generic specs file.

`tools.ld` must select `driver_prefix/ld64.lld` for Mach-O execution.
`reported_programs` is exactly `{ld: driver_prefix/ld}`.
Only the fixed seed binding permits this nominal query spelling.
It never changes the selected execution role or `LD` binding.
Do not use canonical inode equality as linker flavor evidence.
An actual pilot must preserve the observed collect2 and `ld64.lld` route.
Ordinary NativeToolset driver-query checks remain unchanged.

`ports-gcc16-darwin-llvm-mc-seed-v2` adds the source-owned Mach-O platform
version selector for LLD. It keeps the same compiler tuple and nominal query
mapping. Its template path is
`profiles/development-toolchain/bootstrap/darwin-llvm-mc-lld-seed.specs.in`.
The exact template SHA256 is
`ef055eaac77dcb506165e366a984839820edc900c8e39bff5ce1e797945bd15b`.
Only `@DEPLOYMENT@` to `26.0` and `@SDK_VERSION@` to the installed macOS SDK
version are substituted. Each supported SDK version has one recorded exact
instantiation SHA256:

| SDK version | Instantiation SHA256 |
| --- | --- |
| `27.0` (current) | `3ca395b659b167215ba5dbf5eb6f0ef75641bf1e791d21e9c47b7b9f33b23786` |
| `26.5` | `bc8bda2a69069af27154c7da4ba3fa711136558a86a6076f012edf82544c64a2` |

Use the current SDK that is installed on the host. To support a new SDK, add
its version and instantiation hash to this table and the SDK code.
An SDK version that has no recorded row fails closed.
The Ports template appends the modern platform option to the existing link
spec. It does not replace collect2 or the upstream link command.

The v2 binding has two additional required fields: `sdk_version`, a recorded
version string such as `27.0`, and `sdk_metadata`, an exact file reference.
The reference path must equal `SDKSettings.json` in exactly one declared
`host_abi.headers` root. Its SHA256 must equal that root's pinned inventory row.
Current metadata must have `Version` equal to `sdk_version`,
`CanonicalName` equal to `macosx` followed by that version,
`DefaultVariant: macos`, and
`SupportedTargets.macosx.BuildVersionPlatformID: 1`, all strings.
Compiler defaults in that ABI metadata are data. They do not select a tool.
Historical graduation checks keep these identities and the small ABI inventory.
They do not require a retired SDK payload. The live seed admission checks do.
The original v1 fields and hashes remain supported without this extension.

## Seed metadata

The envelope is `emberbsd-bootstrap-seed`, version 1, scope `gcc16-bootstrap`.
Use NativeToolset fields `bundle_root`, `host`, `compiler`, `sources`,
`inventory`, `tools`, `driver_components`, `host_abi`, and `abi_flags`.
Their current-byte, native-header, interpreter, and ABI checks use the same validators.
The compiler is native GCC16. Every member stays in one provisioned bundle.
Do not use `/` as a collection root or external links to hide undeclared tools.
Provisioning copies need preserved origins and acceptance of their new layout.

Add the following fields:

| Field | Required value |
| --- | --- |
| `driver_prefix` | Absolute real in-bundle directory containing every selected GCC driver member. |
| `providers` | Named seed origins. Each has explicit `origin: source-build`, `origin: binary-release`, or scoped `origin: installed-package`. |
| `history` | One provenance record for every provider, with the same keys. |
| `dynamic_dependencies` | Complete recorded executable and DSO graph. |

Each history record has `kind` equal to its provider's `origin`.
`source_archives` and `patches` are explicit ordered arrays.
Each entry has exactly `url` and `file`. `file` is the common `{path, sha256}` reference.
The loader hashes large archives as streams.
For `source-build`, require nonempty source archives and `build_record`.
The provider has the NativeToolset `source`, `recipe`, and `recipe_sha256` fields.
The record references `emberbsd-bootstrap-seed-history`, version 1,
scope `gcc16-bootstrap`, with nonempty `historical_compiler` and a `build_log` reference.
The independent gate checks the origin and completeness of that history.

OS direct-source programs can instead use the strict
[Git-program history](bootstrap-program-history.md) variant.
It binds the actual source proof, finite production and independent output
acceptance with explicit copied-member hashes. Only that variant permits empty
source archives for a `source-build` origin. It issues no qualification.

A `binary-release` history has `release_url`, `release_version`, exact `artifact`
file reference, original `release_metadata` reference, and `embedded_configuration`
(a file reference or null).
It also has `source_commit` (a source pin or null), `historical_build_log`
(file reference or null), `historical_compiler` (text or null), boolean `patches_complete`,
and `publisher_signature` (null or `{file, verification}`).
Signature verification references a JSON record with actual integer exit 0 and
`stdout` and `stderr` references. Independent acceptance checks its actual verifier and meaning.
These records do not select the historical compiler or signature verifier for a build.

The `unknowns` array must list every missing historical input, in sorted order:
`source-archives` for an empty archive list, `source-commit` for a null commit,
`embedded-configuration` when absent, `historical-build-log`, `historical-compiler`,
`historical-patches` when completeness is false,
and `publisher-signature` when absent.
An independent gate must explicitly accept this exact named uncertainty list.
Do not invent unavailable build logs or exact source commits.
This variant pins a binary input release. It does not claim its historical build was qualified-native.
Unknown current tool, driver, native ABI, or DSO inputs are not permitted by this variant.
The new GCC16 producer still requires complete common recipe, source, and patch pins.

Existing installed make and shell payloads use the separate
[installed-package history](bootstrap-installed-inputs.md) variant.
It binds original package metadata and provisioned member SHA256 values.
It does not invent a release archive or historical build log.

`dynamic_dependencies` maps canonical absolute executable or DSO paths to
`{sha256, dependencies: [absolute paths]}`.
Every native tool has an entry. Every edge names another entry.
Nodes stay in the bundle or declared native ABI library roots.
Non-tool nodes need a native shared-library header: thin Mach-O64 type 6 or ELF64 type 3.
The loader rejects missing nodes and changed bytes.
The independent audit must establish graph completeness.
This module does not infer loader searches from headers.
Explicit Darwin shared-cache rows use the [runtime audit contract](darwin-runtime.md).
That branch reconstructs the graph from the pinned runtime bytes.
It does not use a missing-file fallback. Other unsupported formats fail closed.

## Exact common-recipe plan

The envelope is `emberbsd-native-gcc16-bootstrap-plan`, version 1, scope `gcc16-bootstrap`.

| Field | Required value |
| --- | --- |
| `purpose` | Exactly `first-native-gcc16`. |
| `seed_descriptor` | Exact seed descriptor reference. |
| `host`, `sources` | Exact seed host and source pins, including SDK and Ports. |
| `output_compiler` | Expected native GCC16 identity. |
| `ports_export` | `root`, `source` equal to the Ports pin, complete `inventory` reference, and `source_proof` reference. |
| `recipes` | Exactly `lang/gcc16` and its finite named dependency closure. |
| `work_root`, `output_bundle_root` | Exact absolute object and installed output paths. |
| `required_roles` | Explicit nonempty role list for the whole recipe closure. |
| `commands` | Ordered command specifications. |
| `comparison_command` | ID of the explicit GCC `compare` command. |

Each recipe key is its source-relative directory.
Each row has `directory` equal to that key, a complete subtree `inventory` reference,
ordered `patches`, nonempty `distfiles`, and `dependencies`.
The bound-make local-source case below has one explicit exception.
The subtree must equal the corresponding complete export subtree.
List every regular `patches/patch-*` file once.
Each distfile has `url` and a common `file` reference.
Cycles and unrelated recipes fail closed.
Independent acceptance must confirm the dependency list against common recipes.
The SDK does not duplicate recipe dependency resolution.

Each command has unique `id`, `recipe`, `phase`, `role`, `argv`, `cwd`, and `env`.
`phase` is `build`, `compare`, or `install`. `role` is selected `make` or `gmake`.
Build and install commands start with selected make and `-C <export/recipe>`.
Additional `-C`, directory changes, and makefile replacement options are refused.
GNU make eval options and their long-option abbreviations are refused.
Short-option clusters that contain `f`, `C`, or `E` are refused too.
Each recipe builds after its dependencies.
GCC comparison follows all builds. GCC installation follows comparison.
The comparison command belongs to `lang/gcc16` and includes the `compare` target.
Command directories stay in the export or declared work root.

`env` binds SDK-only `PATH`, `CC`, `CXX`, `CPP`, `AS`, `LD`, `HOST_CC`, `HOST_CXX`,
`HOST_SH`, `SHELL`, and `CONFIG_SHELL` to exact selected commands.
It binds `AR`, `RANLIB`, `NM`, `STRIP`, and `PYTHON` when declared.
It binds native recipe flags and `HOST_*` flags to the qualified ABI arrays.
These are native recipes. Do not introduce target flags or target sysroot inputs.
PATH directories with the platform separator are refused.
Make arguments cannot replace these bindings. Assignment names must use literal
ASCII letters, digits, underscores, dots or hyphens. Whitespace and variable
expansion in assignment names are refused. Long options such as `--jobs=2`
are not variable assignments.
Optional environment keys are `DISTDIR`, `WRKOBJDIR`, `PACKAGES`, `PKG_DBDIR`,
`LOCALBASE`, `PREFIX`, `PKGSRC_COMPILER`, `GCC_REQD`, `OSX_SDK_PATH`,
`TZ`, `LC_ALL`, `LANG`, and `TMPDIR`. `PKGSRC_COMPILER` must be `gcc`.
Actual execution audits must establish that nested recipe commands obey these routes.

For a Darwin common-recipe plan with a fixed driver binding, use v2.
Its pinned `SDKSettings.json` selects the exact `OSX_SDK_PATH` header root.
The original v1 binding remains available to the finite pilot. It cannot
infer an SDK root for a full recipe plan.
The plan must also bind these exact Ports recipe environment values:

| Variable | Selected value |
| --- | --- |
| `EMBERBSD_GCC16_SEED_CC`, `EMBERBSD_GCC16_SEED_CXX`, `EMBERBSD_GCC16_SEED_CPP` | Shell-quoted complete seed command for `cc`, `cxx`, or `cpp` |
| `EMBERBSD_GCC16_LLVM_MC`, `EMBERBSD_GCC16_LD64_LLD` | Selected `as` and `ld` executable paths |
| `EMBERBSD_GCC16_DSYMUTIL` | Selected `dsymutil` executable path |
| `EMBERBSD_GCC16_DARWIN_SDK_VERSION` | The fixed binding `sdk_version` |
| `EMBERBSD_GCC16_AR`, `EMBERBSD_GCC16_RANLIB`, `EMBERBSD_GCC16_NM`, `EMBERBSD_GCC16_STRIP` | Corresponding selected executable paths |

All these roles must exist in the descriptor. Missing or conflicting
selectors fail before a recipe can run. Direct make assignments must match
the same values. Conditional, append, immediate and shell assignments are
refused for these selectors. ABI flags remain in their separate arrays.
Unbound and Linux plans keep their existing environment contract.

### Complete plan with the fixed make binding

The Darwin v2 driver binding can use `ports-bootstrap-make-v1` for the full plan.
Keep the same full-plan format, recipe inventories, distfiles and comparison.
Add only `metadata` and `resources` to the full-plan fields above.
The `metadata` object uses the [metadata producer inputs](bootstrap-producer.md).
Its `recipes` list must be the sorted complete recipe closure, with at most 256
recipes. `ports_export` must name the exact same export and source proof.
The source-owned profile, current host facts, complete export inventory,
utility-only PATH and exact generated MAKECONF are checked before execution.
The full plan also requires native `pkg_add` and `pkg_create` roles in
`metadata.native_tools` and `required_roles`. Each selected command must have
one invocation path. MAKECONF sets `NATIVE_PKG_ADD_CMD` and
`NATIVE_PKG_CREATE_CMD` to those exact paths before the first staged install.
The common Ports package rules consume these selectors. They must not use
the future output prefix as a fallback. Metadata-only plans keep their
existing role roster and MAKECONF fields. This check does not qualify a tool.

Set `output_bundle_root` to `work_root/prefix`. All writable package paths come
from `metadata_paths(work_root)`. Each distfile also needs `relative`, its explicit
destination below `work_root/distfiles`, including any `DIST_SUBDIR` component.
Repeated destinations must have the same SHA256. Traversal and overlapping file
and directory destinations are refused. The controller copies only these pinned
files. It does not download missing inputs. Work cannot overlap a declared input.

Eleven local-source recipes can use `distfiles: []`: `archivers/bzip2`, `archivers/libarchive`,
`archivers/pax`, `devel/nbpatch`, `net/fetch`, `net/libfetch`,
`pkgtools/cwrappers`, `pkgtools/digest`,
`pkgtools/libnbcompat`, `pkgtools/mktools`, and `shells/mksh`.
Only this empty bound-make case accepts a `distfile_metadata` file reference
in its recipe row. It pins the raw named-variable log from an accepted metadata
command. The existing `metadata_values()` parser requires the complete fixed
roster. `PKGPATH` must equal this recipe. `DISTFILES` and `PATCHFILES` must be empty.
A positional, incomplete, changed, or diagnostic-bearing log is refused.
The complete recipe inventory must still equal its export subtree.
The SDK requires the local Makefile and the recognized regular source files.
The digest, libnbcompat, libarchive, pax, and fetch recipes also need
`files/configure` and `files/Makefile.in`. The libarchive recipe also requires
`Makefile.common`, `files/libarchive/archive_read.c`, and `files/libarchive/archive.h`.
The bzip2 and nbpatch recipes need their local Makefiles
and named C sources. The mksh recipe needs `files/Build.sh`, `files/main.c`,
and `files/sh.h`. The libfetch recipe needs `files/Makefile`, `files/common.c`,
`files/fetch.c`, and `files/fetch.h`. The C-wrapper and mktools recipes keep
their BSD Makefiles and C sources. Each complete subtree and every carried
patch remain bound;
the required file list does not replace that complete inventory.
No archive is invented or copied for these recipes. All common patches remain required.
Other empty recipes, legacy unbound plans, and this extra field on nonempty rows are refused.

Before execution, the independent caller must match each log reference to the
successful variables command in the actual metadata result. Check that result's
plan, source export, current input revalidation and drainage evidence.
Record this link in the existing independent input-route audit and accept the
exact full-plan hash. The SDK does not treat a caller-created log as acceptance.
The recursive input reference check keeps this log shared and outside new work.
Retain it with the small graduation inputs. Ordinary graduation remains unchanged.

Add `object_directory` to the `lang/gcc16` recipe row. Pin its actual value from
the evaluated `WRKDIR`, `WRKSRC` and `OBJDIR` metadata. It must be below
`work_root/obj`. The SDK does not guess a pkgsrc object path.
`recipe_steps(plan, descriptor, commands, flags)` constructs all commands:

1. Visit dependencies in sorted order, then visit each consumer.
2. Run the recipe's make tuple, `EMBERBSD_DARWIN_BOOTSTRAP=yes`, `-C`, the
   common recipe directory, and `build`.
3. Install each dependency with the same tuple and the `install` target.
4. After the GCC build, run the complete selected `gmake` tuple,
   `--no-print-directory -j1 -C object_directory "--eval=.PHONY: compare" compare`.
5. Install GCC. `comparison_command` is exactly `gcc16-compare`.

GCC writes a file named `compare` after its bootstrap comparison. The fixed
GNU make `--eval` argument marks only that target phony. The separate SDK step
must repeat the comparison even when this stamp exists. It does not use `-B`,
rebuild GCC, delete stage objects or change the upstream comparison command.
A zero make exit is not sufficient evidence when stage 2 was deleted. Independent
output review must retain actual stage 2/3 objects and reject `stage2-lean` or
an empty comparison. The upstream lean guard remains intact.

For a source scheduling regression, run `tests/test_bootstrap_recipe.py` with
a fresh absolute work path and three `--gmake-arg` values for the complete
selected gmake, `SHELL=...` and `MAKECONF=...` tuple. This explicit fixture uses
only the selected make and shell. It checks a pre-existing stamp, equal and
different inputs, and the lean guard. It does not compile or qualify a tool.
The default unittest mode does not execute this fixture.

`recipe_make()` selects the recipe's make and shell from the bound dependency edge.
The Ports profile owns the
[make handoff](https://github.com/oxtech-ember/EmberBSD-Ports/blob/main/profiles/common-build-tools/bootstrap/README.md#make-handoff).
It gives every recipe outside the seed set a direct `devel/bmake` edge.
The seed set is the `devel/bmake` closure, which includes `shells/mksh`, and the
dependencies of `archivers/gzip`. Such a recipe starts with
`output_bundle_root/bin/bmake`, `SHELL=output_bundle_root/bin/mksh` and the bound
`MAKECONF` word. It does not use `-m`, so the Ports bmake reads its own `share/mk`.
A `SHELL=` word on the command line overrides the profile, so the bound initial
shell word must not reach it. `recipe_environment()` sets `SHELL` and
`CONFIG_SHELL` to the same Ports mksh, and `recipe_gmake()` gives the bound gmake
that `SHELL=` word for the GCC comparison step. The other recipes, `devel/bmake`
included, keep the complete bound initial tuples and environment.
The SDK keeps no recipe list. bmake passes `-m` to child makes through `MAKEFLAGS`.

The complete `argv`, `cwd`, `env`, role, ID and order must equal this construction.
Extra targets, assignments, environment entries and replaced tuple tails fail.
Metadata and recipe commands use `metadata_environment()` and the same MAKECONF.
Qualified ABI flags remain unchanged. This serial route does not cap the linker's
internal worker threads. It does not admit parallel nested recipe links.

`resources` has the five existing producer fields. This route accepts
`cpu_jobs: 1`, `link_jobs: 1`, and an integer `runtime_seconds` from 1 to 86400.
`lock_root` and `heavy_link_context` are explicit absolute paths.
The controller retains source and input read leases and an exclusive fresh-work
lease. It holds the shared heavy-link fence over each complete recipe command.
The deadline applies to command execution. Input validation and drainage can
take additional time. Unproven drainage enters the existing nonreturning HOLD.

Run `scripts/bootstrap-seed.py run-bootstrap` with the same six pinned selector
options and repeated `--required-role` as `check`. The returned producer result
has `stage: full-recipe-bootstrap`. It records actual combined logs, exits, input
revalidation and an installed `output_inventory` reference. Completion remains
`COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW`. It never issues qualification,
stage inventories, a stage comparison acceptance or an execution audit.

### Initial Darwin tools

The bound controller also accepts `purpose: initial-darwin-tools`.
This purpose requires `metadata.darwin_tool_phase: initial` and its pinned profile.
The full GCC-root source graph, inventories and dependencies remain mandatory.
`initial_execution_order(plan)` computes the exact transitive `lang/llvm` closure.
It filters the validated full order without removing any LLVM dependency.
`devel/cmake` must be in that closure.
Unrelated future recipes remain recorded and do not execute.

The controller builds and installs each dependency through the existing recipe route.
Its final LLVM build has these five fixed targets, in this order:
`llvm-libtool-darwin`, `llvm-install-name-tool`, `llvm-readtapi`, `llvm-lipo`, `llvm-otool`.
The make word is one `BUILD_TARGET` assignment, followed by `build`.
Other targets, omitted dependencies and changed order are refused before execution.
`comparison_command` is exactly `initial-llvm-tools`; no GCC comparison runs.
Add the evaluated LLVM `object_directory` below `work_root/obj`.
Do not infer that path from a recipe name.

The common initial profile selects AR/RANLIB archives and no LLVM shared dylib.
It keeps all configured backends, options, dependencies and package file lists.
The complete first CMake build uses its ordinary static-library archive rules.
Missing Darwin install-name, RPATH-change or shared TBD-export operations fail explicitly.
They do not acquire tools through `xcrun` or system discovery.
Normal full plans still require the selected Darwin tools before execution.

The result records installed dependency inventory and `initial_tool_output` separately.
The latter binds the plan, profile, LLVM recipe and five actual native images.
It checks the owner-declared `llvm-install-name-tool` and `llvm-otool` aliases against `llvm-objcopy` and `llvm-objdump`.
The `llvm-lipo` and `llvm-otool` tools support the shared Ports Mach-O metadata queries.
It records uninstalled graph nodes, including the partial LLVM package.
It does not qualify a full LLVM package, GCC comparison or ordinary provider.
`graduate` refuses this purpose.
Independent output review and a later scoped tool-input route remain necessary.

This phase retains all other accepted-input requirements.
In particular, it needs native `pkg_add` and `pkg_create` for dependency installation.
Executed Ninja requires `od`; an actual Meson dependency requires `lipo`.
Future outputs cannot satisfy those requirements before they exist.
Source and synthetic checks do not prove a native build, installation, runtime,
C/C++ analysis or memory checks. Those gates remain pending for the selected inputs.

Origin: EmberBSD contributors. AI assistance was used for this interface and its checks.

### Parallel heavy-link control

The same full-plan format accepts one optional `link_control` object.
It has exactly `python` and `launcher`, each an exact `{path, sha256}` reference.
`python.path` must equal the running SDK process's `sys.executable` spelling.
`launcher.path` must equal `scripts/link-launcher.py` in this same SDK source.
The Python invocation can be a symlink. Its SHA256 identifies the resolved
regular executable. Keep the original invocation in the launcher prefix,
including virtual-environment semantics. Do not substitute its resolved path.
The controller leases both that invocation and its resolved target. It checks
the same target and hash under these leases before dispatch and after drainage.
A target change during inspection or execution fails the run.
The launcher remains a regular source file with its own pinned SHA256.
These are SDK control-plane services. They are not seed tools or a new Python
provider. A copied interpreter, alternate alias, foreign script or extra
launcher arguments fail before execution.

This route requires the fixed Ports parallel profile SHA256
`6adbb69112056a867b463fd825a0842294b1eb7e67331433184c1f31d5de518a`.
It keeps the original profile available for serial and historical plans.
`cpu_jobs` is a positive integer no greater than the current `os.cpu_count()`.
An unavailable host CPU count fails closed. The deadline and `link_jobs: 1`
requirements remain.
The outer pkgsrc `make` build and install commands stay serial. They have no
`-j` option, including `-j1`. pkgsrc rejects parallel infrastructure commands.
The direct GNU GCC comparison keeps `-jN`. The common Ports `MAKE_JOBS` policy
passes the accepted job count to inner package builds. It retains each recipe's
`MAKE_JOBS_SAFE` decision.
MAKECONF gets `EMBERBSD_DARWIN_CPU_BUDGET` and `EMBERBSD_DARWIN_JOBS` from that
same accepted job count. It gets `EMBERBSD_GCC16_LINKER_LAUNCHER` from exactly:

```text
python.path launcher.path --lock-root resources.lock_root --context resources.heavy_link_context --
```

The SDK quotes the exact paths. It accepts no caller-supplied prefix string.
The Ports recipe passes this prefix as `GCC_HOST_LINKER_LAUNCHER`.
The complete step environment binds that same variable too. This covers the
direct GNU `compare` invocation, which does not pass through pkgsrc `MAKE_ENV`.
The three GCC `LINKER` and `LINKER_FOR_BUILD` assignments cover heavy language
frontends, LTO, native generators and `libgdiagnostics`. Compiler tuples and
qualified ABI flags do not change. Smaller target runtime links, including
`libgcc` and `libstdc++`, may run concurrently within the CPU budget. This is
one heavy-link slot. It is not serialization of every final link.

With `link_control`, the outer controller keeps its input, SDK source, control
file and writable-work leases. It does not take the heavy-link lock around
make. Each nested launcher takes that lock for its owned link group.
Taking the same lock in both layers would deadlock. The existing launcher
retains the build session and checks link-group drainage. The outer SDK
controller still drains the complete command session and retains its leases
in HOLD when drainage is unproven. The caller must use its host memory pressure
guard to stop the build when the agreed pressure limit is reached.

Historical graduation checks the retained control file references and exact
prefix. It does not reopen a retired Python or SDK source tree, or query a
different host's CPU count. Live admission always checks the current identities.
The independent comparison, execution audit and ordinary qualification gates
remain unchanged.

## Accepted seed receipt

The envelope is `emberbsd-bootstrap-seed-acceptance`, version 1,
scope `gcc16-bootstrap`, status `PASS`.
It binds `descriptor_sha256`, exact `bootstrap_plan` reference, and
`accepted_input_scope: bootstrap-seed`.
`accepted_history_unknowns` maps every provider to its exact sorted uncertainty list.
For source-build origins that list is empty. Omitting this explicit acceptance fails closed.
Repeat `host`, `compiler`, `sources`, `providers`, `host_abi`, `abi_flags`,
`history`, `dynamic_dependencies`, and `driver_prefix` exactly.
Repeat `driver_binding` exactly when the descriptor uses it.
`checks.identity` and `checks.driver_components` use NativeToolset query record shapes.
Query `argv` includes the complete seed tuple. No C/C++ compile probe is required for seed admission.

For the legacy unbound common-recipe plan, `execution_audit` references
`emberbsd-bootstrap-seed-audit`, version 1,
scope `gcc16-bootstrap`, status `PASS`.
Repeat `descriptor_sha256`, `bootstrap_plan`, `tools`, and `dynamic_dependencies`.
Require empty `undeclared_tools`, `path_searches`, `undeclared_abi_inputs`, and `undeclared_dsos`.
Hash checks preserve identity. They do not create independent acceptance.
The first finite pilot uses explicit
[input-and-route-only acceptance](bootstrap-pilot.md) instead.
It does not claim that the compiler chain already executed.
The fixed bound-make full plan uses that same input-and-route-only acceptance
for its first execution. Its audit must bind every exact full-plan command.
This removes no output gate: graduation still needs the actual stage2/stage3
comparison and production execution audit below. Ordinary NativeToolset
selection remains unchanged.

## Installed output assembly

The complete Darwin recipe runner normalizes absolute symlinks whose targets
remain inside its output prefix. It uses the existing prefix-link normalizer.
Each changed link must have exactly one installed package owner. Its immediately
paired `@comment Symlink:` record must match the original literal target.
The assembler changes that record to the new relative target. It leaves all
other package metadata and canonical package archives unchanged.

Before mutation, the assembler checks the complete prefix and package database.
It records both inventories, every selected link, and the original and replacement
`+CONTENTS` bytes in a durable PLANNED ledger. External or missing destinations,
unregistered links, duplicate owners, and mismatched comments fail before mutation.
The final strict inventory must preserve the roster and all regular-file bytes.
A partial failure retains the ledger and backups. It does not retry or roll back
automatically. Canonical package archives retain their original package payload;
the assembly receipt describes the installed SDK layout, not rebuilt packages.
Initial-tool and NetBSD recipe paths retain their existing completion behavior.

For a closed full-plan run that failed only on the strict link inventory check,
use `emberbsd_sdk.bootstrap_output.assemble_completed_bootstrap_output`.
`BOOTSTRAP_OUTPUT_ASSEMBLY_API` is 1. Pass the pinned `bootstrap_plan` and
`completed_result` file references, a fresh absolute `evidence_root`, and the
separate finalizer `finalizer_source` repository/commit pin.
The helper verifies every planned command, successful actual exit, and combined
log. It preserves the original returned and persisted FAILED results.
It executes no producer commands and does not select a historical SDK.

The caller must validate the original inputs with the original producer SDK
before and after assembly. Keep its shared input leases through the whole call.
Verify the finalizer source independently. The helper takes shared plan, result,
and finalizer source leases plus exclusive work and evidence leases in the plan's
lock namespace. Evidence may be below that namespace root; it must not contain
it or overlap work and source inputs. Retain corresponding fences if the caller
uses additional namespaces.

The separate `emberbsd-bootstrap-output-finalization-result` receipt uses version
1 and status `OUTPUT_ASSEMBLED_PENDING_INDEPENDENT_REVIEW`. It binds the original
FAILED result, plan, finalizer source and module hash, assembly ledger, strict
output inventory, and package inventory. It records `commands_reexecuted: false`,
`original_producer_inputs_revalidated_here: false`, and
`ordinary_provider_qualified: false`. The persisted receipt precedes lease closure;
the returned object adds `all_leases_closed: true`. Ordinary exceptions preserve
a FAILED assembly receipt. Interrupts preserve that receipt when possible and
propagate to the caller. Independent acceptance must bind the original-loader
revalidation and this separate assembly evidence. Output graduation and ordinary
qualification below remain required.

## Output graduation

Keep `accepted_input_scope: bootstrap-seed` in the native provider-build record.
Add a `bootstrap_graduation` reference. Do not relabel the seed input as qualified-native.
The ordinary NativeToolset loader checks graduation before normal output qualification.
The seed and original acceptance keep their bootstrap scope.

The graduation envelope is `emberbsd-native-gcc16-bootstrap-graduation`, version 1,
scope `native-build`, status `PASS`. It contains:

* Exact `output_descriptor_sha256`, `seed_descriptor`, `accepted_seed_receipt`, and `bootstrap_plan`.
* `output_inventory` equal to the ordinary descriptor inventory reference.
* `commands`, one result per plan command in order. Each repeats `id`, `argv`, `cwd`, and `env`,
  with integer `actual_exit: 0` and one stable combined `log` file reference from `run_logged`.
  Separate `stdout` and `stderr` fields are refused for build results.
  Full build logs use streamed hashes and are not subject to the JSON metadata size limit.
* `stage_comparison` with `method: gcc-bootstrap-compare`, `status: PASS`, exact `command_id`,
  nonempty `stage2_inventory` and `stage3_inventory` JSON references, and `comparison_log` reference.
* The output `dynamic_dependencies` graph and `execution_audit` reference.

The production audit envelope is `emberbsd-native-gcc16-bootstrap-audit`, version 1,
scope `native-build`, status `PASS`.
Bind `bootstrap_plan`, `output_descriptor_sha256`, sorted `recipe_closure`,
output `dynamic_dependencies`, and `all_commands_drained: true`.
Require the same four empty undeclared-input arrays as the seed audit.
The independent ordinary-output gate must inspect actual nested tool routes,
stage comparison, and DSO evidence before accepting the output receipt hash.
Graduation covers only a provider in the exact common recipe closure.
Its recipe digest matches that recipe's exported `Makefile`.
The ordinary loader still checks fresh C/C++ compile, link, run, driver, and execution evidence.
Graduation alone cannot return a NativeToolset.
A finite pilot plan cannot graduate a provider or replace the recipe plan.

Preserve original descriptors, plan, inventories, acceptances, command logs, and comparison evidence.
Ordinary use does not reselect or require the historical seed payload.
Current ordinary bundle, ABI inputs, and final probes remain verified live.

### Completed BSD installer provider

The current Darwin profile adds common `sysutils/bsdinstall` to the first GCC16
recipe closure. It waits for `PKGPATH` before adding the root policy edge. The
installer package uses the final common mksh and LLVM strip outputs for child
operations. The fixed local-source contract binds its complete exported subtree
and all seven canonical owner files. Named observations must still report empty
`DISTFILES` and `PATCHFILES`.

The initial `bsdinstall-bootstrap` program stays initial-only. Only the package
built in the accepted complete recipe plan can use that plan's graduation route.
The ordinary qualification gate must select the BSD package for role `install`
and check its actual `-U` and child-tool behavior. Generic native header/hash
validation alone does not qualify GNU install as a BSD installer.

### First ordinary foundation

The foundation profile preserves the earlier BSD installer profile identity.
It adds common bmake, GNU make, coreutils, findutils, grep, sed, awk, package
tools, diffutils and Meson providers at the GCC16 root. Their normal dependencies
supply bootstrap mk files and Ninja. Use actual named observations to establish
the complete production graph. The profile does not assert a fixed graph size.

The local-source contract also recognizes `devel/bmake`,
`pkgtools/bootstrap-mk-files`, `pkgtools/pkg_install` and `lang/nawk`. Bind their complete
exported subtrees and every common patch. Require exact named empty `DISTFILES`
and `PATCHFILES` observations. Missing, linked or changed source files still fail.
Nawk keeps its supplied parser and procedure tables. Missing generated source fails.
The local `security/mozilla-rootcerts-openssl` wrapper requires its complete
`Makefile`, `DESCR` and `PLIST` subtree. Its certificate data still comes from
the archived `security/mozilla-rootcerts` recipe. The parent archive is not a
local-source exception.
The local `archivers/bsdtar` recipe requires its complete `Makefile`, `DESCR`
and `PLIST` subtree and exact named empty `DISTFILES` and `PATCHFILES`.
Its `do-extract` copies the canonical `archivers/libarchive/files` source tree.
The same complete export inventory must bind all shared source bytes. Fixed
layout checks also require regular configure, build, tar parser and bsdtar
sources under real directory ancestors. These checks do not replace complete
export validation. They do not accept a second parser or another source root.
The recipe can also copy `archivers/bzip2/files` when builtin bzip2 is not used.
Require that shared source tree conservatively: the fixed metadata fields do
not observe `USE_BUILTIN.bzip2`. This source requirement does not change the
recipe's builtin selection or add a library dependency. Missing, linked or
changed inputs fail before a build. Local-source recognition does not qualify
the archive parser or an installed bsdtar executable.

The accepted initial mksh activates the canonical final-prefix shell selection
in bmake, GNU make and Ninja. Check those configure and installed child routes.

Output graduation still covers only providers built by the accepted full plan.
Initial tools and finite program results remain scoped bootstrap inputs.
Keep ordinary `install` on the completed BSD installer. A later hostname build
can use the qualified compiler and the existing OS-owned source contract.
