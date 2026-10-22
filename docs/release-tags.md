# Prepare a version and tag checked sources

Use `scripts/ports-release.py` after choosing an EmberBSD product version.
The command has two phases. `prepare` changes the source version before a build.
`finalize` checks the selected results before it creates annotated Git tags.
Both phases use Python's standard library and the existing SDK modules.

## Prepare the version before building

Use clean, complete OS, Ports, and SDK source checkouts. Keep build outputs,
source manifests, and logs outside those checkouts. The three roots must differ.

```sh
python3 /absolute/EmberBSD-SDK/scripts/ports-release.py prepare \
  --version 1.1 \
  --repo os=/absolute/EmberBSD \
  --repo ports=/absolute/EmberBSD-Ports \
  --repo sdk=/absolute/EmberBSD-SDK \
  --output /absolute/release/1.1-sources.json
```

The version above is an example. Use the version assigned to your release.
`prepare` changes `EMBER_VERSION` in the OS file `share/mk/bsd.own.mk`.
It commits only that file when the value changes. It creates no tag and pushes
nothing. The NetBSD compatibility version and ABI identifiers do not change.
If the source commit fails, preserve the version edit and inspect Git's error.
The command records the resulting OS, Ports, and SDK commits in a new source
manifest. It refuses to replace an existing manifest.

Build from these exact commits with the existing producers. A version change
requires new OS outputs; changing a label on old artifacts is insufficient.
Export each selected Ports graph with the source manifest as an explicit input:

```text
--shared-input release-sources=/absolute/release/1.1-sources.json
```

Use this argument with the [Ports collector](https://github.com/oxtech-ember/EmberBSD-Ports/blob/main/docs/update-ports.md).
Then use [Ports plans and receipts](ports-plan.md) to record the build results.
Do not add the manifest hash to an old graph to relabel old output.
If any selected source commit changes, prepare a new manifest and update the
affected build inputs. Keep previous source manifests and receipts unchanged.

## Check results and create tags

```sh
python3 /absolute/EmberBSD-SDK/scripts/ports-release.py finalize \
  --sources /absolute/release/1.1-sources.json \
  --repo os=/absolute/EmberBSD \
  --repo ports=/absolute/EmberBSD-Ports \
  --repo sdk=/absolute/EmberBSD-SDK \
  --plan /absolute/release/aarch64-plan.json \
  --receipt /absolute/release/openssl-result.json \
  --receipt /absolute/release/curl-result.json
```

The example lists only two receipt arguments. Supply successful receipts for
every node in the selected candidate graph, including unchanged dependencies
and host tools. Repeat `--plan` for other target configurations and `--receipt`
for their results. Each result is matched to its plan hash.
Use one candidate plan for each target and triplet pair. Combine the selected
package roots in that graph. Conflicting configurations for one pair are refused.

The command checks source commits, source version, graph source bindings,
package input keys, and artifact and log hashes. A plan with no changed nodes
does not bypass these checks. Empty package graphs do not qualify a release.
Failures and incomplete or stale evidence stop tag creation.

All three repositories receive the same tag name, such as `emberbsd-1.1`.
Each tag points to that repository's recorded commit. Its annotation binds the
source commits, graph and package identities, and verification hashes. Local
file paths are excluded. The tags are annotated, not automatically signed.

Existing tags are never replaced. Git cannot commit tag updates atomically
across three repositories. A partial creation failure reports the created refs
and preserves them for inspection. The command never deletes a tag as rollback.
Inspect the reported state before deciding how to complete a partial operation.

Tag creation is local. Publish each exact tag through the repository's normal
Git procedure, for example `git push origin refs/tags/emberbsd-1.1`.
Do not push all local tags or force an update to a published release tag.

## Add release checks

Pass `--checks /absolute/checks.json --logs /absolute/new-check-logs` to
`finalize`. The log directory must be new and outside the source checkouts.
The check file uses this schema:

```json
{
  "schema": "ports-release-checks-v1",
  "checks": [
    {
      "name": "package-regressions",
      "component": "ports",
      "argv": ["python3", "scripts/selected-regressions.py"]
    }
  ]
}
```

Replace the example command with an existing check for the selected release.
Each command runs in its component's source root. Arguments are passed directly,
without a shell. Every command must succeed. The source and artifact evidence
is checked again after the commands finish. A check that changes the selected
inputs or outputs cannot authenticate their previous results.

Add future release tests to this list. The command stores the check-definition
hash and successful log hashes in the tag annotation. Without `--checks`, it
checks source and artifact integrity only. It does not claim that an upstream
test suite, installation, boot, or device acceptance has passed. Receipts remain
trusted local producer declarations; hashes are not independent build proof.

The helper does not assemble OS images, upload packages, or create a GitHub
Release. Those operations retain the [distribution boundary](distribution.md).

The Python module `emberbsd_sdk.ports_release` provides `PORTS_RELEASE_API = 1`.
It exposes `prepare_release` and `create_release`. The check runner uses the
existing SDK process backend. Its [host qualification limits](host-platforms.md)
also apply to release checks.

Binary OS kits use the separate [OS release composition](os-release.md) contract.
Source version tags do not publish or qualify those binaries.
