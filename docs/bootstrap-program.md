# Fixed direct-source bootstrap programs

These finite profiles build initial grep, cmp, or hostname from OS-owned contracts.
It admits only an independently accepted seed and exact plan.
It does not qualify a provider or bypass ordinary NativeToolset admission.
It needs no completed package dependency graph or future build output.
Origin: EmberBSD (AI-assisted). The OS retains its source licenses and authors.

Use `BOOTSTRAP_PROGRAM_API = 1` from `emberbsd_sdk.bootstrap_program`.
The [fixed GNU configure/make variants](bootstrap-gnu-program.md) use the same
entry point and evidence controls with their separate exact source contracts.
The [fixed initial BSD installer](bootstrap-bsdinstall.md) uses that entry point
with two Ports-owned C files and an explicitly accepted static libnbcompat input.
The original source-owner contract is `tools/grep/bootstrap.json` in EmberBSD.
Its SHA256 is
`cdb8f56fd1107b1cc9ccdbe4cc352599e8575b7984f3c6b6a074426c71e56d3e`.
The SDK constructs commands from that data. It does not copy a recipe.

## Accepted inputs

The plan format is `emberbsd-native-gcc16-producer-plan`, version 1,
scope `gcc16-bootstrap`. The stages are `grep-bootstrap`, `cmp-bootstrap`, and
`hostname-bootstrap`. Each stage selects one exact source-owner manifest.
It has exactly `format`, `version`, `scope`, `stage`, `seed_descriptor`, `host`,
`sources`, `work_root`, `required_roles`, `resources`, `program`, and `commands`.
The host and all source pins repeat the seed descriptor.
This profile supports Darwin native GCC16 inputs.
Required roles include `cc`, `sh`, and every selected GCC driver component.
The native seed loader still checks all tools, native ABI inputs, and dependencies.

`program` has exactly `source` and `manifest`.
`manifest` is an exact `{path, sha256}` reference under the source root.
`source` has exactly `root`, `source`, `inventory`, and `source_proof`.
Its `source` repeats the descriptor's `sources.os` pin.
For grep, the real source root contains exactly these 11 regular source files and their
parent directories. Links and additional source members are refused:

- `tools/grep/bootstrap.json` and `tools/grep/README.md`.
- `tools/grep/Makefile` and `tools/Makefile.host`.
- `usr.bin/grep/Makefile` and `usr.bin/grep/grep.h`.
- `usr.bin/grep/fastgrep.c`, `file.c`, `grep.c`, `queue.c`, and `util.c`.

`source_proof` refers to data with exactly `format`, `version`, `source`,
`inventory`, and `members`. Its format is `emberbsd-bootstrap-program-source`,
version 1. `source` and `inventory` repeat the source input fields.
`members` references the complete relative-path map. Each row has exactly
`git_blob` and `sha256`. The SDK compares each current regular file with its
SHA256 and Git SHA1 blob identity. The independent admission gate establishes
membership at the accepted OS commit. A self-consistent map does not establish
that provenance. The parser executes no Git or source hook.

The resource object uses the finite pilot's exact five fields.
It requires one CPU job, one linker slot, and an integer runtime observation
budget from 1 through 120 seconds. Inputs must remain immutable under their
shared leases. Work must be a fresh real path disjoint from every input.

## Fixed commands and results

`program_steps(plan, descriptor, selected_commands, abi_flags)` constructs the
complete ordered command list. The loader reconstructs that list before work.
It accepts no custom argument, target, environment, or executable suffix.

The first five commands compile the ordered C sources from the manifest.
Each uses the accepted `cc` tuple, native `cppflags` and `cflags`, `-v`,
the three source-owner feature defines, and the exact source include directory.
The source contract disables NLS, bzip2, and gzip. It adds no diagnostic suppression.
The sixth command links the five owned objects with the same driver tuple,
native `cflags` and `ldflags`, `-v`, and `-Wl,--threads=1`.
The shared heavy-link context remains exclusive through link drainage.
The SDK checks each actual object's native header and the final native executable.

The SDK creates relative `egrep` and `fgrep` links to the owned output.
It writes the manifest's two fixed input files under the exclusive work lease.
The next 14 commands use the selected absolute shell, its `printf` builtin,
a quoted pipeline, and separate stdout/stderr redirections.
They check the source-owner stdin/file, regular/extended/fixed pattern,
quiet, line-number, multiple-pattern, pattern-file, and alias cases.
The missing-file control must remain absent.
Match, no-match, and invalid-input exits are checked as exact 0, 1, and 2 values.
Successful and no-match cases require exact output bytes.
Error cases require actual nonempty stderr and exact stdout bytes.

