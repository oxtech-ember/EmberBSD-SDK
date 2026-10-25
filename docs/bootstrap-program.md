# Fixed direct-source bootstrap programs

These finite profiles build initial native programs from OS-owned contracts.
They admit only an independently accepted seed and exact plan.
They do not qualify a provider or bypass ordinary NativeToolset admission.
They need no completed package dependency graph or future build output.
Origin: EmberBSD (AI-assisted). The OS retains its source licenses and authors.

Use `BOOTSTRAP_PROGRAM_API = 1` from `emberbsd_sdk.bootstrap_program`.
The [fixed GNU configure/make variants](bootstrap-gnu-program.md) use the same
entry point and evidence controls with their separate exact source contracts.
The [fixed initial BSD installer](bootstrap-bsdinstall.md) uses that entry point
with two Ports-owned C files and an explicitly accepted static libnbcompat input.
The [initial package tools](bootstrap-pkg-install.md) use the canonical pkgsrc
bootstrap mode with the current libarchive and the same compatibility input.
The original source-owner contract is `tools/grep/bootstrap.json` in EmberBSD.
Its SHA256 is
`cdb8f56fd1107b1cc9ccdbe4cc352599e8575b7984f3c6b6a074426c71e56d3e`.
The SDK constructs commands from that data. It does not copy a recipe.

## Accepted inputs

The plan format is `emberbsd-native-gcc16-producer-plan`, version 1,
scope `gcc16-bootstrap`. The stages are `grep-bootstrap`, `cmp-bootstrap`,
`hostname-bootstrap`, `basename-bootstrap`, `dirname-bootstrap`, `cat-bootstrap`,
`head-bootstrap`, `uniq-bootstrap`, `wc-bootstrap`, `tr-bootstrap`, `printf-bootstrap`,
`pwd-bootstrap`, `cut-bootstrap`, `env-bootstrap`, `uname-bootstrap`, `tail-bootstrap`,
and `sort-bootstrap`.
Each stage selects one exact source-owner manifest.
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
Darwin direct-source links also use the fixed `-Wl,-v` diagnostic.
This requests the actual linker version and search paths through collect2.
Inspect that command-reported evidence; it is not kernel execution tracing.
The shared heavy-link context remains exclusive through link drainage.
The SDK checks each actual object's native header and the final native executable.

The SDK creates relative `egrep` and `fgrep` links to the owned output.
It writes the manifest's two fixed input files under the exclusive work lease.
The next 14 commands use the selected absolute mksh and its `print -rn --` builtin,
a quoted pipeline, and separate stdout/stderr redirections.
They check the source-owner stdin/file, regular/extended/fixed pattern,
quiet, line-number, multiple-pattern, pattern-file, and alias cases.
The missing-file control must remain absent.
Match, no-match, and invalid-input exits are checked as exact 0, 1, and 2 values.
Successful and no-match cases require exact output bytes.
Error cases require actual nonempty stderr and exact stdout bytes.
The shared literal-stdin constructor checks the MIRBSD shell family first.
It preserves quoted UTF-8 input bytes without escape processing or an added newline.
No external `printf` is needed to feed stdin in any finite program profile.

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

## Fixed basename and dirname profiles

Each profile uses one existing C implementation and the shared annotation header.
Their manifests are `tools/basename/bootstrap.json` and `tools/dirname/bootstrap.json`.
The respective SHA256 values are:

- `28d250d01b132c2fab6619094ebd2a00862baafb3610d759a0d84ad53874bd46`.
- `581b5d3fced84f25c3b185de626ff78a7eb45009c01b916a255438655ba1dd47`.

For either name, the complete source roster has seven regular files:

- `tools/<name>/Makefile`, `README.md`, and `bootstrap.json`.
- `tools/Makefile.host` and `tools/compat/bootstrap_cdefs.h`.
- `usr.bin/<name>/Makefile` and `<name>.c`.

