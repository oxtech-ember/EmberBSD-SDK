# Initial Darwin utility relocation

> Retired bootstrap admission interface. This document describes historical data.
> Normal native tools are Ports packages in `host-pkg-g16`, built with host GCC16.
> Use [host prefix selection](native-toolset.md); do not create these acceptance receipts for a normal build.

`emberbsd_sdk.bootstrap_relocation` provides `BOOTSTRAP_RELOCATION_API = 1`.
It describes one finite initial utility transformation for GCC16 bootstrap.
The module runs no tool and provisions no payload.
The common producer controller owns execution, input leases, the fresh work
lease, deadline observation, process drainage, and HOLD.
Controller integration must select this stage explicitly.

The fixed Ports profile is
[`darwin-installed-utilities-v1.json`](https://github.com/oxtech-ember/EmberBSD-Ports/blob/79273cf5ead93d45723832017f3c3831c141f651/profiles/common-build-tools/bootstrap/darwin-installed-utilities-v1.json).
Its SHA256 is
`bae4b2d60e37de12b86d23ef4e5333f03f5479c1766fc471704361876c8f211b`.
It names 20 executable regular members, one libintl DSO, and explicit aliases.
External package metadata and original SHA256 rosters remain required.
The profile does not reconstruct their missing historical build inputs.
This interface does not admit an ordinary NativeToolset.

The separate fixed
[`darwin-installed-printf-v1.json`](https://github.com/oxtech-ember/EmberBSD-Ports/blob/main/profiles/common-build-tools/bootstrap/darwin-installed-printf-v1.json)
profile has SHA256
`48927c02453841feaafaaba0c617e3c702821a39cd572d0a1a84822fdca281b7`.
It names only `coreutils-9.11` member `bin/gprintf` and the same libintl DSO.
The original invocation is `gnu/bin/printf`. The final regular member remains
`bin/gprintf`. The selected invocation is the relative alias `bin/printf`.
This profile adds no member to the original 21-member profile or its results.

The separate fixed
[`darwin-installed-build-utilities-v1.json`](https://github.com/oxtech-ember/EmberBSD-Ports/blob/main/profiles/common-build-tools/bootstrap/darwin-installed-build-utilities-v1.json)
profile has SHA256
`a8144875daad5ce2f796ba7891a0ac915144fc179c17fc62ca3278b59cbe7d2e`.
It names thirteen `coreutils-9.11` roles: `chgrp`, `chmod`, `chown`, `cp`,
`install`, `ln`, `ls`, `mkdir`, `mv`, `rm`, `rmdir`, `touch`, and `sleep`.
It also names `pkg_add` and `pkg_create` from `pkg_install-20260227nb3`, and
`install-info` from `pkg_install-info-4.5nb3`.
It includes the same libintl DSO.
Coreutils regular names keep their `g` prefix and select relative aliases.
The package installers retain their `sbin` paths.
The `install-info` role selects `bin/pkg_install-info` without an alias.
An unrelated `bin/install-info` file is outside this profile.
Earlier profiles and their completed results remain unchanged.

The separate fixed
[`darwin-installed-od-v1.json`](https://github.com/oxtech-ember/EmberBSD-Ports/blob/main/profiles/common-build-tools/bootstrap/darwin-installed-od-v1.json)
profile has SHA256
`fde7ae2925def243dac8e23982a42649bf5b2c0fb7cad37b5ccbcf342c6b25e3`.
It names only `coreutils-9.11` member `bin/god` and the same libintl DSO.
The original invocation is `gnu/bin/od`. The selected invocation is the
relative alias `bin/od`. It uses four operations in two complete runs.
The old libintl load is required before transformation. All existing native,
signature, section, load metadata and two-run checks remain required.
Keep original installed history and derived hashes separate. Compare libintl
with the accepted library before provisioning. Do not replace that library.
Earlier profiles and their completed results remain unchanged.

## Fixed plan

Use the existing `emberbsd-native-gcc16-producer-plan`, version 1,
scope `gcc16-bootstrap`, with `stage: utility-relocation`.
The exact fields are `format`, `version`, `scope`, `stage`, `seed_descriptor`,
`host`, `sources`, `work_root`, `required_roles`, `resources`, `relocation`,
and `commands`. The host is Darwin arm64. Sources repeat the selected seed.
The sole required role is `install_name_tool`.
Use one CPU slot, one link slot, and a deadline from 1 through 120 seconds.
Resource lock roots use the existing finite producer contract.

`relocation` has exactly these fields:

| Field | Input |
| --- | --- |
| `profile` | Exact reference to the fixed Ports profile |
| `original_prefix` | Canonical absolute original read root |
| `final_prefix` | Canonical final absolute prefix, ending in `native-utilities-v1` |
| `originals` | Reference to the exact original regular members in the selected profile |

`originals` is a JSON object keyed by each profile `original_member`.
Each value has exactly `path` and `sha256`.
Its path is the original prefix plus that fixed relative member.
Original, work, and final roots must not overlap or have aliased ancestors.
Original absolute symlink chains must match the profile.

`relocation_steps` constructs exactly 42 serial steps for the original profile.
The printf profile constructs four steps: `gprintf` and libintl in each run.
The build-utilities profile constructs 34 steps: sixteen executables and
libintl in each run.
Each step has `phase: transform` and `expected_exit: 0`.
The selected tool must have the exact profile payload hash and a single
`llvm-install-name-tool` invocation spelling.
The alias must already be provisioned and accepted by the seed loader.
The official LLVM archive and source identity remain part of its history.
No command suffix, arbitrary operation, or executable setup callback is accepted.

Each command writes a fresh output in `run-a` or `run-b` under the work root.
The command removes all RPATH entries.
For exactly 17 executables in the original profile, `gprintf` in the printf
profile, or the thirteen coreutils members in the build-utilities profile,
it changes the old absolute libintl load to
`final_prefix/lib/libintl.8.dylib`.
For the DSO, it sets the install ID to that same absolute path.
The module does not add `@loader_path` or ambient search rules.
The environment has an empty PATH and explicit locale, time zone, and work TMPDIR.
Every input file remains unchanged.

`validate_relocation_plan` compares the supplied plan to these exact steps.
It verifies original bytes and the exact old load or install ID before execution.
A successful LLVM `-change` exit alone does not prove that a load was present.
`prepare_relocation_work` creates only the fixed empty output directories.
Call it after the common controller owns the fresh work root exclusively.
`relocation_read_roots` and `relocation_work_outputs` declare the finite roots
and all profile-selected outputs for that controller.

## Output checks and result

`verify_relocation_step` returns the checked output reference for one fixed step.
`verify_relocation_outputs` verifies both complete output sets.
Run both checks while the controller still holds its input and work leases.
The checks support thin generic arm64 Mach-O files up to 16 MiB and 4096 sections.
Unsupported layouts refuse admission.

The native type, CPU, subtype, UUID, flags, build versions, program-section bytes
and metadata, segment protections, entry point, and other unchanged commands
must match the original.
Only the specified load name and DSO ID may change.
No RPATH may remain.
LLVM may update supported LINKEDIT offsets and layout.
Both runs must produce byte-identical complete output hashes.

Each original must have one existing `LC_CODE_SIGNATURE`.
Each output must have the fixed LLVM single CodeDirectory layout with
`ADHOC | LINKER_SIGNED`, SHA256 page hashes, the output basename identifier,
exact code limits, and matching execution-segment fields.
This check does not preserve or assert the original developer signing identity.

The common result must have `stage: utility-relocation` and a `relocation`
object with exactly `profile` and `members`.
Each original-member key maps to `original_sha256`, transformed `sha256`,
two exact `outputs` references, and the fixed `checks` list.
The common status remains `COMMANDS_COMPLETED_PENDING_INDEPENDENT_REVIEW`.
All exact commands need successful exits and retained log references.
Inputs must be revalidated after drainage.
`ordinary_provider_qualified` remains false.
Independent review and pure copy provisioning are separate steps.

## Derived installed history

The bootstrap-only provider origin and history kind are
`installed-package-derived`.
The history has exactly `kind`, `original_history`, `derivation`, and
`provisioned_members`.

`original_history` is the unchanged
[`installed-package` history](bootstrap-installed-inputs.md).
It retains original metadata, original member hashes, source evidence, and
every historical unknown.
`derivation` references the completed finite producer result above.
The validator binds its exact plan, accepted seed, fixed official LLVM tool,
commands, logs, output hashes, and postconditions.
Retained evidence does not reopen the retired original installation.

Each new `provisioned_members` row has exactly `installed_relative`,
`selected_path`, `original_sha256`, and transformed `sha256`.
The mapping must match the fixed profile and derivation result.
Current regular bytes and explicit relative invocation aliases must match
their final bundle paths.
The data-only gettext member uses the same original provenance checks.
An ordinary `installed-package` mapping still requires unchanged original bytes.
Current seed inventory, native-header, independent acceptance, and runtime
closure checks remain required.

Keep a printf derivation separate from an earlier complete utility derivation.
Retain the original coreutils package metadata and its historical unknowns.
The earlier result cannot provide the new `printf` role.
If both profiles use one final prefix, compare the new checked libintl output
with the already accepted library. Do not replace that library or revise its
earlier history. Provision only the newly accepted `gprintf` and relative alias.
The `printf` role requires its completed fixed derivation. The ordinary raw
installed-package role list does not gain this role.

The build-utilities profile also requires its own completed derivation.
Keep separate derived histories for coreutils, pkg_install, pkg_install-info,
and gettext-lib. Each history retains the unchanged original package metadata,
known historical compiler, member hashes, and historical unknowns.
One package's history cannot provide another package's roles.
Compare the checked libintl output with an existing accepted library before
provisioning new members. Do not replace that library or revise older results.
The raw installed-package role lists remain unchanged.
Input admission, actual utility behavior, and complete recipe execution are
separate checks.

## Source and checks

The finite format checks follow LLVM
[`llvmorg-23.1.3`](https://github.com/llvm/llvm-project/tree/0d261d1ca552c95a8f007e061c787ac7132fbcbc).
The command mode comes from
[`llvm-objcopy.cpp`](https://github.com/llvm/llvm-project/blob/0d261d1ca552c95a8f007e061c787ac7132fbcbc/llvm/tools/llvm-objcopy/llvm-objcopy.cpp).
Signature layout and page checks follow
[`MachOLayoutBuilder.h`](https://github.com/llvm/llvm-project/blob/0d261d1ca552c95a8f007e061c787ac7132fbcbc/llvm/lib/ObjCopy/MachO/MachOLayoutBuilder.h),
[`MachOWriter.cpp`](https://github.com/llvm/llvm-project/blob/0d261d1ca552c95a8f007e061c787ac7132fbcbc/llvm/lib/ObjCopy/MachO/MachOWriter.cpp),
and [`MachO.h`](https://github.com/llvm/llvm-project/blob/0d261d1ca552c95a8f007e061c787ac7132fbcbc/llvm/include/llvm/BinaryFormat/MachO.h).
This SDK implementation is original AI-assisted Python code under BSD-2-Clause.

Run `python3 -m unittest discover -s tests -p test_bootstrap_relocation.py`.
Run `python3 -m unittest discover -s tests -p test_bootstrap_relocation_printf.py`
for the separate printf profile controls.
Run `python3 -m unittest discover -s tests -p test_bootstrap_relocation_build_utilities.py`
for the separate build and installation utility profile controls.
These tests use synthetic Mach-O bytes and real fixture symlinks.
They run no native tool and do not qualify the LLVM binary or a native SDK.
