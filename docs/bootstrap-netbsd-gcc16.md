# NetBSD first-native GCC16 recipe route

> Retired bootstrap admission interface. This document describes historical data.
> Normal native tools are Ports packages in `host-pkg-llvm`, built with Clang.
> GCC16 is an explicitly selected alternative.
> Use [host prefix selection](native-toolset.md); do not create these acceptance receipts for a normal build.

The first-native recipe controller has a fixed NetBSD 11 amd64 branch in
`bootstrap-seed.py run-bootstrap`. It uses the existing
`emberbsd-native-gcc16-bootstrap-plan` envelope and the existing independent
graduation flow. It does not issue acceptance or qualify an ordinary
`NativeToolset`.

The plan pins a source-export acceptance and target transfer acceptance. The
transfer receipt is authoritative on the target; validation does not reopen a
foreign exporter checkout. Live validation checks the complete transferred
tree, profile, MAKECONF, utility members, accepted pkgsrc tools, and compiler
seed. Historical graduation checks receipt lineage and retained inventory
bytes without inspecting retired source trees.

`netbsd_profile` is exactly
`netbsd11-amd64-pkgsrc-native-gcc16-v1`. The `netbsd` object contains exactly:

| Field | Binding |
| --- | --- |
| `pkgsrc_tools_acceptance` | Independently pinned `pkgsrc-bootstrap-tools` receipt; it selects only bmake, gmake and mksh. |
| `source_export_acceptance`, `source_transfer_acceptance`, `source_inventory` | Exact current Ports export, target-side full inventory and transfer evidence. |
| `ports_export_root`, `ports_source` | Same root and revision as the common plan's Ports source pin. |
| `profile` | File reference for `<ports_export_root>/EMBERBSD-NETBSD-BOOTSTRAP-MK.CONF`, whose bytes match the transferred inventory. Its source is Ports `profiles/development-toolchain/netbsd-bootstrap.mk.conf`. |
| `host_facts` | `{os: NetBSD, cpu: x86_64, kernel_version: 11.0, pkgsrc_opsys_version: 110000}` on the current target. The SDK derives the last number from the kernel release. |
| `utility_bin`, `utility_tools` | A dedicated directory whose members are exactly the lowercase helper roles in the roster. Each is a pinned regular executable. Compiler, assembler/linker/archive, shell and make roles, undeclared files and symlinks are refused. MAKECONF exposes each as `EMBERBSD_NETBSD_TOOL.<role>` and lists the sorted names in `EMBERBSD_NETBSD_NATIVE_TOOLS`. |
| `makeconf` | Exact file reference. Its contents are rendered by `render_netbsd_makeconf()` and end with the include of the pinned Ports profile. |
| `pkgsrc_tools`, `pkgsrc_mk` | Exact bmake/gmake/mksh paths and `share/mk` tree selected from the independently accepted pkgsrc-bootstrap-tools prefix inventory. |
| `binutils_as`, `binutils_ld` | Exactly `output_bundle_root/bin/gas` and `output_bundle_root/bin/gld`. |

The fixed MAKECONF binds `EMBERBSD_NETBSD_HOST_OS/CPU`, kernel and pkgsrc
release facts, scoped shell/make routes and utility mappings, the source-seed
GCC command tuples, the initial seed AS/LD/AR/RANLIB/NM/STRIP paths, and private
pkgsrc directories. Profile-only facts use the `EMBERBSD_NETBSD_*` and
`EMBERBSD_GCC16_*` namespaces; generic pkgsrc selectors such as `MAKE_JOBS`,
`PKGSRC_COMPILER`, `GCC_REQD`, `GCCBASE`, `LOCALBASE`, `PREFIX`, `WRKOBJDIR`,
`DISTDIR`, `PKG_DBDIR`, and `PACKAGES` retain their standard names. It sets
`EMBERBSD_NETBSD_JOBS=1`. Package bmake commands
use `-m` with the accepted pkgsrc prefix's `share/mk`; they do not pass a
top-level `-j` option. The fixed profile selector
`EMBERBSD_NETBSD_BOOTSTRAP=yes` is present across the full recipe closure.
Ports owns the host/tool routing and confines GCC-specific behavior to
`PKGPATH == lang/gcc16`.

