# First bmake bootstrap seed

> Retired bootstrap admission interface. This document describes historical data.
> Normal native tools are Ports packages in `host-pkg-g16`, built with host GCC16.
> Use [host prefix selection](native-toolset.md); do not create these acceptance receipts for a normal build.

This narrow producer builds the first native `bmake` when no accepted bmake
input exists. It uses the shared [Ports bmake source owner](https://github.com/oxtech-ember/EmberBSD-Ports/blob/main/profiles/common-build-tools/bmake.md)
and one previously accepted native GCC16 compiler-only seed. The resulting
binary stays in `bmake-bootstrap` scope. It does not become a
`NativeToolset`, package acceptance, or general native build provider.

## Inputs

The compiler descriptor binds the historical GCC16 record, its file manifest,
a complete live prefix inventory, the exact `prefix/bin/gcc` binary, its
receipt-bound driver closure and a separate compiler-seed acceptance receipt.
The complete inventory covers file bytes and sizes, directory and file modes,
and symlink targets. Admission compares the live tree to that inventory and
the legacy file-only manifest to every regular file on every use; changing an
unselected file or adding a file fails closed. It checks GCC 16.2.0 and the native
`x86_64-unknown-netbsd11.0` triplet. The driver must still select the recorded
`/usr/bin/as` and `/usr/bin/ld`; their hashes and the observed `ldd` closure
of the compiler, its executable components, and both binutils are explicit
inputs; the static `libgcc.a` remains pinned by the prefix manifest. The runtime
closure follows base-library aliases and is pinned to regular targets under
NetBSD `/usr/lib` or `/lib`. The producer
does not accept a system `cc`, `gcc`, or another unqualified compiler in place
of that driver.

`audit-compiler-candidate` writes a full inventory and a scoped evidence report
with status `PENDING_INDEPENDENT_REVIEW`. It runs GCC identity queries and
checks current runtime dependencies. It does not issue acceptance. A separate
reviewer must compare the report with the historical record and its source
provenance, then issue `emberbsd-bootstrap-bmake-compiler-seed-acceptance`,
version 1, scope `bmake-bootstrap`, status `PASS`. The receipt binds the report,
record, legacy manifest, complete inventory, compiler, driver components,
runtime closure, host, prefix and historical source plan. The selector requires
both the receipt path and a SHA256 supplied by that independent gate. A
self-hash computed by `describe-compiler` is not acceptance.

The independently issued receipt has exactly these fields: `format`, `version`,
`scope`, `status`, `audit_report`, `accepted_record`, `prefix_manifest`,
`prefix_inventory`, `host`, `prefix`, `compiler`, `driver_components`,
`host_runtime`, and `source_provenance`. The three evidence references are
`{path, sha256}` objects and must match the pending audit report byte-for-byte.
The reviewer issues the receipt only after checking the report and historical
source evidence; then the reviewer supplies the receipt's SHA256 separately to
the selector. There is no command in this producer that converts its own audit
report into a passing receipt.

The candidate audit is narrow: it records current GCC identity queries and
runtime closure, and carries forward the historical C/C++ run checks from the
accepted record. It does not certify ordinary compiler-provider suitability.

The source descriptor records an asserted Ports revision and binds `bmake.md`,
`sources.tsv`, the complete bmake import patch, the official 20260912 archive
and its extracted tree. The revision string is an assertion; this selector does
not verify that those files came from that Git commit. It checks every bound
file hash and every extracted file's bytes and executable mode against the archive.
It also verifies that the common Ports manifest pins the archive and import
patch, and that the shared profile still specifies `configure` followed by
`make-bootstrap.sh`.

## Producer route

The fixed plan runs the shared recipe's two bootstrap entry points directly:

1. `/bin/sh ./configure` with `/usr/pkg` prefix, `/usr/pkg/share/mk`,
   `x86_64`, `/usr/pkg/etc`, and the selected `/bin/sh` default.
2. `/bin/sh make-bootstrap.sh`.

Both commands receive `CC` as the absolute accepted GCC16 path. `PATH` is a
private directory containing only the hashed NetBSD base utilities needed by
the configure script. It has no compiler or `make`, `bmake`, or `gmake` entry.
There is no package-manager fallback. The configured script is checked for the
same compiler and Ports path inputs before the compile command runs.

First run the candidate audit and obtain the independently issued compiler
acceptance receipt. Use `describe-compiler` with that receipt path and its
gate-supplied SHA256, and `describe-source` with `--ports-asserted-revision` to
write selectors, then
`make-plan` and `run-producer` to execute the single fixed plan. The producer
keeps its source copy, logs and result in a fresh work directory. A completed
producer returns `COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW` and records
`ordinary_provider_qualified: false`.

Choose the lock root outside the SDK source, descriptors, receipts, source tree,
compiler prefix, work directory and output. The producer and audit reject
overlapping lock roots before creating lock files.

## Separate acceptance

The producer's pending result and log hashes do not independently prove that
the binary came from those commands. Before the behavior audit, an independent
gate must inspect the actual configure and `make-bootstrap.sh` logs, the
source-bound plan and the output file, then issue a separate
`emberbsd-bootstrap-bmake-producer-acceptance` receipt. The gate binds the
exact producer-result, bootstrap-plan, seed and source references, output
path/hash/mode, both command records and their stable log references. It sets
`all_commands_drained` and `inputs_revalidated_after_drain` to `true`,
`ordinary_provider_qualified` to `false`, and uses the fixed scope boundary
`independently reviewed source-bound bmake execution evidence only; no NativeToolset qualification`.
It must not accept a copied executable based only on its expected path or
behavior.

The receipt has exactly these fields: `format`, `version`, `scope`, `status`,
`evidence_level`, `producer_result`, `bootstrap_plan`, `seed_descriptor`,
`source_descriptor`, `output`, `commands`, `all_commands_drained`,
`inputs_revalidated_after_drain`, `ordinary_provider_qualified`, and
`scope_boundary`. Use `status: PASS` and
`evidence_level: independent-producer-evidence-review`. No command in this
producer issues that receipt. A receipt hash is an integrity reference, not a
signature; the CLI requires the independently supplied SHA256 separately from
the receipt contents.

`make-audit-plan` and `run-audit` are a separate invocation and both require
the accepted producer receipt path and its gate-supplied SHA256. The selector
reopens the receipt and requires it to bind the exact pending producer result.
The audit reruns the output's version query, a positive makefile and shell
behavior check, and an expected failing recipe. On the supported NetBSD host,
bmake reports a failing recipe as process exit 1 even when the recipe exits 7;
the audit requires that exit and the recipe's failure marker. A passing audit receipt has
`accepted_input_scope: "bmake-bootstrap"` and
`ordinary_provider_qualified: false`. It neither reuses producer logs as
the independent gate nor proves package builds, a complete makefile binding,
or any other native tool.

After acceptance, the binary can be considered only for the next explicitly
scoped bootstrap step. Ordinary SDK and application builds still require a
complete independently qualified native tool set and the existing tool
selection rules. Physical-device acceptance remains separate.
