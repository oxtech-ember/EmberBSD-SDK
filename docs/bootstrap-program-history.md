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
For cut, the retained configuration must equal the five actual probe results.
Only the final linked cut image can be provisioned, never its control executable.

Historical dynamic oracles retain their independent semantic decision and exact
stream references. The verifier never recomputes hostname, uname or work-root
facts on the current host. Literal streams still match the source-owner contract.
The result retains the exact original seed and accepted receipt references.
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
