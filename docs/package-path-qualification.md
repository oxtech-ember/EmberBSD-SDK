# Diagnose package paths before changing a recipe

Use `scripts/qualify-package-paths.py` as an **opt-in diagnostic** during recipe
or shared framework development. It is not a new install, signature, S3 reuse,
or local compile gate. An unchanged local iteration does not rerun it. Run the
relevant check when changing path generation, installation or relocation.

## Keep the path domains separate

- Build/source roots identify temporary producer inputs, not installed paths.
- Stage root is a physical installation destination, not the target prefix.
- Native prefix is the host tools' installation root. Use the existing declared
  text relocation contract when moving those tools.
- Target logical prefix (for example `/usr/pkg`) is not the physical sysroot.
  Do not rewrite it to the host's sysroot path in target packages.
- The application toolchain's validated `sysroot_alias` is a stable command-line
  spelling for the selected physical sysroot, not permission to reuse a foreign
  target or ABI.

Fix the owning shared generator/contract first. Avoid recipe-specific absolute
paths and blind replacements across archives. CMake can use these contracts,
but replacing make with CMake alone cannot repair a wrong prefix or stale cache.

## Static metadata and a cross-root probe

Choose only text interpreted by consumers, such as `.pc`, `.cmake`, `.la`,
configuration scripts or wrappers. Do not scan arbitrary binaries, debug source
paths or documentation as if every matching string were a runtime defect.
The checker reports matches conservatively, with file names and hashes; it does
not rewrite files or automatically infer which path is legitimate.

```sh
python3 scripts/qualify-package-paths.py \
  --root /tmp/sdk-B \
  --forbid-prefix /tmp/sdk-A --forbid-prefix /tmp/stage-A \
  --metadata lib/pkgconfig/example.pc \
  --metadata lib/cmake/example/example-config.cmake
```

For a host runtime test, install/relocate a disposable fixture A to B through
the existing package relocation procedure. Make A and its stage directory
unavailable yourself, then supply an explicit probe, for example:

```sh
python3 scripts/qualify-package-paths.py \
  --root /tmp/sdk-B --forbid-prefix /tmp/sdk-A \
  --metadata bin/example-config -- /tmp/sdk-B/bin/example-config --version
```

The checker never moves or deletes a caller's tree. It refuses a host probe if
any old root still exists, and refuses to run it after a metadata leak. Commands
run without a shell, in B, with the caller's environment and a bounded timeout.
Probe output is inherited; the final stdout line is the JSON diagnostic. Choose
probes that assert useful behavior (host execution, resource loading, downstream
configure/link), not merely commands that always exit zero. Select a clean
PATH/environment when checking that external host tools cannot mask a missing
package dependency. An exit-zero probe establishes only that supplied probe's
result, not full package portability. Run target binaries only through a separate
explicit target/emulator procedure, never as host probes.

Exit codes: 0 for completed diagnostics without detected failures, 1 for a leak
or failing/timed-out probe, 2 for invalid inputs or inability to run the check.
`not-assessed` is not a passing qualification: no metadata selection means no
static assessment; no probe means no execution assessment. Hardware execution
is always separate. This diagnostic is not a signed release attestation.

Keep the downloaded archive immutable. Relocation affects an installed copy
and its existing pkgdb receipts/checksums, not its signed source artifact. The
regression fixture in `tests/test_path_qualification.py` exercises the existing
text relocation with a longer destination containing a space, removes A, then
loads a resource through the relocated script in B. This is a synthetic host
control, not qualification of all SDK packages.

Host probes run in their own POSIX process group. The synchronous main-thread
runner kills that group after completion, timeout, interruption, SIGTERM or
SIGHUP, and restores the caller's signal handlers. SIGTERM/SIGHUP exit status is
128 plus the signal number. SIGKILL cannot be caught: killing the checker with
SIGKILL does not guarantee descendant cleanup. This is not a containment sandbox;
probes must not deliberately detach into another session/process group. Use
short, trusted fixture commands, not background services.
