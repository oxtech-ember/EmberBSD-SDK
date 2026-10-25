# Git-program bootstrap history

Origin: EmberBSD (AI-assisted).

This `source-build` history variant checks independently accepted OS bootstrap
programs. It uses their actual Git source proofs. It requires no invented source
archive and supplies no copy command or acceptance issuer.
It never qualifies an ordinary NativeToolset provider.

Use `BOOTSTRAP_PROGRAM_HISTORY_API = 1` and
`verify_program_build_history(history, provider_name, descriptor, *, inspect_inputs=True)`
from `emberbsd_sdk.bootstrap_program_history`.
The [seed loader](bootstrap-seed.md) calls this verifier for the explicit
`program_build` variant. The legacy archive and `build_record` route is unchanged.

## Provider and history

The provider has exactly `origin`, `source`, `recipe`, and `recipe_sha256`.
Its origin is `source-build`. Its source selects the original OS pin from the
new descriptor's `sources` map. The recipe is the original
`tools/PROGRAM/bootstrap.json`; its SHA256 is the accepted manifest identity.
Use `os-COMMIT` as each selected tool's version, with the full original OS commit.
These tools do not acquire a package version or a `--version` interface.

The history has exactly these fields:

| Field | Value |
| --- | --- |
| `kind` | `source-build` |
| `source_archives`, `patches` | Empty arrays; this route used the pinned Git snapshot |
| `program_build` | Exact `{path, sha256}` reference to the build-history record |
| `provisioned_members` | Explicit invocation-to-current-file mappings |

Each member has exactly `invocation`, `original_path`, `selected_path`, and
`sha256`. The original path and SHA256 identify the successful linked program.
The invocation must occur in the original manifest. Each selected provider role
has one mapping. Its current basename equals that invocation.
The current member must be a regular executable inside the declared bundle.
Implicit aliases, wrappers, outside links and unrelated roles are refused.
The grep image may provide its explicitly declared `egrep` and `fgrep` names as
regular copies of that same accepted image. No new implementation is introduced.

Use additional source keys to preserve different historical OS commits.
For example, `os_previous` can retain an earlier pin while `os` selects the next
producer's source. Do not rebind an existing output to a newer OS commit.

## Retained build evidence

The build-history record has exactly `format`, `version`, `scope`, `manifest`,
`source_proof`, `bootstrap_plan`, `producer_result`, and `output_acceptance`.
Its format is `emberbsd-bootstrap-program-build-history`, version 1, scope
`gcc16-bootstrap`. The last five fields are exact file references.
Only the fixed OS [direct-source program profiles](bootstrap-program.md) apply.
GNU, installed-package, recipe and ordinary provider routes do not enter here.

The verifier reuses the original program plan validator and command constructor.
It checks the complete retained source snapshot, inventory, members and Git blob
identities. It checks the original manifest pin, source proof and seed selection.
Every finite command must retain its exact argv, environment, phase and expected
exit. Actual exits, raw combined logs, compiler output hashes and semantic streams
must agree with that plan and its final output roster.
Created fixture and probe-copy hashes must match the source-owner bytes.
This comparison does not reopen the retired copies in the old work directory.
For cut, the retained configuration must equal the five actual probe results.
Only the final linked cut image can be provisioned, never its control executable.

Historical dynamic oracles retain their independent semantic decision and exact
stream references. The verifier never recomputes hostname, uname or work-root
facts on the current host. Literal streams still match the source-owner contract.
The result retains the exact original seed and accepted receipt references.
Offline history validation checks their complete, exact unknown map.
Their historical unknowns stay unchanged. An empty unknown list for this new
source build means its own build history is complete; it does not qualify the
historical binary origins in that seed.

Retain the original source snapshot, metadata, logs, semantic streams and cut
configuration. Keep these records outside retired payloads.
Original objects and executables may be retired after verified copying.
The historical verifier does not load the old seed payload or ABI trees.
Its tuple reconstruction uses the original pinned seed metadata only.
The source snapshot must be separate from that retired seed tree.
`inspect_inputs=False` also avoids reading a previous provisioned member.

## Independent output acceptance

The SDK does not write this receipt. An independent gate must inspect the actual
source membership, input admission, compiler and linker routes, semantic results,
runtime evidence and applicable qualification limits before issuing it.
The caller-pinned next seed acceptance binds the complete descriptor and history.
A receipt advertised by an untrusted payload is not independent acceptance.

The receipt has exactly the following fields:

