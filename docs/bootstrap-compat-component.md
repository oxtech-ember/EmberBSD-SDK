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

1. Compile, link and run the mandatory native utimensat control with loader diagnostics.
2. Link two native-function probes for utimens and lutimens.
3. Compile the complete canonical source.
4. Archive its object with selected ar; index it with selected ranlib.
5. Compile and link the source-owned component check.
6. Run that check normally and with the existing loader diagnostic setting.

The control run and final check observation each retain their own loader trace.
No extra command is added for runtime capture.

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
No cp, mv or ls profile is added by this first component.
The separate [initial touch consumer](bootstrap-program.md#initial-touch-and-named-component-inputs)
requires both filetime and parsedate receipts through the shared named component selector.
It keeps their separate configurations, archives, retained evidence, and qualification limits.

## Initial parsedate component

The separate `compat-parsedate-bootstrap` profile uses the complete OS
`lib/libutil/parsedate.y` grammar. The manifest is `tools/compat/bootstrap-parsedate.json`.
Its SHA256 is `7632e1182ea47ed9a48391c0fd93926416de4c272393441d7271bf5bd8473b0e`.
The exact 17-member source proof includes the canonical grammar, three headers,
normal Makefile and source fragment, six provenance files, and four probe/check files.
The OS owns all probes, parser semantics and feature declarations.
The filetime manifest, 12 commands, exports and old receipts do not change.

This profile has 13 ordered commands:

1. Compile, link and run the required native timegm control, with loader diagnostics.
2. Observe both mktime_z and timegm declaration/link probes.
3. Generate `parsedate.c` with the selected native Byacc and fixed `-p __pd -o` arguments.
4. Compile that generated source with the actual component configuration.
5. Archive the single `parsedate.o` member and index the archive.
6. Compile and link the source-owned semantic check.
7. Run that check normally, then with loader diagnostics.

The selected `yacc` role must be one native absolute invocation inside the seed bundle.
The plan requires `cc`, `yacc`, `ar`, `ranlib`, `sh` and the compiler driver roles.
The feature header precedes native headers in controls, probes and generated configuration.
It requests declarations; it does not assert a host capability.
An absent mktime_z can produce a declaration or link failure with exit one.
That result sets `HAVE_MKTIME_Z=0`. The timegm probe must succeed after the required control.
The controller never generates configuration from an incomplete or failed mandatory probe sequence.

The shared generated-source guard requires an absent output before Byacc starts.
After drainage it records the bounded regular file's hash and permission bits.
It rechecks both before later children, after drainage, and during candidate validation.
The parsedate result adds `generated_modes` beside the existing `generated` references.
Older Byacc, expr and filetime result contracts retain their existing fields and checks.
The bounded work roster, source proof, configuration and artifact hash checks also apply.
No alternate parser, executable hook or custom compiler flag is accepted.

The five exports are `libnbcompat.a`, `nbtool_config.h`, `compat_parsedate.h`,
`compat_parsedate_features.h` and `bootstrap_cdefs.h`.
Candidate and acceptance formats remain version 1, with `component: parsedate`.
Consumers must call `validate_input(receipt_ref, descriptor, component='parsedate')` explicitly.
The default remains `filetime`. Receipt, candidate and requested component must agree.
Independent review, read-only exports, retained source evidence and matching native ABI remain mandatory.

Normal OS routing can bind `yacc` to `HOST_YACC` through the pinned OS contract.
The [native routing API](native-os-routing.md) still requires an accepted ordinary toolset.
This finite profile does not grant ordinary tool qualification or run the normal build.
Actual parser generation, GCC16 probes, semantic execution, C analysis and memory checks remain separate gates.


## Initial listing component

The separate `compat-listing-bootstrap` profile consumes the OS-owned
`tools/compat/bootstrap-listing.json` contract, SHA256
`5dbdf88505b5c30d3c29ed7cebfc0209244eecae714baf4a7ca492b3e9266666`.
The exact 35-member source proof includes four complete translation units,
five headers, the shared normal build fragment, configuration sources,
canonical declaration and test provenance, and source-owned controls and probes.
The SDK contains no formatter, ACL implementation, capability preset or semantic table.
The OS owns a scoped NetBSD ACL header adapter and its source-order checks.
This adapter is a bound build input. The five public exports remain unchanged.

There are 19 ordered commands:

1. Compile, link and run the required native control with loader diagnostics.
2. Observe six native declaration/link probes.
3. Compile canonical getbsize, humanize_number, stat_flags and the shared ACL backend.
4. Archive the four ordered objects with selected ar, then index with selected ranlib.
5. Compile and link the OS-owned check, then run it normally and with loader diagnostics.

The selected roles include `cc`, `ar`, `ranlib`, `sh` and the compiler driver roles.
This finite route requires Darwin. It adds no generator or external-tool exception.
Native formatter availability remains evidence; canonical prefixed implementations remain selected.
The stat-flags and Darwin ACL probes must succeed. Every probe result remains recorded.
The source-owned control verifies native string primitives and the Darwin ACL entry convention.
The source-owned check covers formatting, flag parsing, real ACL-bearing and empty fixtures,
no-follow symlink behavior, target preservation and missing-path errors.

The existing bounded work roster, source-proof checks and before-child dependency hashes apply.
The archive checker requires exactly `getbsize.o`, `humanize_number.o`, `stat_flags.o`
and `compat_acl.o`, in that order, with unchanged bytes. Missing, extra, duplicate,
reordered or changed members fail. Standard archive symbol indexes remain allowed.
The semantic check must remove its private fixtures before the runner can continue.

`validate_input(receipt_ref, descriptor, component='listing')` requires a separately
reviewed listing receipt. It cannot consume filetime or parsedate acceptance.
Its five read-only exports are `libnbcompat.a`, `nbtool_config.h`, `compat_listing.h`,
`compat_acl.h` and `bootstrap_cdefs.h`. Their source and output evidence remain required.
Existing filetime and parsedate manifests, command counts and receipts are unchanged.

This addition does not add an ls consumer or qualify complete OS or Ports libnbcompat.
Actual native controls, other filesystems, C analysis, memory validation and ordinary
provider acceptance require separate evidence. Source and synthetic data checks do not replace them.