The utility roster must include `pkg_add`, `pkg_admin`, `pkg_create`,
`pkg_delete`, and `pkg_info`. pkgsrc's `install` target also creates and
registers a package. MAKECONF pins both `NATIVE_PKG_*_CMD` and `PKG_*_CMD`
to these admitted members. Package commands use the isolated `PKG_DBDIR`.
`PKG_REGISTER_SHELLS=no` prevents SDK packages from changing the host shell list.
The base PATH contains only `utility_bin` and the current output's `bin` directory.
Ports may prepend generated wrappers from each owned recipe work directory.
Absolute seed commands do not expose other programs in their input directories.

The recipe DAG must contain `devel/binutils` before `lang/gcc16`. Only the GCC
build and install steps select the source-built `LOCALBASE/bin/gas` and
`LOCALBASE/bin/gld`. The plan keeps a real serial `gmake compare` phase; a
failed comparison remains a failed build result.

`required_roles` names the initial tools consumed by the recipe graph.
Control-plane Python does not provide a native recipe generator role.
The plan may omit an initial `python` role when no recipe consumes it.
A requested role without an accepted command is refused; PATH cannot supply it.
The final ordinary bundle still needs source-built Python for consumers that request it.
Each utility roster entry must match the accepted seed's tool path and SHA256.
Its provider history and bundle bytes therefore receive the shared seed checks.

## Retained NetBSD seed history

The old compiler record remains `emberbsd-bootstrap-compiler-seed` with scope
`bmake-bootstrap`. The NetBSD first-native descriptor uses
`admission: netbsd11-amd64-first-native-v1`; it does not rewrite that record.
Only this plan profile may use provider origins
`retained-local-netbsd-build`, `retained-netbsd-base-tools`, and
`retained-r13-pkgsrc-bootstrap-artifact`.

The local-build history binds the old descriptor, its bmake acceptance,
accepted record and full prefix inventory. It binds the retained producer plan,
five fingerprinted producer scripts, source archive and patch hashes, build and
C/C++ acceptance logs, and each provisioned tool member. It accepts only the
known historical unknowns: command environment, producer attestation, publisher
signature, recipe commit, source commit, and ordinary-provider qualification.
GCC/G++ 12.5.0 remains historical provenance; it does not select the next
compiler. All seven driver components are required, including `lto1` and
`lto-wrapper`.

The base-tools history binds each copied tool's original path, selected bundle
path, and SHA256. A source symlink also pins `resolved_original_path` inside
the fixed NetBSD base executable roots. Live admission checks that target and
its bytes. Existing four-field records remain valid for original regular files.
Historical checks do not resolve retired source aliases.
GCC's retained `--with-as=/usr/bin/as` and
`--with-ld=/usr/bin/ld` settings make `-B` unable to redirect its configured
assembler and linker. The fixed `netbsd11-configured-base-as-ld-seed-v1`
binding records those nominal query results and a hashed read-only compiler
route report. It also pins the Ports-owned
`netbsd11-configured-base-seed.specs.in` template, whose exact `*link_libgcc:`
replacement retains `%D` and removes the historical GCC runtime rpaths. The
instantiated specs must live inside the seed bundle and match that template
byte for byte. Compiler commands apply the fixed specs to `cc`, `cxx`, and
`cpp`; `cc` and `cxx` also receive `-static-libstdc++ -static-libgcc` for the
finite initial link route. Read-only `-###` evidence must retain collect2 and
current-bundle CRT/runtime archive paths, show commands selecting the configured
base assembler and linker, and contain no historical `-rpath` entries. This route evidence
does not qualify runtime behavior or replace later output DSO closure checks.
It admits only `/usr/bin/as` and `/usr/bin/ld` as external
bootstrap members, with current bytes matched to the legacy seed receipt and
the NetBSD base history. Every other seed descriptor tool stays inside its
owned bundle. Separately accepted pkgsrc bootstrap tools retain their own scope.
The input-route acceptance must repeat the binding and exact command plan.
The NetBSD seed snapshots system ABI directories with installed-tree inventory
semantics so dangling system symlinks are recorded as links; DSO closure still
binds each resolved library's current bytes and every dependency edge. These
exceptions are enabled only after the fixed NetBSD binding validates. Ordinary
NativeToolset loading stays bundle-only and uses strict ABI snapshots. The
binding cannot provide a compiler, shell, make, or Python role. Independent
acceptance must still bind the exact plan, nominal driver queries, actual
configured as/ld paths, native ABI inventories, and complete DSO graph. The
final ordinary bundle still uses source-built Binutils `gas` and `gld` from
the common recipe.