| Field | Value |
| --- | --- |
| `format`, `version`, `scope` | `emberbsd-bootstrap-program-output-acceptance`, `1`, `gcc16-bootstrap` |
| `status`, `accepted_input_scope` | `PASS`, `bootstrap-seed` |
| `ordinary_provider_qualified` | `false` |
| `bootstrap_plan`, `producer_result`, `source_proof` | Exact original file references |
| `artifact` | Original linked executable `{path, sha256}` |
| `checks` | Ordered list below |
| `limitations` | Nonempty list of explicit qualification limits |
| `semantic_review` | Exact retained independent semantic review reference |
| `runtime_audit`, `runtime_closure` | Exact retained Darwin output audit and closure references |

The ordered checks are `git-source-membership`, `native-compile-link`, `source-semantics`,
`reported-tool-routes`, and `darwin-output-runtime`.
The semantic review must establish all original cases, including dynamic facts
observed at that time. Its reference is evidence, not a present-day oracle.
The source check must establish membership at the pinned OS commit.
A self-consistent inventory and Git blob map cannot establish that fact alone.
The producer result must retain completed command status, proven drainage,
closed leases and successful input revalidation. Its ordinary qualification
flag remains false. A failed or partially completed result cannot be accepted.

The historical runtime metadata must bind the same executable hash and actual
producer trace. Its complete closure must equal the graph derived from its audit.
This is retained evidence, not a fresh runtime audit of the provisioned layout.
The next seed still needs its own complete inventory and current runtime graph.
Compiler analysis, memory checks, ordinary qualification and release status remain
separate gates. Record pending checks explicitly in the acceptance limitations.

The verifier runs no compiler, shell, Git command or runtime executable.
The tests use synthetic children. They do not establish native execution.

## Ports Byacc history

The fixed `byacc-bootstrap` stage also uses this history envelope.
Its provider binds the original Ports pin, `pkgsrc/devel/byacc/bootstrap.json`,
and tool version `byacc-20260126`. Only the `yacc` invocation is provisionable.
`source_archives` contains the original archive URL and file reference.
`patches` contains both original patch URLs and file references in recipe order.
Patch URLs use the original Ports repository, commit and recipe paths.
Use `bootstrap_byacc.history_origins(plan)` to obtain these exact origin fields.
The acceptance check roster starts with `ports-release-and-patch-membership`.
The other independent checks and the ordinary-provider exclusion stay unchanged.

Verification checks the complete archive-derived tree and both exact patch outputs.
It also checks actual capability observations, the generated configuration,
repeatable parser-generation records, generated source bytes and parser semantics.
The current host never supplies a replacement configuration or expected parser output.
No archive, patch or extra history field is accepted for the existing OS profiles.

## Bounded history traversal

One top-level history validation owns an in-memory context for its nested history graph.
The key includes complete history, provider and descriptor values and the input-inspection mode.
A previously completed node can reuse its unknown-list result within that call only.
No context, result or receipt cache survives the top-level call, including a failed call.

Repeated JSON references still read stable bytes and verify SHA256 on every access.
Within this scope, strict JSON checks can reuse a successful exact-byte validation.
The marker binds the byte digest, length, parser limits and reuse budgets.
Each return uses a new decoded object. Callers never share mutable parsed metadata.
The first read retains duplicate-key, nesting, UTF-8 and finite-number checks.
Changed bytes or limits cannot reuse that marker. Parser errors discard all JSON markers.
At most 1,024 markers cover 64 MiB of distinct validated document bytes.
Only markers are retained, not documents or decoded objects.
When either budget is full, reads continue through the full strict parser.
JSON reads outside this history scope keep their existing strict behavior.

Separate seed loads always start fresh. Historical and current input-inspection modes cannot share a result.

A memo hit rehashes the files observed during the original validation.
It also repeats complete source inventories and checks file and directory identities.
Changed bytes, paths, modes, links or source timestamps refuse the hit.
This preserves archive-derived source proofs that bind modification times.
Results are copied; callers cannot alter cached historical unknowns.
The complete validation still runs once for each distinct node.
Original receipt comparisons and unknown-list checks are unchanged.
Current input inspection never uses a memoized result. Each call repeats native image,
executable permission and invocation alias checks. Only nested historical nodes can reuse results.

Active-node recurrence refuses cycles. The scope permits at most 256 history nodes,
64 active levels and 32768 input guard entries. A tree consumes one entry per member,
plus its root; separately hashed files also consume entries.
An encoded node key or inventory fingerprint input is limited to 16 MiB.
Exceeding a graph or input-guard bound fails validation. It never truncates a graph or omits evidence.
The context supplies no admission and never starts a process.

`test_bootstrap_history_context` checks shared DAG expansion counts, cycles,
bounds, changed source and metadata inputs, unknown-list preservation and fresh calls.

The `expr-bootstrap` route also retains its generated `expr.c` member.
Its `generate-expr` observation must bind that exact output path and SHA256.
History checks the retained bytes and the source-owned generation plan.
It does not run the historical yacc or reopen its retired executable.
The original accepted seed keeps the generator's provider and provenance.