The accepted native libc supplies the path function, `getopt`, `err` and locale APIs.
No configure step, generated source or extra library is selected.
The same forced-header compile and one-object link route applies.
The runner then checks 14 basename cases or 11 dirname cases.
They cover empty, root, repeated-slash, trailing-slash and ordinary paths,
option termination, missing operands, excess operands and invalid options.
Basename also checks matching, absent, complete-name and empty suffixes.
Dirname checks the dot path. Outputs and exits come from the fixed source contract.
A system utility never computes the expected output.
The SDK pins the owner manifest digest and checks the case schema and count.
It does not maintain a second table of semantic arguments or expected output.

The final runtime observation uses the first pinned semantic case's arguments.
Both source manifests select `/usr/file`.
This is a path string; the program does not open it.
The resulting plans have 17 and 14 commands, respectively.
The source, argv, environment, separate streams and pending-qualification checks
are shared with the other direct-source profiles.

Run the source-owner consistency check against an explicit OS checkout:

```sh
EMBERBSD_TEST_OS_SOURCE=/absolute/os python3 -B -m unittest discover \
    -s tests -p test_bootstrap_program_native.py
```

This checks manifests, source rosters and links. It runs no native compiler.
Actual compilation, source analysis and runtime acceptance remain separate.

## Fixed cat, head and uniq profiles

These profiles use the existing OS C implementations and shared annotation header.
Each source roster has the same seven members as the path profiles.
Cat uses `bin/cat`; head and uniq use `usr.bin/<name>`.
Their pinned manifests are `tools/<name>/bootstrap.json`:

- cat: `f9db202d70a9cb0b39a0486f14f137d3fb29b1c00ed75fcffe93aaf86c5faf1a`.
- head: `34f117e1bbcc6801597c85c682b5252075b939815527b9c47b2a04e79fbddfe6`.
- uniq: `88376a81e9c96b455840a0fb636912442cba7325cee89fbbe9df8eb43401236e`.

The normal `tools/cat/Makefile` keeps its compatibility-library behavior.
The new head and uniq host recipes select native libc with `NOCOMPATLIB=yes`.
The direct-source routes need no configure step or compatibility archive.
The accepted native ABI must supply the interfaces documented by each owner.

The OS manifests own 18 cat, 16 head and 16 uniq cases.
They select stdin, files, options, empty inputs, unterminated lines and errors.
Cat and head also check output after an earlier missing file.
SDK checks only the pinned digest, finite schema and case count.
It keeps no second table of semantic arguments or expected output.

Fixture names are lowercase `.txt` basenames; values are literal UTF-8 strings.
The runner writes them only in fresh owned work and revalidates their bytes.
`missing.txt` must remain absent, including dangling symlinks.
No case may replace a fixture with a generated output.
The initial uniq contract therefore excludes its successful output-file operand.
This finite coverage does not remove that capability from the program.

The first pinned case supplies final runtime-observation arguments.
Each profile reads its owned input fixture without waiting for stdin.
There are 21 cat commands and 19 commands each for head and uniq.
All source proof, selected-tool, argv, stream and drain checks remain shared.
Use `test_bootstrap_program_filters.py` with `EMBERBSD_TEST_OS_SOURCE` to check
real source contracts and rejection of changed manifests before a child starts.
These checks do not establish native compilation, C analysis or runtime acceptance.

## Fixed wc and tr profiles

These profiles reuse the OS implementations and the same direct-source runner.
The OS owns every semantic argument and expected stream in `tools/<name>/bootstrap.json`.
The pinned manifest digests are:

- wc: `38e7672e67b4b423062596252fde87ad4b09ee4977d61f53526214d0e8d6ae88`.
- tr: `88114937d4d600fd4c228836657f03c296ffca4badb5dea446cc67768ac6c05b`.

