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

The OS bootstrap-headers operation does not enter libc or consume the common
LLVM runtime source. Its object and completed-stage compatibility therefore
ignores that source manifest and its `EMBER_LLVM_SOURCE` path, while source
preflight still verifies the selected input. Compiler/ABI options, other build
flags, OS headers and output bytes retain their checks. C base and final
objects continue to depend on the selected LLVM runtime source.

`--llvm-runtime-stage` explicitly reuses a completed full runtime and skips
the prerequisite coordinator. Its LLVM version, target, source archive and recipe files must match the
selected inputs. If source preparation selectors or the runtime CLI changed while
the prepared payload and build remain equivalent, pass
`--llvm-runtime-original-source` with the prepared source used by that runtime.
The Ports owner compares both trees, patches and build behavior before reuse.
Select a runtime whose
ABI is compatible with the chosen OS, including libc, C++ ABI, TLS and unwinder;
matching an OS commit alone does not establish compatibility. The manifest
retains the OS revision that actually produced the runtime, which may differ
from the final OS revision. The final SDK
copies `usr/pkg/llvm`; it does not substitute a GCC runtime. GCC retains its
explicit `--compiler-family gcc --gcc-archive ... --pkgsrc ...` path.
The final Clang build enables the base libc++ install paths by default on
`build-sysroot.py` and `build-os-release.py`. `--base-cxx` explicitly enables
this layout, while `--no-base-cxx` retains the historical build-only layout.
The enabled selection sets `MKCXX=yes` and `MKLIBCXX=yes`, and installs
relative aliases from the measured runtime into `usr/include/c++` and
`usr/lib`. Bootstrap phases and GCC builds never install these aliases.
The runtime manifest keeps the Ports revision that produced it. A later
unrelated Ports commit does not invalidate that origin when the relevant
recipe files still match; changing a recorded recipe file does invalidate it.

