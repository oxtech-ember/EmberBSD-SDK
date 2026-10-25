# Initial OS compatibility component

The `compat-filetime-bootstrap` stage builds the complete canonical OS
`lib/libc/gen/utimens.c`. It produces one static archive and public subset headers.
It uses the existing program plan format, source proof, seed admission and pilot runner.
It does not invoke configure, make, an installer or an arbitrary hook.

The fixed source contract is `tools/compat/bootstrap-filetime.json`, SHA256
`1152bef47f8a3dce202dae0252490350e68b29bd90adb7a7cf5942633b4faede`.
The OS owns its probes, semantics, declarations and source fragment.
Its normal compatibility library uses the same canonical translation unit.
The SDK contains no C implementation or host capability preset.

The plan has the same fields as an [OS program plan](bootstrap-program.md#accepted-inputs).
`program` has only `source` and `manifest`. Source proofs bind every Git blob.
The exact roster includes the manifest, canonical C file, three headers,
normal Makefile and fragment, normal configure sources and generated files,
the source README, and four probe/check sources.
Selected roles include `cc`, `ar`, `ranlib`, `sh` and the GCC driver components.
This first finite profile accepts Darwin and the selected native GCC16 ABI.
It adds no external-tool exception.

## Commands and generated inputs

There are 12 ordered commands:

1. Compile, link and run the mandatory native utimensat control.
2. Link two native-function probes for utimens and lutimens.
3. Compile the complete canonical source.
4. Archive its object with selected ar; index it with selected ranlib.
5. Compile and link the source-owned component check.
6. Run that check normally and with the existing loader diagnostic setting.

Only probe exits zero and one are capability observations. All other commands
must return zero. Every diagnostic remains available for independent review.
The controller writes `nbtool_config.h` only after the complete probe sequence.
HAVE_UTIMENSAT derives from the successful mandatory control.
HAVE_UTIMENS and HAVE_LUTIMENS derive from the actual link results.
This header does not claim that full OS configure completed.

Before each child, the controller verifies source bytes, generated configuration,
command order and all preceding artifact hashes. A bounded work-directory scan
rejects every unknown file, directory and symlink. It protects the compiler's
work include path, including nested header names. These checks repeat after drainage.
The archive must contain exactly
the recorded canonical object and optional standard symbol indexes.
The controller refuses extra archive members and pre-existing outputs.

The C check tests fixed file times, no-follow symlink times, an unchanged target
and a missing-path error. It removes only its own named fixtures.
Real execution, nanosecond precision, other filesystems, static analysis and
memory diagnostics remain separate validation requirements.

## Retained component input

`bootstrap_compat_component.candidate(plan_ref, result_ref)` returns
`emberbsd-os-compat-component-candidate` version 1.
Its status is `PENDING_INDEPENDENT_COMPONENT_REVIEW`.
It binds the exact plan, drained producer result, seed, OS source and four exports:
`libnbcompat.a`, `nbtool_config.h`, `compat_filetime.h` and `bootstrap_cdefs.h`.
It verifies command exits, retained logs, artifacts and the archive contents.
It re-reads the common source proof, member list, inventory and pinned manifest.
Missing or changed provenance fails before the candidate returns any exports.
Input validation repeats this check; it does not reuse an earlier successful read.
It does not issue acceptance.

`validate_input(receipt_ref, descriptor)` consumes a separate version 1
`emberbsd-os-compat-component-acceptance` receipt, scope `gcc16-bootstrap`.
Its exact fields are `format`, `version`, `scope`, `status`, `component`,
`candidate`, `independent_review`, `initial_only` and `ordinary_provider_qualified`.
Status is `ACCEPTED_INITIAL_OS_COMPAT_COMPONENT`; component is `filetime`.
The last two values must be true and false, respectively.
Candidate and review are exact path/SHA256 references.

The independent review has exactly `status`, `candidate`, `findings`,
`all_leases_closed` and `ordinary_provider_qualified`.
Status is `REVIEWED_INITIAL_OS_COMPAT_COMPONENT`; findings must be empty.
Leases must be closed; ordinary qualification must be false.
The review binds the same candidate. All exported files must be read-only.
The compiler, native ABI, driver binding and driver tools must match the consumer.
Retained artifacts and original evidence remain required; this interface does not relocate them.

This is an OS file-time subset. It is not the complete OS library or the
separate Ports `pkgtools/libnbcompat` contract.
The [BSD installer](bootstrap-bsdinstall.md) retains its existing Ports input checks.
No cp, mv, ls or touch profile is added by this first component.
