# Common LLVM runtime stages

Ports owns LLVM runtime recipes, source preparation, compiler options and build
orchestration. SDK provides the shared stage validator in
`emberbsd_sdk.llvm_runtime`, with `LLVM_RUNTIME_API == 1`. OS integration uses
the prepared common sources and measured stage artifacts. A runtime stage is
not a completed SDK, an execution result, or an ABI compatibility claim.

## Bootstrap order

The first LLVM SDK cannot require an existing final LLVM SDK. Build in stages:

1. Prepare the EmberBSD headers and common LLVM sources. A builtins-only stage
   consumes those actual inputs without requiring an application sysroot receipt.
2. Build PIC static libunwind with the common recipe. The base OS can consume
   that archive while building its libc and native CRT objects.
3. Use the resulting base headers, libc and CRT to build shared libunwind,
   libc++abi and libc++. Keep the base input tree stable during this operation.
4. Build the complete application sysroot against the full runtime stage.

The OS owns crt0, crti, crtn, crtbegin and crtend. The common runtime producer
does not replace them with objects from another OS. Compiler-rt builtins must
be selected through the matching Clang NetBSD driver in its normal library
ordering; placing an archive before driver-added C++ libraries is insufficient.

The source path is `EMBER_LLVM_SOURCE`. A preliminary static unwind input uses
`EMBER_LIBUNWIND_ARCHIVE`, taken from the stage artifact record. Final runtime
integration additionally selects the measured target resource directory and
keeps the selected compiler's matching resource headers separate.

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