Ports owns LLVM runtime recipes, source preparation, compiler options and build
orchestration. SDK provides the shared stage validator in
`emberbsd_sdk.llvm_runtime`, with `LLVM_RUNTIME_API == 1`. The
[common runtime producer](https://github.com/oxtech-ember/EmberBSD-Ports/blob/main/pkgsrc/lang/llvm/README.runtime.md)
implements builtins, preliminary static unwind and full C++ runtime stages.
The OS consumes the prepared common sources and measured stage artifacts.
These interfaces still require actual end-to-end build and target checks;
a runtime stage is not a completed SDK, an execution result, or an ABI
compatibility claim.

## Base install aliases

`emberbsd_sdk.llvm_runtime_install` provides an opt-in base install adapter for
a verified full runtime. `base_alias_plan(record)` derives relative leaf links
from the existing artifact roles and payload inventory: C++ headers appear
under `usr/include/c++`, and the selected static libraries, shared libraries,
SONAME links and linker entries appear under `usr/lib`. The directories remain
real directories. Library content and headers remain solely in `usr/pkg/llvm`;
there is no second header inventory or copied runtime implementation.

Call `preflight_base_aliases(sysroot, record)` before copying the runtime to
refuse existing conflicts without modifying the destination. After importing
the payload, `install_base_aliases(sysroot, record)` checks its bytes and target
object metadata, repeats the complete preflight and creates the links. Hold the
existing sysroot writer lock throughout both operations. Exact existing links
and expected real directories can be reused; unrelated C++ header paths,
different files, symlink ancestors and case-insensitive path collisions are
errors. The adapter never removes conflicting files.

`validate_base_aliases(sysroot, record)` checks the imported runtime and all
derived base paths, including refusal of unlisted legacy C++ headers. These
checks are enabled for build/import records that select base libc++. Transfer
provenance records the optional value `base_cxx: libcxx`; older manifests and
build records without the selection retain their existing format and behavior.
Preliminary C-only and explicit GCC runtimes do not use the adapter.
The aliases preserve installation paths, not a claim of binary compatibility
with the former libcxxrt implementation. Exceptions, TLS and shared-library
execution still need target runtime checks.

## Bootstrap order

The first LLVM SDK cannot require an existing final LLVM SDK. Its producers
run in this order:

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

The OS inputs are `EMBER_LLVM_SOURCE` for prepared common sources and
`EMBER_LIBUNWIND_ARCHIVE` for the preliminary stage archive. The SDK selects
them for the base build, replacing the bundled compiler-rt and libunwind inputs.
Both the C base and final build use `EMBER_EXTERNAL_LLVM_RUNTIME` and the
manifest-derived `EMBER_LLVM_RESOURCE_DIR`, with runtime phases `bootstrap`
and `full` respectively. Compiler resource headers remain separate and match
the selected native Clang. The application sysroot and release builders invoke
these stages; accepting their outputs still requires actual links and target
execution.

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

`validate_install(sysroot, record, target=..., required_components=...)` checks
the recorded runtime subtree after installation or transfer while allowing
unrelated packages elsewhere in the sysroot. Every prefix ancestor must be a
real directory. All declared files and links must match. Extra regular files
are allowed only in the resource `include` directory derived from the builtins
path; extra links or other files in the runtime prefix are rejected. This
permits separately installed compiler headers without establishing their
provenance. Stage validation still requires an exact inventory.
`runtime_paths(root, record)` derives the target resource and
static unwind paths from measured artifact locations. Completed application
consumers use the [shared compiler commands](application-toolchain.md).

## Native base installation and set lists

`SysrootBuilder.copy_runtime(destination=..., installer=...)` uses the same
verified inventory and final byte/mode checks as an ordinary sysroot copy.
The optional destination must be inside the producer's owned output. Base
aliases use the same installer through
`llvm_runtime_install.install_base_aliases(root, record, installer=...)`.
Omitting these arguments preserves the existing Python copy operation.

`native_runtime_install.NativeRuntimeInstaller(destination, tools, os_source,
run)` supplies the three operations `directory(path, mode)`,
`file(source, destination, mode)` and `link(literal_target, destination, mode)`.
`tools['install']` is the already selected native OS `TOOL_INSTALL` executable;
`run(argv)` executes it in that native environment and reports failures. The
adapter requires the existing distribution `METALOG`, uses native `-U/-M/-D/-N`
metadata handling and literal `-l s` links, and preserves existing directories.
It does not select tools again for each file or implement an mtree serializer.

`llvm_runtime_sets.prepare_sets_overlay(source_lists, destination, record)`
creates a complete list root from the OS `distrib/sets` root and the verified
runtime record (manifest, observed inventory and root mode). The original
lists remain unchanged; only the overlay's base/comp `mi` lists gain derived
payload and alias rows, including observed empty directories. ET_DYN files
and links resolving to them belong to base, including unversioned links that
may also serve the loader. Licenses belong to base. Headers, static archives,
text linker scripts and other development files belong to comp. An alias
inherits its runtime leaf's set. Native list syntax cannot represent whitespace,
backslash or `#` in these paths; such an input is refused before creating output.

Enable the returned root with `EMBER_SETS_SOURCE` only after a clean distribution
has been checked and the runtime imported. Final checkflist and packaging must
use that same root. The helpers provide installation and list mechanisms;
they do not establish C++ ABI or target execution acceptance.

## Shared source extraction

`sourceprep.apply_patch` can keep BSD patch backups outside prepared sources
through `backup_directory`. It creates backup parents for stripped header
paths as well as unstripped paths; the caller retains its chosen patch
arguments. Set `EMBERBSD_TEST_PATCH` to an absolute selected patch executable
and run `python3 -B -m unittest discover -s tests -p test_sourceprep_patch.py`
to check actual changed/new file bytes, source cleanliness, and strip levels
0, 1 and 2. BSD patch can report success after a failed backup while leaving
old bytes in place, so these checks inspect the output files.

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

Archives without a top-level directory use `layout="flat"`; extracted files
are placed under the caller-selected root with the same path, type, size and
case-collision checks. Callers check `sourceprep.ARCHIVE_FLAT_LAYOUT_API == 1`.
The default remains `layout="rooted"`.

Archives that require relative symlinks can opt in with
`internal_symlinks=True` after checking `ARCHIVE_INTERNAL_SYMLINKS_API == 1`.
The selected archive root must not exist before extraction. The extractor
creates links after regular files and rejects absolute, escaping, dangling or
cyclic targets. Hard links and special files remain unsupported;
the default still rejects every link.

`skip_directories` names exact path components below the archive root whose
members are omitted before safety and link checks, while still counting toward
the member bound. Callers check `sourceprep.ARCHIVE_SKIP_DIRECTORIES_API == 1`.
The NetBSD `xsrc.tgz` release set uses it for `CVS` metadata, which the set
records as hard links.
