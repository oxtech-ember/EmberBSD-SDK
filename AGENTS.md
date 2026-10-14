# EmberBSD-SDK contributor instructions

## Temporary build environments and images

Cleanup is a required final step after build, VM, image or validation work.
Apply this policy in local sessions, isolated worktrees and cloud checkouts.

- Before a build, record free space and the exact temporary paths owned by this work.
  Keep scratch data separate from canonical sources and required deliverables.
- Before deletion, preserve sources, local commits, staged/unstaged edits and required
  untracked/ignored files. Extract unique guest-side edits into the source project
  without overwriting unrelated work. Verify the saved data before deleting its origin.
  Preserve credentials privately; never put them in Git or public artifacts.
- Retain required accepted images, rollback images, revisions, checksums and validation
  evidence. Preserve personal data, unique device backups and persistent application data.
- Stop only your own builds and VMs. Check open files, mounts and image backing chains.
  Never remove an active demonstration, another user's environment or a dependency
  of an image that will be retained.
- Remove only an explicit verified list of disposable build directories, toolchains,
  sysroots, temporary VM disks, ISO/RAW images and redundant artifacts from this work.
  Remove exported source copies only after verifying reproducibility and preserving
  every unique change. Never delete canonical source repositories or task branches.
- Before removing a duplicate Git copy, compare refs/commits and verify the retained
  repository or bundle. Keep required history and unfinished work recoverable, with
  the backup copies required by the project's existing policy. Retire worktrees with
  their owning tool after checking local changes and commits.
- A directory named .cache or work is not automatically disposable. Do not purge shared
  caches, Docker disks, images or volumes indiscriminately. Limit cleanup to known,
  owned temporary resources; shared or persistent data needs separate authorization.
- On pause, failure or handoff, retain what continuation needs. For every retained
  large temporary environment, record its path, size, purpose and cleanup condition.
  Age alone is not a deletion criterion. Do not resume paused device work during cleanup.
- After cleanup, verify removed paths and actual free space using df on the host.
  Record reclaimed space and justified retained environments in the existing handoff
  or final response. Logical image capacity is not proof of reclaimed disk blocks.

Read README.md and the relevant component documentation before changes.
Follow CONTRIBUTING.md when it is present. Preserve licenses and source attribution.
Keep public documentation and commit messages in English.
