# Initial NetBSD nawk bootstrap

The common Ports `pkgsrc/lang/nawk` recipe owns the C sources, generated parser,
command-shell adaptation, test probe, license, and `bootstrap.json` manifest.
SDK supplies a fixed initial producer. It does not maintain another awk source
or accept an arbitrary build command.

This profile is `nawk-bootstrap` with
`netbsd_profile: netbsd11-amd64-nawk-initial-v1`.
It requires the NetBSD 11 amd64 first-native seed admission. Its only extra
input tool is `sh` from an independently accepted
[pkgsrc bootstrap tools receipt](bootstrap-netbsd-gcc16.md).
It does not use make, awk, yacc, a generated shell script, or `/bin/sh`.
Other native hosts and cross recipes keep their existing routes.

## Prepare the source and plan

Use one clean, pinned Ports commit. Export the exact source roster defined by
`bootstrap_nawk.SOURCES`, `HEADERS`, `PROVENANCE`, `PROBE`, and
`MANIFEST_RELATIVE`. Keep all files and directories read-only.
`GENERATED.json` must match the committed parser outputs and generator inputs.
Do not regenerate these files with an unaccepted tool.

The source contract is a JSON file with these fields:

| Field | Value |
| --- | --- |
| `root` | Absolute path to the immutable source snapshot |
| `source` | Exact Ports repository and commit from the seed descriptor |
| `inventory` | Path and SHA256 of `snapshot_tree(root)` |
| `source_proof` | Path and SHA256 of the source proof |

The source proof uses `emberbsd-bootstrap-program-source`, version 1.
It has `source`, `inventory`, and `members` references. The members document
maps each file path to its Git blob ID and SHA256. The independent gate must
check actual Git membership. A self-consistent export is not this proof.
Keep `SOURCE.json`, `GENERATED.json`, the recipe, and the source licenses.

Prepare a fresh seed descriptor with the selected source commits and unchanged
accepted compiler provenance. The old recipe acceptance does not accept this
new plan. Keep the original historical unknowns. Do not relabel retained tools
as an ordinary SDK provider.

Prepare a shell runtime JSON document. Its format is
`emberbsd-netbsd-nawk-shell-runtime`, version 1, scope `gcc16-bootstrap`.
It contains `host`, the exact `pkgsrc_tools_acceptance` reference,
`selected_shell` with the accepted path and SHA256, and `nodes`.
Use the ELF node schema described below. Its only graph root is the selected
shell; include every library and interpreter target. The plan validator checks
these current bytes and ELF metadata before any producer child. The independent
input gate must establish that this graph is complete.

This fixed profile requires `/etc/ld.so.conf` to be absent. A file, directory,
symlink, or dangling symlink causes refusal. The runner takes a read lease on
that exact path. It checks absence before any child and after all children
drain. It does not read or inventory the rest of `/etc`. A host that needs this
loader configuration requires a supported contract extension before use.

Use the selected SDK control Python:

```sh
"$sdk_python" scripts/bootstrap-seed.py plan-nawk \
  --descriptor /absolute/seed.json --descriptor-sha256 SEED_SHA256 \
  --program-source /absolute/nawk-source.json \
  --program-source-sha256 SOURCE_CONTRACT_SHA256 \
  --pkgsrc-tools-acceptance /absolute/pkgsrc-acceptance.json \
  --pkgsrc-tools-acceptance-sha256 PKGSRC_ACCEPTANCE_SHA256 \
  --shell-runtime /absolute/shell-runtime.json \
  --shell-runtime-sha256 SHELL_RUNTIME_SHA256 \
  --work-root /absolute/new-nawk-work \
  --lock-root /absolute/locks \
  --heavy-link-context /absolute/native-link-context \
  --output /absolute/nawk-plan.json
```

This command reads inputs and writes a new plan. It starts no child and issues
no acceptance. It refuses an existing output file.
The finite route has ten C compilations, two links, and eight semantic cases.
The resource agreement uses one CPU job and one link job. Its execution budget
is at most 120 seconds. Startup validation and drainage can take longer.

Every compile uses the exact selected GCC16 command, ABI flags, and
`-DNAWK_COMMAND_SHELL="/absolute/accepted/mksh"` as one argument.
The checked-in parser needs no generator. The C probe uses the same macro.
The probe tests error, signal, mask, interruption, and pipe behavior. Seven awk
cases check fields, status values, the chosen shell, and both pipe directions.
The semantic PATH contains one absent work path. Tests use shell builtins;
the missing-command case requires status 127.

## Accept and execute the initial plan

An independent reviewer must check the complete source roster, generated-file
provenance, compiler binding, ABI inventories, shell receipt, shell runtime,
and every planned route. The reviewer supplies a new exact
`emberbsd-bootstrap-seed-acceptance` receipt and input-route audit.
That receipt must bind the descriptor SHA256 and this plan SHA256.
The existing `input-and-route-only` evidence boundary applies.