Wc compiles `usr.bin/wc/wc.c`. Its 18 cases cover counts, longest lines,
stdin, files, totals, empty input and errors. It uses the shared absent-file gate.
The first case reads `input.txt` for the direct runtime observation.
There are 21 commands, including one compile and one link.
The unchanged source derives stack buffers from native `MAXBSIZE`; check the host stack budget.

Tr compiles `usr.bin/tr/str.c` and `usr.bin/tr/tr.c`, with its `extern.h`.
Its 18 stdin cases cover translation, character classes, deletion, squeezing,
complements, ranges, repetition, equivalence, escapes and errors.
It has no file operands or source fixture files.
The direct runtime observation uses valid operands and EOF; semantic cases supply nonempty stdin separately.
There are 22 commands, including two compiles and one link.

The common OS annotation header supplies guarded `__arraycount` for tr's class table.
Its array-size division preserves the element type and any existing host definition.
The full OS compatibility header is unchanged. SDK contains no macro or libc substitute.
Use `test_bootstrap_program_filters.py` and `test_bootstrap_program_tr.py` with
`EMBERBSD_TEST_OS_SOURCE` for source and metadata checks.
Native GCC16 compilation, C analysis, runtime and memory acceptance remain separate.

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

## Fixed printf profile

The manifest is `tools/printf/bootstrap.json`. Its SHA256 is
`f21cac7215af7aeaf2a57e0eacc78dadc67c63d934e14bf1cf6f521a02da4282`.
The seven source files are the manifest, README, host makefile,
`tools/Makefile.host`, `tools/compat/bootstrap_cdefs.h`, and
`usr.bin/printf/Makefile` with `usr.bin/printf/printf.c`.

The profile compiles one unchanged OS C file against the accepted native libc.
It adds only the shared source-header forced include, then links that object.
The 24 OS-owned literal cases cover formatting, conversions, escapes and errors.
SDK validates their structure and pinned bytes; it does not copy their expected outputs.
No fixture file or shell printf builtin is needed.
The final runtime observation repeats the first successful argument list.
There are 27 commands. Native compilation, C analysis and memory acceptance remain separate.

## Fixed pwd profile

The manifest is `tools/pwd/bootstrap.json`. Its SHA256 is
`598906e66805438e5386f6f12478d69147e36d020849dec655d7a1826026a6bd`.
The seven source files are the manifest, README, host makefile,
`tools/Makefile.host`, `tools/compat/bootstrap_cdefs.h`, and
`bin/pwd/Makefile` with `bin/pwd/pwd.c`.

The profile compiles one unchanged OS C file against the accepted native libc.
Its ten OS-owned cases cover physical and logical modes, option order and errors.
Success cases use `stdout_work_root: true` instead of a literal `stdout` field.
Only this profile accepts that exact boolean, and only for exit zero.
The verifier compares actual stdout with the canonical controller work root plus one newline.
It does not use a child-reported expected path or the caller's `PWD` environment.
Wrong nonempty output fails. The fixed command environment gains no override.
Error cases retain literal stdout and nonempty stderr checks.

There are 13 commands, including the final default-invocation runtime observation.
The cases do not cover logical symlink aliases or invalid `PWD` values.
Native compilation, C analysis and memory acceptance remain separate.

## Fixed cut and common efun probes

The cut profile pins `tools/cut/bootstrap.json` at
`169b46d73ce9b36094bd5589bc6def4a061f78fe5d9d3c2579641d41f5c181dc`.
Its `compat` reference selects `tools/compat/bootstrap-efun.json` at
`3aa76b9c12fbb0581205ac1477b5d1f11b79cf980acffbc74e2c9972410ac7d1`.
These are OS-owned contracts. SDK does not copy C probe bodies or semantic cases.
The full source proof includes the common recipe, source fragment, declarations,
namespace header, conversion template, control C file and all five probe C files.
It also includes the unchanged cut C source and its included `x_cut.c` template.

