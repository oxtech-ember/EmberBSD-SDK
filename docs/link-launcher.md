# Shared linker resource

Use `scripts/link-launcher.py` to serialize memory-intensive foreground links
from independent builds on one Darwin host. This optional resource fence does
not replace the build controller's source, prefix or workspace locks.

```sh
python3 scripts/link-launcher.py --lock-root /absolute/objects \
    --context /absolute/objects/coordination/heavy-link -- gcc-command link-arguments
```

Select one CPython 3.15 executable and one checked SDK revision for all callers.
Use the same canonical context and lock-root paths in every build. The context
is a resource name. Its directory does not have to exist. The lock root must
already be a real directory. The launcher uses the existing `ContextLocks`
exclusive lock format. It waits when that resource is busy. An invalid lock
entry is an error. No lock-file unlink is required or permitted during use.

The command inherits its exact arguments, environment, working directory and
standard input/output/error. No shell is added. Its normal exit status is
preserved. A command signal becomes `128 + signal`. Launcher failures return
125. An interrupted lock waiter starts no command.

The launcher creates a separate process group in the inherited build session.
It does not create a new session. An outer SDK producer therefore retains its
normal visibility of the whole link chain. Link commands must retain their
effective UID, session ID and process group. Use foreground compiler and linker
drivers. Detached daemons and commands that change their process group are
outside this interface.

The parent retains the exclusive resource until the leader is reaped and its
group is empty. A successful leader that leaves a live descendant is a failed
launch. `SIGINT`, `SIGTERM` and `SIGHUP` stop only that link group. Drainage sends
TERM, then KILL when necessary. If shutdown cannot be proved, the launcher
prints `HOLD` and retains the resource. Inspect the outer owned build session
before reuse. Uncatchable process termination and host failure cannot preserve
a process-owned flock fence.

When selected, the Darwin native make configuration exposes the same service as
`EMBERBSD_DARWIN_LINKER_LAUNCHER`, a shell-quoted argument prefix, and
`EMBERBSD_DARWIN_CMAKE_LINKER_LAUNCHER`, a CMake argument list. Consumers append
their selected compiler command to the appropriate form. The existing
`EMBERBSD_GCC16_LINKER_LAUNCHER` remains available to GCC bootstrap consumers.

For CMake, set `CMAKE_C_LINKER_LAUNCHER` and `CMAKE_CXX_LINKER_LAUNCHER` to the
semicolon-separated Python executable, launcher path and its four option
arguments. These launchers apply to executable, shared-library and module links.
They do not change the compiler. A build system with a native link pool should
also limit its own concurrent links to one. A global fence does not set the
number of compile jobs or impose a memory-pressure policy.

Run the native contracts with:

```sh
python3 -m unittest discover -s tests -p 'test_link_launcher.py'
```

The native tests cover Darwin resource exclusion, inherited stdio and session,
normal/signal status, interrupted waiters and descendant drainage. Other hosts
are refused before lock-file writes. These controls do not qualify a target
package, link result or memory limit.
