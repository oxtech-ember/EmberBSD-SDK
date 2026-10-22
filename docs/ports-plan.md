# Ports update plans

`ports-plan.py` helps a coding agent plan work from evaluated Ports metadata.
It reads JSON and hashes local artifacts. It never runs builds, resolves package
versions, edits sources, calls an LLM, or accesses Git. The agent runs existing
Ports producers, inspects failures, patches consumers, and repeats the checks.

Use the reusable `emberbsd_sdk.ports_plan` module with `PORTS_PLAN_API = 1`.
Its public functions are `lock_graph`, `validate_lock`, `make_plan`,
`validate_plan`, `record_result`, and `plan_status`. Each function raises `SDKError` for a
contract violation. The CLI also catches filesystem errors.

## Evaluate and lock a graph

A Ports-owned collector must evaluate dependencies with the package framework.
It must resolve package paths and validate version constraints before export.
The SDK does not parse package Makefiles or implement a version resolver.

Export this exact schema. SHA256 values below are abbreviated for readability;
real input requires 64 lowercase hexadecimal characters.

```json
{
  "schema": "ports-graph-v1",
  "context": {
    "target": "aarch64",
    "triplet": "aarch64--netbsd",
    "toolchain_sha256": "...",
    "base_sha256": "...",
    "shared_inputs": {"pkgsrc-mk": "...", "host-config": "..."}
  },
  "provenance": {"source": "https://example.org/ports.git", "revision": "commit-id"},
  "nodes": [
    {
      "id": "target:security/openssl",
      "version": "3.6.0",
      "input_sha256": "...",
      "dependencies": []
    },
    {
      "id": "target:www/curl",
      "version": "8.16.0",
      "input_sha256": "...",
      "dependencies": [{"id": "target:security/openssl", "kind": "runtime"}]
    }
  ]
}
```

Dependency kinds are `build`, `tool`, `runtime`, and `test`. All kinds affect
input keys and ordering. Multiple kinds may refer to the same node. Duplicate
nodes and duplicate `(id, kind)` edges fail validation. Missing dependencies,
cycles, extra fields, and unknown schemas also fail validation. JSON reads
reject duplicate map keys and use the SDK depth limit. The Ports CLI uses a
64 MiB per-document limit for both reads and writes. This allows full package
graphs with embedded baseline and candidate locks. Other SDK readers retain
their own limits; module callers must select `PORTS_JSON_LIMIT` explicitly when
reading Ports documents through `read_json`.

The collector owns target and triplet validation. Node IDs are opaque strings.
Use distinct IDs for host tools and target packages, such as `host:devel/bmake`
and `target:devel/bmake`. A cross graph must evaluate each context correctly.
Do not describe native host tools as target packages. The planner cannot infer
machine context from a dependency kind or qualify cross metadata by itself.

`input_sha256` must bind the package recipe, patches, distfile checksums,
evaluated options, configuration, and other package-specific inputs. Hash
shared build rules and host configuration in `shared_inputs`. Toolchain and
base hashes must identify the actual declared inputs. These declarations do
not themselves qualify a toolchain or sysroot.

```sh
python3 scripts/ports-plan.py lock old-graph.json --output old-lock.json
python3 scripts/ports-plan.py lock new-graph.json --output new-lock.json
python3 scripts/ports-plan.py plan old-lock.json new-lock.json --output plan.json
python3 scripts/ports-plan.py status plan.json
```

Output creation is exclusive. Keep old locks and plans; use new output paths.
The CLI refuses an output larger than the JSON read limit before creating it.
Reduce the selected graph scope when a plan exceeds this limit.
The planner normalizes order and hashes canonical UTF-8 JSON with sorted keys,
compact separators, and no trailing newline. Each package key includes its
ID, version, package input hash, context, and sorted dependency keys and kinds.
Source provenance is retained in the lock hash but excluded from package keys.
An unrelated source commit alone does not invalidate unrelated packages.

The plan embeds both locks. It lists new, changed, removed, and impacted nodes,
changed context fields, per-node reasons, and deterministic topological waves.
Impact follows reverse dependencies in both snapshots. A shared context change
invalidates all candidate nodes. A target or triplet change is explicit and
also invalidates all candidate nodes. Never reuse old target artifacts for it.
Waves describe ordering, not permission to execute all nodes concurrently.
Removed nodes are reported for review; the tool never deletes their files.

## Validate results and resume

Record a receipt after a producer finishes. Supply the explicit outcome and
producer log. The command fills in plan and dependency keys and hashes evidence.
It does not infer success from existing artifacts or inspect a process exit code.

```sh
python3 scripts/ports-plan.py record plan.json target:security/openssl \
  --outcome success --log /absolute/logs/openssl.log \
  --artifact /absolute/packages/openssl.tgz --output openssl-result.json
python3 scripts/ports-plan.py record plan.json target:www/curl \
  --outcome failure --log /absolute/logs/curl.log --output curl-result.json
```

The result uses this exact schema:

```json
{
  "schema": "ports-result-v1",
  "plan_sha256": "...",
  "id": "target:www/curl",
  "input_key": "...",
  "dependency_input_keys": {"target:security/openssl": "..."},
  "outcome": "success",
  "log": {"path": "/absolute/logs/curl.log", "sha256": "..."},
  "artifacts": [{"path": "/absolute/packages/curl.tgz", "sha256": "..."}]
}
```

The command copies keys from the candidate lock and plan. It lists each direct
dependency once.
A `success` receipt needs at least one artifact. A `failure` receipt requires
an empty artifact list. Treat producer exit status and artifact provenance as
separate evidence: never create success evidence for an old output after a
failed producer. Receipts are trusted local declarations, not signatures or
proof that a producer actually executed. Keep the referenced producer log unchanged; status also verifies its hash.

```sh
python3 scripts/ports-plan.py status plan.json \
  --receipt openssl-result.json --receipt curl-result.json
```

Status rehashes artifact and log files and checks the plan and input keys. It rejects
stale plans, stale keys, duplicate result nodes, absent or modified artifacts,
and success evidence with unverified or failed dependencies. Every dependency
needs matching success evidence, including unchanged baseline packages. An
unchanged input key does not prove that a package cache contains valid bytes.
`required_evidence` lists unchanged dependencies that still need verification.

`ready` lists impacted nodes whose dependencies have accepted evidence.
`failed` lists failed impacted nodes. `blocked` maps pending nodes to missing
or failed direct dependencies. `accepted` lists all verified result nodes.
`status` exits zero when the query validates, even when work is failed or
blocked. Read the JSON fields to decide the next action. Invalid input or
evidence exits nonzero.

`complete` means that every impacted candidate node has artifact evidence.
It does not confirm removal of old packages, ABI compatibility, installation,
regressions, target execution, or physical hardware acceptance.

After a failure, inspect the existing producer log and fix the responsible
recipe or consumer. Export and lock the changed graph, then create a new plan.
Old receipts fail the new plan hash. Reuse a prior artifact only after checking
its unchanged input key and provenance. Write a fresh receipt for the new plan;
status rehashes its bytes. Provide one chosen receipt per node. To retry the
same inputs, retain the failed receipt as history and select its replacement
in the next explicit status call.

The tests cover an OpenSSL update, consumer failure, a compatibility patch,
resume, stale evidence, changed edges, removals, cycles, shared inputs, and CLI
output refusal. These are host contract tests with synthetic artifacts. They
do not establish a real package build or target acceptance.
