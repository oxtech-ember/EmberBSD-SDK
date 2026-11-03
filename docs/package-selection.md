# Selecting Ports packages

This page covers source builds. For already built components, the
[binary catalog](binary-catalog.md) lists and plans the same versionless sets
without a Ports checkout or bmake on the reader.

An SDK workspace starts with no selected packages. Choose versionless package
roots for one task; the shared Ports controller resolves target and native
dependencies, reuses compatible packages and objects, and installs into the
configured build prefixes and target sysroot. It does not install software on
a board. An empty selection does no work; it never means “build everything.”

Package sets live in the Ports source tree at
`profiles/package-sets/*.txt`. Each non-comment line is a versionless
`PKGPATH`, optionally suffixed with `@native`. Sets name only their roots;
they do not duplicate recipes, versions, dependency resolution or toolchains.
Additional `--package` arguments add roots to the selected set. Repeated roots
are collapsed while preserving order. The target architecture and ABI come
from the selected `MAKECONF`; choose them separately from package roots.

List and inspect sets without a configured SDK or bmake:

```sh
python3 scripts/packages.py --ports-root /path/EmberBSD-Ports list
python3 scripts/packages.py --ports-root /path/EmberBSD-Ports show --set plasma
```

For `plan` and `build`, set `BMAKE`, `MAKECONF` and `PKGSRCDIR` to the selected
build inputs. `--host-prefix` is checked through the SDK native-prefix API; its
bmake must be the same executable as `BMAKE`, and its shell and restricted PATH
are selected from that prefix. `--state` must be an absolute path to the same
persistent cross-bulk state directory for both commands. The controller checks
the target identity and reports the selected roots and dependency-closure role
counts while holding its state lock. `plan` prints this before returning;
`build` prints it before compilation begins.

```sh
export BMAKE=/path/host-pkg-llvm/bin/bmake
export MAKECONF=/path/plasma-cross.mk.conf
export PKGSRCDIR=/path/prepared-pkgsrc
python3 scripts/packages.py --ports-root /path/EmberBSD-Ports plan \
  --host-prefix /path/host-pkg-llvm --state /path/sdk/state --set plasma
python3 scripts/packages.py --ports-root /path/EmberBSD-Ports build \
  --host-prefix /path/host-pkg-llvm --state /path/sdk/state --set plasma
```

The `plasma` set selects only the session root `sysutils/ember-plasma-session`;
the graph supplies its dependencies, including Perl where required. A plan
orders the selected closure. A build orders and builds it under the controller's
single state lock. Both reuse compatible state; neither clears shared
dependencies or objects when a selection is removed.

The model follows explicit component selection in [rustup profiles](https://rust-lang.github.io/rustup/concepts/profiles.html)
and root lists with dependency closure in the [FreeBSD Handbook's Poudriere
package-build guide](https://docs.freebsd.org/en/books/handbook/ports/#ports-poudriere).
