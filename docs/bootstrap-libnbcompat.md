# Initial Ports libnbcompat

The `ports-libnbcompat-bootstrap` stage builds the complete common Ports library.
It uses native GCC16 and the accounted Darwin arm64 ABI. It is an initial input,
not an ordinary library provider or a complete SDK. Origin: EmberBSD (AI-assisted).

## Source and command contract

The fixed owner is `profiles/common-build-tools/bootstrap/libnbcompat.json` in Ports.
Its SHA256 is `340ca308f4d7ccf4618fa7856249e520464ff1e68923bf2689eb353655ef18a0`.
The producer uses the existing envelope, scope `gcc16-bootstrap`, and public
`bootstrap_program.program_steps` and `bootstrap_pilot.run_producer` interfaces.
There is no separate executor.

`program` contains exactly `source`, `owner`, `manifest`, `export`, and `profile`.
The source is a read-only derived program tree with exact member timestamps.
It contains the complete `pkgtools/libnbcompat/` subtree, `bootstrap/bootstrap`,
and their ancestor directories. The bytes must match the canonical Ports export.
The owner has four Git files: the manifest, `pkgsrc/export.conf`, the common
diagnostic helper, and its libnbcompat infrastructure patch. The export and
profile retain the existing complete Ports metadata proof.

The three commands configure, build, and stage the canonical library.
Both common options, `--enable-bsd-getopt` and `--enable-db`, must match the recipe
and bootstrap script. Configuration uses a fresh cache, an owned prefix, and
the exact selected compiler, preprocessor, binutils, shell, and native ABI flags.
The build uses the selected make tuple, including its command-line `SHELL`.
Installation uses the canonical `install-sh -c`; its default move operation is
not selected. The stage has the complete 40-header and static-archive package list.

All manifest utility roles are mandatory, including `cp`, `mv`, `ls`, and `diff`.
The selected native `printf` is also required; the common mksh has no such builtin.
The owned PATH contains only their exact selected aliases. Each child checks
the alias roster and targets again. Configuration diagnostics use the common
Ports helper and the selected `uname`. Unavailable legacy platform queries
remain unavailable. They are not recorded as successful executed operations.

The existing pilot retains shared inputs, exclusive owned work, the finite
deadline, drainage, failed-drain HOLD, and post-drain input validation. Each
command holds the common heavy-link lease. The budget is at most 1200 seconds.
The canonical source bytes, modes, and timestamps stay unchanged in owned work.
Generated configuration bytes and modes remain bound before subsequent children.
Archive members must match all configured native objects, including DB objects.
Canonical `${LIBOBJDIR}name$U.o` entries use explicit empty command-line bindings
for both variables. Nonempty assignments, other expressions and paths are refused.
Installation checks every header against its source or generated file. It checks
the installed archive member by member; indexing may change archive metadata.

## Independent component acceptance

`bootstrap_libnbcompat.candidate(plan_ref, result_ref)` returns pending evidence.
All three actual commands must have integer exit 0. The result must name the
exact plan and original seed acceptance. Drainage, revalidation, and closed
leases must be true. Incomplete and failed standalone producers are refused.
No candidate function issues acceptance.

After drainage, seal only the installed stage read-only. Do not alter source
work or generated files. The candidate permits this removal of write bits.
Then retain an independent review with status
`REVIEWED_INITIAL_PORTS_LIBNBCOMPAT_COMPONENT`, its exact `candidate` reference,
`findings: []`, `all_leases_closed: true`, and `ordinary_provider_qualified: false`.

The separate receipt has format `emberbsd-ports-libnbcompat-component-acceptance`,
version 1, scope `gcc16-bootstrap`, status
`ACCEPTED_INITIAL_PORTS_LIBNBCOMPAT_COMPONENT`, and recipe `pkgtools/libnbcompat`.
Its remaining fields are `candidate`, `independent_review`, `initial_only: true`,
and `ordinary_provider_qualified: false`.

Both BSD install and pkg_install call the same `validate_input(compat, descriptor)`.
The five-field `compat` object remains unchanged. It names `root`, `inventory`,
`producer_result`, `bootstrap_plan`, and `component_receipt`.
The validator checks the sealed stage, original metadata, complete output proof,
and compiler/ABI compatibility. It preserves original source identities and
does not reopen historical compiler or ABI payloads. Source and generated work
must remain available for this new component contract.

The exact older `ACCEPTED_INITIAL_LIBNBCOMPAT_INPUT` receipt remains supported.
Only that retained-copy branch permits a later failure in a complete SDK recipe,
after its two successful library commands. Its original bytes and unknowns stay
historical. An OS file-time or parsedate component cannot replace this Ports library.

Source and synthetic data checks do not qualify native outputs. A real run still
requires exact input admission, selected-route review, independent component
acceptance, C analysis, memory checks, and applicable consumer checks.
