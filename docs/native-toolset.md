# Native toolset API 1

Use one explicit native toolset for each build configuration.
Keep this selection separate from the target compiler and target sysroot.
Read the [tool selection policy](tool-selection.md) first.

`emberbsd_sdk.native_toolset.NATIVE_TOOLSET_API` is `1`.
The module uses Python and its standard library.
It does not run compilers, search `PATH`, query tools, or install packages.
It checks current files and an independently accepted producer receipt.
The SDK package `API_VERSION` remains `1`.

```python
from emberbsd_sdk.native_toolset import NATIVE_TOOLSET_API, load_native_toolset

if NATIVE_TOOLSET_API != 1:
    raise RuntimeError("Unsupported native toolset API")

native = load_native_toolset(
    "/absolute/evidence/native-toolset.json",
    descriptor_sha256=descriptor_hash,
    accepted_producer_receipt={
        "path": "/absolute/evidence/qualification.json",
        "sha256": accepted_receipt_hash,
    },
    required_roles=("cc", "cxx", "make", "python"),
)
host_cc = native.tool("cc")
host_cppflags = native.abi_flags["cppflags"]
```

Obtain `accepted_receipt_hash` from the producer acceptance gate.
Do not accept a hash that an untrusted bundle supplies for itself.
A matching hash preserves evidence identity. It does not establish trust.
The qualification producer must inspect actual build commands and tool searches.
It must accept the native C and C++ compile, link, and run results.
This loader does not replace that producer or perform that acceptance decision.
A compiler name, version output, copied binary, symlink, or file hash is insufficient.
Bootstrap seed qualification has a separate scope. This API refuses bootstrap inputs.
The [first-native GCC16 contract](bootstrap-seed.md) defines separate seed admission
and output graduation. Graduation does not replace final qualification.

## Consumer interface

`load_native_toolset(path, *, descriptor_sha256, accepted_producer_receipt,
required_roles)` returns an immutable `NativeToolset`.
The descriptor path is absolute. Both SHA256 arguments are lowercase hex strings.
`accepted_producer_receipt` has exactly `path` and `sha256` keys.
`required_roles` is an explicit nonempty list, tuple, set, or frozen set.
The loader always requires native `cc`, `cxx`, `cpp`, `as`, and `ld`.
It also verifies every declared member and all seven GCC driver components.
It raises `emberbsd_sdk.errors.SDKError` on refusal.

| Member | Result |
| --- | --- |
| `tool(role)` | The selected absolute executable path. A missing role raises `SDKError`. |
| `tools` | Immutable role-to-path mapping. |
| `host` | Immutable actual host OS and CPU mapping. |
| `compiler` | Immutable GCC family, full version, and native triplet mapping. |
| `abi_flags` | Immutable mapping of `cppflags`, `cflags`, `cxxflags`, and `ldflags` to string tuples. |
| `as_dict()` | A new JSON-compatible selection object. |

Standard consumer roles are `cc`, `cxx`, `cpp`, `as`, `ld`, `ar`, `ranlib`,
`nm`, `strip`, `make`, `cmake`, `meson`, `ninja`, `pkg-config`, `python`, and `sh`.
Additional explicit roles can name qualified generators or GCC components.
Role names match `[a-z][a-z0-9_-]*`. No aliases are inferred.

Load once for one configuration under the consumer's normal workspace leases.
The consumer owns build-system variable mapping and command verification.
For make, map the selected paths to the producer's native-tool variables.
Map ABI flags only to native flags such as `HOST_CPPFLAGS` and `HOST_LDFLAGS`.
Keep them outside target flags and target pkg-config searches.
Use the SDK publication API if the consumer publishes a generated make include.
Do not invent another descriptor, selection cache, or qualification format.
Keep selected files immutable until all dependent commands finish.
The loader checks bytes at selection time. It does not hold a lifetime lease.

## Descriptor schema

The UTF-8 JSON descriptor has this envelope:

```json
{"format": "emberbsd-native-toolset", "version": 1, "scope": "native-build"}
```

The following fields are required in addition to the envelope:

