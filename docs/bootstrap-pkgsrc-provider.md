# Scoped pkgsrc bootstrap provider

> Retired bootstrap admission interface. This document describes historical data.
> Normal native tools are Ports packages in `host-pkg-g16`, built with host GCC16.
> Use [host prefix selection](native-toolset.md); do not create these acceptance receipts for a normal build.

This provider consumes a separately accepted current Ports/pkgsrc source export,
its target-side transfer acceptance, and the accepted GCC16 compiler-only seed.
It runs on NetBSD 11.0 amd64. The seed remains scoped to `bmake-bootstrap`;
using it here does not qualify a normal native toolset.

The fixed route runs exactly three commands:

1. `bootstrap/bootstrap --full` creates bmake, pkg_install and the pkgsrc
   bootstrap tools under a fresh private work/prefix tree. `CC`, `CXX`, and
   `CPP` are the exact compiler path from the accepted seed. The host PATH
   contains only hashed, explicitly selected base utilities, including
   `touch`, which the bootstrap uses to verify that its work directory is
   writable; `chown`/`chgrp`, which `install-sh` uses for its `-o`/`-g`
   options; and `ar`, which libnbcompat configure discovers in this PATH and
   uses to create its archive. It also pins the `lorder` archive-tool closure:
   `lorder`, `join`, `mktemp`, `nm`, `ranlib`, `tsort`, `sed`, `sort`, and
   `rm`. NetBSD's `lorder` script checks its helper commands in `PATH`; if one
   is missing, it falls back to `/bin:/usr/bin`. The full private closure
   prevents that ambient fallback. `mktemp` and `tsort` use their accepted
   native `TOOLS_PLATFORM` mappings; `ar`, `groups`, `lorder`, `join`, `nm`,
   `ranlib`, `uname`, and `whoami` stay private PATH aliases because the
   native map does not define those routes.
   `strip` and the remaining bounded pkgsrc utility closure are pinned too.
   After bootstrap, the runner inserts explicit
   `TOOLS_PLATFORM.*` assignments into the generated `mk.conf`, inside the
   pkgsrc settings block, so mapped absolute NetBSD tool paths cannot bypass
   the private aliases. The assignments point to the four bootstrap outputs
   (`awk`, `install`, `sed`, `sh`) or the matching hashed utility alias; the
   `bsdtar` mapping uses the pinned `tar` alias. The NetBSD `mkdir -p` and
   `gzip -nf ${GZIP}` command options are preserved. `[`, `false`, `test` and
   `true` stay shell builtins. `SIZE=:` is exported to all three commands (and
   passed to package bmake), disabling mksh's optional host `size` probe in
   both bootstrap and package phases. The accepted GCC16 path and its prefix
   are explicit; `USE_NATIVE_GCC=yes` makes pkgsrc use that already validated
   compiler instead of recursively building another GCC package. `DISTDIR`
   points to a private child of the fresh work root in both phases. The
   bootstrap script writes `WRKOBJDIR=<bootstrap-work>/wrk` into its generated
   `BOOTSTRAP_MKCONF`; the plan records that effective path as
   `bootstrap_pkgsrc_work` and does not pre-create the bootstrap work tree.
   The later explicit package commands use the separate private
   `<work-root>/pkgsrc-work` path through their bmake arguments. Both work
   trees and downloads stay outside the accepted source export. Work paths are restricted to a safe
   character set before any bootstrap command runs, because they become
   unquoted pkgsrc make assignments. The bootstrap source receives
   `--preserve-path` and does not search ambient make/compiler names.
2. Build and install `shells/mksh` from the accepted source tree using the
   just-produced bmake and isolated pkgsrc configuration. Pass the planned
   `MAKE_JOBS` value as a pkgsrc setting, but do not add top-level `-j`;
   pkgsrc rejects parallel make flags at its infrastructure entry point and
   applies package-level parallelism according to each package's policy.
3. Build and install `devel/gmake` using that same bmake and compiler.

The work root, bootstrap prefix, pkgdb, distfiles, provider `TMPDIR`, and home
are children of a fresh caller-selected build directory. NetBSD's `lorder`
script uses three hard-coded random file templates under host `/tmp` and removes
them with its exit trap; `TMPDIR` does not redirect these paths. The result
records this bounded host temporary-file side effect. Neither `/usr/pkg` nor
`/usr/pkgsrc` is a target. The plan binds the exact two acceptance receipts,
compiler seed descriptor, full Python SDK source roster, host utility hashes,
source paths, resource limits and command vectors. The runner leases referenced
inputs and the work tree, records each actual exit and drained log, checks the
three executable outputs, inventories the new prefix, and revalidates inputs
after process drainage. Before inventory, a finalizer rewrites absolute links
whose lexical and resolved targets both remain inside the output prefix to
relative links; it records each rewrite in `normalized_output_links`. Missing,
external, escaping, or special-file targets remain fatal. This keeps the
inventory's strict relocatable-link contract intact.

Run the route only on the target NetBSD build host:

1. Provide a hash-bound host utility map with the fixed role roster expected by
   `make-provider-plan`, including the selected pkgsrc `USE_TOOLS` closure,
   configure utilities, the lorder helper closure, and the seed toolchain's
   pinned `/usr/bin/as` and `/usr/bin/ld`. Keep `ar`, `as`, `groups`, `join`,
   `ld`, `lorder`, `nm`, `ranlib`, `uname`, and `whoami` as private PATH aliases. Do not invent
   `TOOLS_PLATFORM` mappings for them. NetBSD's native map supplies the
   `mktemp` and `tsort` routes used here.
2. Use `make-provider-plan` to write the data-only plan and review its path,
   compiler selection, private prefix and exact command list.
3. Run `run-provider` with the plan path and SHA256.
4. Have an independent review inspect the source and transfer receipts, exact
   plan, all command logs and exits, process drainage, output inventory and
   installed binary identities. The producer result remains pending until that
   review issues an acceptance receipt.

A pending or accepted result from this route is only evidence for these pkgsrc
bootstrap tools. It does not qualify an ordinary `NativeToolset`, build a
target's wider software set, or authorize target installation. Keep package
builds, target installation and physical hardware checks as separate gates.
