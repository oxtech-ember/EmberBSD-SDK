# Current Ports pkgsrc source export

The current EmberBSD-Ports tree deliberately disables the old standalone
`prepare-pkgsrc.py` entry point. Run its `export_pkgsrc` callable only through
`scripts/bootstrap-pkgsrc.py`, which pins the clean Ports Git root, initialized
`upstream/pkgsrc` gitlink, full tracked source inventory, exact exporter and
`pkgsrc/export.conf` bytes, SDK implementation, selected Python 3.15+,
Git and patch tools, and a fresh private export work directory.

This stage exports the pinned upstream package tree and Ports overlays. It
does not compile packages, qualify a compiler or make program, or issue a
passing receipt. Its result remains
`EXPORT_COMPLETED_PENDING_INDEPENDENT_REVIEW` until a separate gate reviews the
source, actual exporter command records, drained process session and complete
export inventory. Do not start `bootstrap --full` or a package build until that
gate supplies the receipt path and SHA256, then the target-side transfer is
independently revalidated and accepted.

The source manifest is a JSON map over every tracked Ports file, link,
directory and gitlink. Regular file rows bind the Git blob, SHA256, size and
Git executable mode. The source walker rejects untracked or ignored Ports
inputs, path-type changes, sparse or hidden index entries, and a pkgsrc
checkout that differs from its gitlink. The pkgsrc checkout itself is bound
to its clean commit; the exporter creates its upstream tree from that commit
with `git archive`, never from untracked submodule files. Both Git roots must
also have exactly one configured `origin` URL matching the canonical HTTPS
repository identifier in the source descriptor; the independent gate still
checks the claimed commits against those upstream sources.

The SDK accepts exactly two executable selections for this stage: Git and
patch. The Git executable must be named `git`. The selected patch executable
must be named `patch` or `gpatch`. The latter name supports GNU patch without
an alias. Both selections retain their exact absolute path and SHA256 checks.
The worker receives a restricted PATH containing those exact tool
directories and the selected Python directory. Compiler and make names cannot
be supplied through those selectors. The worker imports only
`scripts/prepare-pkgsrc.py` from the hashed clean Ports source and passes the
fixed `selection` and runner objects to `export_pkgsrc`.

The user flow is:

1. Write the full Ports manifest under the common SDK evidence tree with
   `make-source-manifest`.
2. Write a data-only source descriptor with `describe-source`.
3. Write a fixed export plan with `make-export-plan`, using exact absolute
   paths for the selected Python, Git and patch executables and a fresh child
   of the existing common build tree.
4. Review the plan, then run `run-export`. It keeps shared input leases and an
   exclusive work lease until the worker's process session is drained and all
   inputs are revalidated.
5. Have an independent gate inspect the pending result and issue a
   `emberbsd-bootstrap-pkgsrc-export-acceptance` receipt. Supply that receipt
   and its independently computed SHA256 to the later bootstrap consumer.
6. Transfer the accepted export and its complete inventory to a fresh
   subdirectory of the common target build tree. Have an independent target
   gate compare the copied inventory bytes with the export acceptance, walk
   and hash the complete target tree, and issue a separate transfer receipt.
   The target SDK checks that receipt with `check-transfer-acceptance`; it
   does not try to open the exporter host's output path after transfer.

The gate receipt has exactly `format`, `version`, `scope`, `status`,
`evidence_level`, `producer_result`, `export_plan`, `source_descriptor`,
`output_root`, `output_inventory`, `source_proof`, `commands`,
`controller_command`, `all_commands_drained`, `inputs_revalidated_after_drain`,
and `scope_boundary`. Its format is
`emberbsd-bootstrap-pkgsrc-export-acceptance`, version 1, scope
`pkgsrc-source-export`, status `PASS`, evidence level
`independent-source-export-review`, and scope boundary
`independently reviewed current Ports source export only; no pkgsrc build or NativeToolset qualification`.
The receipt binds the exact pending result, plan and source descriptor refs;
output root, full inventory and source proof; every selected Git/patch command
and the outer worker command; plus drained and post-drain revalidation flags.
The SDK only verifies such a gate receipt. It contains no command to produce
one.

After the worker's owned process session drains, the SDK revalidates all source
and plan inputs, verifies the full export inventory, and removes only its
fixed `scratch/pkgsrc.tar` file and scratch directory. A scratch tree with any
other entry is preserved and fails closed. If process drainage is unproven,
the work tree, scratch archive and leases remain held for inspection.

The target-side transfer receipt has exactly `format`, `version`, `scope`,
`status`, `evidence_level`, `source_export_acceptance`, `source_inventory`,
`output_root`, `output_inventory`, `transfer_report`, `source_proof`,
`normalization_policy`, `inputs_revalidated_after_transfer`, and
`scope_boundary`. Its format is
`emberbsd-bootstrap-pkgsrc-transfer-acceptance`, version 1, scope
`pkgsrc-source-transfer`, status `PASS`, evidence level
`independent-target-source-revalidation`, and scope boundary
`independently revalidated current pkgsrc source tree at target; no package build or NativeToolset qualification`.
The target gate copies the accepted source inventory bytes into the target
evidence directory; that copy must retain the source acceptance's SHA256.
Tree comparison requires exact paths, content, executable file modes,
directory modes and link targets. The shared transport rule ignores only
symlink `lstat` mode, which POSIX does not preserve portably. The target
inventory records its actual modes and is revalidated when the SDK checks the
receipt. The source transfer report is separately hashed. The SDK only
verifies the gate-issued receipt; it has no receipt-creation command.
