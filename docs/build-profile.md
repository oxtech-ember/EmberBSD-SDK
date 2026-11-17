# Build profiles and build plans

A build profile selects what to build. `profile-plan` finds the closure of a
profile and prints the build plan. It does not build, download, install or
write build results. This is stage 2 of the
[build graph](https://github.com/oxtech-ember/ember-wiki/blob/main/docs/strategy/sborochnyj-graf.md):
the generator and `profile-build` come in stage 3, Ports nodes in stage 4.

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
  "host_bfd_config": "/work/binutils/host-bfd.mk"
}
```

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
  Git tree, target, make flags, `MK*` options and `MKREPRO_TIMESTAMP` (the
  commit time). Kernels and sets also bind `os.buildinfo`.

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