One final command runs `grep -q needle input.txt` with
`DYLD_PRINT_LIBRARIES=1`. It preserves the raw runtime observation log separately.
The 14 semantic cases have no DYLD diagnostic environment.
No library search override is accepted. Runtime text is observed evidence;
this profile does not claim complete runtime dependency admission or kernel tracing.

## Fixed cmp and hostname profiles

The cmp manifest is `tools/cmp/bootstrap.json`. Its SHA256 is
`a3267d3387b08780fb1e372f5a742dccd9faa84568292506d3389f35e869ad56`.
Its complete source roster has 11 regular files:

- `tools/cmp/Makefile`, `README.md`, and `bootstrap.json`.
- `tools/Makefile.host` and `tools/compat/bootstrap_cdefs.h`.
- `usr.bin/cmp/Makefile`, `extern.h`, `cmp.c`, `misc.c`, `regular.c`, and `special.c`.

The four C files compile in that order. Only the declared `usr.bin/cmp` include
directory is added. The forced include is the absolute source-rooted
`tools/compat/bootstrap_cdefs.h` path. Native ABI and compiler arguments remain
unchanged. The header retains source identifiers and the real noreturn attribute
where the host headers do not provide them. It adds no function stub.
The next command links the four objects under the shared heavy-link lease.
The 18 source-owner semantic cases cover equality, differences, silent and list
modes, stdin, skips, short files, missing files, and invalid arguments.
They require their exact 0, 1, or 2 exits and distinct output streams.
Error cases marked `stderr_nonempty` require actual nonempty stderr.
Literal fixture strings can be empty. The controller writes their exact bytes.
The final observation is `cmp -s same.txt same.txt` with DYLD library diagnostics.
This profile has 24 commands.

The hostname manifest is `tools/hostname/bootstrap.json`. Its SHA256 is
`32450fa3c2d68e63c13f1d32795f4d3e14cab8b5beac909ee27242cef279750d`.
Its complete source roster has seven regular files:

- `tools/hostname/Makefile`, `README.md`, and `bootstrap.json`.
- `tools/Makefile.host` and `tools/compat/bootstrap_cdefs.h`.
- `bin/hostname/Makefile` and `hostname.c`.

It compiles one C file with the same source-rooted forced include, then links it.
The only semantic argument tails are `[]`, `[-s]`, and `[-z]`.
The first two require exit 0. Their fixed `stdout_nonempty` manifest sentinel
selects an exact Python `socket.gethostname()` oracle with a final newline.
The short case uses the part before the first dot. Arbitrary nonempty text fails.
The invalid-option case requires exit 1, empty stdout, and nonempty stderr.
No hostname operand or setter is accepted. The final DYLD observation invokes
the owned hostname with no argument. This profile has six commands.

Both profiles use the same source inventory and Git blob proof checks as grep.
They add no aliases, C++ compiler requirement, compatibility library, custom flags,
or provider qualification. Their initial use requires the declared host libc
interfaces. These fixed contracts do not establish support on another host OS.

## Common execution and evidence

All fixed commands use the existing finite controller, `run_logged`, input/source
shared leases, exclusive work lease, deadline observation, and failed-drain HOLD.
`TMPDIR` is the owned work path. Only selected tool directories enter PATH.
Startup, validation, inspection, and drainage can add wall time to the command
observation budget. Undrained logs remain unfinalized paths, without content hashes.

Run the exact accepted plan with `scripts/bootstrap-seed.py run-producer`.
Use its existing descriptor, acceptance, and plan path/hash selectors and
repeated `--required-role` arguments. The Python entry point is
`bootstrap_pilot.run_producer`.
The raw result format is `emberbsd-native-gcc16-producer-result`.
Each command retains its actual exit and merged `run_logged` log reference.
Each semantic case also records its distinct actual stdout/stderr file references.
The merged log is never presented as either stream.
Files and receipts stay under work EX until their final writes complete.
Post-drain input revalidation remains mandatory.
Completion is `COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW`, and
`ordinary_provider_qualified` remains false. A failure is retained without retry.
Accept the actual source, compiler/linker routes, semantic results, and runtime
evidence separately before provisioning this output as a later bootstrap input.
