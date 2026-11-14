# Reuse by input key

This is the common reuse rule for EmberBSD builds, stages, sysroots, packages
and releases. Keep this rule here; skills, procedures and producers link to this canonical page.

## The input key

An input key identifies the inputs of one build step. Make it from hashes that
Git already stores and from declared parameters:

| Input | Key value |
| --- | --- |
| Source | Git tree hash from `git rev-parse <commit>:<path>`, or the commit hash |
| Compiler | Compiler key or compiler revision |
| Configuration | Declared configuration values |
| ABI | Target ABI |
| Target | Target architecture and triplet |

Include every real input of the step in its key. Add any previously omitted input
that the step reads. Use the existing Git hash and clean worktree to identify
sources directly, instead of computing another source hash or creating an archive.

## Reuse rule

1. Make the input key for the step.
2. If the worktree is dirty, build the step from its changed inputs.
3. If the key matches a recorded key, reuse the result.
4. If the key does not match, build the step again.

Reuse sources, stages and outputs directly when the recorded key matches.
A failed build records no key; keep older outputs at the same path ineligible
for reuse as that failed build's result.

## Apply the existing identity

Use the existing Git hash or input key as the identity of unchanged inputs.
Apply it at preflight, stage reuse and release reuse. A matching key is sufficient;
reuse the recorded result directly, including at the final reuse decision.

Checksums of downloaded archives stay necessary. Git does not identify them.
Build, package, target and hardware tests stay separate from reuse. They test
the result; they do not decide reuse.

## Revision is not bookkeeping

Do not raise a package revision for accounting alone: review notes, verification
results, CI reports, provenance and transport retries belong to their own
records. Change package identity when installed payload or package metadata
changes. Never roll back a published revision or publish different bytes under
an existing package name.

Keep these concepts distinct when designing a producer contract:

- Provenance records the source revision and verification history.
- A build key covers actual sources, patches, options, compiler, target/ABI and
  relevant build dependency inputs.
- A package key additionally covers install layout, PLIST and package metadata.
- The published package name and artifact digest identify the immutable archive.

The current Ports graph v1 has a single conservative `input_sha256`; it does
**not** attest separate build/package equivalence. Its graph-level provenance
revision already does not affect node input keys. Do not treat a raw recipe hash
as a proven semantic build key, strip `nbN` from catalog comparisons, or reuse an
older dependency merely because the upstream version matches. Exact package and
runtime-closure checks remain until the producing contract supplies verifiable
compatible identities. Security patches, headers, static libraries and build
tools may matter even when an ABI name is unchanged.

This separation is not a request to sign every local edit or run release
qualification on every compile. Local incremental steps keep using their input
keys; downloaded/public artifacts keep their integrity/authenticity checks.
Path checks are an [opt-in diagnostic](package-path-qualification.md), separate
from signatures, normal local iteration and target qualification.
