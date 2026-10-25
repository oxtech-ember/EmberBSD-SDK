# Installed-package bootstrap inputs

An existing installed package is a separate seed history kind.
It is not a binary release archive or a source-build receipt.
Use it only with the [scoped first GCC16 bootstrap contract](bootstrap-seed.md).
An independent gate must accept the exact input bytes, historical unknowns,
native ABI, tool routes, runtime closure, and one plan.
This history kind does not issue acceptance or create an ordinary NativeToolset.

The seed provider has `origin: installed-package`.
Its history has `kind: installed-package` and exactly these fields:

| Field | Value |
| --- | --- |
| `package_name` | The exact original `+CONTENTS` package name |
| `package_path` | The exact original `+BUILD_INFO` `PKGPATH` |
| `installation_prefix` | Original absolute prefix, used only as provenance |
| `package_metadata` | Exact `build_info` and `contents` file references |
| `installed_member_inventory` | Reference to the preserved finite SHA256 member roster |
| `provisioned_members` | Explicit original-to-provisioned member mappings |
| `source_correlation` | Exact reference to source-owner evidence of its stated scope |
| `source_archives`, `patches` | Common ordered `{url, file}` arrays |
| `package_archive` | Actual package archive reference or null |
| `source_commit` | Historical source pin or null |
| `historical_build_log` | Actual historical build log reference or null |
| `historical_compiler` | Exact reported `CC_VERSION` text or null |
| `historical_toolchain_closure` | Actual historical toolchain evidence reference or null |
| `patches_complete` | Boolean historical patch completeness |
| `unknowns` | Exact sorted missing-input labels below |

The original metadata remains unchanged.
The loader reads it as data. It does not source or execute package directives.
It checks one matching `@name` and one matching `PKGPATH` line.
A non-null `historical_compiler` must match the original `CC_VERSION` line.
Preserve that text when the metadata contains it. Do not replace a reported
compiler value with a missing-input claim.
That reported text does not prove the historical compiler binary identity.
It never selects a compiler for a new process.
Do not rename `+BUILD_INFO` or `+CONTENTS` as a build log, package archive,
or toolchain closure. References with identical metadata bytes are refused in
those evidence fields. Missing evidence stays null.
No release URL or archive is invented for an installed payload.

This finite variant supports only these package paths and tool roles:

| Package path | Permitted selected roles |
| --- | --- |
| `devel/bmake` | `make` |
| `devel/gmake` | `gmake` |
| `shells/mksh` | `sh` |
| `pkgtools/bootstrap-mk-files` | Data members only |
| `lang/nawk` | `awk` |
| `textproc/grep` | `grep` |
| `textproc/gsed` | `sed` |
| `textproc/nbsed` | `sed` |
| `sysutils/coreutils` | `basename`, `cat`, `cut`, `date`, `dirname`, `env`, `expr`, `head`, `id`, `od`, `pwd`, `sort`, `tail`, `tr`, `uname`, `uniq`, `wc` |
| `pkgtools/pkg_install` | `pkg_info`, `pkg_admin` |

The package name starts with the corresponding package name and a version.
Each package can provide only its mapped roles. No installed input can provide
compiler, assembler, linker, or interpreter roles through this extension.
An original regular member can supply a selected copy. Preserve original GNU
invocation symlinks in the roster. Do not present symlink text as file bytes.
The [metadata adapter](bootstrap-producer.md) binds accepted invocation aliases separately.
Runtime DSO closure still requires its actual pinned bytes and routes.
This table does not permit DSO relocation or a changed installed-member hash.
The separate [finite relocation contract](bootstrap-relocation.md) retains
original provenance in the bootstrap-only `installed-package-derived` kind.
The ordinary provider contract remains unchanged.

An existing SDK `nbsed` can supply a temporary bootstrap `sed` input.
Keep its original `textproc/nbsed` identity, package metadata, member hashes,
historical tool routes, and unknowns. Do not relabel it as `textproc/gsed`.
Its admission still requires independent review of the exact provisioned bytes,
current runtime closure, and consumer plan. This mapping does not accept a payload.
Use that input to build the current common Ports `textproc/gsed` recipe with
all common patches and its selected dependencies. Replace the temporary input
after the new GNU sed output passes its required checks.
This route does not qualify an ordinary provider or the canonical OS sed build.

## Preserve and correlate members

Capture original package metadata and member SHA256 values under the owner's
input leases. Preserve those small records outside the provisioned payload.
The SDK loader does not reopen the historical installation prefix.
It does not copy files, query make, install packages, or reconstruct a build log.

`installed_member_inventory` references a JSON object keyed by normalized,
source-relative installed member paths. Each used row has `kind: file` and
the original member `sha256`. Other captured inventory properties may remain.
This is a finite roster, not a claim that the entire installation was captured.
Legacy package MD5 fields are supplementary. They do not replace SHA256 capture.

Each `provisioned_members` row has exactly `installed_relative`, `selected_path`,
and `sha256`. The selected path is absolute, normalized, unique, and inside the
provisioned seed bundle. Its resolved target must also stay inside that bundle.
Its current content hash must equal the preserved original member hash.
Every selected tool for the provider must have the same selected path and
content hash in a mapping. Plain mk files can use the same mapping contract.
The normal complete seed inventory and native-header checks still apply.

`source_correlation` can establish a limited fact, such as the source and exact
substitutions for installed mk text. It does not establish that an installed
binary came from that source build. Independent acceptance must inspect the
evidence's scope and retain historical source or toolchain gaps.
Current GCC16 recipe source pins do not retroactively qualify an old binary.

## Historical unknowns and admission

The loader computes the sorted `unknowns` array:

- `package-archive` when `package_archive` is null.
- `source-commit` when `source_commit` is null.
- `historical-build-log` when that log is null.
- `historical-compiler` when the reported compiler field is null.
- `historical-toolchain-closure` when that evidence is null.
- `historical-patches` when `patches_complete` is false.
- `source-archives` when the archive list is empty.

The accepted seed receipt must repeat every provider's exact array in
`accepted_history_unknowns`. Omitting an unknown does not create evidence.
Hashing original installed bytes is not independent acceptance.
Do not use this kind as an ordinary provider or relabel its bootstrap scope.

This contract admits no make execution by itself.
The later exact recipe plan must also bind its source-owned make policy,
full mk closure, explicit shell, `MAKECONF`, utility and ABI routes, resource
limits, process ownership, drainage, and output qualification.
The first C/C++ pilot does not depend on those make inputs.

For later graduation, retain original metadata, the member roster, mappings,
and source correlation. Historical validation can check those identities
without reopening a retired installed tree or provisioned seed payload.