| Field | API 1 value |
| --- | --- |
| `bundle_root` | Absolute real directory with real ancestors. |
| `host` | Exactly `{"os": platform.system(), "cpu": platform.machine()}` for the actual host. |
| `compiler` | `{"family":"gcc","version":"16.2.0","triplet":"aarch64-apple-darwin25"}`, with the actual qualified version and native triplet. |
| `sources` | Mapping with at least `sdk` and `ports` pins. Each pin has `repository` and a full lowercase 40-hex `commit`. |
| `providers` | Named providers. Each has a `source` key from `sources`, a source-relative `recipe`, and `recipe_sha256`. |
| `inventory` | File reference for the complete `inventory.snapshot_tree(bundle_root)` JSON object. Store it outside the bundle. |
| `tools` | Explicit role-to-member mapping described below. |
| `driver_components` | Exactly `cc1`, `cc1plus`, `collect2`, `lto1`, `lto-wrapper`, `as`, and `ld`, each mapped to an explicit native member role. The `as` and `ld` values must be `as` and `ld`. |
| `host_abi` | Separate nonempty `headers` and `libraries` arrays. Each entry has `root` and an `inventory` file reference for that complete real input directory. |
| `abi_flags` | Exactly `cppflags`, `cflags`, `cxxflags`, and `ldflags`, each a JSON array of arguments. |

A file reference has exactly `{"path":"/absolute/path","sha256":"<64 lowercase hex>"}`.
The loader verifies the hash before it parses or uses referenced evidence.
The descriptor does not contain the accepted qualification receipt hash.
The separate receipt binds the descriptor hash. This keeps the evidence graph acyclic.

The native tuple has a CPU, an optional vendor, and one supported OS suffix.
An empty vendor is permitted, as in `x86_64--netbsd`.
Linux suffixes are `linux`, `linux-gnu`, or `linux-musl`.
Darwin and NetBSD suffixes can include a numeric version, such as `darwin25.0.0`.
A vendor must not be an OS name. Conflicting OS tags or extra suffixes are refused,
even when recorded `-dumpmachine` output repeats the same invalid tuple.

Each tool member has `path`, `sha256`, `version`, `provider`, and `kind`.
The path is absolute and inside `bundle_root`.
The resolved member must also stay inside that root and be executable.
General system roots and tool trees cannot be bundle providers, even with an accepted receipt.
This includes `/usr`, general `/usr/local`, Homebrew, MacPorts, Linuxbrew, and Apple developer tool trees.
A dedicated provider such as `/usr/local/emberbsd-sdk/native` remains permitted.
Live checks also compare filesystem identity for case-insensitive paths.

Relative in-bundle symlinks are permitted only when the complete inventory binds them.
Absolute or external bundle symlinks are refused.
`kind` is `native` or `script`.
A native member must have a host executable header.
API 1 supports little-endian ELF64 and thin little-endian Mach-O64 executables
for qualified x86_64/amd64 and aarch64/arm64 hosts on Linux, NetBSD, or Darwin.
Unsupported binary formats fail closed. Header checks do not qualify executable behavior.
The separate [Darwin runtime audit](darwin-runtime.md) can inspect a shared library
with one fat32 generic arm64 slice. This does not permit fat tool executables,
change ordinary tool selection, or qualify a runtime dependency by its header.

A script also declares an `interpreter` role for a native member in the same bundle.
Its first line must be exactly `#!<absolute selected interpreter>`, followed by LF.
The selected interpreter path must not contain whitespace.
The kernel cannot select a whitespace-containing interpreter path from this shebang form.
`/usr/bin/env` and implicit interpreters are refused.
Compiler, preprocessor, and driver component roles cannot be scripts.
The accepted producer must establish that a script is its declared upstream tool.
Renaming a compiler wrapper does not establish that provenance.

