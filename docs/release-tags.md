# Prepare a version and tag its sources

Use `scripts/ports-release.py` after choosing an EmberBSD product version.
The command has two phases. `prepare` changes the source version before a build.
`finalize` creates annotated Git tags for the prepared sources.
A person starts each phase deliberately and does the release checks.
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
If any selected source commit changes, prepare a new manifest.
Keep previous source manifests unchanged.

## Create tags

```sh
python3 /absolute/EmberBSD-SDK/scripts/ports-release.py finalize \
  --sources /absolute/release/1.1-sources.json \
  --repo os=/absolute/EmberBSD \
  --repo ports=/absolute/EmberBSD-Ports \
  --repo sdk=/absolute/EmberBSD-SDK
```

The command checks that the three checkouts are clean and that their HEAD
commits and the OS version match the source manifest. It refuses an existing
tag. It requires no package plans, build receipts or check logs.
Run the release checks before this step.

All three repositories receive the same tag name, such as `emberbsd-1.1`.
Each tag points to that repository's recorded commit. Its annotation records the
version, the three source commits and the source manifest hash. Local file
paths are excluded. The tags are annotated, not automatically signed.

Existing tags are never replaced. Git cannot commit tag updates atomically
across three repositories. A partial creation failure reports the created refs
and preserves them for inspection. The command never deletes a tag as rollback.
Inspect the reported state before deciding how to complete a partial operation.

Tag creation is local. Publish each exact tag through the repository's normal
Git procedure, for example `git push origin refs/tags/emberbsd-1.1`.
Do not push all local tags or force an update to a published release tag.

The helper does not assemble OS images, upload packages, or create a GitHub
Release. Those operations retain the [distribution boundary](distribution.md).

The Python module `emberbsd_sdk.ports_release` provides `PORTS_RELEASE_API = 2`.
It exposes `prepare_release` and `create_release(sources_path, repos)`.
Version 1 also required package plans and receipts; version 2 removes them.

Binary OS kits use the separate [OS release composition](os-release.md) contract.
Source version tags do not publish or qualify those binaries.
