# Build profiles and build plans

A build profile selects the target, compiler families and OS components.
`profile-plan` evaluates dependencies, caches its queries and prints the work
still needed. `profile-build` uses Ninja to prepare shared sources and build
the selected kernels or release sets with their OS tools. For an application
sysroot, [install a signed artifact](sdk-client.md#application-compiler-and-sysroot)
or [build the sysroot](sysroot.md).

## Profile

The profile is JSON with three independent inputs. It contains no paths, no
package versions and no recipes: versions come from the Ports recipes, paths
come from the client configuration. Start from an example:
[kernel](profiles/kernel.json) or [release sets](profiles/sets.json).

```json
{
  "schema": "emberbsd.build-profile.v1",
  "platform": {"target": "aarch64", "compiler": {"host": "clang", "target": "clang"}},
  "os": {"kernels": ["EMBER64"], "sets": [], "mk": {}, "buildinfo": ""},
  "ports": {}
}
```

- `platform.target` is a [target](../src/emberbsd_sdk/targets.py) name.
  `platform.compiler` selects the `host` and `target` families, `clang` or
  `gcc`.
- `os.kernels` names kernel configurations of the target machine: a
  general-purpose configuration of the OS, or a trial configuration in a
  directory of `build.kernel_configs`. `os.sets`
  names installation sets: the OS release builder makes `base`, `comp` and
  `etc` together, so the plan accepts exactly these three, for the aarch64
  target with Clang (the `aarch64-sysroot` release). With sets, the plan
  refuses `os.buildinfo` for now: the release builder sets `BUILDINFO` (from
  the OS commit) itself.
  `os.mk` maps `MK*` options to `yes` or `no`. The plan refuses a name that
  no file of the OS `share/mk` has: make ignores such a name, but the changed
  options would build all again in new directories. With sets, they replace the
  options of the release profile; the release builder sets `MKCXX`,
  `MKLIBCXX`, `MKLLVM`, `MKLLVMRT`, `MKX11` and `MKREPRO` itself. A release
  with other `MK*` options uses other object, tools and LLVM directories,
  because make does not see a changed option in the kept objects: the first
  build of a new set of options also builds the LLVM stages. The objects of
  each set of options are kept for the next build. Its receipt is for this
  profile build only: release finalization and target acceptance refuse it.
  `os.buildinfo` is the release metadata that the kernel records (`BUILDINFO`).
  Select at least one kernel or set.
- Use `"ports": {}` or empty `sets` and `packages` lists. To select Ports
  roots, pass `--set` or `--package` to the package client's `plan`,
  `source-plan` or `install` command. [SDK package client](sdk-client.md)

## Plan the profile

Add the OS checkout and the common Binutils build to the
[client configuration](sdk-client.md). The Ports checkout is the existing
`source.ports_root`:

```json
"build": {
  "os_source": "/work/EmberBSD",
  "host_bfd_config": "/work/binutils/host-bfd.mk",
  "work": "/work/build",
  "distfiles": "/work/distfiles",
  "sources": {"EMBER_LLVM_SOURCE": "/work/llvm-source"}
}
```

`work` holds the plan, `build.ninja`, the node records and the build outputs.
`distfiles` holds the archives of the Ports recipes. `sources` selects prepared
trees that the plan uses as they are; the common LLVM source is such an input.
`kernel_configs` lists directories of kernel configurations outside the OS,
for example a trial `boards/<id>/configs/<scenario>` of DevBoards. A name
there must not be the name of an OS configuration. The kernel key binds the
configurations of that directory, not its manifest or README.

```sh
python3 scripts/packages.py --config /work/client.json profile-plan --profile /work/kernel.json > plan.json
```

The command prints the plan on standard output. On standard error it prints
the node count of each decision and one line for each node to build, with
the reason.

The plan keeps the results of its slow queries in `CACHE_DIR/plan`: the walks
of the OS make guards and the output of the Ports tools (recipe identities and
the export fingerprint). The key of a result is all of its inputs: the Git
tree of each checkout that the query reads and its changed paths, the make
flags and directories, the target, the host make or gpatch, and the SDK code
and Python of the plan. A changed file has the hash of its bytes in the key; an
untracked directory has the size and time of its files; a changed submodule
has its commit. Ignored files are not in the key: a query that reads an
ignored file can give a result of the earlier file. A result is kept only if
its inputs did not change while the query ran. Remove the directory to remove
old results. `build-sysroot.py` and `build-os-release.py` keep the cache of
their source plans in `SDK/obj/plan-cache`.

## Plan schema `build-graph-v2`

`profile-plan` emits `build-graph-v2` for OS, prepared source, export and
runtime nodes. The Ports exporter and `ports-plan.py` use
[`ports-graph-v1`](ports-plan.md) for evaluated package dependencies.

The plan contains the normalized `profile` and its `profile_sha256`, the
commit, tree and changed paths of each checkout (`os`, `ports`, `sdk`), the
`nodes` and the `waves`. Each node has:

| Field | Value |
| --- | --- |
| `id`, `kind`, `identity` | What the node is: `runtime`, `source`, `export` or `os-stage` |
| `inputs` | The [input key](input-keys.md) parts: Git trees, target, compiler, `MK*` options, `MKREPRO_TIMESTAMP`, release metadata |
| `input_key` | SHA256 of the node identity, its inputs and the keys of its dependencies |
| `dependencies` | Node IDs that this node needs |
| `reasons` | A profile field, or `needed by` the consumer node |
| `decision` | `ready` or `build`, with the reason |
| `outputs` | Outputs that the next stage reads |

`waves` lists the nodes in dependency order. A wave describes order, not
permission to run all its nodes at the same time.

The plan has these nodes:

- `runtime:host-prefix`: the selected native Ports prefix. Its key binds the
  host, the host ABI flags and each tool that the OS native-tool contract
  requires, by its prefix-relative path, to the `+CONTENTS` and
  `+BUILD_VERSION` of its pkgdb owner.
- `runtime:target-toolchain`: the Clang, LLD and LLVM programs of the same
  prefix that the OS builds with (`EXTERNAL_TOOLCHAIN`), keyed the same way.
  The plan supports only the Clang target family for now.
- `runtime:host-binutils`: the common Binutils build, keyed by the
  `inputs.txt` that its producer writes next to `host-bfd.mk`.
- `source:EMBER_*_SOURCE`: a prepared common source. The OS file
  `ember/shared-sources.json` names its Ports package and producer. The plan
  reads it through the OS tool `ember/tools/shared-sources.py describe`, which
  refuses names that differ from the source table of `share/mk/bsd.sys.mk`.
  The plan refuses a producer that names another `PACKAGE`. Ports names the
  current recipe: `scripts/prepared-source-current.py --describe <package>`
  prints the effective recipe identity (overlay or `upstream/pkgsrc`, with
  the Ports patches), for most owners the one that a prepared receipt records, and the distfiles of
  the effective `distinfo`. The key binds that identity and the Git trees of
  the producer, the shared Ports producer helpers and the SDK modules used
  to prepare and validate source trees. The libedit key also
  binds `src/emberbsd_sdk/libedit.py`, which prepares it. A source of a
  producer under `scripts/source-inputs/` also needs `ports:pkgsrc-export`.
- `ports:pkgsrc-export`: one shared pkgsrc export for the Ports producers,
  keyed by the Git fingerprint that `scripts/pkgsrc-export-stage.py
  fingerprint` prints. The stage refuses uncommitted, untracked and ignored
  files in its inputs, and so does the plan: a local edit below `pkgsrc/`
  stops the plan, also for a source that does not use the edited recipe.
  Commit such edits first.
- `os:tools`, `os:kernel:<config>` and `os:sets` bind the OS Git tree,
  target, make flags, `MK*` options and `MKREPRO_TIMESTAMP` (the commit
  time). Kernels and sets also bind `os.buildinfo`. The plan lists the Git
  object and file mode of each SDK runner or shared module used by these
  stages. Sets additionally bind the release, sysroot and LLVM producers.
  When a producer starts using another SDK file, add it to its group in
  [build_profile.py](../src/emberbsd_sdk/build_profile.py). Changes confined
  to SDK documentation, tests or QML tooling preserve the OS keys. Old OS
  and prepared-source receipts made with broader or incomplete keys are
  reevaluated once under the new inputs.

For an explicitly selected source, planning and release reuse hash the same
manifest: `emberbsd-prepared-source.json`, `libedit-source.json` or
`llvm-source.json`, in that order.

The OS supplies the dependency facts; the plan does not list them a second
time. Tools and sets need the sources whose makefile guards their
[build traversal reaches](../src/emberbsd_sdk/prepared_sources.py). A kernel
needs the sources that `ember/shared-sources.json` declares for its
configuration; the OS kernel build stops when the kernel reads other sources.

A node with local changes in a keyed input path, or with such a dependency,
has a null `input_key`. The planner schedules it on each run. When a producer
succeeds, `profile-build` records the node ID, input key and output paths in
`work/results/<id>.json`, replacing `:` in the ID with `_`. Reuse requires
a matching non-null key and all expected outputs.
The virtual `os:tools` edge records completion with an empty outputs map.
Runtime nodes are
`ready`: the plan does not install them. Downloads from signed
`current` exist only for packages; OS stages and sources have no download
decision. The plan does not fetch distfiles: fetch them with pkgsrc
(`bmake fetch`) into `build.distfiles`.

## Build the profile

```sh
python3 scripts/packages.py --config /work/client.json profile-build --profile /work/kernel.json
```

The command writes the plan and `build.ninja` into `work` and runs Ninja from
the host prefix. When all nodes are ready, Ninja does not run. `--explain`
passes `-d explain` to Ninja.

Ninja 1.13 or later receives a GNU make FIFO jobserver through `MAKEFLAGS`.
The SDK joins an inherited FIFO directly when its advertised capacity fits
the profile budget. Ninja receives no explicit `-j`. For a larger or
unspecified parent capacity, the SDK borrows up to its local limit and runs
a private FIFO with those slots until the profile exits. An explicit serial
make budget, a pipe jobserver or a numeric make budget without FIFO
authentication gives the profile its caller's one slot. Standalone builds create a FIFO with `resolve_jobs() - 1` tokens
at `work/.jobserver` (in a private temporary directory for a work path with
whitespace). Each edge holds one implicit slot. An OS stage borrows free
tokens before starting bmake, passes the resulting count as `NETBSD2_JOBS`
or the release builder's `jobs`, and returns those same bytes when it exits.
This count is fixed for the stage. A source producer uses one slot.
SIGKILL can lose borrowed tokens for the lifetime of that jobserver.

Only pending nodes appear in the generated Ninja file. Each has a phony
prerequisite so an old Ninja stamp cannot suppress a decision to rebuild a
missing artifact, even when its recorded key still matches. Each edge writes
`results/<id>.json` after its producer succeeds. On the next invocation
the planner checks the outputs again. An owner with local changes can use
its incremental builder on each run.

- `ports:pkgsrc-export` runs `scripts/pkgsrc-export-stage.py prepare` into
  `work/sdk/stages/pkgsrc-export`.
- A source node runs its producer with the common interface of
  `scripts/source-inputs`: `prepare --distdir DISTFILES --pkgsrc-export STAGE`
  with `gpatch` of the host prefix; a recipe without distfiles gets no
  `--distdir`. The producer selects its archives and names a missing one.
  The SDK prepares libedit itself. The output directory name contains the
  input key.
- `os:tools` has no command of its own: `build.sh` of each consumer builds
  or reuses the tools.
- `os:kernel:<config>` runs `ember/build-kernel.sh WORK/kernels CONFIG` with
  the selected host prefix, target toolchain, Binutils, prepared sources,
  `MKREPRO_TIMESTAMP` and `BUILDINFO` of the plan. OS stages share one Ninja
  pool of depth 1; `build.sh` runs the jobs that the stage took from the
  jobserver.
- `os:sets` runs the OS release builder (`OSReleaseBuilder`, the code of
  `build-os-release.py`) in the Prebuild SDK workspace `work/sdk`, which
  `profile-build` makes for the profile target before Ninja runs. It gives
  the builder the prepared sources of the plan, the host prefix as compiler
  and native tools, the Binutils directory of `host_bfd_config` and
  `build.distfiles`. The builder reuses its build state only for the same
  tools and prepared sources, so the state directories are named by one
  context key `<ctx>`: the keys of the host prefix, target toolchain and
  Binutils, and the path and manifest of each prepared source, which the
  builder compares. These are the objects and
  tools of `build.sh -u` (`work/sdk/obj/os-<ctx>`, `tools-<ctx>`), the LLVM
  phase objects (`llvm-<ctx>`), the LLVM runtime (`llvm-runtime-<ctx>`) and
  the keyed LLVM stages (`work/sdk/stages/llvm-<ctx>`). A changed source tree
  builds the OS again in new directories. A development
  build continues the LLVM stages that an interrupted run left. The release
  goes to `work/sdk/stages/os-sets-<ctx>`, and the record names its set files.
  The compiler flags name this install directory (`--sysroot` and the prefix
  maps), so a release with the kept objects uses the same directory, and make
  does not configure the GNU programs of the OS again. A new release replaces
  the release of an earlier run with the same `<ctx>`.
  The builder needs clean OS, Ports and SDK checkouts, so `profile-build`
  refuses a sets node with local changes. The kept objects are used for an
  earlier OS commit too, if it removes no file of the kept objects other
  than documents (see [source history](source-history.md)). The build states of other contexts stay in `obj` and
  `stages`.

  The key of `os:sets` also binds the shared Ports export, the prepared-source
  validators, the common cross-libtool producer and its patch. The release
  consumes that export even with all sources selected. It binds the OS commit
  because the builder writes it into `BUILDINFO`. The plan walks the release tree with the final `MKCXX=yes` and
  `MKLIBCXX=yes` of the Clang release. It also selects the sources that
  `release.select` of `ember/shared-sources.json` names although no make guard
  requires them: LLVM, and the OpenSSL of Ports, for which the other Ports
  sources of the release are made. A sets plan needs an OS whose list has
  `release.select`.

