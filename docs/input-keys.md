# Reuse by input key

This is the common reuse rule for EmberBSD builds, stages, sysroots, packages
and releases. Skills, procedures and producers link to this page. Do not copy
it into other documents.

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

The key must include every real input of the step. If a step reads an input
that the key does not include, add that input to the key. Do not add a new
hash computation for sources. Git already identifies them. A clean worktree
and its Git revision identify the sources. Do not create `source.tar.gz` or
another source archive to identify a source.

## Reuse rule

1. Make the input key for the step.
2. If the worktree is dirty, do not reuse. Build the step.
3. If the key matches a recorded key, reuse the result.
4. If the key does not match, build the step again.

A matching key is sufficient for reuse. Do not reread, rehash or re-archive the
sources, stages or outputs. A failed build records no key. Thus an older output
at the same path is not reused.

## Checks that are not necessary

Do not add checks that reread or rehash inputs that a git hash or input key
already identifies.

- Do not add a preflight or a final recheck of these inputs.
- Do not verify a stage again when its input key matches.
- Do not verify a release again when its input key matches.
- Do not compare file contents with a record when the key matches.

Checksums of downloaded archives stay necessary. Git does not identify them.
Build, package, target and hardware tests stay separate from reuse. They test
the result; they do not decide reuse.
