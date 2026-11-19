# Build profiles and build plans

A build profile selects what to build. `profile-plan` finds the closure of a
profile and prints the build plan. It does not build, download, install or
write build results. `profile-build` plans the profile and lets Ninja run the
nodes that are not ready. This is stages 2 and 3 of the
[build graph](https://github.com/oxtech-ember/ember-wiki/blob/main/docs/strategy/sborochnyj-graf.md):
`profile-build` builds prepared sources, the tools, kernels and the release
sets now; sysroots follow, Ports nodes come in stage 4.

## Profile

The profile is JSON with three independent inputs. It contains no paths, no
package versions and no recipes: versions come from the Ports recipes, paths
come from the client configuration.

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
  `gcc`. `platform.board` is an optional board name.
- `os.kernels` names kernel configurations of the target machine. `os.sets`
  names installation sets: the OS release builder makes `base`, `comp` and
  `etc` together, so the plan accepts exactly these three, for the aarch64
  target with Clang (the `aarch64-sysroot` release). With sets, the plan
  refuses `os.buildinfo` for now: the release builder sets `BUILDINFO` (from
  the OS commit) itself.
  `os.mk` maps `MK*` options to `yes` or `no`. With sets, they replace the
  options of the release profile; the release builder sets `MKCXX`,
  `MKLIBCXX`, `MKLLVM`, `MKLLVMRT`, `MKX11` and `MKREPRO` itself. A release
  with other `MK*` options uses other object, tools and LLVM directories,
  because make does not see a changed option in the kept objects: the first
  build of a new set of options also builds the LLVM stages. The objects of
  each set of options are kept for the next build. Its receipt is for this
  profile build only: release finalization and target acceptance refuse it.
  `os.buildinfo` is the release metadata that the kernel records (`BUILDINFO`).
  Select at least one kernel or set.
- `ports` accepts `sets` and `packages` but refuses roots until stage 4. Plan
  Ports with [`plan` or `source-plan`](sdk-client.md) until then.

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

```sh
python3 scripts/packages.py --config /work/client.json profile-plan --profile /work/kernel.json > plan.json
```

The command prints the plan on standard output and a summary on standard error.

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

`build-graph-v2` succeeds the Ports schema [`ports-graph-v1`](ports-plan.md). It
adds OS, prepared source and runtime nodes. `ports-plan.py` keeps
`ports-graph-v1` until stage 4 moves it to this schema.

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
  the producer and of the shared Ports producer helpers; the libedit key also
  binds `src/emberbsd_sdk/libedit.py`, which prepares it. A source of a
  producer under `scripts/source-inputs/` also needs `ports:pkgsrc-export`.
- `ports:pkgsrc-export`: one shared pkgsrc export for the Ports producers,
  keyed by the Git fingerprint that `scripts/pkgsrc-export-stage.py
  fingerprint` prints. The stage refuses uncommitted, untracked and ignored
  files in its inputs, and so does the plan: a local edit below `pkgsrc/`
  stops the plan, also for a source that does not use the edited recipe.
  Commit such edits first.
- `os:tools`, `os:kernel:<config>` and `os:sets`: OS stages, keyed by the OS
  Git tree, the SDK `scripts` and `src` trees (the runners and builders; not
  the documents and tests), target, make flags, `MK*` options and `MKREPRO_TIMESTAMP`
  (the commit time). Kernels and sets also bind `os.buildinfo`.

The OS supplies the dependency facts; the plan does not list them a second
time. Tools and sets need the sources whose makefile guards their
[build traversal reaches](../src/emberbsd_sdk/prepared_sources.py). A kernel
needs the sources that `ember/shared-sources.json` declares for its
configuration; the OS kernel build stops when the kernel reads other sources.

A node with local changes in an input path, or with such a dependency, has
no `input_key` (`null`) and is `build`: a local edit has no reusable identity,
so stage 3 records no result for it. Another node is `build` when no recorded
result has its input key. Stage 3 records results in
`STATE_DIR/build/results/<id>.json`, with `:` in the ID written as `_`, and
the `input_key`; a matching record makes the node `ready`. Runtime nodes are
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

The job budget of `resolve_jobs` is one GNU make jobserver: a FIFO
`work/.jobserver` with one token for each job after the first. The command
needs Ninja 1.13 or later, which takes its jobs from the jobserver through
`MAKEFLAGS` when no `-j` is given; it stops on older Ninja. The budget
replaces the `MAKEFLAGS` of an outer make, so a `profile-build` under make has
its own budget. Each edge holds one job. An OS stage runs bmake, which has its
own job pool, so it takes the free tokens before `build.sh` starts, passes one
job per slot as `NETBSD2_JOBS`, and gives the tokens back when it stops. It
takes them once: a stage that starts while other edges hold tokens keeps fewer
jobs. A source producer is one job and gets no `MAKEFLAGS`; an edge that runs
GNU make with this `MAKEFLAGS` needs GNU make 4.4 or later. A job that stops on
SIGKILL loses its tokens for the rest of the run; each run makes a new FIFO.

Each node that is not ready is one Ninja edge. Its command runs the internal
`profile-node` command with the node ID and its input key, so a changed key
runs the edge again. A node without a key depends on a phony target that never
exists, so it runs on each build; the incremental builder of its owner skips
unchanged work. An edge writes `results/<id>.json` with the input key and the
outputs only after its producer succeeds. A failed or interrupted build keeps
no record, and the next `profile-build` runs that node again. Records of other
nodes keep them ready.

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
  goes to `work/sdk/stages/os-sets-<key>`, and the record names its set files.
  The builder needs clean OS, Ports and SDK checkouts, so `profile-build`
  refuses a sets node with local changes. The builder refuses objects of an
  OS commit that is not a descendant of the last one; remove
  `work/sdk/obj/os-<ctx>`, `tools-<ctx>` and `llvm-<ctx>` then. Earlier
  releases and build states stay in `obj` and `stages`.

  The key of `os:sets` also binds the Ports tree, which has the producers
  that the builder runs, and the OS commit, which the builder writes into
  `BUILDINFO`. The plan walks the release tree with the final `MKCXX=yes` and
  `MKLIBCXX=yes` of the Clang release. It also selects the sources that
  `release.select` of `ember/shared-sources.json` names although no make guard
  requires them: LLVM, and the OpenSSL of Ports, for which the other Ports
  sources of the release are made. A sets plan needs an OS whose list has
  `release.select`.

