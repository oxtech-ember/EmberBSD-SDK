# Common LLVM runtime stages

The Clang `build-sysroot.py` path builds its first base in dependency order.
Supply `--llvm-source` prepared by the common Ports producer and a new
`--runtime-work` below the SDK object directory. The builder installs OS
headers, asks Ports for builtins and PIC static libunwind, builds the OS C
libraries and CRT, asks Ports for the full C++ runtime, and then builds the
final sysroot. This path does not require a previous application SDK.

The headers, C base and final target objects have separate configurations.
They share the selected native tools tree; subsequent `tools` operations are
incremental. Completed bootstrap stages are checked against their inputs and
payload before reuse. Failed or incompatible stages are left intact for
diagnosis. `--plan` describes the dependency order without creating outputs;
later commands depend on the artifacts measured by their producers.

`--llvm-runtime-stage` explicitly reuses a completed full runtime and skips
the prerequisite coordinator. Its LLVM version, target, source archive,
recipe files and OS revision must match the selected inputs. The final SDK
copies `usr/pkg/llvm`; it does not substitute a GCC runtime. GCC retains its
explicit `--compiler-family gcc --gcc-archive ... --pkgsrc ...` path.
The runtime manifest keeps the Ports revision that produced it. A later
unrelated Ports commit does not invalidate that origin when the relevant
recipe files still match; changing a recorded recipe file does invalidate it.