ABI flag arrays support `-I`, `-L`, `-isystem`, `-isysroot`, `--sysroot`,
joined `-I/path` and `-L/path`, and `--sysroot=/path`.
Search paths must resolve inside the bundle or an explicit native ABI input root.
API 1 also accepts `-O[0-3sg]`, `-g[0-3]?`, `-fPIC`, `-fPIE`, `-fno-common`,
`-pthread`, `-std=...`, `-D...`, `-U...`, `-Wall`, `-Wextra`, `-Werror`, and `-Wno-...`.
For a Darwin host, `-mmacosx-version-min=MAJOR[.MINOR[.PATCH]]` is also accepted.
Each numeric component has one to three decimal digits. The major component is
positive. A component has no leading zero unless it is zero. For example,
`-mmacosx-version-min=26.0` preserves the qualified deployment minimum in both
compile and link arguments. The same flag is refused for other host OS values.
Live payload checks and saved selection checks use the same host-aware grammar.
This option does not select another compiler, linker, sysroot or search path.
Other arguments fail closed. Response files and driver override flags are refused.
Qualify a contract extension before adding an unsupported flag.
Do not use native ABI roots to authorize a system build tool or target input.

## Accepted producer receipt

The qualification receipt has this envelope:

```json
{
  "format": "emberbsd-native-toolset-qualification",
  "version": 1,
  "scope": "native-build",
  "status": "PASS",
  "descriptor_sha256": "<exact descriptor SHA256>"
}
```

It repeats `host`, `compiler`, `sources`, `providers`, `host_abi`, and `abi_flags`
exactly from the descriptor. It also contains these fields:

* `provider_builds` maps each provider name to a provider-build JSON file reference.
* `checks.cc` and `checks.cxx` each contain `source`, `object`, `artifact`,
  `compile`, `link`, and `run`. The first three are file references.
  The source bytes must equal the module's `NATIVE_C_PROBE` or `NATIVE_CXX_PROBE`
  constant. Use a `.c` suffix for C and a `.cc` suffix for C++.
  The object must have a matching host relocatable-object header.
  The artifact must have a matching host executable header and an executable mode.
  All six probe files must have distinct identities. None can alias a selected tool.
* `checks.identity.cc` and `checks.identity.cxx` each map `-dumpfullversion` and
  `-dumpmachine` to actual query command records with matching stdout.
* `checks.driver_components` maps all seven component names to actual
  `-print-prog-name=<component>` command records.
  Use `cxx` for `cc1plus` and `cc` for the other queries.
  Stdout must be exactly the selected absolute member path, apart from surrounding whitespace.
  A bare `as` or `ld` fails. An external absolute path also fails.
* `execution_audit` references the accepted execution-audit JSON object.

A command record has an `argv` array, integer `actual_exit: 0`, and `stdout`
and `stderr` file references. `argv[0]` must select the declared absolute tool.
Identity and driver query arrays must match the complete required query exactly.
API 1 accepts only these complete argument arrays. Each item is one argument:

```python
language_flags = native.abi_flags["cflags"]  # Use cxxflags for the C++ probe.
compile_argv = [native.tool(role), *native.abi_flags["cppflags"], *language_flags,
                "-c", source_path, "-o", object_path]
link_argv = [native.tool(role), *language_flags, object_path,
             *native.abi_flags["ldflags"], "-o", artifact_path]
run_argv = [artifact_path]
```

Compile and link stdout must be empty.
Run stdout must be exactly `emberbsd-native-c-v1` followed by LF for C,
or `emberbsd-native-cxx-v1` followed by LF for C++.
Run stderr must be empty. All three actual exit values must be integer zero.
A `--version` command cannot replace compilation, linking, or execution.
Another existing executable cannot replace a probe output.
The trusted producer must check complete probe commands, input identity, output behavior,
and execution closure before it publishes a PASS receipt.
This loader verifies that these accepted records still have the pinned bytes.

