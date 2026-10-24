# First native GCC16 bootstrap

This interface admits a seed for one first-native-GCC16 plan.
It does not admit that seed for ordinary builds or issue acceptance.
The new output still needs [NativeToolset qualification](native-toolset.md).
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
No general driver options, response files, or `-specs` field are accepted.
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
The check command does not provide this execution controller.

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
| `providers` | Named seed origins. Each has explicit `origin: source-build` or `origin: binary-release`. |
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

A `binary-release` history has `release_url`, `release_version`, exact `artifact`
file reference, original `release_metadata` reference, and `embedded_configuration` reference.
It also has `source_commit` (a source pin or null), `historical_build_log`
(file reference or null), `historical_compiler` (text or null), boolean `patches_complete`,
and `publisher_signature` (null or `{file, verification}`).
Signature verification references a JSON record with actual integer exit 0 and
`stdout` and `stderr` references. Independent acceptance checks its actual verifier and meaning.
These records do not select the historical compiler or signature verifier for a build.

The `unknowns` array must list every missing historical input, in sorted order:
`source-archives` for an empty archive list, `source-commit` for a null commit,
`historical-build-log`, `historical-compiler`, `historical-patches` when completeness is false,
and `publisher-signature` when absent.
An independent gate must explicitly accept this exact named uncertainty list.
Do not invent unavailable build logs or exact source commits.
This variant pins a binary input release. It does not claim its historical build was qualified-native.
Unknown current tool, driver, native ABI, or DSO inputs are not permitted by this variant.
The new GCC16 producer still requires complete common recipe, source, and patch pins.

`dynamic_dependencies` maps canonical absolute executable or DSO paths to
`{sha256, dependencies: [absolute paths]}`.
Every native tool has an entry. Every edge names another entry.
Nodes stay in the bundle or declared native ABI library roots.
Non-tool nodes need a native shared-library header: thin Mach-O64 type 6 or ELF64 type 3.
The loader rejects missing nodes and changed bytes.
The independent audit must establish graph completeness.
This module does not infer loader searches from headers.
Unsupported loader, shared-cache, or DSO formats need an explicit contract extension.

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
The subtree must equal the corresponding complete export subtree.
List every regular `patches/patch-*` file once.
Each distfile has `url` and a common `file` reference.
Cycles and unrelated recipes fail closed.
Independent acceptance must confirm the dependency list against common recipes.
The SDK does not duplicate recipe dependency resolution.

Each command has unique `id`, `recipe`, `phase`, `role`, `argv`, `cwd`, and `env`.
`phase` is `build`, `compare`, or `install`. `role` is selected `make` or `gmake`.
Build and install commands start with selected make and `-C <export/recipe>`.
Additional `-C` and makefile replacement options are refused.
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
Make arguments cannot replace these bindings.
Optional environment keys are `DISTDIR`, `WRKOBJDIR`, `PACKAGES`, `PKG_DBDIR`,
`LOCALBASE`, `PREFIX`, `PKGSRC_COMPILER`, `GCC_REQD`, `OSX_SDK_PATH`,
`TZ`, `LC_ALL`, `LANG`, and `TMPDIR`. `PKGSRC_COMPILER` must be `gcc`.
Actual execution audits must establish that nested recipe commands obey these routes.

## Accepted seed receipt

The envelope is `emberbsd-bootstrap-seed-acceptance`, version 1,
scope `gcc16-bootstrap`, status `PASS`.
It binds `descriptor_sha256`, exact `bootstrap_plan` reference, and
`accepted_input_scope: bootstrap-seed`.
`accepted_history_unknowns` maps every provider to its exact sorted uncertainty list.
For source-build origins that list is empty. Omitting this explicit acceptance fails closed.
Repeat `host`, `compiler`, `sources`, `providers`, `host_abi`, `abi_flags`,
`history`, `dynamic_dependencies`, and `driver_prefix` exactly.
`checks.identity` and `checks.driver_components` use NativeToolset query record shapes.
Query `argv` includes the complete seed tuple. No C/C++ compile probe is required for seed admission.

`execution_audit` references `emberbsd-bootstrap-seed-audit`, version 1,
scope `gcc16-bootstrap`, status `PASS`.
Repeat `descriptor_sha256`, `bootstrap_plan`, `tools`, and `dynamic_dependencies`.
Require empty `undeclared_tools`, `path_searches`, `undeclared_abi_inputs`, and `undeclared_dsos`.
Hash checks preserve identity. They do not create independent acceptance.

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

Preserve original descriptors, plan, inventories, acceptances, command logs, and comparison evidence.
Ordinary use does not reselect or require the historical seed payload.
Current ordinary bundle, ABI inputs, and final probes remain verified live.