Use `scripts/bootstrap-seed.py check` and then `run-producer`.
Both commands take the existing six path/hash options:
`--descriptor`, `--descriptor-sha256`, `--accepted-seed-receipt`,
`--accepted-seed-receipt-sha256`, `--bootstrap-plan`, and
`--bootstrap-plan-sha256`. Repeat `--required-role` for every role from the
prepared plan. The role set contains `cc`, `sh`, and the seed's driver roles.
The shell is loaded from `program.pkgsrc_tools_acceptance`. Its initial ELF
closure is pinned by `program.shell_runtime`. It need not exist
in the compiler seed's tool table. Make and gmake are not added to this route.

The shared runner takes source, compiler, ABI, SDK, shell-provider, and evidence
read leases. It requires fresh work. It retains normal heavy-link locking,
process drainage, input revalidation, and failure evidence.
A successful result is `COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW`.
`ordinary_provider_qualified` remains false.

## Accept the output for the next bootstrap seed

Use the existing
[program history envelope](bootstrap-program-history.md). The provider recipe
is `pkgsrc/lang/nawk/bootstrap.json`. Its source is the original Ports commit.
Its version is `nawk-20260426`. The only provisionable roles are `nawk` and
`awk`. Copy the accepted executable bytes to regular files with those names.
The probe is not a provisionable tool.

The output acceptance keeps the existing format and exact fields. Its checks
must be, in order:

1. `git-source-membership`
2. `native-compile-link`
3. `source-semantics`
4. `reported-tool-routes`
5. `netbsd-output-runtime`
6. `selected-command-shell`

The independent gate must inspect the actual compile and link logs, all exits,
the C probe and awk outputs, source membership, and complete runtime traces.
Capture each finite semantic command with NetBSD ktrace. Decode with an
explicitly selected observation tool. Record both files and their SHA256.
The observer must establish all successful exec paths and loaded DSOs from the
raw evidence. These JSON readers bind the observations; they do not decode a
ktrace stream or establish its completeness.
Each raw or decoded trace must be a nonempty regular file of at most 1 GiB.
The reader checks its SHA256 in chunks of at most 1 MiB. It refuses symlinks,
changed file identities, truncation, and growth during inspection. A shared
trace reference can cover several observations. JSON metadata retains its
separate 16 MiB limit. Historical reuse retains the same file guards.

The runtime audit has format
`emberbsd-netbsd-nawk-bootstrap-runtime-audit`, version 1, scope
`producer-output`, and these fields:

| Field | Value |
| --- | --- |
| `host` | The exact producer host |
| `artifact` | Linked nawk path and SHA256 |
| `selected_shell` | Compiled-in shell path and SHA256 from its accepted result |
| `nodes` | Complete ELF graph rooted at nawk and the selected shell |
| `observations` | One entry for each semantic command, in plan order |

Each node has `sha256`, `elf`, `interpreter`, `interpreter_target`, and
`dependencies`. `elf` is the complete `read_elf_dynamic(path, machine=62)`
result. `dependencies` maps every `DT_NEEDED` name to its audited canonical
node path. `interpreter` preserves the literal `PT_INTERP` path.
`interpreter_target` is its audited canonical loader path. Both values are
null when no interpreter exists. The loader and its dependencies are graph
nodes. Keep the loader's canonical root in the descriptor's host ABI library
inventory, even when it is separate from `/lib` and `/usr/lib`. The same node
schema applies to the initial shell runtime input.

Each observation has the exact `command` object from the finite plan, `ktrace`
and `decoded_trace` references, `executed_paths`, `undeclared_tools`, and
`unresolved_dsos`. The last two lists must be empty. `executed_paths` contains
the selected shell and the case's executable, without duplicates. Deliberate
failed exec attempts in the C and missing-command tests are not successful
executions. The reviewer must still inspect them.

Derive the closure document with
`bootstrap_nawk_runtime.runtime_closure(audit, audit_reference)`.
The output graph must retain every initial shell runtime node unchanged.
The acceptance pins this document and the audit. It also retains a nonempty
semantic review and qualification limits. A producer must not issue its own
independent acceptance.

The next seed loader verifies the exact history and current copied ELF bytes.
It rechecks the shell, loader aliases, and all runtime library bytes.
It also requires `/etc/ld.so.conf` to remain absent. The next bootstrap runner
retains the exact read lease for that path when it reuses this nawk provider.
It refuses a selected `sh` route that differs from the path compiled into nawk.
The original compiler bundle and original output may be retired after their
required evidence is retained. The selected shell and runtime remain live
inputs. A shell-path change requires a new nawk build and acceptance.

## Qualification limits

This producer admits one initial utility. It does not establish a complete
Native SDK, stage 2/3 compiler comparison, package closure, ordinary tool
qualification, or a distributable bundle. Complete those existing gates before
ordinary native package builds.

DSO TLS and `dlopen` behavior require actual shared-object consumer probes.
An executable-only TLS test can be relaxed by the linker and does not prove
that the runtime loader supports the same relocation in a DSO.
This utility producer does not change that separate qualification requirement.

The SDK tests use synthetic ELF and process fixtures. Set
`EMBERBSD_TEST_PORTS_SOURCE` to the selected Ports source to run the real source
and manifest data check. These checks do not constitute native compilation or
runtime acceptance.