A provider-build receipt has format `emberbsd-native-provider-build`, version `1`,
scope `native-build`, status `PASS`, and these fields:
`host`, the full `source` pin, `recipe`, `recipe_sha256`, `compiler`,
`accepted_input_scope: "qualified-native"`, and a `build_log` file reference.
The loader hashes the full build log as a stream. The JSON metadata size limit
does not truncate or reject a large compiler log. Changed log bytes are refused.
The compiler must be native GCC16. The source and recipe must match the provider.
The acceptance gate must qualify that build and its compiler inputs.
A historical Clang build or unqualified bootstrap seed is not an ordinary provider.
For the first native GCC16, the separate [bootstrap contract](bootstrap-seed.md)
permits `accepted_input_scope: "bootstrap-seed"` only with its complete
`bootstrap_graduation` evidence reference. The seed keeps its original scope.
The output must still pass every ordinary qualification check below.

The execution audit has format `emberbsd-native-execution-audit`, version `1`,
scope `native-build`, status `PASS`, matching `descriptor_sha256` and `host`,
and `tools` equal to the complete descriptor tool records.
Its `undeclared_tools`, `path_searches`, and `undeclared_abi_inputs` arrays must be empty.
Only the trusted producer can establish that its tracing and audit are complete.
These JSON assertions do not authenticate themselves.

## CLI output

Run one check for all roles needed by the configuration:

```sh
python3 scripts/host-toolset.py check \
  --descriptor /absolute/evidence/native-toolset.json \
  --descriptor-sha256 "$descriptor_hash" \
  --producer-receipt /absolute/evidence/qualification.json \
  --producer-receipt-sha256 "$accepted_receipt_hash" \
  --required-role cc --required-role cxx --required-role make --required-role python
```

Success writes one JSON object to stdout and returns `0`.
Refusal writes a diagnostic to stderr and returns `1`.
Invalid CLI arguments return `2`.
The normalized object has this stable shape:

```json
{
  "format": "emberbsd-native-toolset-selection",
  "version": 1,
  "api": 1,
  "descriptor_sha256": "<descriptor hash>",
  "producer_receipt": {"path": "/absolute/qualification.json", "sha256": "<receipt hash>"},
  "bundle_root": "/absolute/native-bundle",
  "host": {"os": "Darwin", "cpu": "arm64"},
  "compiler": {"family": "gcc", "version": "16.2.0", "triplet": "aarch64-apple-darwin25"},
  "tools": {"cc": "/absolute/native-bundle/bin/gcc"},
  "abi_flags": {"cppflags": [], "cflags": [], "cxxflags": [], "ldflags": []}
}
```

The example abbreviates `tools`. Actual output includes every validated role.
Do not use normalized output alone as a new acceptance receipt.

## Finite checks and remaining integration

Run `python3 -m unittest discover -s tests -p test_native_toolset.py`.
The tests use synthetic metadata, executable headers, and log files.
They run no compiler and qualify no real native bundle.
They check immutable selection, CLI serialization, byte and source changes,
host and target mismatch, bootstrap and Clang refusal, external members,
compiler scripts, implicit interpreters, probe failure, metadata-only probes,
source/object/artifact substitution, output mismatch, driver escapes,
audit escapes, and ABI flag separation.

The sysroot and release builders use this API before compiler or runtime queries.
They require explicit descriptor and accepted-receipt pins. Compiler and Python options remain checked aliases.
Commands preserve the qualified selected paths, native ABI flags, and declared-tool `PATH`.
The builders repeat this same loader before outputs, before each dispatch, and after producer execution.
Detected drift after context preparation quarantines both object trees. Restoring input bytes does not permit their reuse.
Keep the selected files immutable; these checks do not provide a lifetime lease.

Actual builds remain blocked before preflight until OS nested native-tool routing is qualified.
There is no override flag. See the [remaining enforcement gaps](tool-selection.md#remaining-enforcement-gaps).
Fixture dispatch tests override admission only inside test subclasses. They qualify no real provider or OS build.

`validate_selection(value, required_roles=...)` checks the existing serialized selection without reading local files.
It reuses the compiler and ABI argument grammar. It checks recorded consistency, not live qualification.
New sysroot receipts use version 2; release profiles use schema 3.
They bind this selection and its input pins to tool paths, native flags, and the producer environment.
Historical sysroot version 1 and release schema 2 retain their original portable verification contracts.
They do not acquire native-toolset qualification from historical verification.