The finite base-tool roster also admits `/usr/bin/gzip`, `/usr/bin/gunzip`,
`/usr/bin/gzcat`, `/usr/bin/xz`, and `/usr/bin/xzcat` when the evaluated
bootstrap graph requests those roles. Their selected copies retain the same
basename because these NetBSD utilities can select behavior from `argv[0]`.
The list is role-specific; it does not admit an arbitrary system directory.
The same rule covers the named base utilities consumed by the recipe graph
and its package installation steps. `_BASE_TOOL_PATHS` fixes each original
path; retained history and independent acceptance bind every selected copy.

The GCC prefix's `liblto_plugin.so` is bound separately as a retained shared
object, using its exact old prefix inventory path and hash. The read-only
compiler trace must show the exact `-plugin` path, and the DSO graph must
contain a separately labeled runtime-load edge from selected `/usr/bin/ld` to
that in-bundle plugin, based on GCC's `-###` `-plugin` argument. It is not a
`DT_NEEDED` dependency or proof that the linker loaded it; the NetBSD finite
pilot forces `-flto` at compile and link for both C and C++, then audits the
resulting DSO closure. The
retained plugin's ELF records
`/usr/pkg/gcc12/x86_64--netbsd/lib/.` and `/usr/pkg/gcc12/lib/.` as RPATH
directories. This fixed admission requires both directories to be absent. It pins
the exact `readelf -d` report and rechecks those absences and the plugin bytes;
if either path appears, the seed must be reassessed. This narrow seed metadata cannot relax ordinary
NativeToolset or final-output runtime checks.

The route evidence includes GCC driver queries and `-###` traces. It does not
claim that a compiler child, assembler, or linker was executed during this
input-only admission.

The existing r13 pkgsrc provider receipt stays limited to `make`, `gmake`, and
`sh`. An initial `digest` input requires a separate fresh acceptance receipt
that names only the source-built r13 `prefix/bin/digest` artifact, its complete
provider output inventory, exact provider plan and result, the unchanged
three-role pkgsrc-tools acceptance, source transfer and export receipts,
compiler-seed receipt, command logs, and accepted history unknowns. Each
plan, result, and output-inventory reference must be the original reference
from that three-role acceptance. Every
completed digest command must match the plan's exact id, phase, cwd, argv,
environment, and expected exit. A hashed semantic report binds the exact
artifact and fixture to fixed SHA256, SHA512, and BLAKE2s golden outputs. The
selected copy must match that artifact's SHA256 inside the admitted seed
bundle. This history does not qualify `digest` for ordinary use.

## Admission limit

The available compiler record is an `emberbsd-bootstrap-compiler-seed` with
scope `bmake-bootstrap`. It remains valid only for its recorded bootstrap
purpose. It cannot be renamed to `gcc16-bootstrap`, used as an ordinary
provider, or loaded as a complete `emberbsd-bootstrap-seed`. The NetBSD recipe
controller therefore requires a separately accepted full GCC16 seed descriptor
before it can run. A future NetBSD first-native admission must preserve the old
seed provenance and explicitly bind any transient OS assembler/linker inputs;
the final ordinary output must still use this plan's source-built binutils.

The runner records a pending-review result with drained commands and output
inventory. A separate reviewer must inspect the seed admission, source and
transfer receipts, fixed profile, command logs and exits, input revalidation,
and complete output. Only an independent graduation and NativeToolset receipt
can authorize ordinary native builds.