A positive native control compiles, links and runs before any capability probe.
The three declaration probes compile with the selected GCC16 and native ABI.
The two function probes compile and link with the same tuple and explicit linker flags.
Their only permitted exits are 0 and 1, encoded as `expected_exit: [0, 1]`.
Only these exact reconstructed cut steps accept two outcomes.
Other commands retain their single fixed exit requirement.
A zero observation requires an actual native object or executable before recording availability.
A one observation records unavailability for that exact invocation and preserves its diagnostics.
Unexpected exits, a failed control, or incomplete results stop the route.
Review negative diagnostics; an exit alone does not identify the cause of a compiler failure.

After all five drained observations, SDK writes the initial-only `nbtool_config.h`.
Its five `HAVE_*` values come from actual results, not the plan or an ABI preset.
It includes the OS shared annotation and efun declaration headers.
Later steps recheck its bytes. It is not a full OS configure result.
The OS normal compatibility library uses the same declaration header and source fragment.

The next commands compile five complete C files: cut, efun, reallocarr, strtoi and strtou.
They link directly; no previous compatibility archive or package graph is required.
The unchanged allocator and conversion implementations retain their original BSD notices.
All links keep the pilot heavy-link lease. The same source leases and drain gates apply.
The 22 owner cases exercise byte, character and field selection, errors and allocation growth.
The final observation reads the fixed input file with DYLD library diagnostics.
There are 37 commands. Configuration observations, output hashes and raw logs remain separate.
The result is pending independent review and never qualifies an ordinary provider.

Run `test_bootstrap_program_cut` with `EMBERBSD_TEST_OS_SOURCE` for source consistency.
Its compiler children are synthetic. Real compiler probes, C analysis and memory validation
must run separately with the independently accepted exact source and seed plan.

## Fixed env and uname profiles

The OS owns `tools/env/bootstrap.json` and `tools/uname/bootstrap.json`.
Their SHA256 pins are:

- env: `b16ad21f0c3b820ba1345c40606f42ee379eb92c3d098448e0e61b5c83b39846`.
- uname: `668f39859e9bcf7995f9402e046bf1598527f2db294515a40dceb895ca97148d`.

Each source proof has seven members: the manifest, README, host makefile,
`tools/Makefile.host`, `tools/compat/bootstrap_cdefs.h`, the source makefile and one C file.
Both profiles use the same selected native compiler, ABI and shell as other direct programs.
They introduce no compatibility library or alternate command executor.

The env profile has nineteen literal cases and 22 commands.
It tests assignment, clearing, NUL separators, removal, directory changes and error statuses.
Its child cases execute the same owned `./env` output from the work directory.
This tests inheritance and child exit propagation without a host command or default path search.
The preflight requires `missing.txt` to be absent before a missing-command or directory case.
An existing nonexecutable command is represented by the work directory itself.
The final runtime observation uses `env -i` without a child.
This profile does not qualify arbitrary external child programs or other PATH providers.

The uname profile has fifteen cases and 18 commands.
Successful cases declare a nonempty unique list of fields in `stdout_uname`.
The finite names are `sysname`, `nodename`, `release`, `version`, `machine`, and `machine_arch`.
Only uname accepts this field, only with exit zero, and never alongside literal stdout.
The OS manifest owns field order and option semantics, including the distinction between `-a` and `-ap`.
SDK reads the five standard fields with `os.uname()`.
For machine architecture, the Darwin controller maps `os.uname().machine` to a known processor family.
The native-host, compiler and Mach-O ABI checks must agree before this oracle runs.
The C program independently selects the compiler's family, as in Apple's native uname.
ARM maps to `arm`, x86 to `i386`, and PowerPC to `powerpc`.
Unknown machines and failed API calls fail verification without fallback.
No foreign-function call or external command is needed.
Darwin declares `HW_MACHINE_ARCH`, but current kernels can reject that MIB with `ENOENT`.
The non-Apple C implementation keeps its existing sysctl route.
The verifier joins exact field bytes with spaces and appends one newline.
Embedded version whitespace remains unchanged. Child output never supplies expected values.

