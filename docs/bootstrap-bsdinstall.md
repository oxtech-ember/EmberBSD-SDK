# Fixed initial BSD installer

> Retired bootstrap admission interface. This document describes historical data.
> Normal native tools are Ports packages in `host-pkg-g16`, built with host GCC16.
> Use [host prefix selection](native-toolset.md); do not create these acceptance receipts for a normal build.

The `bsdinstall-bootstrap` stage builds the canonical pkgsrc BSD installer.
It uses native GCC16, two C files, and one retained static libnbcompat input.
It uses the existing `bootstrap_program.program_steps` and
`bootstrap_pilot.run_producer` interfaces. It adds no process executor.
It does not qualify a native toolset, a library, or a completed SDK.
Origin: EmberBSD (AI-assisted). The BSD sources retain their original licenses.

## Source contract

The fixed Ports manifest is
`profiles/common-build-tools/bootstrap/bsdinstall.json`, SHA256
`3fbcfcd31129ead586d82e4a22e05c0c8512139d4b0554cbfb660e9eab2cb2d9`.
It selects two ordered C files and 19 semantic cases.
The plan uses the existing producer format, version 1, scope `gcc16-bootstrap`.
It has the same top-level fields as the direct-source program profiles.
The host must be Darwin arm64. Resources are one CPU job, one link slot,
and a positive observation budget of at most 120 seconds.

`program` has exactly `source`, `owner`, `manifest`, and `compat`.
Both tree inputs use `root`, `source`, `inventory`, and `source_proof`.
Both source pins repeat `descriptor.sources.ports`.
They use the existing `emberbsd-bootstrap-program-source` proof, version 1.

The owner has three regular Git members:

- The fixed JSON manifest.
- `profiles/common-build-tools/patches/pkgsrc-bsdinstall-native-shell.patch`.
- `pkgsrc/export.conf`.

The derived source has seven regular files under `sysutils/bsdinstall`:
`Makefile`, `DESCR`, `PLIST`, `files/Makefile`, `files/bsdinstall.c`,
`files/setmode.c`, and `files/bsdinstall.1`.
The complete physical tree is read-only. Links and extra files are refused.
Its proof records content SHA256 and every member's `mtime_ns`.
These patched files are derived from the pinned pkgsrc Git tree.
They are not literal members of the Ports Git commit.
The independent admission gate verifies canonical export and ordered patch
derivation before it accepts the program plan.
The SDK compares all current bytes and timestamps. It executes no patch hook.

## Retained compatibility input

Both consumers use the shared [Ports component validator](bootstrap-libnbcompat.md).
It accepts the complete finite Ports stage or the exact historical receipt below.
OS component archives are separate inputs and cannot replace this library.

`program.compat` has exactly `root`, `inventory`, `producer_result`,
`bootstrap_plan`, and `component_receipt`. Each reference has `path` and `sha256`.
The real read-only root contains only regular headers under `include/`,
the static `lib/libnbcompat.a`, and their directories.
The SDK checks its complete physical inventory and static archive signature.
It uses the exact include root and library filename. It adds no library search.

`component_receipt` is an independently accepted initial input, with exactly:

- `status`: `ACCEPTED_INITIAL_LIBNBCOMPAT_INPUT`.
- `scope`: `gcc16-bootstrap`.
- `recipe`: `pkgtools/libnbcompat`.
- `root`, `inventory`, `producer_result`, and `bootstrap_plan`, repeated exactly.
- `preparation` and `independent_review`, each an exact file reference.
- `initial_only`: `true`; `ordinary_provider_qualified`: `false`.

The SDK reads the existing prepared-component data. Its status must be
`PREPARED_INITIAL_LIBRARY_INPUT_ONLY`. Its source result and plan must match.
The complete `copied_members` map must preserve original and retained file
hashes. Original paths must be under that historical plan's output prefix.
The map is evidence of a retained copy, not a new recipe or provider.
The independent review and explicit admission establish copy provenance.
The review must have status `REVIEWED_INITIAL_LIBNBCOMPAT_INPUT_CANDIDATE`,
no findings, closed review leases, the same preparation reference, and
`ordinary_provider_qualified: false`.
The SDK does not reopen or relabel historical logs and original output paths.

The historical producer result can be `FAILED` or
`COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW`. In both cases, its first
two actual commands must exactly match `r000-build` and `r000-install` for
`pkgtools/libnbcompat`, with integer exit 0. Drainage, post-drain input
revalidation, and closed leases must all be true. A later failure remains a
failure of that full SDK. It does not invalidate these completed component
commands or admit any later output.
The saved seed reference must match the historical result and plan.
Its host, compiler, ABI, GCC driver records, prefix, and binding must match
the new program descriptor. Historical source pins stay historical.

## Commands and observations

The stage requires `cc`, `sh`, `strip`, and the selected GCC driver components.
Compilation preserves the complete selected C driver tuple and native ABI flags.
The two fixed defines select the exact shell and strip executable in C source.
Both commands must have one word. A command tail is refused.
The strip path must not need shell quoting. The canonical `-S` route constructs
its strip command text inside the selected shell.
No ambient `STRIP` variable is passed.
The linker uses the exact static archive, native linker flags,
and `-Wl,--threads=1`. Darwin links also request the fixed `-Wl,-v` diagnostic.
This is command-reported linker evidence, not kernel execution tracing.
The existing heavy-link EX lease lasts through drainage.

There are 23 commands: two compilations, one link, 19 semantic cases, and one
runtime observation. Cases use the selected mksh, fixed umask 022, quoted
operands, and separate stdout and stderr files. They check bytes, file types,
modes, required absence, and native headers after strip. The `@PROGRAM@` operand
in the two fixed strip cases resolves only to the built installer.
The shared `print -rn --` stdin route preserves bytes without an external formatter.
The pinned after-install hook also uses mksh `print` builtins only.
The canonical two-file build does not enable `SUPPORT_MTREE`; the metalog case
requires its real refusal. It does not claim an implemented metadata writer.
The last command records `DYLD_PRINT_LIBRARIES=1` separately.

The existing controller retains shared source, compatibility-root, and input
leases, a fresh exclusive work lease, the finite deadline, and failed-drain HOLD.
It revalidates all inputs after drainage. Semantic results and runtime text
remain evidence for independent review. No ordinary qualification follows.
