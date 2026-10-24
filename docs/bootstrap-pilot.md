# First C/C++ bootstrap pilot

This finite pilot runs an independently accepted GCC16 seed once.
It preserves actual failure and command evidence. It does not issue acceptance.
It does not require a completed common-recipe build or a qualified ordinary SDK.
The [full GCC16 recipe plan](bootstrap-seed.md) follows a successful independent
review of the pilot. The seed remains a bootstrap input at every stage.

## Inputs and plan

Use `BOOTSTRAP_PILOT_API = 1` from `emberbsd_sdk.bootstrap_pilot`.
Use the [bootstrap descriptor and receipt](bootstrap-seed.md).
All current binary, driver, native ABI, and runtime inputs must be pinned.
Historical binary-release unknowns require explicit independent acceptance.
No metadata PASS claims that compiler children already ran.

The plan envelope is `emberbsd-native-gcc16-pilot-plan`, version 1,
scope `gcc16-bootstrap`. It has exactly these fields:

| Field | Value |
| --- | --- |
| `purpose` | `native-c-and-cxx-pilot` |
| `seed_descriptor` | Exact `{path, sha256}` reference |
| `host`, `sources` | Exact descriptor values |
| `work_root` | Fresh absolute output directory with real ancestors |
| `required_roles` | `cc`, `cxx`, `cpp`, and all seven selected GCC driver roles |
| `resources` | The finite resource agreement below |
| `commands` | Exact output from `pilot_steps` |

`resources` has exactly `cpu_jobs: 1`, `link_jobs: 1`, integer
`runtime_seconds` from 1 through 120, an existing absolute `lock_root`,
and the shared absolute `heavy_link_context` used by other build owners.
The controller holds that resource fence around each link command and its drain.
Coordinate the CPU and shared linker window before execution.

`pilot_steps(descriptor, commands, abi_flags, work_root)` creates six commands:
C compile, link, run, then C++ compile, link, run.
`commands` is the exact bootstrap tuple mapping, not a PATH lookup.
The sources are `NATIVE_C_PROBE` and `NATIVE_CXX_PROBE` from NativeToolset.
Each compile and link uses `-v` and the selected ABI flags.
Each Darwin link also uses source-owned `-Wl,--threads=1`.
Darwin artifact runs use `DYLD_PRINT_LIBRARIES=1`.
They do not set a library search override.
These diagnostic and resource selectors are pilot policy. They do not extend
the ordinary ABI flag grammar or change ordinary NativeToolset probes.
Every environment is complete and contains only selected tool directories,
`LC_ALL=C`, `TZ=UTC`, `TMPDIR=work_root`, and the explicit runtime observation
flag when applicable. Compiler temporary files stay in the declared work tree.
Shells, make tools, recipe exports, and dependency queries are not pilot inputs.
Declared tools still pass the common current-byte checks.

The accepted seed receipt must add `evidence_level: input-and-route-only` and
`input_route_audit`, an exact reference to independently accepted metadata.
The audit envelope is `emberbsd-bootstrap-seed-input-route-audit`, version 1,
scope `gcc16-bootstrap`, status `PASS`.
It repeats `descriptor_sha256`, `bootstrap_plan`, `tools`,
`dynamic_dependencies`, and `expected_commands` exactly.
It sets `execution_observed: false` and has empty `unresolved_tool_routes`,
`unresolved_abi_inputs`, and `unresolved_dsos` arrays.
Those arrays describe input and planned-route resolution. They do not assert
an observed child execution closure or a qualified ordinary provider.
The pilot does not require the full recipe plan's `execution_audit` field.

## Execute once

Obtain the accepted receipt hash from an independent gate.
Run the selected SDK CLI with its explicit host Python:

```sh
/absolute/python3.15 /absolute/sdk/scripts/bootstrap-seed.py run-pilot \
  --descriptor /absolute/evidence/seed.json --descriptor-sha256 SEED_SHA256 \
  --accepted-seed-receipt /absolute/evidence/accepted.json \
  --accepted-seed-receipt-sha256 ACCEPTED_SHA256 \
  --bootstrap-plan /absolute/evidence/pilot.json --bootstrap-plan-sha256 PLAN_SHA256 \
  --required-role cc --required-role cxx
```

The Python entry point is `run_pilot` with the same loader arguments.
Use the main thread. The caller supplies signal policy as documented by
`process.run_logged`. The CLI maps SIGINT, SIGTERM, and SIGHUP to interruption.
Keep the SDK source and input trees immutable throughout the call.
The controller holds shared input and SDK leases and an exclusive work lease.
It validates the accepted inputs before the first child and after all child drains.
It creates the work directory once. Reuse fails before another child starts.

The runtime observation budget starts before the first command.
Each `run_logged` call receives the remaining budget.
First completion observed at or after its deadline is refused.
Input checks, process startup, inspection, and drainage can add wall time.
This is not a hard upper bound on the entire CLI duration.
A failed ownership drain enters HOLD while all input, work, and active link
fences remain open. Preserve and inspect that owned session before reuse.
The HOLD receipt records the owned SID and `unfinalized_log_path`.
An undrained child can still change its log. The controller does not hash that
live file or label it as a stable log reference.
The controller does not retry a failed command or replace a failed work tree.

## Evidence and next step

`work_root/result.json` records exact `argv`, `cwd`, complete `env`, observed
integer exit when available, and one combined log reference for each attempted command.
Unknown exit remains null. A completed command can have exit 0 and a refused output.
Native object and executable headers and output hashes are checked separately.
The input descriptor and accepted plan are revalidated after drainage.
All persistent work writes finish while the work lease is held.
Only the returned result and CLI stdout can add the observed post-close
`all_leases_closed: true` fact. The work file does not claim this fact in advance.

Completed commands yield `COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW`.
Failures retain `FAILED` and the actual available evidence.
`ordinary_provider_qualified` remains false.
GCC `-v` logs report selected command routes. The runtime loader prints observed
loaded libraries. Neither is kernel execution tracing or automatic DSO qualification.
Inspect actual assembler, collect2, linker, artifact, and runtime library evidence.
The fixed Darwin seed must show the Mach-O `ld64.lld` route.
Do not infer that route from the nominal `-print-prog-name=ld` query.
Resolve any new output runtime routes before ordinary-provider acceptance.
Then execute the pinned common GCC16 recipe plan, preserve stage comparison,
and obtain the separate ordinary-output qualification.