Run `test_bootstrap_program_env_uname` with `EMBERBSD_TEST_OS_SOURCE` for source consistency.
Unit tests use synthetic children and API replies. Native compilation, actual API results,
C analysis and memory acceptance remain separate checks before qualification.

## Fixed tail profile and bounded data

The OS owns `tools/tail/bootstrap.json`. Its fixed pin binds all semantic cases.
The profile compiles the five complete `usr.bin/tail` sources and uses their shared header.
The twelve source members include both makefiles, the host integration, annotation header and README.
It keeps the existing `tac` invocation as a relative alias of the same output.
The shared runner creates only manifest-declared aliases after the link check.

There are 31 cases and 38 commands. Eight files exercise ordinary and large inputs.
Cases cover line and byte counts, reverse output, headers, stdin, errors and the existing alias.
Large regular files exercise the 10 MiB mapping boundary and a nonaligned final window.
The same owner cases cover the repaired page-aligned mapping and exact window boundaries.
Follow and kqueue code remain unchanged. Real append, rotation and truncation coverage remains pending.
The pipe `-f` case checks the existing pipe behavior only.

Fixture files and literal stdout accept strings or bounded segment lists.
Each list contains one to sixteen strings or objects with exactly `text` and `repeat`.
The text is nonempty UTF-8; repeat is an integer from zero through 33554432.
Boolean counts, nested lists, extra fields and invalid Unicode refuse before child execution.
Each decoded value is limited to 32 MiB. All fixture files together are limited to 64 MiB.
The shared decoder validates byte sizes before allocating repeated data.
It serves fixture creation, current semantic checks and historical semantic checks.
A larger retained stream is read only up to its validated expected size.
The ordinary 16 MiB metadata bound remains unchanged. Existing string cases keep exact UTF-8 bytes.
Stdin and nonempty-error predicates keep their existing contracts.

Run `test_bootstrap_program_data` and `test_bootstrap_program_tail` with `EMBERBSD_TEST_OS_SOURCE`.
Unit checks use synthetic compiler children. Native GCC16 execution, independent output review,
C analysis and memory validation remain separate gates.

## Fixed sort profile

The OS owns `tools/sort/bootstrap.json`, its 36 semantic cases and all fixture bytes.
Its SHA256 pin is `ed61a6a5caae08acd3334bf5769f22df82930cc2a7fd4ae8cdfcf4abffdfd2db`.
The profile retains nine complete sort C files and their three local headers.
It reuses the same four efun sources, five probes and generated configuration as cut.
The source proof has 35 members. No previous compatibility archive is required.

A shared finite efun profile set selects probe preparation, execution, outputs and historical checks.
The source schema still fixes each program's source roster, headers, compiler flags and manifest hash.
Changed probe bytes, configuration facts or source semantics refuse the route.
The historical check derives configuration bytes from the original actual observations.
Current host facts and provisioned headers cannot replace those observations.

The 59 commands include the ABI control, five probes, thirteen program-source compiles,
one link, 36 semantic cases and one runtime observation.
The owner cases cover numeric and field ordering, stable keys, character filters, merging,
order checks, error statuses, NUL data and partial final lines.
A 131073-record fixture crosses the ordinary record-count limit and exercises temporary merges.
A separate long-key fixture exercises line-buffer growth beyond 64 KiB.
The existing bounded decoder creates these fixtures and verifies their exact output bytes.
The NUL-record case reads an owned file; it does not pass NUL bytes through shell stdin.
`LC_ALL=C` and the owned work directory as `TMPDIR` remain fixed in the common runner.

The initial profile does not qualify `-o`, replacement permissions, non-C locales or long-line `-c` behavior.
The C implementation retains these operations. Actual compilation, C analysis and memory validation are separate gates.
Run `test_bootstrap_program_sort`, `test_bootstrap_program_cut` and `test_bootstrap_program_history`
with `EMBERBSD_TEST_OS_SOURCE` for source and synthetic-runner checks.
