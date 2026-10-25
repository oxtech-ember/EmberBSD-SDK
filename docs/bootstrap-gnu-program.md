# Fixed initial GNU utility programs

These initial-only profiles build GNU diffutils 3.12, findutils 4.11.0,
and GNU make 4.4.1.
They use `BOOTSTRAP_PROGRAM_API = 1`, the existing producer plan version 1,
and `bootstrap_pilot.run_producer`. They add no executor or result format.
They issue no ordinary provider qualification.
Origin: EmberBSD (AI-assisted). GNU source licenses remain with each release.

## Source contracts

The exact Ports specifications are:

| Stage | Source-owner path | SHA256 |
| --- | --- | --- |
| `diffutils-bootstrap` | `profiles/common-build-tools/bootstrap/gnu-diffutils.json` | `7da6d3dd3c973e9e9550f88266d8dc3b612c6319ef9b33b1d09828d56cfb0f50` |
| `findutils-bootstrap` | `profiles/common-build-tools/bootstrap/gnu-findutils.json` | `dca85a054a5a4af27925eba8b78abc2f3ec093e5e874c2b70b7321169dba27b7` |
| `gmake-bootstrap` | `profiles/common-build-tools/bootstrap/gnu-gmake.json` | `111fe6fbec75de0facb45c6559f31d44bd5f7e535fd47bb6a6fc6f21498b6cec` |

The specification retains the canonical recipe, configure arguments, make
targets, output paths, required roles, literal fixtures, and semantic cases.
The SDK accepts no alternative target or command suffix.
The initial `--disable-nls` option does not change the ordinary recipes' NLS policy.

The plan keeps the direct-program fields documented in
[bootstrap-program.md](bootstrap-program.md). Its `program` object has exactly
`source`, `owner`, `manifest`, `archive`, and `patches`.
`manifest` and `archive` are regular `{path, sha256}` references.
`patches` is the complete ordered list of canonical patch references.
The archive must match the recipe's SHA256, SHA512, BLAKE2s and Size values.
The fixed GNU make recipe publishes SHA512, BLAKE2s and Size. Its archive must
match those three values and its raw SHA256 file reference.
Each patch must also match its pkgsrc distinfo SHA1 after complete LF records
that contain `$NetBSD` are removed. Its raw SHA256 and owner Git blob remain pinned.

`source` and `owner` each have `root`, `source`, `inventory`, and `source_proof`.
Both source pins repeat `sources.ports`. For the release tree this names its
recipe owner. It does not claim that release files are committed to Ports.
The complete trees contain only regular files and directories.
Both proofs use `emberbsd-bootstrap-program-source`, version 1, with exactly
`format`, `version`, `source`, `inventory`, and `members`.

The owner tree contains its selected specification and exact canonical recipe
files. Diffutils needs `DESCR`, `Makefile`, `PLIST`, `distinfo`, `options.mk`, and
all three patches. Findutils needs `DESCR`, `Makefile`, `PLIST`, `distinfo`, and
both patches. Paths keep their `pkgsrc/devel/diffutils` or
`pkgsrc/sysutils/findutils` prefix. Its member rows are the existing file-only
`{git_blob, sha256}` records. The independent gate verifies Ports Git membership.
GNU make needs `DESCR`, `Makefile`, `PLIST`, `distinfo`, `options.mk`, and all six
patches under `upstream/pkgsrc/devel/gmake`. Those recipe files belong to the
pinned pkgsrc Git submodule. The gate must verify the Ports gitlink and each
submodule blob. The top-level specification remains a Ports Git member.

The prepared release tree contains all release files after those ordered patches.
Its proof member map covers every inventory entry. File rows have `sha256` and
`mtime_ns`. Directory rows have only `mtime_ns`. These are content and timestamp
records, with no Git membership claim. The independent input audit must establish
the exact archive-to-patches-to-complete-tree relation before acceptance.
The live SDK checks every prepared byte, mode and timestamp. It runs no patch tool.
Do not replace that independent provenance check with a self-reported status.

## Fixed execution

The diffutils and findutils resource agreements require one CPU job. GNU make
requires two CPU jobs for its fixed `-j2` semantic case. Its shell bootstrap
compiles sequentially. All three agreements require one heavy-link slot and
at most 1200 seconds of command observation. Every configure and build command holds the
outer shared heavy-link context exclusively through complete child drainage.
No nested launcher field is accepted. This serial route does not change the
separate parallel full-SDK build policy.

The controller copies the full admitted release tree into fresh `work/source`.
It retains every mtime. It adds owner-write permission to files and owner access
to directories in the private copy. It checks the resulting bytes, modes and
timestamps before any child. The admitted source remains unchanged.

`work/bin` contains only exact native utility roles listed by the specification.
Each alias names the accepted absolute invocation. No system PATH is appended.
Compiler and make roles never enter this raw alias directory.
The old find and xargs providers are absent. Diff is absent while building
diffutils. Findutils requires the accepted source-built diff.
The cmp and hostname histories must also be accepted source builds.

GCC, C++, CPP, assembler, linker, archive, symbol, strip, shell and make command
tuples remain explicit. Native ABI arguments occur once in their named flag
variables. `LDFLAGS` also selects `-Wl,--threads=1`.
Configure uses explicit equal build and host triplets from the accepted compiler.
`CONFIG_SITE=/dev/null` prevents an ambient site file. No cached capability result
is supplied. Each make target uses the complete accepted GNU make tuple,
including its selected `SHELL` argument, and `-j1`.

GNU make uses `gnu-configure-bootstrap`. It requires no `make` or `gmake` input
role, command variable, or utility alias. After configure, the selected shell
runs the release's `build.sh`. The script builds the complete `source/make`
executable. It does not need a later make target. The complete release proof
also includes `README`, `build.sh`, and `build.cfg.in`.
The selected shell must have an accepted source-build provider and history.
The canonical recipe's `PKGSRC_DEFAULT_SHELL` define embeds that shell path.
Its semantic case checks the resulting default shell.
The script expands compiler and flag variables without `eval`. This fixed
route accepts literal command and ABI words without whitespace or shell
metacharacters. The SDK retains the C string quotes in the generated define.
The other two profiles keep their existing shell quoting rules.

Only the fixed seven diff cases or fifteen find/xargs cases run.
They use the selected shell's printf builtin and distinct stdout/stderr files.
Every case with a child appends only the selected shell tuple and its fixed
source-owner `command_args`. Xargs always has that explicit command.
It never uses its implicit echo child in these cases.
Expected exits, exact streams, and marked nonempty error streams remain mandatory.
The SDK checks every built output's native executable header before semantic cases.
One final `--version` invocation per output retains DYLD library observations.
Diffutils has 12 commands. Findutils has 22 commands. GNU make has 11 commands,
including eight cases for its version, default shell, space-containing variable,
recursive make, two-job execution, question mode, recipe failure and missing target.

The existing source/input shared leases, work exclusive lease, deadline,
failed-drain HOLD, raw log references, and post-drain revalidation apply.
Completion remains `COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW` with
`ordinary_provider_qualified: false`. Actual source routes, semantics and runtime
evidence need independent acceptance before any later input provisioning.
