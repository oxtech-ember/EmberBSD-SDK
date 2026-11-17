# Build profiles and build plans

A build profile selects what to build. `profile-plan` finds the closure of a
profile and prints the build plan. It does not build, download, install or
write build results. `profile-build` plans the profile and lets Ninja run the
nodes that are not ready. This is stages 2 and 3 of the
[build graph](https://github.com/oxtech-ember/ember-wiki/blob/main/docs/strategy/sborochnyj-graf.md):
`profile-build` builds prepared sources, the tools and kernels now; sysroots
and sets follow, Ports nodes come in stage 4.

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
  names installation sets. `os.mk` maps `MK*` options to `yes` or `no`.
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

## Plan schema `build-graph-v2`

`build-graph-v2` succeeds the Ports schema [`ports-graph-v1`](ports-plan.md). It
adds OS, prepared source and runtime nodes. `ports-plan.py` keeps
`ports-graph-v1` until stage 4 moves it to this schema.

The plan contains the normalized `profile` and its `profile_sha256`, the
commit, tree and changed paths of each checkout (`os`, `ports`, `sdk`), the
`nodes` and the `waves`. Each node has:

| Field | Value |
| --- | --- |
| `id`, `kind`, `identity` | What the node is: `runtime`, `source` or `os-stage` |
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
  The plan refuses a producer that names another `PACKAGE`. The key binds the
  Git trees of the producer, of `pkgsrc/<package>` and of the shared Ports
  producer helpers.
- `os:tools`, `os:kernel:<config>` and `os:sets`: OS stages, keyed by the OS
  and SDK Git trees, target, make flags, `MK*` options and `MKREPRO_TIMESTAMP`
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
decision yet.

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

- A source node runs `prepare` of its Ports producer
  (`scripts/source-inputs/<name>/source.py`) with the recipe distfile and
  `gpatch` of the host prefix. The output directory name contains the input
  key. A missing distfile stops the build before Ninja runs.
- `os:tools` has no command of its own: `build.sh` of each consumer builds
  or reuses the tools.
- `os:kernel:<config>` runs `ember/build-kernel.sh WORK/kernels CONFIG` with
  the selected host prefix, target toolchain, Binutils, prepared sources,
  `MKREPRO_TIMESTAMP` and `BUILDINFO` of the plan. OS stages share one Ninja
  pool of depth 1; `build.sh` runs the jobs that the stage took from the
  jobserver.