Ports owns LLVM runtime recipes, source preparation, compiler options and build
orchestration. SDK provides the shared stage validator in
`emberbsd_sdk.llvm_runtime`, with `LLVM_RUNTIME_API == 1`. The
[builtins producer](https://github.com/oxtech-ember/EmberBSD-Ports/blob/main/pkgsrc/lang/llvm/README.runtime.md)
implements the first component stage. OS integration and the full runtime
producer remain incomplete. A runtime stage is not a completed SDK, an
execution result, or an ABI compatibility claim.

## Bootstrap order

The first LLVM SDK cannot require an existing final LLVM SDK. Complete the
producer and OS integration in this order:

1. Prepare the limited EmberBSD base headers with the native OS
   [`bootstrap-headers` operation](https://github.com/oxtech-ember/EmberBSD/blob/main/tools/docs/cross-build.md#headers-and-crt-for-runtime-bootstrap)
   and prepare the common LLVM sources. The OS requires the canonical prepared
   libedit source and excludes the old compiler runtime headers. A builtins-only
   stage consumes these actual inputs without an application sysroot receipt.
2. Build PIC static libunwind with the common recipe. The base OS can consume
   that archive while building its libc.
3. Use the resulting base headers, libc and CRT to build shared libunwind,
   libc++abi and libc++. Keep the base input tree stable during this operation.
4. Build the complete application sysroot against the full runtime stage.

The OS owns crt0, crti, crtn, crtbegin and crtend. The common runtime producer
does not replace them with objects from another OS. The OS `bootstrap-crt`
operation installs the limited headers and builds only the OS CRT objects;
it may run before builtins or libunwind because it does not link a target
executable or shared library. These operations do not create a full base or
application sysroot. Their control-flow and make-metadata checks do not claim
actual compilation with the selected native prefix. Compiler-rt builtins must
be selected through the matching Clang NetBSD driver in its normal library
ordering; placing an archive before driver-added C++ libraries is insufficient.

The proposed OS inputs are `EMBER_LLVM_SOURCE` for prepared common sources and
`EMBER_LIBUNWIND_ARCHIVE` for the preliminary stage archive. These selectors
are not implemented in the OS build yet. Its current libc build still refers
to the bundled compiler-rt and libunwind sources. Replace those consumers
before accepting a base built with the common LLVM runtime. Final integration
must also select the target resource directory and keep the selected
compiler's matching resource headers separate. The application sysroot and
release builders continue to reject Clang until this integration is complete.

## Manifest

The stage contains `runtime.json` and an install tree below `usr/pkg/llvm`.
Paths in the manifest are relative; the source archive field is a basename.
SHA256 and SHA512 values use lowercase hexadecimal. The object has exactly:

| Field | Meaning |
| --- | --- |
| `format`, `version` | `emberbsd-llvm-runtime`, integer `1` |
| `compiler_family`, `llvm_version` | `clang`, numeric LLVM release |
| `target`, `triplet`, `machine_arch`, `elf_machine` | One matching SDK target definition |
| `components` | One of the profiles below |
| `prefix` | `usr/pkg/llvm` |
| `source` | `{archive, sha512}` for the common source archive |
| `recipe` | `{ports_revision, files}` with recipe-relative file SHA256 values |
| `base` | `{os_revision, files, symlinks}` describing the actual base input tree |
| `artifacts` | Measured stage-relative output paths, described below |
| `files`, `symlinks` | Complete payload file SHA256 and relative symlink maps, excluding `runtime.json` |
| Optional `bootstrap` | `{manifest_sha256, files}` for a consumed bootstrap stage; artifact paths remain stage-relative |

The component profiles are:

- `['builtins']`: `artifacts.builtins` is the builtins archive.
- `['builtins', 'libunwind-static']`: additionally,
  `artifacts.libunwind = {static: PATH}` identifies the preliminary archive.
- `['builtins', 'libunwind', 'libcxxabi', 'libcxx']`: each named library has
  `{static: PATH, shared: PATH}` and `artifacts.cxx_headers` identifies the C++
  header directory. It contains `vector`. All artifact paths come from the
  installation, rather than an assumed library suffix or SONAME.

Full `libunwind` also satisfies a caller requesting `libunwind-static`.
Applications require all four full components. A preliminary archive does not
claim to supply a shared runtime. Record PIC and other ABI options through the
owning recipe and verify actual links; an ELF header alone cannot prove PIC.
When a full build consumes the earlier bootstrap rather than producing its own
builtins, the producer records `bootstrap` and verifies that stage's source,
recipe, target and unchanged base inputs before copying its artifacts. A record
without this field does not claim such an external dependency.

## Shared validator

```python
from emberbsd_sdk import llvm_runtime

assert llvm_runtime.LLVM_RUNTIME_API == 1
record = llvm_runtime.validate_stage(
    stage, target="aarch64", required_components=llvm_runtime.FULL_COMPONENTS,
    ports=ports_source, ports_revision=selected_ports_commit, base=base_sysroot)
```

`validate_identity(record, *, target=None, required_components=())` checks the
portable shape without accessing local paths. `validate_stage` additionally
checks the complete payload, contained links, target ELF types, LLVM shared
library SONAMEs, runtime dependencies/search paths and nonempty archive members.
Optional `ports` checks declared recipe files and requires the explicit
`ports_revision` from the caller's source inspection; it does not find Git on
`PATH`. Optional `base` checks the recorded input files and links as a subset,
so adding the next base layer does not invalidate unchanged headers. It never
requires a final SDK manifest. Both functions return the record or raise
`SDKError`. These checks cannot discover omitted recipe dependencies or prove
compiler options, runtime behavior, ABI compatibility or deployment closure.

## Shared source extraction

Runtime preparation uses `emberbsd_sdk.sourceprep` with
`SOURCE_PREPARATION_API == 2`:

```python
sourceprep.extract_archive(
    archive, staging_directory, archive_root, "LLVM",
    components=("compiler-rt", "libunwind", "libcxx", "libcxxabi", "runtimes",
                "cmake", "third-party", "libc", "llvm/cmake"),
    max_members=500000, max_bytes=4 * 1024**3)
```

The caller verifies the archive checksum and chooses bounds for its recipe.
Components are relative paths below the archive root and may name files or
directories. Every requested component must exist. The member bound covers
the entire archive; the byte bound covers selected file data. Extraction checks
case collisions, including implicit directories, for filesystems without case
sensitivity. Links and special files remain unsupported. The LLVM 23.1.4
runtime source subset above contains no links.
The caller owns an empty staging directory and removes partial output after a
failure. Defaults retain the original 100,000-member, 256 MiB, no-links behavior
used by libedit and libelf preparation.
